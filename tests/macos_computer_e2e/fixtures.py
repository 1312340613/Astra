"""Safe support code for explicit real macOS Computer Use acceptance tests."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import math
import os
import plistlib
import re
import stat
import subprocess
import time
import zipfile
from collections.abc import Awaitable, Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape

from agent.runtime.computer_backend import (
    ComputerAppCatalog,
    ComputerArtifact,
    ComputerBackendAppState,
    ComputerSessionManager,
    ComputerTarget,
)
from agent.runtime.computer_protocol import ComputerSnapshot, ComputerSnapshotTextDetailMode
from agent.runtime.tools.computer import LocalComputerRuntime, register_local_computer_runtime
from agent.runtime.tools.registry import ToolRegistry

PROJECT_ROOT = Path(__file__).resolve().parents[2]
PRODUCTION_HELPER = Path(os.environ.get("ASTRA_COMPUTER_HELPER_PATH") or (
    PROJECT_ROOT
    / ".astra/bin/AstraMacComputerHelper.app/Contents/MacOS/AstraMacComputerHelper"
))
E2E_SWITCH = "ASTRA_MACOS_COMPUTER_E2E"
SECURE_MARKER = "ASTRA-FIXTURE-SECRET-DO-NOT-EXPOSE"
_MATRIX_ACTIONS = frozenset({"click", "double_click", "scroll", "drag"})
_MATRIX_BUNDLE_ID = re.compile(
    r"[A-Za-z0-9][A-Za-z0-9-]*(?:\.[A-Za-z0-9][A-Za-z0-9-]*)+"
)
_MATRIX_VERSION = re.compile(
    r"[0-9]+(?:\.[0-9]+){0,7}(?:[-+][A-Za-z0-9][A-Za-z0-9.-]{0,31})?"
)
_DETERMINISTIC_FIXTURE_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4nGNgYGD4DwABBAEAX+XDSwAAAABJRU5ErkJggg=="
)


@dataclass(frozen=True)
class CooperativeMatrixRecord:
    bundle_id: str
    app_version: str
    action: str
    cursor_before: tuple[float, float]
    cursor_after: tuple[float, float]
    exact_window: bool
    fresh_effect: bool
    outside_side_effects: bool
    held_input_clean: bool
    evidence_source: str = "deterministic"
    target_pid: int | None = None
    sentinel_pid: int | None = None
    frontmost_before: int | None = None
    frontmost_during: int | None = None
    frontmost_after: int | None = None
    receiving_window: str = ""

    @property
    def rejection_reasons(self) -> tuple[str, ...]:
        reasons: list[str] = []
        if not isinstance(self.bundle_id, str) or _MATRIX_BUNDLE_ID.fullmatch(self.bundle_id) is None:
            reasons.append("invalid_bundle_id")
        if not isinstance(self.app_version, str) or _MATRIX_VERSION.fullmatch(self.app_version) is None:
            reasons.append("invalid_app_version")
        if self.action not in _MATRIX_ACTIONS:
            reasons.append("unsupported_action")
        for name, point in (
            ("cursor_before", self.cursor_before),
            ("cursor_after", self.cursor_after),
        ):
            if (
                not isinstance(point, tuple)
                or len(point) != 2
                or any(
                    isinstance(value, bool)
                    or not isinstance(value, (int, float))
                    or not math.isfinite(float(value))
                    for value in point
                )
            ):
                reasons.append(f"invalid_{name}")
        if (
            not any(reason.startswith("invalid_cursor") for reason in reasons)
            and tuple(map(float, self.cursor_before)) != tuple(map(float, self.cursor_after))
        ):
            reasons.append("cursor_moved")
        if self.exact_window is not True:
            reasons.append("wrong_receiving_window")
        if self.fresh_effect is not True:
            reasons.append("missing_fresh_effect")
        if self.outside_side_effects is not False:
            reasons.append("outside_side_effect")
        if self.held_input_clean is not True:
            reasons.append("held_input_not_clean")
        if self.evidence_source not in {"deterministic", "real"}:
            reasons.append("invalid_evidence_source")
        if self.evidence_source == "real":
            positive_pids = (
                isinstance(self.target_pid, int)
                and not isinstance(self.target_pid, bool)
                and self.target_pid > 0
                and isinstance(self.sentinel_pid, int)
                and not isinstance(self.sentinel_pid, bool)
                and self.sentinel_pid > 0
            )
            if not positive_pids:
                reasons.append("missing_pid_evidence")
            elif self.target_pid == self.sentinel_pid:
                reasons.append("sentinel_matches_target")
            if self.frontmost_before != self.sentinel_pid:
                reasons.append("wrong_frontmost_before")
            if self.frontmost_during != self.target_pid:
                reasons.append("wrong_frontmost_during")
            if self.frontmost_after != self.sentinel_pid:
                reasons.append("wrong_frontmost_after")
            if not isinstance(self.receiving_window, str) or not self.receiving_window:
                reasons.append("missing_receiving_window")
        return tuple(reasons)

    @property
    def accepted(self) -> bool:
        return not self.rejection_reasons

    @property
    def accepted_actions(self) -> frozenset[str]:
        return frozenset({self.action}) if self.accepted else frozenset()

    def to_mapping(self) -> dict[str, Any]:
        return {
            "bundle_id": self.bundle_id,
            "app_version": self.app_version,
            "action": self.action,
            "cursor_before": list(self.cursor_before),
            "cursor_after": list(self.cursor_after),
            "exact_window": self.exact_window,
            "fresh_effect": self.fresh_effect,
            "outside_side_effects": self.outside_side_effects,
            "held_input_clean": self.held_input_clean,
            "evidence_source": self.evidence_source,
            "target_pid": self.target_pid,
            "sentinel_pid": self.sentinel_pid,
            "frontmost_before": self.frontmost_before,
            "frontmost_during": self.frontmost_during,
            "frontmost_after": self.frontmost_after,
            "receiving_window": self.receiving_window,
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> CooperativeMatrixRecord:
        required = {
            "bundle_id", "app_version", "action", "cursor_before", "cursor_after",
            "exact_window", "fresh_effect", "outside_side_effects", "held_input_clean",
            "evidence_source", "target_pid", "sentinel_pid", "frontmost_before",
            "frontmost_during", "frontmost_after", "receiving_window",
        }
        if not isinstance(value, Mapping) or set(value) != required:
            raise ValueError("matrix record fields are invalid")
        before = value["cursor_before"]
        after = value["cursor_after"]
        if not isinstance(before, (list, tuple)) or not isinstance(after, (list, tuple)):
            raise TypeError("matrix cursor fields are invalid")
        return cls(
            **{
                **dict(value),
                "cursor_before": tuple(before),
                "cursor_after": tuple(after),
            }
        )


def accepted_compatibility_entries(
    records: list[CooperativeMatrixRecord],
) -> list[dict[str, object]]:
    accepted: dict[tuple[str, str], set[str]] = {}
    for record in records:
        if not record.accepted:
            continue
        accepted.setdefault((record.bundle_id, record.app_version), set()).add(record.action)
    return [
        {
            "bundle_id": bundle_id,
            "app_version": app_version,
            "capabilities": [{
                "backend": "pid_pointer",
                "enabled_actions": sorted(actions),
            }],
        }
        for (bundle_id, app_version), actions in sorted(accepted.items())
    ]


class DeterministicGetAppStateBackend:
    """Non-live backend for exact-ref transaction and artifact acceptance.

    Its frontmost PID and cursor are immutable sentinels, not claims about the
    current macOS desktop. Native code separately checks the real values.
    """

    def __init__(self) -> None:
        self.frontmost_pid = 4242
        self.cursor_position = (640.0, 480.0)
        self.activation_calls: list[object] = []
        self.input_calls: list[object] = []
        self.calls: list[tuple[str, object]] = []
        self.catalog_generation = 0
        self._app_ref = ""
        self._window_ref = ""
        self._snapshot_sequence = 0

    async def bind_artifact_directory(self, directory_fd: int) -> None:
        self.calls.append(("bind_artifact_directory", directory_fd))

    async def status(self) -> dict[str, object]:
        self.calls.append(("status", None))
        return {
            "supported": True,
            "protocol_version": 4,
            "permissions": {"accessibility": True, "screen_recording": True},
        }

    async def apps(self) -> ComputerAppCatalog:
        self.calls.append(("apps", None))
        self.catalog_generation += 1
        self._app_ref = f"app_fixture_{self.catalog_generation}"
        self._window_ref = f"win_fixture_{self.catalog_generation}"
        return ComputerAppCatalog(self.catalog_generation, ({
            "app_ref": self._app_ref,
            "name": "AstraComputerFixture",
            "bundle_id": "dev.astra.computer-fixture",
            "app_version": "1.0.0",
            "windows": [{
                "window_ref": self._window_ref,
                "title": "Astra Computer Fixture",
                "bounds": {"x": 100, "y": 100, "width": 640, "height": 680},
            }],
        },))

    async def get_app_state(
        self,
        target: ComputerTarget,
        scope: str,
        artifact: ComputerArtifact,
        *,
        text_detail: ComputerSnapshotTextDetailMode = ComputerSnapshotTextDetailMode.OFF,
        text_detail_artifact: ComputerArtifact | None = None,
    ) -> ComputerBackendAppState:
        self.calls.append(("get_app_state", (target, scope, text_detail.value)))
        if target != ComputerTarget(self._app_ref, self._window_ref):
            raise AssertionError("manager passed stale deterministic Fixture refs")
        if scope != "target_window":
            raise AssertionError("manager changed deterministic Fixture scope")

        self._snapshot_sequence += 1
        snapshot_id = f"fixture-state-{self._snapshot_sequence}"
        self._publish(artifact, _DETERMINISTIC_FIXTURE_PNG)
        payload: dict[str, object] = {
            "image_artifact": artifact.filename,
            "logical_size": {"width": 1, "height": 1},
            "pixel_size": {"width": 1, "height": 1},
            "backing_scale": 1,
            "capture_bounds": {"x": 100, "y": 100, "width": 640, "height": 680},
            "ax_tree": {
                "role": "AXWindow",
                "subrole": "AXStandardWindow",
                "title": "Astra Computer Fixture",
                "identifier": "astra.window",
                "element_ref": f"{snapshot_id}:0",
                "children": [{
                    "role": "AXStaticText",
                    "subrole": "AXText",
                    "label": "App state transaction marker",
                    "value": "app-state:primary",
                    "identifier": "astra.app_state_marker",
                    "element_ref": f"{snapshot_id}:1",
                }],
            },
        }
        if text_detail is ComputerSnapshotTextDetailMode.ON:
            if text_detail_artifact is None:
                raise AssertionError("manager omitted deterministic detail artifact")
            document = self._detail_document(snapshot_id)
            detail = json.dumps(
                document,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode()
            self._publish(text_detail_artifact, detail)
            payload.update({
                "text_detail_artifact": text_detail_artifact.filename,
                "text_detail_metadata": {
                    "schema_version": 1,
                    "snapshot_id": snapshot_id,
                    "coverage": "reported_ax_subtree",
                    "node_count": 2,
                    "max_depth_observed": 1,
                    "byte_count": len(detail),
                    "sha256": hashlib.sha256(detail).hexdigest(),
                    "truncated": False,
                    "truncation_reasons": [],
                },
            })
        return ComputerBackendAppState(
            catalog_generation=self.catalog_generation,
            target=target,
            snapshot=ComputerSnapshot(snapshot_id, payload),
        )

    async def select(self, app_ref: str, window_ref: str) -> ComputerTarget:
        self.calls.append(("select", (app_ref, window_ref)))
        raise AssertionError("deterministic get_app_state acceptance must not select")

    async def close(self) -> None:
        self.calls.append(("close", None))

    @staticmethod
    def _publish(artifact: ComputerArtifact, content: bytes) -> None:
        descriptor = os.open(
            artifact.filename,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
            dir_fd=artifact.directory_fd,
        )
        try:
            offset = 0
            while offset < len(content):
                offset += os.write(descriptor, content[offset:])
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    @staticmethod
    def _detail_document(snapshot_id: str) -> dict[str, object]:
        return {
            "schema_version": 1,
            "snapshot_id": snapshot_id,
            "coverage": "reported_ax_subtree",
            "limits": {
                "maximum_depth": 20,
                "maximum_nodes": 4_000,
                "maximum_structural_string_bytes": 4 * 1_024,
                "maximum_value_bytes": 256 * 1_024,
                "maximum_aggregate_text_bytes": 4 * 1_024 * 1_024,
                "maximum_final_bytes": 8 * 1_024 * 1_024,
                "wall_clock_ms": 5_000,
            },
            "stats": {
                "node_count": 2,
                "max_depth_observed": 1,
                "truncated": False,
                "truncation_reasons": [],
            },
            "root": {
                "node_id": "node_0",
                "role": "AXWindow",
                "subrole": "AXStandardWindow",
                "title": "Astra Computer Fixture",
                "children": [{
                    "node_id": "node_1",
                    "role": "AXStaticText",
                    "subrole": "AXText",
                    "label": "App state transaction marker",
                    "value": "app-state:primary",
                }],
            },
        }


@dataclass
class DeterministicGetAppStateHarness:
    cache_root: Path
    backend: DeterministicGetAppStateBackend = field(init=False)
    manager: ComputerSessionManager = field(init=False)

    def __post_init__(self) -> None:
        self.backend = DeterministicGetAppStateBackend()
        self.manager = ComputerSessionManager(self.backend, cache_root=self.cache_root)


def run(awaitable: Awaitable[Any]) -> Any:
    return asyncio.run(awaitable)


def create_minimal_docx(path: Path, *, marker: str) -> None:
    """Create a private three-part OOXML document without third-party packages."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w+b", closefd=False) as stream:
            with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                archive.writestr(
                    "[Content_Types].xml",
                    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                    '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                    '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
                    '<Default Extension="xml" ContentType="application/xml"/>'
                    '<Override PartName="/word/document.xml" '
                    'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
                    "</Types>",
                )
                archive.writestr(
                    "_rels/.rels",
                    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                    '<Relationship Id="rId1" '
                    'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
                    'Target="word/document.xml"/>'
                    "</Relationships>",
                )
                archive.writestr(
                    "word/document.xml",
                    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                    '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
                    f"<w:body><w:p><w:r><w:t>{escape(marker)}</w:t></w:r></w:p>"
                    '<w:sectPr><w:pgSz w:w="12240" w:h="15840"/></w:sectPr></w:body></w:document>',
                )
            stream.flush()
            os.fsync(stream.fileno())
    finally:
        os.close(descriptor)
    os.chmod(path, 0o600)


def extract_docx_text(path: Path) -> str:
    with zipfile.ZipFile(path) as archive:
        return archive.read("word/document.xml").decode("utf-8")


def extract_pdf_text(path: Path) -> str:
    from pypdf import PdfReader

    return "\n".join(page.extract_text() or "" for page in PdfReader(path).pages)


def assert_private_workspace(root: Path) -> None:
    mode = stat.S_IMODE(root.stat().st_mode)
    if mode != 0o700:
        raise AssertionError(f"E2E workspace must be mode 0700, got {mode:o}: {root}")


def approval_path_is_within_workspace(known_path: str, workspace: Path) -> bool:
    if not known_path:
        return True
    candidate = Path(known_path).resolve(strict=False)
    canonical_workspace = workspace.resolve(strict=True)
    try:
        candidate.relative_to(canonical_workspace)
    except ValueError:
        return False
    return True


def action_result_metadata(result: Mapping[str, Any]) -> Mapping[str, Any]:
    """Return native action metadata from success snapshots or failure details."""
    details = result.get("details")
    if isinstance(details, Mapping):
        nested = details.get("action_result")
        if isinstance(nested, Mapping):
            return nested
        if "last_acknowledged_action" in details:
            return details
    fresh_output = result.get("fresh_output")
    if isinstance(fresh_output, str):
        try:
            payload = json.loads(fresh_output)
        except (TypeError, ValueError):
            return {}
        if isinstance(payload, Mapping):
            action_result = payload.get("action_result")
            if isinstance(action_result, Mapping):
                return action_result
    return {}


async def retry_preinput_stale_once(
    call_raw: Callable[..., Awaitable[dict[str, Any]]],
    refresh: Callable[[], Awaitable[dict[str, Any]]],
    *,
    snapshot: Mapping[str, Any],
    actions: list[dict[str, Any]],
) -> dict[str, Any]:
    """Retry once only when the native guard proves input never started."""
    result = await call_raw(
        "computer_act",
        snapshot_id=str(snapshot["snapshot_id"]),
        actions=actions,
    )
    if result.get("code") != "stale_snapshot":
        return result
    acknowledged = action_result_metadata(result).get("last_acknowledged_action", -1)
    if isinstance(acknowledged, int) and not isinstance(acknowledged, bool) and acknowledged >= 0:
        return result
    fresh = await refresh()
    return await call_raw(
        "computer_act",
        snapshot_id=str(fresh["snapshot_id"]),
        actions=actions,
    )


def find_nodes(value: Any, predicate: Callable[[Mapping[str, Any]], bool]) -> list[dict[str, Any]]:
    matches: list[dict[str, Any]] = []

    def visit(item: Any) -> None:
        if isinstance(item, Mapping):
            node = dict(item)
            if predicate(node):
                matches.append(node)
            for child in item.values():
                visit(child)
        elif isinstance(item, list):
            for child in item:
                visit(child)

    visit(value)
    return matches


def node_text(node: Mapping[str, Any]) -> str:
    return " ".join(
        str(node.get(key) or "")
        for key in ("role", "subrole", "label", "title", "help", "value")
    )


def first_node(value: Any, predicate: Callable[[Mapping[str, Any]], bool], description: str) -> dict[str, Any]:
    matches = find_nodes(value, predicate)
    if not matches:
        visible = [node_text(node)[:240] for node in find_nodes(value, lambda _node: True)]
        raise AssertionError(f"missing {description}; visible AX nodes={visible[:80]!r}")
    return matches[0]


async def observe_focused_text_field(
    computer: "ComputerHarness",
    *,
    bundle_id: str,
    description: str,
    title: str = "",
    timeout: float = 8.0,
) -> dict[str, Any]:
    """Return the first app state whose AX tree exposes a focused text field.

    Retry pre-input catalog races and ordinary incomplete observations within a
    bounded deadline. Other native failures stay visible to the test.
    """
    predicate: Callable[[Mapping[str, Any]], bool] = (
        lambda node: node.get("role") == "AXTextField" and node.get("focused") is True
    )
    deadline = time.monotonic() + timeout
    latest: dict[str, Any] | None = None
    last_error = ""
    while True:
        result: dict[str, Any] = {}
        try:
            app_ref, window_ref, _ = await computer.wait_for_window(
                bundle_id=bundle_id, title=title, timeout=2,
            )
            result = await computer.call_raw(
                "computer_get_app_state", app_ref=app_ref, window_ref=window_ref,
            )
        except AssertionError as exc:
            if not str(exc).startswith("target window not discovered"):
                raise
            last_error = str(exc)
        if result.get("error"):
            last_error = f"{result.get('code')}: {result.get('error')}"
            if result.get("code") not in {"stale_target", "target_gone"}:
                raise AssertionError(last_error)
        elif result:
            latest = json.loads(result["fresh_output"])
            if find_nodes(latest["ax_tree"], predicate):
                return latest
        if time.monotonic() >= deadline:
            if latest is not None:
                first_node(latest["ax_tree"], predicate, description)
            raise AssertionError(f"missing {description}; last observation error={last_error!r}")
        await asyncio.sleep(0.25)


@dataclass
class ApprovalRecord:
    request: dict[str, Any]
    decision: str


@dataclass
class ComputerHarness:
    workspace: Path
    approval_decider: Callable[[dict[str, Any]], str] = lambda _request: "once"
    approved_external_test_paths: set[str] = field(default_factory=set)
    registry: ToolRegistry = field(init=False)
    runtime: LocalComputerRuntime = field(init=False)
    approvals: list[ApprovalRecord] = field(default_factory=list, init=False)
    audits: list[dict[str, Any]] = field(default_factory=list, init=False)
    events: list[dict[str, Any]] = field(default_factory=list, init=False)

    def __post_init__(self) -> None:
        assert_private_workspace(self.workspace)
        if not PRODUCTION_HELPER.is_file():
            raise AssertionError(
                f"signed production helper missing: {PRODUCTION_HELPER}; "
                "run bash scripts/build_macos_computer_helper.sh"
            )
        self.registry = ToolRegistry(artifact_dir=self.workspace / "tool-results")
        runtime = register_local_computer_runtime(
            self.registry,
            helper_path=PRODUCTION_HELPER,
            cache_root=self.workspace / "computer-cache",
            model_capabilities={"tools", "vision"},
        )
        if runtime is None:
            raise AssertionError("Computer Use runtime did not register on this host")
        self.runtime = runtime

        async def approve(request: dict[str, Any]) -> str:
            safe_request = dict(request)
            arguments = safe_request.get("arguments")
            known_path = (
                str(arguments.get("known_path") or "")
                if isinstance(arguments, Mapping) else ""
            )
            if safe_request.get("kind") == "computer_write" and known_path:
                if (
                    not approval_path_is_within_workspace(known_path, self.workspace)
                    and known_path not in self.approved_external_test_paths
                ):
                    decision = "deny"
                else:
                    decision = self.approval_decider(safe_request)
            else:
                decision = self.approval_decider(safe_request)
            self.approvals.append(ApprovalRecord(dict(request), decision))
            return decision

        self.registry.set_approval_handler(approve)
        self.registry.set_approval_audit_handler(lambda event: self.audits.append(dict(event)))
        self.registry.hooks.on_tool_result(
            lambda name, args, result, _tool: self.events.append(
                {"kind": "result", "name": name, "args": args, "result": result}
            )
        )
        self.registry.hooks.on_tool_error(
            lambda name, args, error, _tool: self.events.append(
                {"kind": "error", "name": name, "args": args, "error": error}
            )
        )

    async def call(self, name: str, **arguments: Any) -> dict[str, Any]:
        result = await self.call_raw(name, **arguments)
        if result.get("error"):
            raise AssertionError(
                f"{name} failed code={result.get('code')}: {result.get('error')}"
            )
        return result

    async def call_raw(self, name: str, **arguments: Any) -> dict[str, Any]:
        return await self.registry.execute(
            name,
            arguments,
            call_id=f"e2e-{name}-{time.monotonic_ns()}",
        )

    async def status(self) -> dict[str, Any]:
        return json.loads((await self.call("computer_status"))["output"])

    async def apps(self) -> list[dict[str, Any]]:
        result = await self.call("computer_apps")
        output = result["output"]
        if result.get("output_truncated"):
            artifact = Path(result["artifact_path"]).resolve()
            assert artifact.is_relative_to(self.workspace.resolve())
            output = artifact.read_text(encoding="utf-8")
        return json.loads(output)["apps"]

    async def wait_for_window(
        self,
        *,
        app_name: str = "",
        bundle_id: str = "",
        title: str = "",
        timeout: float = 15,
    ) -> tuple[str, str, dict[str, Any]]:
        deadline = time.monotonic() + timeout
        latest: list[dict[str, Any]] = []
        while time.monotonic() < deadline:
            latest = await self.apps()
            for app in latest:
                if app_name and app_name.casefold() not in str(app.get("name") or "").casefold():
                    continue
                if bundle_id and str(app.get("bundle_id") or "") != bundle_id:
                    continue
                for window in app.get("windows", []):
                    if title and title.casefold() not in str(window.get("title") or "").casefold():
                        continue
                    return str(app["app_ref"]), str(window["window_ref"]), dict(window)
            await asyncio.sleep(0.2)
        catalog = [
            (app.get("name"), app.get("bundle_id"), [window.get("title") for window in app.get("windows", [])])
            for app in latest
        ]
        raise AssertionError(
            f"target window not discovered app_name={app_name!r} bundle_id={bundle_id!r} "
            f"title={title!r}; catalog={catalog!r}"
        )

    async def focus(self, app_ref: str, window_ref: str) -> None:
        await self.call("computer_focus", app_ref=app_ref, window_ref=window_ref)

    async def snapshot(self) -> dict[str, Any]:
        result = await self.call("computer_snapshot", scope="target_window")
        payload = json.loads(result["fresh_output"])
        payload["_private_result"] = result.get("_private_result", {})
        return payload

    async def act(self, snapshot_id: str, actions: list[dict[str, Any]]) -> dict[str, Any]:
        result = await self.call("computer_act", snapshot_id=snapshot_id, actions=actions)
        payload = json.loads(result["fresh_output"])
        payload["_private_result"] = result.get("_private_result", {})
        return payload

    async def close(self) -> None:
        try:
            await self.call("computer_close")
        finally:
            await self.runtime.shutdown()


def run_command(arguments: list[str], *, timeout: float = 30) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        arguments,
        cwd=PROJECT_ROOT,
        text=True,
        capture_output=True,
        timeout=timeout,
        check=True,
    )


def fixture_binary() -> Path:
    run_command(
        [
            "swift",
            "build",
            "--package-path",
            str(PROJECT_ROOT / "native/macos-computer-helper"),
            "--product",
            "AstraComputerFixture",
        ]
    )
    bin_path = run_command(
        [
            "swift",
            "build",
            "--package-path",
            str(PROJECT_ROOT / "native/macos-computer-helper"),
            "--show-bin-path",
        ]
    ).stdout.strip()
    return (Path(bin_path) / "AstraComputerFixture").resolve(strict=True)


@contextmanager
def launched_fixture() -> Iterator[subprocess.Popen[bytes]]:
    process = subprocess.Popen(
        [fixture_binary()],
        cwd=PROJECT_ROOT,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    try:
        # ScreenCaptureKit can publish the window before its AXWindow is ready.
        # This is setup settling only; action retries still remain prohibited.
        time.sleep(0.5)
        yield process
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)


def bundle_identifier(application: Path) -> str:
    with (application / "Contents/Info.plist").open("rb") as stream:
        value = plistlib.load(stream).get("CFBundleIdentifier")
    if not isinstance(value, str) or not value:
        raise AssertionError(f"application has no bundle identifier: {application}")
    return value


async def wait_for_path(path: Path, *, present: bool = True, timeout: float = 10) -> None:
    deadline = time.monotonic() + timeout
    while path.exists() is not present and time.monotonic() < deadline:
        await asyncio.sleep(0.1)
    if path.exists() is not present:
        state = "appear" if present else "disappear"
        raise AssertionError(f"timed out waiting for {path} to {state}")


async def open_application(*arguments: str) -> None:
    process = await asyncio.create_subprocess_exec(
        "open",
        *arguments,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    _stdout, stderr = await process.communicate()
    if process.returncode != 0:
        raise AssertionError(f"open failed ({process.returncode}): {stderr.decode(errors='replace')[:2_000]}")

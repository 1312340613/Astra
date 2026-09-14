"""Optional MCP client manager that adapts remote tools into ToolRegistry."""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import logging
import os
import re
from types import SimpleNamespace
import uuid
from contextlib import AsyncExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .tool_failure import ToolFailure
from .process_env import mark_agent_environment
from .tools.approval import ScopedApprovalStore
from .tools.registry import ToolDef, ToolRegistry
from .project_trust import ProjectTrust


logger = logging.getLogger(__name__)

_SAFE_NAME = re.compile(r"[^a-zA-Z0-9_-]+")
_ENV_VALUE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
_MCP_ROOT_KEYS = {"servers"}
_MCP_IMAGE_EXTENSIONS = {
    "image/gif": ".gif",
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
}
_MCP_RETRYABLE_ERROR = re.compile(
    r"\b(?:408|425|429|500|502|503|504)\b|connection|rate.?limit|temporar|timed?\s*out|timeout",
    re.IGNORECASE,
)
_MCP_SERVER_KEYS = {
    "enabled",
    "transport",
    "command",
    "args",
    "env",
    "url",
    "headers",
    "timeout",
    "startup_timeout",
    "risk",
    "tool_risks",
    "include_tools",
    "exclude_tools",
}
_DEFAULT_MCP_STARTUP_CONCURRENCY = 4
_MAX_MCP_STARTUP_CONCURRENCY = 8


def _expand_env(value: Any) -> Any:
    if isinstance(value, str):
        return _ENV_VALUE.sub(lambda match: os.getenv(match.group(1), ""), value)
    if isinstance(value, list):
        return [_expand_env(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _expand_env(item) for key, item in value.items()}
    return value


def mcp_tool_name(server: str, tool: str) -> str:
    clean_server = _SAFE_NAME.sub("_", server).strip("_") or "server"
    clean_tool = _SAFE_NAME.sub("_", tool).strip("_") or "tool"
    return f"mcp__{clean_server}__{clean_tool}"


def _exception_summary(exc: BaseException) -> str:
    """Flatten an exception group into a compact status-safe description."""
    pending = [exc]
    leaves: list[str] = []
    while pending:
        current = pending.pop(0)
        children = getattr(current, "exceptions", ())
        if isinstance(children, tuple) and children:
            pending[0:0] = list(children)
            continue
        message = str(current).strip()
        summary = type(current).__name__ + (f": {message}" if message else "")
        if summary not in leaves:
            leaves.append(summary)
    return "; ".join(leaves)[:1_000]


@dataclass(frozen=True)
class MCPServerStatus:
    name: str
    state: str
    tools: int = 0
    error: str = ""


@dataclass
class _MCPStartupResult:
    state: str
    stack: AsyncExitStack | None = None
    session: Any = None
    deadline: float = 0.0
    timeout: float = 0.0
    response: Any = None
    error: str = ""


class MCPManager:
    @staticmethod
    async def _list_all_tools(session: Any) -> Any:
        """Finish pagination before publishing a new registry generation."""
        gathered: list[Any] = []
        seen_cursors: set[str] = set()
        seen_names: set[str] = set()
        cursor = None
        for _ in range(100):
            page = await session.list_tools() if cursor is None else await session.list_tools(cursor=cursor)
            remote_tools = getattr(page, "tools", None)
            if not isinstance(remote_tools, (list, tuple)):
                raise ValueError("MCP tool page must contain a tools array")
            for tool in remote_tools:
                name = getattr(tool, "name", None)
                if not isinstance(name, str) or not name.strip() or name in seen_names:
                    raise ValueError("MCP tool list contains an empty or duplicate name")
                seen_names.add(name)
                gathered.append(tool)
            cursor = getattr(page, "nextCursor", None)
            if cursor is None:
                cursor = getattr(page, "next_cursor", None)
            if cursor is None or cursor == "":
                return SimpleNamespace(tools=gathered)
            if not isinstance(cursor, str) or cursor in seen_cursors:
                raise ValueError("MCP tool pagination returned an invalid or repeated cursor")
            seen_cursors.add(cursor)
        raise ValueError("MCP tool pagination exceeded 100 pages")

    def __init__(
        self,
        config_path: str | Path | None = None,
        max_output_chars: int = 100_000,
        startup_timeout: float | None = None,
        startup_concurrency: int | None = None,
    ):
        default = os.getenv("AGENT_MCP_CONFIG", ".astra/mcp.json")
        self.config_path = Path(config_path or default).expanduser()
        self._project_config_allowed = (
            config_path is not None
            or ProjectTrust.for_path(Path.cwd()).permits(self.config_path)
        )
        self.max_output_chars = max_output_chars
        if startup_timeout is None:
            try:
                startup_timeout = float(os.getenv("MCP_STARTUP_TIMEOUT", "15"))
            except ValueError:
                startup_timeout = 15.0
        self.startup_timeout = max(1.0, startup_timeout)
        if startup_concurrency is None:
            raw_concurrency = os.getenv(
                "MCP_STARTUP_CONCURRENCY",
                str(_DEFAULT_MCP_STARTUP_CONCURRENCY),
            )
            try:
                startup_concurrency = int(raw_concurrency)
            except ValueError:
                logger.warning(
                    "invalid MCP_STARTUP_CONCURRENCY; using %s",
                    _DEFAULT_MCP_STARTUP_CONCURRENCY,
                )
                startup_concurrency = _DEFAULT_MCP_STARTUP_CONCURRENCY
        self.startup_concurrency = min(
            _MAX_MCP_STARTUP_CONCURRENCY,
            max(1, startup_concurrency),
        )
        self.statuses: list[MCPServerStatus] = []
        self.config_warnings: list[str] = []
        self._stacks: list[AsyncExitStack] = []
        self._server_stacks: dict[str, AsyncExitStack] = {}
        self._sessions: dict[str, Any] = {}
        self._server_configs: dict[str, dict] = {}
        self._registry: ToolRegistry | None = None
        self._background_task: asyncio.Task | None = None
        self._reconnect_event = asyncio.Event()
        self._tool_refresh_servers: set[str] = set()
        self._status_listener: Callable[[tuple[MCPServerStatus, ...]], None] | None = None
        self._closed = False

    @property
    def loaded_tools(self) -> int:
        return sum(status.tools for status in self.statuses if status.state == "ready")

    def set_status_listener(
        self,
        listener: Callable[[tuple[MCPServerStatus, ...]], None] | None,
        *,
        emit_current: bool = False,
    ) -> None:
        """Observe lifecycle changes without making MCP startup block readiness."""
        self._status_listener = listener
        if emit_current:
            self._notify_status_change()

    def _notify_status_change(self) -> None:
        listener = self._status_listener
        if listener is None:
            return
        try:
            listener(tuple(self.statuses))
        except Exception:
            logger.exception("MCP status listener failed")

    def _replace_statuses(self, statuses: list[MCPServerStatus]) -> None:
        self.statuses = statuses
        self._notify_status_change()

    @staticmethod
    async def _safe_aclose(stack: AsyncExitStack, *, context: str) -> str:
        """Close a partially started MCP stack without hiding its primary failure."""
        try:
            await stack.aclose()
        except Exception as exc:
            detail = _exception_summary(exc)
            logger.warning("MCP cleanup failed after %s: %s", context, detail, exc_info=True)
            return detail
        return ""

    async def load(self, registry: ToolRegistry) -> None:
        self._registry = registry
        self.config_warnings = []
        if not self._project_config_allowed:
            self._replace_statuses([
                MCPServerStatus(
                    "config",
                    "disabled",
                    error="project-local MCP config is disabled until the project is trusted",
                )
            ])
            return
        if not self.config_path.exists():
            self._replace_statuses([
                MCPServerStatus("config", "disabled", error=f"not found: {self.config_path}")
            ])
            return
        try:
            payload = json.loads(self.config_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            self._replace_statuses([MCPServerStatus("config", "error", error=str(exc))])
            return
        self.config_warnings = self._validate_config(payload)
        servers = payload.get("servers", {}) if isinstance(payload, dict) else {}
        if not isinstance(servers, dict):
            self._replace_statuses([
                MCPServerStatus("config", "error", error="'servers' must be an object")
            ])
            return
        try:
            import mcp  # noqa: F401 - availability check; clients are imported on demand
        except ModuleNotFoundError:
            self._replace_statuses([
                MCPServerStatus("mcp", "unavailable", error="install with: pip install -e .[mcp]")
            ])
            return

        self._server_configs.clear()
        initial_statuses = []
        for name, raw in servers.items():
            if not isinstance(raw, dict):
                initial_statuses.append(
                    MCPServerStatus(str(name), "error", error="server config must be an object")
                )
            elif raw.get("enabled", True) is False:
                initial_statuses.append(MCPServerStatus(str(name), "disabled"))
            else:
                initial_statuses.append(MCPServerStatus(str(name), "connecting"))
        self._replace_statuses(initial_statuses)

        active_servers: list[tuple[str, dict]] = []
        for name, raw in servers.items():
            if not isinstance(raw, dict):
                continue
            if raw.get("enabled", True) is False:
                continue
            server_name = str(name)
            expanded = _expand_env(raw)
            self._server_configs[server_name] = expanded
            active_servers.append((server_name, expanded))

        if active_servers:
            connections: list[tuple[str, dict, _MCPStartupResult]] = []
            tasks: list[asyncio.Task] = []
            try:
                # AsyncExitStack/anyio context ownership stays in this
                # lifecycle task; only the server handshakes below fan out.
                for name, raw in active_servers:
                    result = await self._open_one(name, raw)
                    if result.state != "connected":
                        state = "auth_required" if self._auth_failure(result.error) else "error"
                        self._set_status(name, state, error=result.error)
                        continue
                    assert result.stack is not None
                    self._stacks.append(result.stack)
                    self._server_stacks[name] = result.stack
                    self._sessions[name] = result.session
                    connections.append((name, raw, result))

                semaphore = asyncio.Semaphore(self.startup_concurrency)
                tasks = [
                    asyncio.create_task(
                        self._initialize_one(result, semaphore),
                        name=f"astra-mcp-startup:{name}",
                    )
                    for name, raw, result in connections
                ]
                results = await asyncio.gather(*tasks, return_exceptions=True)
            except asyncio.CancelledError:
                for task in tasks:
                    if not task.done():
                        task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                # A background lifecycle task retains ownership of accepted
                # stacks and closes them in _run_background's finally block.
                # Direct callers have no such owner, so close on cancellation.
                if self._background_task is not asyncio.current_task():
                    await self._close_stacks(context="cancelled MCP startup")
                raise
            for (name, raw, connection), result in zip(connections, results):
                if isinstance(result, asyncio.CancelledError):
                    for task in tasks:
                        if not task.done():
                            task.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)
                    raise result
                if isinstance(result, BaseException):
                    error = f"{type(result).__name__}: {result}"
                    state = "auth_required" if self._auth_failure(error) else "error"
                    await self._discard_server(name, context=f"{name} startup failure")
                    self._set_status(name, state, error=error)
                    continue
                if result.state == "ready":
                    try:
                        tools = list(getattr(result.response, "tools", []) or [])
                        registered = self._register_tools(
                            registry,
                            name,
                            connection.session,
                            tools,
                            raw,
                        )
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        cleanup_warning = await self._discard_server(
                            name,
                            context=f"{name} startup registration failure",
                        )
                        error = f"{type(exc).__name__}: {exc}"
                        if cleanup_warning:
                            error += f"; cleanup warning: {cleanup_warning}"
                        state = "auth_required" if self._auth_failure(error) else "error"
                        self._set_status(name, state, error=error)
                        continue
                    self._set_status(name, "ready", tools=registered)
                    continue
                cleanup_warning = await self._discard_server(
                    name,
                    context=f"{name} startup {result.state}",
                )
                error = result.error
                if cleanup_warning:
                    error += f"; cleanup warning: {cleanup_warning}"
                state = "auth_required" if self._auth_failure(error) else "error"
                self._set_status(name, state, error=error)
        if not self.statuses:
            self._replace_statuses([MCPServerStatus("config", "ready", tools=0)])

    async def _open_one(self, name: str, raw: dict) -> _MCPStartupResult:
        stack = AsyncExitStack()
        server_timeout = self.startup_timeout
        try:
            server_timeout = max(
                1.0,
                float(raw.get("startup_timeout", self.startup_timeout)),
            )
            deadline = asyncio.get_running_loop().time() + server_timeout
            async with asyncio.timeout_at(deadline):
                session = await self._enter_server_session(name, raw, stack)
            return _MCPStartupResult(
                "connected",
                stack=stack,
                session=session,
                deadline=deadline,
                timeout=server_timeout,
            )
        except asyncio.CancelledError:
            await self._safe_aclose(stack, context=f"{name} cancelled startup")
            raise
        except TimeoutError:
            cleanup_warning = await self._safe_aclose(stack, context=f"{name} startup timeout")
            error = f"startup timed out after {server_timeout:g}s"
            if cleanup_warning:
                error += f"; cleanup warning: {cleanup_warning}"
            return _MCPStartupResult("timeout", error=error)
        except Exception as exc:
            cleanup_warning = await self._safe_aclose(stack, context=f"{name} startup failure")
            error = f"{type(exc).__name__}: {exc}"
            if cleanup_warning:
                error += f"; cleanup warning: {cleanup_warning}"
            return _MCPStartupResult("error", error=error)

    async def _initialize_one(
        self,
        connection: _MCPStartupResult,
        semaphore: asyncio.Semaphore,
    ) -> _MCPStartupResult:
        """Initialize one opened server under the startup concurrency bound."""
        async with semaphore:
            try:
                remaining = connection.deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    raise TimeoutError
                async with asyncio.timeout(remaining):
                    await connection.session.initialize()
                    response = await self._list_all_tools(connection.session)
                return _MCPStartupResult("ready", response=response)
            except asyncio.CancelledError:
                raise
            except TimeoutError:
                return _MCPStartupResult(
                    "timeout",
                    error=f"startup timed out after {connection.timeout:g}s",
                )
            except Exception as exc:
                return _MCPStartupResult("error", error=f"{type(exc).__name__}: {exc}")

    def start_background(self, registry: ToolRegistry) -> asyncio.Task:
        """Begin optional MCP initialization without delaying agent readiness."""
        if self._background_task is not None and not self._background_task.done():
            return self._background_task
        self._closed = False
        self._registry = registry
        self._replace_statuses([MCPServerStatus("startup", "connecting")])
        self._background_task = asyncio.create_task(
            self._run_background(),
            name="astra-mcp-lifecycle",
        )
        return self._background_task

    async def _run_background(self) -> None:
        try:
            await self.load(self._registry or ToolRegistry())
            while not self._closed:
                failed = {
                    status.name
                    for status in self.statuses
                    if status.state in {"error", "auth_required"}
                    and status.name in self._server_configs
                }
                if not failed and not self._tool_refresh_servers:
                    await self._reconnect_event.wait()
                    self._reconnect_event.clear()
                elif failed and not self._tool_refresh_servers:
                    try:
                        retry_after = max(
                            1.0,
                            float(os.getenv("MCP_RECONNECT_INTERVAL", "30")),
                        )
                    except ValueError:
                        retry_after = 30.0
                    try:
                        await asyncio.wait_for(self._reconnect_event.wait(), timeout=retry_after)
                    except asyncio.TimeoutError:
                        pass
                    self._reconnect_event.clear()
                if self._closed:
                    break
                refresh = set(self._tool_refresh_servers)
                self._tool_refresh_servers.difference_update(refresh)
                for name in sorted(refresh):
                    await self._refresh_server_tools(name)
                failed = {
                    status.name
                    for status in self.statuses
                    if status.state in {"error", "auth_required"}
                    and status.name in self._server_configs
                }
                for name in sorted(failed):
                    await self._reconnect_server(name)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("MCP background lifecycle failed")
        finally:
            # AsyncExitStack/anyio cancel scopes must be exited by the same
            # lifecycle task that entered them.
            await self._close_stacks(context="background lifecycle shutdown")
            self._server_stacks.clear()
            self._sessions.clear()

    async def _close_stacks(self, *, context: str) -> None:
        while self._stacks:
            await self._safe_aclose(self._stacks.pop(), context=context)

    def request_reconnect(self, server: str | None = None) -> None:
        """Wake the lifecycle after login/config recovery or a failed tool call."""
        if server and server in self._server_configs:
            self._set_status(server, "error", error="reconnect requested")
        self._reconnect_event.set()

    def _message_handler(self, server: str):
        async def handle(message: Any) -> None:
            root = getattr(message, "root", None)
            if type(root).__name__ != "ToolListChangedNotification":
                return
            self._tool_refresh_servers.add(server)
            self._reconnect_event.set()

        return handle

    async def _refresh_server_tools(self, name: str) -> None:
        registry = self._registry
        session = self._sessions.get(name)
        raw = self._server_configs.get(name)
        if registry is None or session is None or raw is None or self._closed:
            return
        try:
            async with asyncio.timeout(max(1.0, float(raw.get("startup_timeout", self.startup_timeout)))):
                response = await self._list_all_tools(session)
            tools = list(getattr(response, "tools", []) or [])
            registered = self._register_tools(registry, name, session, tools, raw)
            self._set_status(name, "ready", tools=registered)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            # A failed listing must not tear down a working connection or
            # remove its last validated tool generation. Invocation failures
            # still trigger the normal reconnect path.
            previous = next((item for item in self.statuses if item.name == name), None)
            self._set_status(name, "ready", tools=previous.tools if previous else 0,
                             error=f"Tool refresh failed; retaining previous tools: {error}")

    def _set_status(self, name: str, state: str, *, tools: int = 0, error: str = "") -> None:
        replacement = MCPServerStatus(name, state, tools=tools, error=error)
        for index, status in enumerate(self.statuses):
            if status.name == name:
                self.statuses[index] = replacement
                self._notify_status_change()
                return
        self.statuses.append(replacement)
        self._notify_status_change()

    @staticmethod
    def _auth_failure(error: str) -> bool:
        return bool(re.search(r"\b401\b|unauthori[sz]ed|oauth|authentication", error, re.IGNORECASE))

    async def _discard_server(self, name: str, *, context: str) -> str:
        self._sessions.pop(name, None)
        stack = self._server_stacks.pop(name, None)
        if stack is None:
            return ""
        if stack in self._stacks:
            self._stacks.remove(stack)
        return await self._safe_aclose(stack, context=context)

    async def _reconnect_server(self, name: str) -> None:
        registry = self._registry
        raw = self._server_configs.get(name)
        if registry is None or raw is None or self._closed:
            return
        await self._discard_server(name, context=f"{name} reconnect")
        self._set_status(name, "connecting")
        await self._connect_one(registry, name, raw)

    async def _enter_server_session(
        self,
        name: str,
        raw: dict,
        stack: AsyncExitStack,
    ) -> Any:
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        transport = str(raw.get("transport", "stdio")).lower()
        if transport == "stdio":
            params = StdioServerParameters(
                command=str(raw["command"]),
                args=[str(item) for item in raw.get("args", [])],
                env=dict(mark_agent_environment({
                    **os.environ,
                    **{str(k): str(v) for k, v in raw.get("env", {}).items()},
                })),
            )
            # Redirect MCP subprocess stderr to a log file so it doesn't leak
            # into the TUI frontend as [backend] noise.
            log_dir = Path(os.getenv("AGENT_LOG_DIR", ".logs"))
            log_dir.mkdir(parents=True, exist_ok=True)
            errlog_path = log_dir / f"mcp-{name}.log"
            errlog_file = errlog_path.open("a", encoding="utf-8")
            started_at = __import__("datetime").datetime.now().isoformat()
            errlog_file.write(f"\n--- MCP server '{name}' started at {started_at} ---\n")
            errlog_file.flush()
            stack.callback(errlog_file.close)
            streams = await stack.enter_async_context(stdio_client(params, errlog=errlog_file))
        elif transport in {"http", "streamable-http"}:
            from mcp.client.streamable_http import streamablehttp_client

            streams = await stack.enter_async_context(streamablehttp_client(
                str(raw["url"]),
                headers={str(k): str(v) for k, v in raw.get("headers", {}).items()},
            ))
        else:
            raise ValueError(f"unsupported transport: {transport}")
        return await stack.enter_async_context(ClientSession(
            streams[0],
            streams[1],
            message_handler=self._message_handler(name),
        ))

    async def _connect_one(self, registry: ToolRegistry, name: str, raw: dict) -> None:
        """Connect one parsed server while keeping its registered tool names stable."""
        try:
            import mcp  # noqa: F401 - availability check
        except ModuleNotFoundError:
            self._set_status(name, "unavailable", error="install with: pip install -e .[mcp]")
            return
        stack = AsyncExitStack()
        server_timeout = max(1.0, float(raw.get("startup_timeout", self.startup_timeout)))
        try:
            async with asyncio.timeout(server_timeout):
                session = await self._enter_server_session(name, raw, stack)
                await session.initialize()
                response = await self._list_all_tools(session)
            tools = list(getattr(response, "tools", []) or [])
            registered = self._register_tools(registry, name, session, tools, raw)
            self._sessions[name] = session
            self._server_stacks[name] = stack
            self._stacks.append(stack)
            self._set_status(name, "ready", tools=registered)
        except asyncio.CancelledError:
            await self._safe_aclose(stack, context=f"{name} cancelled reconnect")
            raise
        except TimeoutError:
            cleanup_warning = await self._safe_aclose(stack, context=f"{name} startup timeout")
            error = f"startup timed out after {server_timeout:g}s"
            if cleanup_warning:
                error += f"; cleanup warning: {cleanup_warning}"
            self._set_status(name, "error", error=error)
        except Exception as exc:
            await self._safe_aclose(stack, context=f"{name} reconnect failure")
            error = f"{type(exc).__name__}: {exc}"
            state = "auth_required" if self._auth_failure(error) else "error"
            self._set_status(name, state, error=error)

    @staticmethod
    def _validate_config(payload: Any) -> list[str]:
        if not isinstance(payload, dict):
            return ["root config must be an object"]
        warnings = [f"unknown root field: {key}" for key in sorted(set(payload) - _MCP_ROOT_KEYS)]
        servers = payload.get("servers", {})
        if not isinstance(servers, dict):
            return warnings
        for name, raw in servers.items():
            if not isinstance(raw, dict):
                continue
            prefix = f"servers.{name}"
            warnings.extend(
                f"unknown or unsupported field: {prefix}.{key}"
                for key in sorted(set(raw) - _MCP_SERVER_KEYS)
            )
            if raw.get("enabled", True) is False:
                continue
            transport = str(raw.get("transport", "stdio")).lower()
            if transport == "stdio" and not raw.get("command"):
                warnings.append(f"missing required field: {prefix}.command")
            if transport in {"http", "streamable-http"} and not raw.get("url"):
                warnings.append(f"missing required field: {prefix}.url")
            for field in ("include_tools", "exclude_tools"):
                value = raw.get(field)
                if value is not None and (
                    not isinstance(value, list)
                    or any(not isinstance(item, str) or not item.strip() for item in value)
                ):
                    warnings.append(f"{prefix}.{field} must be a list of non-empty tool names")
        return warnings

    def _register_tools(
        self,
        registry: ToolRegistry,
        server: str,
        session: Any,
        tools: list[Any],
        config: dict,
    ) -> int:
        default_risk = str(config.get("risk", "network"))
        risk_overrides = config.get("tool_risks", {})
        configured_include = config.get("include_tools")
        include_tools = (
            {str(item) for item in configured_include}
            if isinstance(configured_include, list)
            else None
        )
        configured_exclude = config.get("exclude_tools")
        exclude_tools = (
            {str(item) for item in configured_exclude}
            if isinstance(configured_exclude, list)
            else set()
        )
        side_effect_approvals = ScopedApprovalStore(
            enabled=lambda: registry.approval_handler is not None,
            approved_scopes=registry.approved_permission_scopes,
        )
        definitions: list[ToolDef] = []
        for remote in tools:
            remote_name = str(getattr(remote, "name", ""))
            if (include_tools is not None and remote_name not in include_tools) or remote_name in exclude_tools:
                continue
            local_name = mcp_tool_name(server, remote_name)
            schema = getattr(remote, "inputSchema", None) or {"type": "object", "properties": {}}
            description = str(getattr(remote, "description", "") or f"MCP tool {remote_name} from {server}")
            risk = str(risk_overrides.get(remote_name, default_risk)) if isinstance(risk_overrides, dict) else default_risk

            async def invoke(
                _remote_name=remote_name,
                _local_name=local_name,
                _server=server,
                _artifact_dir=registry.artifact_dir,
                **kwargs,
            ):
                current_session = self._sessions.get(_server, session)
                try:
                    result = await current_session.call_tool(_remote_name, arguments=kwargs)
                except Exception as exc:
                    error = f"{type(exc).__name__}: {exc}"
                    state = "auth_required" if self._auth_failure(error) else "error"
                    self._sessions.pop(_server, None)
                    self._set_status(_server, state, error=error)
                    self._reconnect_event.set()
                    return ToolFailure(
                        code="mcp_auth_required" if state == "auth_required" else "mcp_unavailable",
                        message=f"MCP server {_server} is reconnecting: {error}",
                        retryable=True,
                        recovery_hint="Retry after the MCP server reconnects; re-authenticate first if required.",
                        tool_name=_local_name,
                        details={"mcp_server": _server, "mcp_tool": _remote_name},
                    )
                content = list(getattr(result, "content", []) or [])
                structured = getattr(result, "structuredContent", None)
                if structured is None:
                    structured = getattr(result, "structured_content", None)
                if structured is not None:
                    text = json.dumps(structured, ensure_ascii=False)
                else:
                    parts = []
                    for block in content:
                        if self._image_block(block) is not None:
                            continue
                        value = getattr(block, "text", None)
                        if value is None and hasattr(block, "model_dump"):
                            value = json.dumps(block.model_dump(), ensure_ascii=False)
                        parts.append(str(value if value is not None else block))
                    text = "\n".join(parts)
                is_error = bool(
                    getattr(result, "isError", False)
                    or getattr(result, "is_error", False)
                )
                if is_error:
                    detail = text.strip() or "MCP server returned isError=true without an error message"
                    detail = detail[: self.max_output_chars]
                    return ToolFailure(
                        code="mcp_tool_error",
                        message=f"[MCPToolError] {_server}/{_remote_name}: {detail}",
                        retryable=bool(_MCP_RETRYABLE_ERROR.search(detail)),
                        recovery_hint=(
                            "Check the MCP server configuration and logs. Retry only for a transient "
                            "network, timeout, rate-limit, or server error."
                        ),
                        tool_name=_local_name,
                        details={
                            "mcp_server": _server,
                            "mcp_tool": _remote_name,
                            "remote_output": detail,
                        },
                    )
                image_paths = self._persist_image_blocks(
                    content,
                    artifact_dir=_artifact_dir,
                    server=_server,
                    tool=_remote_name,
                )
                if image_paths:
                    question = str(
                        kwargs.get("question")
                        or kwargs.get("prompt")
                        or kwargs.get("instruction")
                        or ""
                    ).strip()
                    return json.dumps(
                        {
                            "type": "image_attachment",
                            "image_paths": image_paths,
                            "question": question,
                            "text": text[: self.max_output_chars],
                            "source": f"mcp:{_server}/{_remote_name}",
                        },
                        ensure_ascii=False,
                    )
                return text

            def permission_check(
                args: dict,
                _server=server,
                _remote_name=remote_name,
                _risk=risk,
            ) -> dict | None:
                if _risk not in {"write", "execute", "secret"}:
                    return None
                argument_preview = None
                if _risk == "secret":
                    argument_preview = {
                        str(key): "<redacted>"
                        for key in args
                    }
                return side_effect_approvals.request(
                    scope=f"mcp-side-effect:{_server}:{_remote_name}",
                    kind="mcp_side_effect",
                    operation=f"Call MCP tool {_remote_name}",
                    target=f"{_server}/{_remote_name}",
                    reason=f"MCP tool declares {_risk} side effects",
                    detail=(
                        "Session approval is limited to this exact server/tool pair. "
                        "The MCP server remains responsible for its advertised operation."
                    ),
                    arguments=argument_preview,
                )

            definitions.append(ToolDef(
                name=local_name,
                description=description,
                parameters=schema,
                fn=invoke,
                timeout=float(config.get("timeout", 60)),
                risk=risk,
                approval="on_risk",
                group=f"mcp:{_SAFE_NAME.sub('_', server).strip('_') or 'server'}",
                permission_check=permission_check,
                permission_grant=side_effect_approvals.grant,
            ))
        registry.replace_owned_tools(f"mcp:{server}", definitions)
        return len(definitions)

    @staticmethod
    def _image_block(block: Any) -> tuple[str, str] | None:
        block_type = str(getattr(block, "type", "") or "").lower()
        mime = str(
            getattr(block, "mimeType", None)
            or getattr(block, "mime_type", None)
            or ""
        ).lower()
        data = getattr(block, "data", None)
        if block_type != "image" or mime not in _MCP_IMAGE_EXTENSIONS or not isinstance(data, str):
            return None
        return mime, data

    @classmethod
    def _persist_image_blocks(
        cls,
        content: list[Any],
        *,
        artifact_dir: Path,
        server: str,
        tool: str,
    ) -> list[str]:
        max_bytes = int(os.getenv("MAX_AUTO_TOOL_IMAGE_BYTES", str(10 * 1024 * 1024)))
        output_dir = artifact_dir / "mcp-images"
        paths: list[str] = []
        for block in content:
            image = cls._image_block(block)
            if image is None:
                continue
            mime, encoded = image
            if len(encoded) > (max_bytes * 4 // 3) + 8:
                continue
            try:
                payload = base64.b64decode(encoded, validate=True)
            except (ValueError, binascii.Error):
                continue
            if not payload or len(payload) > max_bytes:
                continue
            output_dir.mkdir(parents=True, exist_ok=True)
            prefix = _SAFE_NAME.sub("_", f"{server}_{tool}").strip("_") or "mcp_image"
            path = output_dir / f"{prefix}_{uuid.uuid4().hex}{_MCP_IMAGE_EXTENSIONS[mime]}"
            path.write_bytes(payload)
            paths.append(str(path.resolve()))
        return paths

    def report(self) -> str:
        lines = [f"MCP config: {self.config_path}"]
        lines.extend(f"  warning: {warning}" for warning in self.config_warnings)
        for status in self.statuses:
            detail = f" ({status.tools} tools)" if status.tools else ""
            if status.error:
                detail += f" — {status.error}"
            lines.append(f"  {status.name}: {status.state}{detail}")
        return "\n".join(lines)

    async def close(self) -> None:
        self._closed = True
        self._reconnect_event.set()
        task = self._background_task
        self._background_task = None
        had_background_task = task is not None
        if task is not None and task is not asyncio.current_task() and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        if not had_background_task:
            while self._stacks:
                await self._safe_aclose(self._stacks.pop(), context="manager shutdown")
        self._server_stacks.clear()
        self._sessions.clear()


async def initialize_mcp_tools(
    registry: ToolRegistry,
    config_path: str | Path | None = None,
    *,
    background: bool = False,
) -> MCPManager:
    manager = MCPManager(config_path)
    if background:
        manager.start_background(registry)
    else:
        await manager.load(registry)
    return manager

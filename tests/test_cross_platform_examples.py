from __future__ import annotations

import json
import re
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
README = PROJECT_ROOT / "README.md"
ENV_EXAMPLE = PROJECT_ROOT / ".env.example"
FILESYSTEM_EXAMPLE = PROJECT_ROOT / "config" / "filesystem.example.json"
CHANNELS_EXAMPLE = PROJECT_ROOT / "config" / "channels.example.json"
GITIGNORE = PROJECT_ROOT / ".gitignore"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _markdown_section(markdown: str, heading: str) -> str:
    lines = markdown.splitlines(keepends=True)
    heading_match: re.Match[str] | None = None
    heading_index = -1
    for index, line in enumerate(lines):
        match = re.fullmatch(r"(?P<marks>#{1,6}) (?P<title>.+?)\s*\n?", line)
        if match and match.group("title") == heading:
            heading_match = match
            heading_index = index
            break
    assert heading_match is not None, f"missing Markdown heading: {heading}"

    level = len(heading_match.group("marks"))
    in_fence = False
    section_lines: list[str] = []
    for line in lines[heading_index + 1 :]:
        if line.startswith("```"):
            in_fence = not in_fence
        if not in_fence:
            match = re.match(r"^(?P<marks>#{1,6}) ", line)
            if match and len(match.group("marks")) <= level:
                break
        section_lines.append(line)
    return "".join(section_lines)


def _fenced_blocks(section: str) -> list[tuple[str, str]]:
    return re.findall(r"^```([^\n]*)\n(.*?)\n```[ \t]*$", section, re.MULTILINE | re.DOTALL)


def _env_block_containing(*keys: str) -> str:
    lines = _read(ENV_EXAMPLE).splitlines()
    indices = [
        next(index for index, line in enumerate(lines) if line.lstrip("# ").startswith(f"{key}="))
        for key in keys
    ]
    start = min(indices)
    while start > 0 and lines[start - 1].strip():
        start -= 1
    end = max(indices) + 1
    while end < len(lines) and lines[end].strip():
        end += 1
    return "\n".join(lines[start:end])


def _active_env_values() -> dict[str, str]:
    values: dict[str, str] = {}
    for raw_line in _read(ENV_EXAMPLE).splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key] = value
    return values


def test_state_defaults_use_astra_directory() -> None:
    values = _active_env_values()

    assert values["AGENT_FILESYSTEM_CONFIG"] == ".astra/filesystem.json"
    assert values["TOOL_RESULT_DIR"] == ".astra/tool-results"
    assert values["AGENT_MCP_CONFIG"] == ".astra/mcp.json"
    assert values["AGENT_SKILLS_PATH"] == ".astra/skills"
    assert ".agent_system" not in _read(ENV_EXAMPLE)


def test_committed_examples_have_no_developer_specific_paths() -> None:
    example_text = "\n".join(
        _read(path)
        for path in (README, ENV_EXAMPLE, FILESYSTEM_EXAMPLE, CHANNELS_EXAMPLE)
    )

    assert not re.search(r"/(?:Users|home)/(?!your-(?:user|name)(?:/|\b)|example(?:/|\b)|user(?:/|\b))[^/\s]+/", example_text)
    assert "桌面" not in example_text
    assert not re.search(r"(?i)[a-z]:\\[^\n]*(?:agent[_-]lab|astra-master)", example_text)


def test_json_examples_are_valid_and_use_portable_placeholders() -> None:
    filesystem = json.loads(_read(FILESYSTEM_EXAMPLE))
    channels = json.loads(_read(CHANNELS_EXAMPLE))

    assert filesystem["version"] == 1
    assert filesystem["roots"] == [{"path": "shared-files", "mode": "ro"}]
    assert channels["qq"]["send_file_roots"] == ["outputs"]


def test_readme_documents_supported_install_and_start_paths() -> None:
    readme = _read(README)
    installation = _markdown_section(readme, "First installation")
    assert "Python 3.11" in installation
    assert "Node.js 18" in installation
    # The unified launcher guide groups platform-specific examples within each
    # workflow instead of keeping the retired per-platform --setup-only guide.
    for heading in ("First installation", "Upgrade an older checkout"):
        blocks = _fenced_blocks(_markdown_section(readme, heading))
        windows = next(block for language, block in blocks if language == "powershell")
        posix = next(block for language, block in blocks if language == "bash")
        assert ".\\astra.bat setup --install-command" in windows
        assert "./astra.sh" not in windows
        assert "./astra.sh setup --install-command" in posix
        assert ".\\astra.bat" not in posix
        if heading == "Upgrade an older checkout":
            assert "git pull --ff-only" in windows and "git pull --ff-only" in posix
    assert "`--setup-only`" in readme  # Still documented as a compatibility entry point.
    assert "scripts\\astra-migrate.bat" in readme
    assert "./scripts/astra-migrate.sh" in readme
    assert "scripts\\checkmail.bat" in readme
    assert "./scripts/checkmail.sh" in readme
    assert "phase_t_gate.cmd" in readme
    assert "./scripts/phase_t_gate.sh" in readme


def test_readme_documents_safe_cross_platform_checkout_cleanup() -> None:
    readme = _read(README)
    section = _markdown_section(readme, "Moving a checkout between platforms")
    blocks = _fenced_blocks(section)
    powershell = next(block for language, block in blocks if language == "powershell")
    bash = next(block for language, block in blocks if language == "bash")
    windows_match = re.search(
        r"^Remove-Item .*? (?P<targets>\.venv, ui-tui\\node_modules)$",
        powershell,
        re.MULTILINE,
    )
    posix_match = re.search(r"^rm -rf (?P<targets>.+)$", bash, re.MULTILINE)

    assert windows_match is not None
    assert {target.strip().replace("\\", "/") for target in windows_match["targets"].split(",")} == {
        ".venv",
        "ui-tui/node_modules",
    }
    assert posix_match is not None
    assert set(posix_match["targets"].split()) == {".venv", "ui-tui/node_modules"}
    assert all(".astra" not in block for _language, block in blocks)
    assert re.search(r"(?i)\b(?:keep|preserve)\b[^\n]*`\.env`[^\n]*`\.astra`[^\n]*`\.sessions`", section)
    assert all(filename in section for filename in ("settings.json", "filesystem.json", "models.yaml"))


def test_minimal_bash_timeout_documentation_is_adjacent_and_platform_specific() -> None:
    readme_section = _markdown_section(_read(README), "Minimal Bash environment")
    env_block = _env_block_containing(
        "ASTRA_PERSISTENT_BASH_TIMEOUT",
        "ASTRA_WSL_PERSISTENT_TIMEOUT",
    )

    for documentation in (readme_section, env_block):
        assert "ASTRA_PERSISTENT_BASH_TIMEOUT" in documentation
        assert "ASTRA_WSL_PERSISTENT_TIMEOUT" in documentation
        assert "300" in documentation
        assert "timeout" in documentation.casefold()
        assert re.search(r"(?is)ASTRA_PERSISTENT_BASH_TIMEOUT.{0,180}(?:preferred|takes precedence)", documentation)
        assert re.search(r"(?is)ASTRA_WSL_PERSISTENT_TIMEOUT.{0,180}(?:legacy|fallback)", documentation)
        assert "WSL" in documentation
        assert "native" in documentation
        assert "macOS/Linux" in documentation


def test_browser_extractor_migration_warning_is_adjacent_to_variables() -> None:
    readme_section = _markdown_section(_read(README), "Web search")
    env_block = _env_block_containing("BROWSER_EXTRACT_ARGV", "WSL_EXTRACT_CMD")

    for documentation in (readme_section, env_block):
        assert "BROWSER_EXTRACT_ARGV" in documentation
        assert "BROWSER_EXTRACT_STATUS_CMD" in documentation
        assert "JSON" in documentation
        assert re.search(r"(?is)BROWSER_EXTRACT_CMD.{0,180}(?:preferred|takes precedence)", documentation)
        assert re.search(r"(?is)WSL_EXTRACT_CMD.{0,180}(?:fallback|legacy)", documentation)
        assert re.search(r"(?is)(?:copied|moving).{0,100}Windows.{0,100}macOS/Linux", documentation)
        assert re.search(r"(?is)(?:unset|remove|replace).{0,120}WSL_EXTRACT_CMD", documentation)
        assert re.search(r"(?is)WSL_EXTRACT_CMD.{0,180}every platform", documentation)


def test_readme_documents_platform_specific_integrations() -> None:
    readme = _read(README)

    assert "BROWSER_EXTRACT_CMD" in readme
    assert "WSL_EXTRACT_CMD" in readme
    assert "COMFYUI_LIFECYCLE" in readme
    assert "COMFYUI_NATIVE_ROOT" in readme
    assert "COMFYUI_NATIVE_PYTHON" in readme
    assert "build-sandbox-image.ps1" in readme
    assert "build-sandbox-image.sh" in readme


def test_readme_has_separate_windows_and_macos_path_examples() -> None:
    readme = _read(README)

    assert '"D:\\\\shared"' in readme
    assert '"\\\\\\\\wsl.localhost\\\\Ubuntu\\\\home\\\\user"' in readme
    assert '"/Users/your-name/shared-files"' in readme
    assert "D:\\\\allowed\\\\outputs" in readme
    assert "/Users/your-name/allowed/outputs" in readme


def test_readme_documents_explicit_hermes_paths() -> None:
    readme = _read(README)

    assert "scripts/import_hermes_history.py" in readme
    assert "--source-db" in readme
    assert "--target-db" in readme
    assert "HERMES_DB" in readme
    assert "ASTRA_SESSIONS_DB" in readme


def test_gitignore_preserves_current_and_legacy_state() -> None:
    ignored = _read(GITIGNORE).splitlines()

    assert ".astra/" in ignored
    assert ".agent_system/" in ignored
    assert "ui-tui/node_modules/" in ignored

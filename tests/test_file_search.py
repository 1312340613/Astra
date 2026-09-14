import asyncio
import os

import pytest

from agent.runtime.tools.files import register_file_tools
from agent.runtime.tools.registry import ToolRegistry


def run(coro):
    return asyncio.run(coro)


@pytest.mark.parametrize("relative", [".sandbox_notes.txt", ".sandbox_tmp/notes.txt", ".astra/notes.txt"])
@pytest.mark.parametrize("tool,pattern", [("search_files", "**/*notes*"), ("search_text", "retention needle")])
def test_explicit_search_overrides_all_noise_filters(tmp_path, relative, tool, pattern):
    target = tmp_path / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("retention needle\n", encoding="utf-8")
    registry = ToolRegistry()
    register_file_tools(registry, str(tmp_path))
    default = run(registry.execute(tool, {"pattern": pattern}))
    assert relative not in default["output"].replace("\\", "/")
    included = run(registry.execute(tool, {"pattern": pattern, "include_ignored": True}))
    assert included["error"] == ""
    assert relative in included["output"].replace("\\", "/")


@pytest.mark.parametrize("relative", [".env", ".astra/diagnostics/log.txt", ".sandbox_notes.txt", "private-key-notes.md"])
def test_authorized_file_reads_preserve_contents_regardless_of_name(tmp_path, relative):
    target = tmp_path / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    text = "ordinary notes\napi_key=synthetic-config-value\n"
    target.write_text(text, encoding="utf-8")
    registry = ToolRegistry()
    register_file_tools(registry, str(tmp_path))
    result = run(registry.execute("read_file", {"path": relative}))
    assert result["error"] == ""
    assert "api_key=synthetic-config-value" in result["output"]


@pytest.mark.parametrize("include_ignored", [False, True])
def test_filename_near_miss_respects_sandbox_override(tmp_path, include_ignored):
    (tmp_path / ".sandbox_tmp").mkdir()
    (tmp_path / ".sandbox_tmp/configure.py").write_text("", encoding="utf-8")
    registry = ToolRegistry()
    register_file_tools(registry, str(tmp_path))
    result = run(registry.execute("search_files", {"pattern": "**/configre.py", "include_ignored": include_ignored}))
    assert ("configure.py" in result["output"]) is include_ignored


def test_search_files_skips_noise_but_allows_explicit_search(tmp_path):
    (tmp_path / "agent").mkdir()
    (tmp_path / "agent" / "keep.py").write_text("", encoding="utf-8")
    for directory in (".astra", ".agent_system", ".venv", "node_modules"):
        target = tmp_path / directory
        target.mkdir()
        (target / "hidden.py").write_text("", encoding="utf-8")

    registry = ToolRegistry()
    register_file_tools(registry, str(tmp_path))

    default = run(registry.execute("search_files", {"pattern": "**/*.py"}))
    assert default["error"] == ""
    assert default["output"] == os.path.join("agent", "keep.py")

    explicit = run(
        registry.execute(
            "search_files",
            {"path": str(tmp_path / ".astra"), "pattern": "**/*.py"},
        )
    )
    assert explicit["error"] == ""
    assert explicit["output"] == "hidden.py"


def test_search_files_can_include_ignored_directories(tmp_path):
    (tmp_path / ".venv").mkdir()
    (tmp_path / ".venv" / "dependency.py").write_text("", encoding="utf-8")

    registry = ToolRegistry()
    register_file_tools(registry, str(tmp_path))

    result = run(
        registry.execute(
            "search_files",
            {"pattern": "**/*.py", "include_ignored": True},
        )
    )
    assert result["error"] == ""
    assert result["output"] == os.path.join(".venv", "dependency.py")


def test_search_files_no_match_suggests_near_miss(tmp_path):
    (tmp_path / "configure.py").write_text("x = 1\n", encoding="utf-8")

    registry = ToolRegistry()
    register_file_tools(registry, str(tmp_path))

    result = run(
        registry.execute(
            "search_files",
            {"pattern": "**/configre.py"},
        )
    )
    assert result["error"] == ""
    assert "No files matching" in result["output"]
    assert "configure.py" in result["output"]


def test_search_files_near_miss_respects_directory(tmp_path):
    """Near-miss suggestions stay inside the query's directory."""
    (tmp_path / "src").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "src" / "configure.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "tests" / "configure.py").write_text("x = 1\n", encoding="utf-8")

    registry = ToolRegistry()
    register_file_tools(registry, str(tmp_path))

    result = run(
        registry.execute(
            "search_files",
            {"pattern": "src/configre.py"},
        )
    )
    assert result["error"] == ""
    assert "No files matching" in result["output"]
    assert os.path.join("src", "configure.py") in result["output"]
    assert "tests" not in result["output"]


def test_search_files_no_match_no_suggestion_when_unrelated(tmp_path):
    (tmp_path / "main.py").write_text("x = 1\n", encoding="utf-8")

    registry = ToolRegistry()
    register_file_tools(registry, str(tmp_path))

    result = run(
        registry.execute(
            "search_files",
            {"pattern": "**/zzzzqqqq.py"},
        )
    )
    assert result["error"] == ""
    assert "No files matching" in result["output"]
    assert "Did you mean" not in result["output"]

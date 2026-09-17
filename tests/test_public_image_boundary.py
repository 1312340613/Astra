"""Keep the retired, installation-specific generator out of public releases."""

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_public_tree_omits_private_generator_and_fixtures():
    for relative_path in (
        "agent/runtime/tools/comfyui.py",
        "tests/test_comfyui_tools.py",
    ):
        assert not (ROOT / relative_path).exists(), relative_path


def test_public_runtime_and_guides_do_not_expose_retired_generator():
    # ComfyUI is still a supported PNG metadata format. Only the retired tool
    # names, registration function, and environment namespace are prohibited.
    retired_identifier = re.compile(r"\b(?:comfyui_\w+|register_comfyui_tools)\b", re.IGNORECASE)
    sources = [
        ROOT / ".env.example",
        ROOT / "docs" / "integrations.md",
        ROOT / "docs" / "zh-CN" / "integrations.md",
        *sorted((ROOT / "agent").rglob("*.py")),
        *sorted((ROOT / "ui-tui" / "src").rglob("*.ts")),
        *sorted((ROOT / "ui-tui" / "src").rglob("*.tsx")),
    ]
    violations = [
        str(path.relative_to(ROOT))
        for path in sources
        if retired_identifier.search(path.read_text(encoding="utf-8"))
    ]
    assert not violations, f"Retired generator references in public sources: {violations}"

"""Recognition of framework-owned suffixes in pre-separation session prompts."""

import re


SKILL_CATALOG_INTRO = (
    "开始编码/调试/测试/审查任务前，检查下方目录。有匹配的 Skill 必须先 skill_view 读取再执行。"
)


def split_legacy_suffix(prompt: str, current_suffix: str = "") -> tuple[str, int, int]:
    """Remove only complete, known generated trailing blocks.

    Historical project text has no unambiguous closing marker. Only an exact
    match to the currently loaded project block is removable; unknown/custom
    text stays intact. Catalog versions may differ, but must match the actual
    generator's grammar, intro and builtin core entry.
    """
    project = current_suffix.split("<available-skills>", 1)[0].strip()
    if not project.startswith("## Project guidance: "):
        project = ""
    remaining = prompt
    catalogs = projects = 0
    while True:
        tail = remaining.rstrip()
        marker = "\n\n<available-skills>\n"
        base, separator, body = tail.rpartition(marker)
        lines = body.splitlines()
        if (
            separator and base.strip() and len(lines) >= 4
            and lines[0] == SKILL_CATALOG_INTRO
            and lines[-1] == "</available-skills>"
            and any(line.startswith("- astra-core: ") and line.endswith(" [origin: builtin]")
                    for line in lines[1:-1])
            and all(
                re.fullmatch(r"\[[a-zA-Z0-9_-]+\]", line)
                or re.fullmatch(r"- [a-zA-Z0-9_-]+: .+ \[origin: (builtin|user|auto)\]", line)
                for line in lines[1:-1]
            )
        ):
            remaining = base
            catalogs += 1
        elif project and tail.endswith("\n\n" + project) and tail[:-len(project)].strip():
            remaining = tail[:-(len(project) + 2)]
            projects += 1
        else:
            break
    return remaining, catalogs, projects

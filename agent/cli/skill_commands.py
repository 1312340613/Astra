"""Shared parsing for /skills across CLI frontends."""

from agent.runtime.skills import SkillStore


SKILLS_USAGE = (
    "Usage:\n"
    "  /skills\n"
    "  /skills show <name> [file]\n"
    "  /skills create [name] [description]   # draft together before saving\n"
    "  /skills create --template <name> <description>   # create an empty template"
)


def execute_skill_command(store: SkillStore, args: list[str]) -> tuple[str, str]:
    action = args[0].lower() if args else "list"
    try:
        if action in {"list", "status"}:
            items = store.list()
            if not items:
                return f"Skills directory: {store.root}\nNo local skills installed.", ""
            lines = [f"Local skills ({len(items)}):"]
            lines.extend(
                f"  [{item['origin']}] [{item['category']}] {item['name']} — {item['description']} ({item['files']} files)"
                for item in items
            )
            lines.append("Origins: auto = automatically summarized; user = user-added/protected; builtin = packaged rules.")
            return "\n".join(lines), ""
        if action == "show" and len(args) >= 2:
            return store.view(args[1], args[2] if len(args) >= 3 else "SKILL.md"), ""
        if action == "create" and len(args) >= 3:
            name = args[1]
            description = " ".join(args[2:]).strip()
            content = f"---\nname: {name}\ndescription: {description}\n---\n\n# {name}\n\n## When to use\n\nDescribe the reusable trigger.\n\n## Workflow\n\n1. Add the verified procedure.\n"
            result = store.create(name, content, category="user")
            return f"Created user skill {result['name']} at {store.root / 'user' / result['name'] / 'SKILL.md'}. Excluded from /learn review.", ""
        return "", SKILLS_USAGE
    except (OSError, UnicodeError, ValueError) as exc:
        return "", str(exc)

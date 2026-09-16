"""Local browser lifecycle controls shared by the terminal frontends."""
import asyncio

from agent.runtime.tool_failure import ToolFailure


async def execute_browser_command(args, registry) -> tuple[str, str]:
    if len(args) > 1 or (args and args[0] not in {"status", "stop"}):
        return "", "Usage: /browser [status|stop]"
    action = args[0] if args else "status"
    tool = registry.get("browser_" + action)
    if tool is None:
        return "", "Browser control is unavailable."
    try:
        result = await asyncio.wait_for(tool.fn(), timeout=5)
    except TimeoutError:
        return "", "Browser release is still pending; new browser work waits for cleanup. Use /browser status to inspect."
    except Exception as exc:
        return "", f"Browser {action} failed: {exc}. Use /browser stop to retry cleanup."
    if isinstance(result, ToolFailure):
        return "", result.message
    return str(result), ""

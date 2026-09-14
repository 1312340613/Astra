"""Direct child of the Node fixture: checks independent parent + native reader."""
import asyncio
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from agent.cli.appshot_windows import read_windows_appshot
from agent.cli.appshots import AppshotValidationError
from agent.runtime.appshot_windows_process import read_windows_recipient_identity
from agent.runtime.appshot_media import AppshotMediaStore
from agent.cli.appshot_admission import AppshotAdmission
from agent.runtime.context import AgentContext


async def main():
    if sys.argv[1] == "--reload-media":
        helper, root = sys.argv[2:]
        os.environ["ASTRA_COMPUTER_HELPER_PATH"] = helper
        from agent.cli import sessions
        sessions.SESSION_DIR = Path(root)
        context = AgentContext()
        context.set_session(str(sessions.session_path("accepted")))
        assert context.load()
        assert "data:image/png;base64," in str(context.get_prompt())
        sessions.rename_session("accepted", "renamed")
        context.set_session(str(sessions.session_path("renamed")))
        assert context.load()
        assert "data:image/png;base64," in str(context.get_prompt())
        sessions.delete_session("renamed")
        print(json.dumps({"ok": True, "reload_after_broker_exit": True, "session_rename_delete": True}))
        return
    helper, path, broker, session, root, expected = sys.argv[1:]
    os.environ["ASTRA_COMPUTER_HELPER_PATH"] = helper
    assert read_windows_recipient_identity().wire() == json.loads(expected)
    try:
        await read_windows_appshot(helper, path, broker_id=broker, session_id="wrong-session", runtime_root=root)
    except AppshotValidationError:
        pass
    else:
        raise AssertionError("wrong session accepted")
    decoded = await read_windows_appshot(helper, path, broker_id=broker, session_id=session, runtime_root=root)
    assert decoded.width == decoded.height == 1
    assert decoded.projection["coverage"] == "unavailable"
    context = AgentContext()
    context.set_session(str(Path(root).parent / "accepted.json"))
    events, launched = [], []
    lock = asyncio.Lock()
    async def prepare(msg):
        assert "UNTRUSTED" in str(msg.to_chat_content()).upper()
        assert not any(part.get("type") == "appshot_image" for part in msg.to_chat_content())
    def launch(msg, text):
        # This replaces provider scheduling only; actual admission and native
        # media persistence run normally. No model/network call is made.
        content = msg.to_storage_content()
        assert "manifest_path" not in str(content)
        assert "base64," not in str(content)
        assert "media_id" in str(content)
        assert "base64," in str(msg.to_chat_content())
        context.add_user(content)
        context.save()
        launched.append(msg)
        lock.release()
        return True
    intake = AppshotAdmission(lock=lock, busy=lambda: False, context=lambda: context,
        prepare=prepare, launch=launch, send=events.append, runtime_root=root)
    command = {"submission_id": "synthetic-explicit-submit", "text": "Inspect synthetic [Appshot #1]",
        "appshot_session_id": session, "appshot_broker_id": broker,
        "appshots": [{"label": "[Appshot #1]", "manifest_path": path}]}
    await intake.submit(command)
    assert events[-1]["type"] == "message_accepted", events
    await intake.submit(command)
    assert events[-1]["replayed"] and len(launched) == 1
    assert not lock.locked()
    saved = set(AppshotMediaStore(context.session_path).path.iterdir())
    intake.launch = lambda *args: False
    await intake.submit({**command, "submission_id": "rollback"})
    assert events[-1]["type"] == "message_rejected"
    assert set(AppshotMediaStore(context.session_path).path.iterdir()) == saved
    assert intake.status("rollback")["status"] == "rejected" and not lock.locked()
    # Cancellation after verification must release the reservation and record a
    # rejection without allocating media or scheduling a provider turn.
    entered = asyncio.Event()
    async def cancelled_prepare(msg):
        entered.set()
        await asyncio.Future()
    intake.prepare = cancelled_prepare
    pending = asyncio.create_task(intake.submit({**command, "submission_id": "cancelled"}))
    await asyncio.wait_for(entered.wait(), 2)
    pending.cancel()
    try:
        await pending
    except asyncio.CancelledError:
        pass
    assert intake.status("cancelled")["status"] == "rejected" and not lock.locked()
    assert set(AppshotMediaStore(context.session_path).path.iterdir()) == saved
    print(json.dumps({"ok": True, "independent_parent": True, "png_pixels": True, "uia_semantics": True,
        "wrong_session_rejected": True, "admission_accepted": True, "media_durable": True,
        "replay_deduplicated": True, "rollback_preserved_existing": True, "cancelled_admission": True,
        "model_called": False}))


asyncio.run(main())

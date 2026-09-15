import asyncio
import os

import pytest

from agent.runtime.self_protection import HostProcessGuard, ProcessIdentity, SelfProtectionError


ROWS = [
    ProcessIdentity(4100, 4000, 4000, "python", "python -m agent.cli.backend", "start-a"),
    ProcessIdentity(4000, 3900, 4000, "node", "node /work/ui-tui/src/index.tsx", "start-b"),
    ProcessIdentity(3900, 3800, 3900, "astra", "python /bin/astra", "start-c"),
    ProcessIdentity(5000, 1, 5000, "python", "python /work/other.py", "start-d"),
]


def guard(rows=None):
    return HostProcessGuard(
        owner_pid=4100,
        parent_pids=[4000, 3900],
        snapshot=lambda: rows if rows is not None else ROWS,
        service_pids=lambda: [],
    )


@pytest.mark.parametrize("command", [
    "kill 4100", "kill -TERM 4100", "/bin/kill -9 4000", "kill -- -4000",
    "kill -9 -1", "kill 0", "pkill -f agent.cli.backend", "killall node",
    "kill $(pgrep -f agent.cli.backend)", "kill `pgrep -f agent.cli.backend`",
    "pid=4100; kill $pid", "sh -c 'kill 4100'", "env kill 4100",
    "printf '4100\\n' | xargs kill", "taskkill /PID 4100 /F",
    "Stop-Process -Id 4100 -Force", "taskkill /IM node.exe /F",
    'python3 -c "import os; os.kill(4100, 15)"',
])
def test_recognized_host_termination_is_rejected(command):
    with pytest.raises(SelfProtectionError, match="Astra"):
        guard().check_shell(command)


@pytest.mark.parametrize("command", [
    "kill 5000", "kill -0 4100", "pkill -f other.py", "killall sleep",
    "echo 'kill 4100'", "rg 'pkill -f astra' tests", "ps -p 4100",
    "python -c 'print(4100)'", "taskkill /PID 5000 /F",
])
def test_inspection_and_unrelated_processes_are_allowed(command):
    guard().check_shell(command)


def test_wsl_numbers_do_not_identify_windows_hosts_but_interop_does():
    guard().check_shell("kill 4100; pkill python", foreign_namespace=True)
    guard().check_shell("bash -c 'kill 4100'", foreign_namespace=True)
    with pytest.raises(SelfProtectionError):
        guard().check_shell("taskkill.exe /PID 4100 /F", foreign_namespace=True)
    with pytest.raises(SelfProtectionError):
        guard().check_shell('powershell.exe -Command "Stop-Process -Id 4100"', foreign_namespace=True)


def test_reused_parent_pid_is_not_protected():
    rows = list(ROWS)
    subject = guard(rows)
    subject.check_shell("kill 5000")  # Pin the verified runtime-parent identity.
    rows[1] = ProcessIdentity(4000, 1, 4000, "sleep", "sleep 30", "new-start")
    subject.check_shell("kill 4000")


@pytest.mark.parametrize("code", [
    "import os; os.kill(4100, 15)",
    "from os import kill as stop; stop(4100, 9)",
    "import os as system; target=4100; system.kill(target, 15)",
    "import os; os.killpg(4000, 15)",
])
def test_literal_python_termination_is_checked(code):
    with pytest.raises(SelfProtectionError):
        guard().check_python(code)


def test_python_inspection_and_child_self_exit_are_not_rejected():
    guard().check_python("import os; os.kill(5000, 15); os.kill(4100, 0)")
    guard().check_python("import os; os.kill(os.getpid(), 15)")


def test_explicit_owned_service_is_protected():
    subject = HostProcessGuard(owner_pid=4100, parent_pids=[], snapshot=lambda: ROWS,
                               service_pids=lambda: [5000])
    with pytest.raises(SelfProtectionError):
        subject.check_shell("kill 5000")


def test_local_execution_blocks_before_starting_child(tmp_path, monkeypatch):
    from agent.sandbox.local import LocalSandbox
    sandbox = LocalSandbox(workdir=str(tmp_path))
    monkeypatch.setattr(sandbox, "process_guard", guard())

    async def unexpected(*args, **kwargs):
        pytest.fail("A protected command must not reach a child process")

    monkeypatch.setattr(asyncio, "create_subprocess_shell", unexpected)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", unexpected)
    with pytest.raises(SelfProtectionError):
        asyncio.run(sandbox.execute_shell("kill 4100"))
    with pytest.raises(SelfProtectionError):
        asyncio.run(sandbox.execute_python("import os; os.kill(4100, 15)"))


def test_real_current_process_is_guarded_without_sending_signal():
    with pytest.raises(SelfProtectionError):
        HostProcessGuard().check_shell(f"kill {os.getpid()}")

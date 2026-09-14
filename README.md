# Astra ✦

A personal AI agent for everyday tasks, powered primarily by model APIs.
Astra combines computer use, memory, multimodal input, and an Ink terminal
interface, with sandboxed tools, resumable sessions, and optional local models,
MCP, and OpenTelemetry integrations.

> **Astra** (拉丁语"星辰") — Lyra 是天琴座，Astra 是整个系统。

## Install and start

Astra uses one launcher on Windows, macOS and Linux. **Use a Git source
installation for the current pre-release version.** A complete public release
installer is not available yet; the Python wheel alone does not include the
full Ink interface or macOS native helper.

Start here: [first installation](#first-installation) ·
[upgrade an older checkout](#upgrade-an-older-checkout) ·
[daily updates](#daily-updates) ·
[troubleshooting](#troubleshooting-and-recovery) ·
[installation and data locations](#installation-and-data-locations).

### First installation

Install Python 3.11+, Node.js 18+ (including npm), and Git. Setup uses the
committed Python and Node dependency locks. If uv is missing, setup installs a
private copy; it does not install Python packages globally. On Windows, the
launcher runs in CMD or PowerShell; the agent's [Bash tool](#minimal-bash-environment)
uses WSL separately.

**Windows — CMD or PowerShell:**

```powershell
git clone https://github.com/1312340613/Astra.git
cd Astra
.\astra.bat setup --install-command
```

**macOS or Linux:**

```bash
git clone https://github.com/1312340613/Astra.git
cd Astra
./astra.sh setup --install-command
```

Setup prepares `.venv`, installs and builds the interface, and registers the
`astra` command in your user PATH. It preserves an existing `.env`; otherwise,
it creates one from `.env.example`. Configure your model connection in that file
or use the [model connection commands](#model-connections) after starting Astra.
Installation checks do not require a model connection or API key.

Open a **new terminal** so PATH is refreshed, change to the project you want to
work on, and run:

```text
astra doctor
astra
```

The current directory becomes the workspace unless `SANDBOX_WORKDIR` explicitly
selects another one. The launcher finds its own installation independently of
your working project. Normal startup checks the environment; it does not fetch
code or install dependencies.

### Upgrade an older checkout

An older installation needs **one manual pull** to obtain the new launcher.
Close Astra sessions and services using that installation first, then run these
commands from its existing source directory.

**Windows — CMD or PowerShell:**

```powershell
git pull --ff-only
.\astra.bat setup --install-command
```

**macOS or Linux:**

```bash
git pull --ff-only
./astra.sh setup --install-command
```

If the pull fails, resolve the reported Git issue before continuing. Existing
configuration, conversations and memory are retained. Open a new terminal and
run `astra doctor`; subsequent updates use `astra update`.

### Daily updates

After changes have been pushed from another machine, check for them from any
working directory:

```text
astra update --check
```

Checking fetches and compares the current branch's configured Git upstream,
such as `origin/main`. It can run while Astra is active and does not replace
working files or restart sessions. To apply an update, close interactive Astra
sessions; the updater pauses and restores recognized companion services for you:

```text
astra update
astra doctor
astra
```

The updater fast-forwards to a fixed commit, synchronizes affected dependencies,
builds the interface when needed, and verifies the result. An `applied` result
means the installation is ready for the **next launch**; sessions are not
automatically restarted. Source updates transfer code, not configuration or
conversation history between machines.

Use the output to check what happened:

| Output | Meaning |
| --- | --- |
| `outcome: available` | The checked upstream commit differs from this checkout. The check has not applied it. |
| `outcome: applied` | The update completed. Run `astra` to start the updated version. |
| `outcome: current` | The checkout already matches the checked upstream commit. |
| `outcome: cancelled` | You cancelled the update; the working files were left unchanged. |
| `local_changes: True` or `dirty: True` | Local file differences remain. This is expected after keeping local files and can coexist with a successful update. |

Local changes are compared with the incoming version before applying it. Edits
to files that the update does not touch are kept automatically. If a local file
also changes upstream, the terminal lists the local files and offers:

1. **Keep local files** (default): keep each locally changed file whole, including
   its contents, deletion and executable mode, and update the other files.
2. **Back up and overwrite local files**: save the listed local files, then use
   their incoming versions. This choice covers all listed local files.
3. **Cancel**: leave the installation as it is.

Keeping is not a text merge. A successful update can still show `dirty: True`
because your local files were retained. Staged and unstaged edits remain separate.
An untracked or ignored file that an incoming file would replace also requires
a choice. Configuration, conversations and memory are not overwrite candidates.

Backups remain under `.astra/launcher/local-changes/<id>/` after success; the
result prints that location. `files/` contains the original file contents using
their original relative paths, and `manifest.json` records their types and
deletions. Keep the complete backup until you no longer need it.

For scripts, choose `astra update --keep-local` or `astra update --overwrite-local`
explicitly. `--json` never opens an interactive prompt; overlapping changes
without an explicit policy stop before any file is replaced. An already-current,
healthy installation is reported as current without discarding local edits.

Divergent/detached branches, unsupported file/directory conflicts and shared
dependency directories still require resolution. The updater does not switch
branches or delete local commits. After a manual `git pull`, run `astra setup`
if startup reports changed or unverified dependencies.

### Command reference

Run these commands in your terminal. They are separate from slash commands
inside a conversation; for example, `astra doctor` checks installation health,
while `/doctor` checks the running application's connections.

| Command | Use it to |
| --- | --- |
| `astra` | Start the Ink interface in your working project. |
| `astra version --json` | Inspect the installation, commit, paths and tracked running-instance versions. |
| `astra doctor` | Check local installation health without a model key or network connection. |
| `astra setup` | Prepare or verify the current source dependencies. |
| `astra setup --install-command` | Prepare dependencies and register the user command. |
| `astra setup --command-only` | Register only the command and PATH, leaving dependencies alone. |
| `astra setup --extra notebook` | Enable and remember an optional dependency group; repeat `--extra` for more groups. |
| `astra update --check` | Fetch and compare the configured upstream without applying changes. |
| `astra update` | Apply a source update and verify the installation. |
| `astra update --keep-local` | Keep locally changed files exactly and update other files. |
| `astra update --overwrite-local` | Back up locally changed files, then use incoming versions. |
| `astra setup --repair` | Rebuild dependencies for the current source, including intentional metadata edits. |
| `astra update --repair` | Repair the current clean commit without fetching a newer one. |
| `astra update --recover` | Recover an interrupted setup or update. |
| `astra --cli` | Start the legacy text CLI explicitly. |

`setup`, `update`, `version` and `doctor` accept `--json` for structured results.
For setup without PATH changes, use `astra setup`; to register a command in a
directory you manage, use `astra setup --command-only --bin-dir PATH --no-path`.
Before registration, substitute `.\astra.bat` on Windows or `./astra.sh` on POSIX
for `astra` when running from the source directory.

The source wrappers also accept a `PYTHON` executable override; for example,
`PYTHON=python3.11 ./astra.sh setup` on macOS/Linux. `--setup-only` remains
an alias for setup. Use `astra setup --command-only` to register only the command.
The agent connects to configured model endpoints but does not manage their server
processes.

### Troubleshooting and recovery

| What you see | What to do |
| --- | --- |
| `astra` is missing or starts an old copy | Open a new terminal. Run `where.exe astra` in CMD/PowerShell or `command -v astra` on POSIX. Register the intended checkout with its source wrapper and `setup --command-only`. Another installer's command is not overwritten. |
| Dependencies are unverified or missing | Close instances using the installation, then run `astra setup`; use `astra setup --repair` if the environment is damaged. |
| Processes are still using the installation | Close the listed interactive Astra sessions. Recognized companion services are paused and restored automatically; an unknown process still needs to be stopped through its owner. |
| Code updated, but a service did not restart | Run `astra update --recover`. This retries the recorded service restoration without reverting the successfully installed code. |
| Local files need a choice | Run `astra update` in a terminal and select Keep, Overwrite or Cancel; scripts can use `--keep-local` or `--overwrite-local`. |
| A diverged branch blocks an update | Inspect `git status --short` and `git branch -vv` in the source directory and resolve the Git history. Overwrite applies to local files, not local commits. |
| No tracking upstream or no Git history | Configure the intended branch's Git upstream. A downloaded source ZIP needs a Git clone to use `astra update`. |
| Windows reports the updating Python environment is in use | Invoke `.\astra.bat update` from the source directory so maintenance runs outside the environment it replaces. |
| Setup/update was interrupted | Close remaining holders and run `astra update --recover`, then `astra doctor`. Retain `.astra/launcher/pending.json` and its transaction snapshots until recovery completes. |
| A copied or moved checkout reports stale environments | Recreate its generated dependencies and register the command at the new path; see [moving a checkout](#moving-a-checkout-between-platforms). |

Failed updates restore the previous source, original staged/unstaged local work,
and affected generated directories. They do not restore an old conversation
database over newer user data. If a
file lock or concurrent edit prevents recovery, Astra retains its recovery
record and reports the next action. Do not delete user data or the journal to
bypass the error.

<details>
<summary>Automatic companion-service restart, and upgrading an older updater</summary>

Current `astra update` and dependency repair remember which companion services
were enabled, pause them before changing the runtime, and restore the same
configuration afterwards. On macOS this covers the installed browser bridge,
activity recorder, activity summarizer and history sync belonging to this
installation, plus any shared MLX embedding worker started by it. Embedding
workers use their authenticated local stop endpoint after active encoding ends;
restoration starts model preparation in the background. Periodic services are
paused even between scheduled runs. On
Windows, the opt-in activity recorder is stopped gracefully and restarted with
its original arguments and pause setting; it owns its browser bridge and summary
worker. Linux currently installs no equivalent companion service.

Services that were off stay off. Appshot starts with its next Astra session;
external model and MCP servers keep their own lifecycle. A check, cancelled local
file choice, or healthy `outcome: current` does not restart services.

`services_status: restored` confirms that the enabled schedules/recorder processes
were restored. If it says `restart_failed`, the command exits nonzero and keeps
the recovery record: run `astra update --recover`. A failed update restores
services after rollback; incomplete rollback keeps them paused until recovery.
Service progress is printed separately from `--json` output.

An older installed updater may still report the browser recorder as a blocker.
Closing the TUI does not stop that background service. For that one-time upgrade,
temporarily unload the installed browser service before updating:

```bash
launchctl bootout "gui/$(id -u)/com.astra.activity-browser-bridge"
```

After maintenance or recovery completes, restore the same saved configuration:

```bash
launchctl bootstrap "gui/$(id -u)" "$HOME/Library/LaunchAgents/com.astra.activity-browser-bridge.plist"
```

These steps apply only to an already installed browser recorder. They preserve
its configuration and pairing data; other services use their own stop/start
procedures.

</details>

### Installation and data locations

`astra version` and `astra doctor` report which installation owns the command.
The installation location determines what an update changes, regardless of the
project you are working in.

| Location or operation | Source installation | Package-installed distribution |
| --- | --- | --- |
| Program files | The actual source checkout. | The original installer's package directory. |
| Update method | `astra update`, using the configured Git upstream. | Upgrade through the original installer; Git updates are refused. |
| Default configuration | `<source>/.env` | `<user data>/.env` |
| Default private state | `<source>/.astra` | Platform user-data directory. |
| Default conversations | `<source>/.sessions` | `<user data>/sessions` |

Installed-distribution user data defaults to `%LOCALAPPDATA%\Astra` on Windows,
`~/Library/Application Support/Astra` on macOS, and `$XDG_DATA_HOME/astra`
(normally `~/.local/share/astra`) on Linux. `ASTRA_HOME` selects a different
private-data directory and defaults sessions to its `sessions` subdirectory;
explicit per-store overrides retain precedence. Source `.env` stays beside the
source checkout.

The source command is installed in `%LOCALAPPDATA%\Astra\bin` on Windows or
`~/.local/bin` on POSIX. Setup updates only the user's PATH configuration.
Package ownership and data isolation are in place, while a complete release
bundle remains future work. `astra doctor` reports resources missing from a
Python-only wheel.

See [the launcher and update guide](docs/launcher-update.md) for environment
ownership, transaction records and recovery details, and [Verify](#verify) for
the automated checks and platform acceptance requirements.

The source checkout includes the 163 mail workflow at
`.astra/skills/operations/163-email-sync/SKILL.md`, where the existing
`SkillStore` discovers repository-local skills after the normal project-trust
check. Setup does not copy, install, or update it. The narrow `.gitignore`
exception tracks only that file; mail cache, settings, sessions, and all other
`.astra` state remain local and ignored. This repository-local integration is
source-checkout scoped and works with both POSIX and Windows launchers.

### Reproducible development and maintenance setup

`uv.lock` and `ui-tui/package-lock.json` are committed. With Python 3.11+, Node
18+ and [uv](https://docs.astral.sh/uv/) installed, run from the repository root
on any supported platform:

```text
uv sync --locked --python 3.11 --extra dev --extra mcp --extra tracing --extra server --extra notebook
npm --prefix ui-tui ci
```

The development extra pins pytest, Ruff and Pyright. Add `--extra embedding`
when the local embedding backend is needed; model downloads and native helper
installation are separate. On an existing environment, `uv sync --inexact`
preserves additional installed packages. The shared `astra setup` command uses
`uv sync --locked --inexact`, records enabled extras and dependency freshness,
and runs these locked installation steps plus the interface build. Dependency
conflict checks still apply. The manual commands above remain useful for
development and release checks; use `astra setup` afterward to record the
verified environment.

The Python wheel contains the Agent, bundled model profiles and Session Recall.
The Ink UI, repository-local skills and macOS native helper require the source
checkout and their respective setup steps.

### Moving a checkout between platforms

A Windows virtual environment and native Node modules cannot be reused on
macOS (or vice versa). A checkout moved to another absolute path also needs its
environments recreated. Close sessions and services using the checkout, remove
only the generated dependency directories, then rebuild them and register the
command at the new source location:

```powershell
# Moving to Windows
Remove-Item -Recurse -Force -ErrorAction SilentlyContinue .venv, ui-tui\node_modules
.\astra.bat setup --install-command
```

```bash
# Moving to macOS/Linux
rm -rf .venv ui-tui/node_modules
./astra.sh setup --install-command
```

Keep `.env`, `.astra` and `.sessions`: they contain configuration, tasks, memory,
conversations and other user state. Open a new terminal after registration.
Before starting on the new host, inspect `.astra/settings.json`,
`.astra/filesystem.json`, and `.astra/models.yaml` and replace any absolute path
that belongs to the old platform. Legacy `.agent_system` data can be merged with
`.\scripts\astra-migrate.bat` on Windows or `./scripts/astra-migrate.sh` on macOS/Linux.

## Optional OpenAI-compatible API

The API runs separately from the TUI and requires the `server` extra:

```text
uv run --locked --extra server python -m agent.cli.api_server
```

It defaults to `127.0.0.1:8900`. `ASTRA_API_HOST` and `ASTRA_API_PORT` override
the binding. Set `ASTRA_API_KEY` in the environment or repository `.env` to
require `Authorization: Bearer <key>` on **every endpoint**, including `/health`
and `/v1/models`. The API key is separate from the upstream model API key.
Binding to a non-loopback address requires this key; startup rejects an
unauthenticated public binding. Requests carrying a browser `Origin` header
also require configured bearer authentication, even on loopback.

`POST /v1/chat/completions` supports streaming and non-streaming responses.
Malformed consumed fields return HTTP 400 before session or Agent work. This
bridge exposes Astra's local tools to authenticated callers; use a trusted
network or a TLS reverse proxy for remote access. Native messaging channels
remain a separate integration.

## Native messaging channels

The Ink backend owns optional messaging adapters in the same process. Incoming
messages go directly to the existing ReAct agent; AstrBot and the experimental
OpenAI-compatible API bridge are not involved. Each private chat and group gets
an isolated persistent session under `.sessions/channel_*`, and channel turns
share one lifecycle lock with the TUI so a stateful agent is never used
concurrently with two different contexts.

QQ currently uses a native OneBot v11 reverse-WebSocket adapter. NapCat remains
the QQ protocol client and connects directly to Astra. Copy the example before
enabling it:

```powershell
# Windows
New-Item -ItemType Directory -Force .astra | Out-Null
Copy-Item config/channels.example.json .astra/channels.json
```

```bash
# macOS/Linux
mkdir -p .astra
cp config/channels.example.json .astra/channels.json
```

Then set `qq.enabled` to `true`. The safe default listens on
`127.0.0.1:2280`; configure NapCat's reverse WebSocket URL as
`ws://127.0.0.1:2280`. If an access token is configured in NapCat, put the same
value in `ASTRA_QQ_ACCESS_TOKEN`—the token itself is never stored in the JSON
file. Private messages are accepted by default. Group messages require either
an @mention or a configured wake prefix (default `/`) to avoid unsolicited
replies.

Only one service may own port 2280. Stop or reconfigure AstrBot before enabling
the native QQ adapter. Adapter startup failure is reported without taking down
the TUI, and all adapters are closed when the Astra backend exits. Tool
operations that would normally require an interactive approval are denied on
messaging channels rather than waiting forever for a hidden prompt.

QQ image segments are downloaded with public-URL checks and bounded size, then
passed to vision-capable models as native multimodal content. Local file sending
is deliberately more restrictive: enable `send_files_enabled`, list trusted
sender QQ IDs in `file_allow_users` (or `allow_users`), and restrict
`send_file_roots` to output directories that are safe to share. The
`channel_send_file` tool rejects paths outside those roots and common credential
files even when requested by the model.

The channel contract is protocol-neutral so a Weixin iLink adapter can join the
same manager without changing Agent routing or session isolation. The first
native release intentionally implements QQ/OneBot only; it does not silently
take over the existing Hermes Weixin login.

## Model connections

Bundled profiles live in `config/models.yaml`. Machine-specific profiles are
written to `.astra/models.yaml` and override bundled entries without
storing API-key values.

```text
/model
/connect my-local http://127.0.0.1:8084/v1 LLM_API_KEY
/doctor
```

The third `/connect` argument is the environment-variable name holding the API
key; it defaults to `LLM_API_KEY`.

### DeepSeek model migration

Astra uses `deepseek-flash` (DeepSeek V4.1 Flash) for its built-in DeepSeek
profile, including native image input, thinking controls, and the `flash` /
`fast` worker aliases. On the official `api.deepseek.com` endpoint, saved V4
Flash, Vision Exp, and temporary V4.1 selections resolve to this profile;
retired names are not offered as separate models. `deepseek-v4-pro` remains a
selectable text-only profile of its own. Custom and local endpoints keep their
own model IDs. The 1M context and 384K output allowances are unchanged.

The [September 10 announcement](https://api-docs.deepseek.com/zh-cn/updates/)
retires V4 Flash and Vision Exp immediately; the scheduled September 14 V4 Pro
redirect was later reversed — DeepSeek keeps serving `deepseek-v4-pro` with
unchanged billing. Astra keeps V4 Pro selectable as its own model. Existing
Astra processes need a restart to load the updated adapter.

### DeepSeek vision tiling

The `deepseek-flash` profile preserves detail in large local or
base64 images with original-pixel tiling. The preference defaults to ON, applies
immediately, and is saved as `vision_tiles_enabled` in `.astra/settings.json`:

```text
/vision-tiles
/vision-tiles on
/vision-tiles off
```

When enabled for that configured DeepSeek vision model, Astra creates lossless
768 x 768 detail crops with 64 px overlap plus an overview. Requests are capped
at 32 images; if every crop does not fit, the model selects additional crops
through the request-scoped tile tool. Generated tiles are cached under
`.astra/image-cache/tiles` with bounded cleanup.

Original-pixel tiling is guaranteed only for local paths and base64 image data.
External image URLs are sent directly because Astra does not download them, and
small animated GIFs are sent directly without tiling. An animated GIF that
requires tiling fails closed with a recoverable error instead of silently
sending a provider-downscaled image.
`/vision-tiles off` restores direct image sending, so DeepSeek may downscale
large images and lose detail. Other model profiles remain unaffected.

## Model time context

User messages carry a model-only timestamp such as
`<message_time>2026-09-14T00:30:00+08:00 周一</message_time>`. The weekday comes from
the same local date as the timestamp; replaying history keeps the original date.
Relative-date anchors also include a weekday derived from their saved date.
The visible time rail is unchanged, and output/history filters recognize both
old ISO-only markers and the weekday form. Existing sessions need no migration.
Use `current_time` when an exact current time or a different timezone is needed.

## Runtime responsiveness and profiling

Task checkpoints, approval persistence, active session saves and Session Recall writes run off the
backend event loop. One bounded worker persists and sends events in order,
including replay responses. A slow database therefore does not freeze the loop
that accepts input and cancels model work. Durable tool claims still finish
before a tool can execute; cancellation drains an in-flight write before task
cleanup. Accepted events drain on a normal backend exit.

For a latency diagnosis, set `ASTRA_PROFILE_QUERY=1` in your installation's
`.env`, restart Astra, reproduce the delay, then exit normally. This enables:

- `.astra/query-profile.jsonl`: request preparation, first model event, first
  reasoning, first answer text and first tool batch, plus cache statistics.
- `.astra/runtime-profile.jsonl`: tool queue/approval/execution/postprocessing,
  storage I/O, event persistence/delivery and frontend handling/React commit.
  Request and call identities are hashed so related records can be compared.

With a source installation, run these from the installation directory using its
environment (`.venv/bin/python` on macOS/Linux, `.venv\Scripts\python.exe` on
Windows):

```text
python -m agent.runtime.latency
python -m agent.runtime.query_profiler
python scripts/benchmark_runtime_responsiveness.py --samples 20 --lock-ms 150
```

The runtime report defaults to the latest backend lifetime; `--generation ID`
selects an earlier one in the file. It reports sample count, median, p95 and
maximum separately for each stage. The offline benchmark uses temporary SQLite
databases and no model calls. It measures event-loop stalls separately from
durable delivery time; a database lock still has to clear.
See the [runtime profiling and diagnosis guide](docs/runtime-responsiveness.md).

Profiling is off by default and does not change the model or reasoning effort.
It records timing metadata, not prompts, tool arguments or results. Runtime
records have a bounded queue and a 5 MiB file with two rotated backups. Dropped
records, write failures and unacknowledged frontend samples appear in the final
profiler summary. A report without that final summary has `complete: false`. Frontend
samples use a separate monotonic clock: React commit is not terminal paint,
and the receipt round trip includes transport and backend input processing.
With synchronous rendering, event handling and React commit can overlap.
When `ASTRA_HOME` is set, the profiler files live there instead of `.astra`.

## Persona

Astra bundles one public persona, **Lyra**, for work and everyday conversation.
It uses a general collaboration style and does not assume a private relationship
or personal history with the user. `/persona` lists the profile; `/persona lyra`
selects it. Writing and bar modes use separate, public creative briefs.
See [persona behavior and session compatibility](docs/persona.md).

## Reasoning intensity

`/mode` controls reasoning intensity independently of persona, tools and output budgets.

```text
/mode
/mode low
/mode high
/mode max
```

The default is `high`. The selected `reasoning_effort` is saved in
`.astra/settings.json` and applies to the next model request. The DeepSeek
adapter sends this parameter; other adapters keep their existing behavior and
report that the preference is not applied. Model configuration continues to
own the output budget. `/think` controls reasoning visibility separately.

Old saved `agent_mode` values migrate as `coding` → `max`, `chat` → `high`.
The coding/chat commands and `/mode code` selector are retired. Normal sessions
use native tools; tool exposure is no longer a user mode setting.
The status bar retains `TOOLS NATIVE` as a read-only runtime indicator after the
effort label, followed by the model, context usage and latest tool result.
The model stays in the summary while tools run. `Ctrl+L` opens session and
context details without repeating the model, reasoning indicator or latest
tool result. Narrow windows shorten secondary labels to keep the model,
context usage and expansion shortcut visible; Computer Use control state
takes priority in very small windows.

After each successful model request, the status bar shows its average output
speed in `tok/s` (`t/s` in compact layouts). This uses the provider's
`completion_tokens` divided by that request's elapsed time, including the
first-token wait and excluding tool execution. DeepSeek's count includes
reasoning tokens. The expanded `SPEED` row shows the token count and duration;
this is the latest completed request's average, not a live token estimate.
Missing usage does not produce an estimated rate. Switching models or sessions
clears the previous measurement.

## Structured clarification

During non-trivial coding work, Astra may pause to show a question card with
selectable options and a custom answer. The answer resumes the same agent turn.
Once the request is clear, Astra can populate the Working Plan panel, establish
Goal Mode, and begin work when the original request authorized implementation.

A choice is not a security approval. Filesystem, shell, MCP, workflow, and
host-execution operations still use their existing approval checks. On
messaging channels the interactive question tool returns a recoverable failure,
so the model can ask again in ordinary text instead of opening a hidden wait.

## Minimal Bash environment

Minimal Mode's `bash` tool keeps one persistent interactive shell, so working
directory and exported variables survive between calls. On Windows that shell
runs as WSL Bash; on macOS/Linux it runs as native Bash and reports the neutral
`posix` environment.

Each command has a 300-second timeout by default. `ASTRA_PERSISTENT_BASH_TIMEOUT`
is the preferred cross-platform override and takes precedence.
`ASTRA_WSL_PERSISTENT_TIMEOUT` is the legacy fallback, consulted only when the
preferred variable is unset; despite its name, it remains accepted for backward
compatibility on both WSL and native hosts.

## Host filesystem access

Python and shell execution use Docker isolation by default. Use `/sandbox` to
inspect the active backend, `/sandbox off` for guarded host execution (needed
for commands such as `wsl.exe`), and `/sandbox on` to return to Docker. The
choice is applied immediately and saved in `.astra/settings.json`.
Host execution still keeps the local timeout, output limit, and dangerous-command
checks; it is not equivalent to unrestricted shell access. The
`read_file`, `search_files`, `write_file`, and `edit_file` tools instead use an
explicit host-filesystem policy, so approved Windows drives and WSL UNC paths
can be accessed without exposing them to arbitrary sandboxed commands.

The current workspace is always read-write. Copy
`config/filesystem.example.json` to `.astra/filesystem.json` and replace its
portable `shared-files` placeholder with the host path you intend to expose.
Add extra roots as `ro` or `rw`; external roots should normally stay read-only.
`AGENT_FILESYSTEM_CONFIG` can select a different policy file.

The default Docker image is the small `python:3.12-slim` base image. For
Minimal `run_code` checks that need project dependencies or `pytest`, build the
shared development image and opt into it:

```powershell
.\scripts\build-sandbox-image.ps1
$env:ASTRA_DOCKER_IMAGE = "astra-sandbox:agent-system-dev"
```

On macOS/Linux, use the matching POSIX wrapper:

```bash
./scripts/build-sandbox-image.sh
export ASTRA_DOCKER_IMAGE=astra-sandbox:agent-system-dev
```

The development image mirrors the base dependencies and the `mcp`, `server`,
`tracing`, and `dev` extras from `pyproject.toml`. It also includes Git,
ripgrep, and curl. It exposes no Docker CLI, Node.js/npm commands, browsers, or
GUI tools; Pyright alone uses a private Node runtime supplied by its Python
package, and TUI checks remain host-side. Containing curl does not enable
networking: Astra still launches the sandbox with `--network none` by default.
A strict root `.dockerignore` sends only the sandbox Dockerfile and requirements
file to Docker, so project secrets, state, virtual environments, dependencies,
and unrelated source never enter the build context.

The same setting can be added to `.env`. Image building needs network access
once; containers still run with `--network none` unless the active sandbox is
explicitly configured otherwise. The project source remains mounted at
`/workspace`, so checks use the current working tree rather than a copy baked
into the image.

Windows example:

```json
{
  "roots": [
    {"path": "D:\\shared", "mode": "ro"},
    {"path": "\\\\wsl.localhost\\Ubuntu\\home\\user", "mode": "ro"}
  ]
}
```

macOS example:

```json
{
  "roots": [
    {"path": "/Users/your-name/shared-files", "mode": "ro"}
  ]
}
```

The same rule applies to `send_file_roots` in `.astra/channels.json`: use a
Windows path such as `D:\\allowed\\outputs`, or a macOS path such as
`/Users/your-name/allowed/outputs`. The committed channel example uses the
portable repository-relative `outputs` placeholder.

Paths outside the configured roots are rejected. A read-only root can never be
written or edited through the file tools.

## TUI themes

Use `/theme` to list the Ink TUI themes. `/theme hermes`, `/theme classic`,
`/theme nord`, `/theme dracula`, `/theme solarized`, and `/theme gruvbox`
switch immediately. `hermes` is the default warm gold-and-cream skin modeled
after Hermes CLI; `classic` preserves the original bright ANSI styling. The
selection is saved separately in `.astra/tui-settings.json`.

## MCP

Install the `mcp` extra and create `.astra/mcp.json`:

```json
{
  "servers": {
    "filesystem": {
      "transport": "stdio",
      "command": "python",
      "args": ["path/to/server.py"]
    }
  }
}
```

MCP tools are exposed as `mcp__<server>__<tool>`. Use `/mcp` to inspect loaded
servers and `/doctor` for connection failures.

## Web search

`search_web` supports `provider=auto|exa|searxng`. Use `/search` to inspect the
default and `/search auto|exa|searxng` to switch it persistently. In `auto`
mode, news, research, model-release and benchmark queries prefer Exa; ordinary
queries start with SearXNG. Empty results or provider failures fall back between
Exa and SearXNG inside the same tool call. Search has no fixed per-turn call
limit; the general repeated-call and ReAct iteration guards still prevent true
infinite loops. DuckDuckGo is not used.

Set `EXA_API_KEY` directly, or set `EXA_ENV_FILE` to another dotenv file that
contains `EXA_API_KEY`. The latter reuses a credential without copying the
secret into this project. `/doctor` reports only whether Exa is configured and
which source was used; it never prints the key.

`web_extract` has an independent provider waterfall. `WEB_EXTRACT_PROVIDER=auto`
tries configured APIs in `Tavily -> Exa -> Parallel` order, then local/cloud
Firecrawl, and finally direct HTTP with main-content HTML-to-Markdown conversion.
Failures fall through per URL, so one successful page is not fetched again by
later providers. Set `provider` on one tool call, or set
`WEB_EXTRACT_PROVIDER=tavily|exa|parallel|firecrawl|http`, to choose the first
rung explicitly. Pages over 15,000 characters return a 75/25 head-tail window
and save the full Markdown under `.astra/cache/web`.

The optional browser extractor is most safely configured with
`BROWSER_EXTRACT_ARGV`, a JSON array containing the executable and each fixed
argument separately. For example, Windows can use
`["C:\\Program Files\\Browser Extract\\extract.exe","--render"]`. Legacy shell
strings remain supported: `BROWSER_EXTRACT_CMD` is preferred and takes
precedence over `WSL_EXTRACT_CMD`. The backward-compatible `WSL_EXTRACT_CMD`
remains a legacy fallback on every platform. Astra runs those strings through
`cmd.exe` on Windows or Bash on POSIX. On Windows, each extractor subprocess
receives the URL through its own child-only environment and a fixed quoted
`%ASTRA_BROWSER_EXTRACT_URL%` placeholder with delayed expansion disabled; on
POSIX it is a separate positional argument. Raw double quotes, CR, LF, and NUL
are rejected in Windows legacy-mode URLs—percent-encode them or use
`BROWSER_EXTRACT_ARGV`. Shell expansion such as `$HOME` remains available
without interpolating URL text into the shell program. Set
`BROWSER_EXTRACT_STATUS_CMD` to an independent readiness command for a custom
extractor; otherwise Astra checks only that its executable or shell wrapper is
ready and does not send a synthetic URL or status argument.

When no extractor setting is present, Windows Python (`sys.platform=win32`)
invokes the built-in extractor through WSL. macOS and all Linux Python
processes—including Python running inside WSL—use native Bash. Standard Chrome,
Edge, and Chromium application locations are discovered on macOS in addition
to the existing Windows/Linux candidates.

When moving a checkout copied from Windows to macOS/Linux, unset
`WSL_EXTRACT_CMD` or replace it with a valid native `BROWSER_EXTRACT_ARGV` or
`BROWSER_EXTRACT_CMD`. The legacy `WSL_EXTRACT_CMD` fallback is read on every
platform, so a Windows value that starts with `wsl` would otherwise be attempted
on macOS/Linux.

Proxy routing is process-dynamic. Astra reads `HTTP_PROXY`, `HTTPS_PROXY`, and
`ALL_PROXY`. In `ASTRA_PROXY_MODE=auto` it uses the configured proxy only while
that endpoint is reachable, so stopping or restarting a local proxy does not
require restarting Astra. The launchers select `off` unless you explicitly set
`ASTRA_PROXY_MODE`; use `auto` to enable reachability probing or `always` to skip
the probe. Local/private URLs and hosts listed in `NO_PROXY` always connect
directly, which lets local SearXNG or Firecrawl coexist with search APIs that
need a proxy.

## Conclave multi-expert research

Conclave orchestrates parallel SearXNG searches across 12 domain-specialist
experts, then synthesises findings into a structured report. Use the slash
command to ask a question directly:

```text
/conclave 2026年本地 LLM 部署方案对比
```

Configuration commands are auto-completed in the TUI menu:

```text
/conclave config                    ← 查看当前配置
/conclave config chairperson <profile> ← 设置主席模型配置；active 使用当前模型
/conclave config expert_list          ← 多选专家（Tab 追加，Enter 提交）
/conclave config sources 5            ← 每位专家最多来源数
```

Expert list supports **multi-select**: Tab to append experts one by one, comma
separated automatically, all experts remain visible for further selection:

```text
Tab 学术专家 → /conclave config expert_list 学术专家,
Tab arXiv 专家 → /conclave config expert_list 学术专家, arXiv 专家,
Tab GitHub 专家 → /conclave config expert_list 学术专家, arXiv 专家, GitHub 专家,
Enter → ✓ 专家列表: 学术专家, arXiv 专家, GitHub 专家
```

Configuration is persisted to `~/.config/hermes/conclave.json` automatically.

## ComfyUI image generation

The image tool group keeps the legacy NoobAI/JANKU route and adds a separate
Anima/Qwen route:

- `comfyui_draw`: legacy NoobAI workflow, waits for and downloads its result.
- `comfyui_start` / `comfyui_stop`: manage the selected WSL or native instance,
  or report the action required for an externally managed server. Lifecycle
  success is always verified against the real HTTP API.
- `comfyui_anima_status`: verifies Anima models, Qwen CLIP/VAE and locked LoRAs.
- `comfyui_anima_draw`: submits the Anima v2 signature-lineart workflow and
  returns immediately with a `prompt_id`; it deliberately does not poll.
- `comfyui_result`: checks a submitted prompt once and, when complete,
  downloads and attaches the image for visual inspection.

Anima uses `COMFYUI_ANIMA_SERVER` when set and otherwise shares
`COMFYUI_SERVER`. The locked defaults are Anima v2, Qwen 0.6B CLIP, Qwen Image
VAE, Turbo 1.0, Aesthetic 0.6, Lyra lineart v2 0.8, 16 steps and CFG 1.0.
Routine image requests prefer these dedicated tools. Shell and direct API remain
available for unsupported workflows and targeted diagnosis; a draw submission is
only considered successful when ComfyUI returns a real `prompt_id`.

Set `COMFYUI_LIFECYCLE` to `auto`, `wsl`, `native`, or `external`. `auto` keeps
the existing WSL behavior on Windows. On macOS and Linux it selects `native`
only when both `COMFYUI_NATIVE_ROOT` and `COMFYUI_NATIVE_PYTHON` are configured;
otherwise Astra treats the server as externally managed. Native mode launches
the configured `main.py` in its own process group, writes Astra-owned PID
metadata below the workspace `.astra` directory, and verifies the recorded
command and process identity before stopping it. `COMFYUI_NATIVE_LOG` optionally
sets the native log path. In every mode, `/system_stats` remains the authoritative
readiness and shutdown check.

For a ComfyUI process you start yourself on macOS, leave lifecycle management
external and point Astra at it:

```dotenv
COMFYUI_SERVER=http://127.0.0.1:8188
COMFYUI_LIFECYCLE=external
```

To let Astra manage a native instance instead, set `COMFYUI_NATIVE_ROOT` to the
ComfyUI directory and `COMFYUI_NATIVE_PYTHON` to its Python executable (absolute,
or relative to that root). Do not set native paths on Windows when retaining the
default WSL lifecycle.

## Durable tasks

Every user turn is journaled to `.astra/tasks.db` using SQLite WAL.
LLM/tool boundaries create checkpoints, completed tool results are reused after
a restart, and tools left in an uncertain in-flight state are never replayed
automatically.

```text
/tasks
/tasks <task-id>
/resume <task-id>
/cancel [task-id]
```

The Ink TUI keeps reading commands while a task runs. The first `Ctrl+C`
requests a durable cancellation; pressing it again forces the process to exit.
Resume is restricted to the original session so its conversation checkpoint is
available.

YOLO can be changed while a reply is streaming or tools are running:
`/yolo` toggles it, `/yolo on` and `/yolo off` set it explicitly, and
`/yolo status` reports the current setting. `Ctrl+Y` also toggles it from
approval panels, questions, and tool details without submitting the input draft.
The input bar displays `YOLO` while enabled. Turning it on releases pending
approvals that support YOLO with a one-time decision; mandatory permission
boundaries still apply. Turning it off restores approval checks at subsequent
tool boundaries, including running Team members. It does not cancel a tool
that has already been authorized or erase separately granted session permissions.
YOLO is available in Work and Minimal modes. Commands that cannot run during a
reply show an explanation; `/help` lists the available controls.

Verbose tool output is collapsed in terminal history by default. Run
`/tool <id>` to open a numbered result, or press `Ctrl+O` for the latest result.
The detail panel supports arrow keys and PageUp/PageDown; press Escape or
`Ctrl+O` to close it. `TUI_TOOL_COLLAPSE_CHARS`, `TUI_TOOL_COLLAPSE_LINES`, and
`TUI_MAX_TOOL_RESULTS` control the thresholds and retained detail records.
Slash-command suggestions use a fixed-height scrolling window so filtering does
not leak old menu rows into terminal scrollback. `TUI_COMMAND_MENU_ROWS` defaults
to 8; arrow keys still traverse every matching command.

Pasting an image file path into the composer preserves text already typed before
or after the cursor. Image paths stay separate from the question when converted
to `[Image #N]` placeholders and when submitted. Pasting does not send the message;
press Enter when the draft is ready.

## Task state, conversation state, and core memory

Core memory is global and intentionally small. `MEMORY.md` and `USER.md` are
the direct always-injected sources of truth, not mirrors of SQLite records.
They use fixed hard limits and are added to temporary context for the current turn;
the persisted user message remains unchanged. There is currently no
project-folder scope.

Durable execution uses `TaskRun → Step/TaskEvent` in `.astra/tasks.db`.
Every request receives an independent TaskRun with checkpoints, tool results,
blocking state, cancellation, and verification evidence. Use `/tasks` to inspect
the execution journal. Long-horizon work is opt-in through `/goal`, which adds
independent verification and bounded automatic continuation without guessing
intent from ordinary conversation.

Coding journals record what commands actually did: terminal status, exit code,
and bounded output. A successful command or file readback does not certify a
source change; `/tasks` shows these receipts as **UNVERIFIED** until assessed.
Background and interrupted executions remain distinct from completed commands.
The model should report relevant checks and their limits without repeating checks
just to dismiss a reminder. Goal verification includes command arguments and
both ends of long output; a completion verdict with empty evidence cannot finish
a goal. The verifier remains a model judgement, not a guarantee of correctness.

Routine requests do not inject a duplicate task ID, `running` label, or original
question. A compact `<task-state>` is included only when the journal contains
recovery, blocking, scheduling, or tool progress worth carrying forward. It is
capped at 1,000 characters, omits LLM bookkeeping, and prioritizes uncertain tool
outcomes over recent successes. This uses actual task state, not input keyword
matching or an extra model call. The turn's context stays frozen across tool
iterations; `/tasks`, checkpoints, and explicit resume retain the full journal
and original objective.

Session-isolated working memory in `.astra/memory.db` is now injected as
small `<conversation-state>` only: temporary constraints, open questions,
assumptions, and turn notes. Legacy goal/plan/progress/artifact fields and plan
APIs remain readable for compatibility, but are no longer execution truth and
are not injected into the prompt.

Always-visible memory has exactly two direct Markdown sources: `MEMORY.md`
(hard limit 2,200 characters) and `USER.md` (hard limit 1,375 characters).
Writes that exceed either fixed limit are rejected, and a second read-side guard
prevents legacy or manually edited oversized files from expanding the model
prompt. There is no Auto Core and no configurable extra L0 budget.

Structured historical memory is stored locally in `.astra/memory.db` and does
not require Hindsight. Evidence-backed preferences, user facts, episodes, task
references, and observations retain provenance, confidence, and lifecycle
metadata. Core Markdown remains the small always-visible source; SQLite records
are retrieved only when relevant, at most four records and 2,400 rendered
characters per turn. Project/configuration questions can match local
observations without requiring the user to say “remember”; trivial turns,
slash commands, and unrelated questions skip automatic recall. SQLite FTS5 is
used when available, with deterministic substring fallback for Chinese text.

Personal facts and corrections are written through the model's memory tools or
explicit `/memory` commands. Astra does not turn conversation sentences into facts
using patterns such as “I like”, “remember”, or “change to”. Optional tool-authored
retention is off by default (`MEMORY_AUTO_RETAIN=0`); enabling it admits only tools
that explicitly provide attributable evidence from successful calls. Diagnostics
show the active runtime setting. Old `conservative` / `evolving` mode values still
load, but both use this restricted tool-evidence policy.

Background memory maintenance only expires explicit deadlines and retires exact
observation duplicates with matching provenance, scope and lifetime. It preserves
the original records, does not increase confidence for repetition, and does not
promote temporary episodes into permanent facts. Existing memories are not
rewritten on upgrade; use `/memory inspect`, `correct` and `forget` to review them.
Semantic recall treats ordinary English words in Chinese questions as soft terms;
explicit literals and code identifiers still constrain matching results.

Hindsight is an optional federated semantic-recall enhancement. When enabled,
its results are merged with the authoritative builtin store; when disabled or
unavailable, builtin retention and recall continue locally. Current user
statements, Core Markdown, concrete TaskRun results, Goal state, and temporary
conversation state always take precedence over recalled records.

**On-demand local history** is also available through `session_search`. The model
can consult previous conversations, environment notes, fixes and decisions when
relevant, without waiting for the user to say “remember”. Normal TUI conversations
continue to be recorded by the existing Session Recall writer. Its files survive
closing the terminal; this does not need a timer or an always-open terminal.
Existing isolated-mode recording restrictions remain in place.

Historical observations are read directly from `.astra/learning.db` (override:
`AGENT_LEARNING_PATH`). Existing `pending` and `applied` observations are available
without re-importing or approving them. Rejected, superseded and rolled-back
entries are excluded. Queries do not change the original records or copy them
into the always-visible memory or automatic Context Index. This is a lightweight
local lookup on Mac and Windows; Windows can continue using Hindsight alongside it.

Model tool examples (these are tool arguments, not slash commands):

```text
session_search(query="ComfyUI pip")
session_search(query="ComfyUI pip", source_type="learning")
session_search(source_type="learning")
session_search(record_id="learning:lr_...")
```

Search shows bounded excerpts and separately counts conversation and observation
results. Each observation has its original ID, date and source session; expanding
it also shows the saved evidence. `has_more` means more matches exist; narrow the
keywords or increase `limit` (up to 20 per archive). Read `truncated` before claiming
the full original was inspected. Missing and unreadable archives are reported
separately from a successful search with no matches. Observation search uses
Chinese/English keyword matching, not semantic search or FTS5 boolean operators.
Old notes can be stale or wrong: `applied` describes the old learning workflow,
not current verification. Current instructions and live evidence take precedence.
Skills remain in their own library; `/learn review` only curates automatic skills.

For example, asking “ComfyUI 这个环境装依赖应该用哪个命令？” can lead the model to
look up a previous environment note, then verify the current environment before
recommending a command. A retrieved note alone is not permission to execute it.

Hindsight runs its own LLM for retain, consolidation and reflect. Astra's
`HINDSIGHT_PROCESSING_MODEL=deepseek-flash` is a diagnostic label only; it does
not change the server. On the Windows/WSL host running Hindsight, update its
actual service environment (for example `profiles/main.env`):

```dotenv
HINDSIGHT_API_LLM_MODEL=deepseek-flash
HINDSIGHT_API_RETAIN_LLM_MODEL=deepseek-flash
HINDSIGHT_API_CONSOLIDATION_LLM_MODEL=deepseek-flash
HINDSIGHT_API_REFLECT_LLM_MODEL=deepseek-flash
```

Keep the existing DeepSeek provider, endpoint, credentials and bank settings.
Reload/recreate the service with this environment, including any separate
workers, and check its effective configuration. A plain container restart does
not import changes to a Compose env file. Per-operation or bank-level model
overrides must also agree; see the [Hindsight configuration reference](https://hindsight.vectorize.io/developer/configuration).
Pulling Astra alone does not update a separately deployed Hindsight service.

```text
/memory
/memory remember <stable agent or environment fact>
/memory remember-user <stable user profile or preference>
/tasks [id]
/memory working temporary_constraints <temporary constraint>
/memory inspect [query|id]
/memory timeline <id>
/memory why
/memory correct <id> <replacement text>
/memory forget <id>
/memory clear-working
```

The model can use the `memory` tool to maintain the same stores. Credential-like
content is rejected. `MEMORY.md` defaults to 2,200 characters and `USER.md` to
1,375 characters. Core additions do not require approval; model-initiated
deletion is blocked until the user explicitly runs `/memory forget <id>`.

### Importing Hermes history

The importer defaults to `~/.hermes/state.db` and the repository-local
`.astra/sessions.db`. Preview an import with explicit paths before writing:

```powershell
# Windows reading a Hermes database in WSL
python scripts/import_hermes_history.py --dry-run `
  --source-db "\\wsl.localhost\Ubuntu\home\your-name\.hermes\state.db" `
  --target-db ".astra\sessions.db"
```

```bash
# macOS/Linux
python3 scripts/import_hermes_history.py --dry-run \
  --source-db "$HOME/.hermes/state.db" \
  --target-db ".astra/sessions.db"
```

After reviewing the preview, repeat without `--dry-run`. `HERMES_DB` and
`ASTRA_SESSIONS_DB` provide the same path overrides for scheduled runs; explicit
`--source-db` and `--target-db` arguments take precedence.

### Proactive Context Index

The Proactive Context Index is Astra's **native, optional memory recommendation
pipeline**. Query planning, reciprocal-rank fusion (RRF), diversity selection
(MMR), evidence budgets, and feedback all run in Astra's Python components.
It requires no external memory framework, memory server, graph database, or
auxiliary LLM. Existing embedding backends remain optional. It is **off by
default**, and ordinary Markdown core memory stays unchanged.

```text
/context-index session   # Astra's local records and session history
/context-index all       # Also consider cached Activity history
/context-index off       # Restore the existing memory path
/context-index why       # Explain the last selection and its cost
/context-index feedback R1 useful      # Explicit feedback on a displayed entry
/context-index feedback R1 irrelevant  # Lower its priority for similar tasks
/context-index feedback R1 outdated    # Ranking feedback; does not rewrite facts
```

Astra builds a bounded query from the current request. Resume requests can also
use recent user turns and existing task checkpoints; independent questions do
not inherit the previous task's search terms. It then:

- searches local records, session text, and cached Activity channels in parallel;
- adds optional semantic candidates for session history and structured records
  using the existing warm embedding backend, with one query encoding shared
  across sources and text retrieval retained when encoding is unavailable;
- uses the existing FTS5 trigram index for long Chinese queries, with partial
  term matches and a bounded short-term fallback; native lexical candidates
  need two matching terms for multi-term queries, preventing a lone common word
  from qualifying, and are ranked by coverage before pool limits; session and
  Activity combine indexed term hits instead of repeatedly scanning long bodies;
- fuses channel **ranks**, without comparing BM25 and cosine as the same score;
- selects up to **four complementary entries across sources**, instead of fixed
  Session/Activity quotas;
- uses recency only for resume, time, or explicit recent-history queries, excludes already visible or
  out-of-window evidence, and allows an empty recommendation;
- renders useful previews, dropping entries that cannot fit rather than leaving
  empty descriptions, and freezes the result for the current user turn.

Current-question exclusion happens before the session candidate limit, so a
self-hit cannot block older partial matches. Chinese request phrases such as
“帮我看看这个” are removed before generating search terms. Simple arithmetic
such as `2+2` skips retrieval, while substantive questions containing an
expression remain eligible. The lexical policy
matches text; the optional semantic channel can retrieve some paraphrases.
Neither is evidence that the final answer will improve.
Named ASCII targets in mixed Chinese queries, acronyms, and code identifiers
use token boundaries: `NUS` does not match `sinus` or `NUS2`. Output destinations
such as “把今天的经验更新进 skill” or “写入 README.md 和 CHANGELOG.md” do not
constrain the history being summarized; a named subject in a following
instruction remains a retrieval target.
Indexable named targets narrow Session/Activity scans before common-word scoring,
with short-target alternatives preserved.
Overlapping Chinese grams share query-character credit before Session/Record
pool limits and during fusion; two adjacent trigrams cover four characters,
not six. The original partial-recall floor remains, so short Chinese phrases
are still searchable. See the [native lexical policy](docs/native-memory-recommendation.md)
and run `python -m pytest -q tests/test_context_index_precision.py` for synthetic
positive/negative pairs and cross-source regressions.
For latency acceptance, run `python scripts/phase_t_gate.py --context-index-performance`;
the four performance tests are skipped by default in ordinary pytest runs.

To prepare session/record vectors explicitly, install the existing `embedding`
extra, configure the [platform backend](docs/context-index-platforms.md), and run:

```console
python -m agent.runtime.context_index.semantic_indexer
```

This processes at most 128 changed entries per source; rerun to continue a
backlog. After restarting Astra with recommendation enabled and an existing
vector file, a background worker maintains bounded batches. Query handling
only reads the index and validates each candidate against its current source
version. Changed, deleted, expired, current-message and future evidence cannot
qualify through stale vectors. No new package, memory service or model reranker
is added. Ordinary MD memory remains separate.

On macOS, Astra's own shared embedding worker keeps **one model per local user
and model identity**, across Astra sessions and checkouts using the same runtime
directory. Client processes do not load MLX. Encoding uses one text at a time,
up to 512 tokens, and releases temporary Metal buffers after each text.
Background session/record maintenance processes at most 16 changes per source
per cycle, with one maintainer per vector file. Busy or cold workers preserve
text retrieval. The worker exits after about 90 seconds without active clients;
existing Astra sessions must be restarted to release their old in-process models.

The 4B model still has a real memory cost: a local 24-request synthetic probe
measured about **4.2 GiB physical footprint / 5.4 GiB peak**, with two independent
clients sharing that worker. This is a measured workload, not a hard memory
ceiling. Run `python -m agent.evals.context_index_benchmark.resources` on macOS
with the model already cached to check sharing, released caches, and memory
budgets. See [runtime controls](docs/context-index-platforms.md#macos).

The [public quality replay](docs/native-memory-recommendation.md#quality-replay)
contains 24 synthetic scenarios, split before tuning into 16 development and
8 holdout scenarios (192 source/query combinations). With the local Qwen3
embedding backend and a development-selected similarity floor of 0.75, the
64-case holdout improved Recall@4 from 43.8% to 50.0%; negative-query injection
remained 18.8%. Warm recommendation p95 was 54 ms including query encoding
on the development Mac. These small synthetic results do not establish
downstream answer quality, large-archive model latency, or Windows model quality.
Near-topic questions whose requested facts are absent remain a known limitation.

Native recommendations read Astra's structured records directly and share the
same budget with session/activity evidence; they do not call an external memory
provider or additionally inject the old dynamic recalled-record block. Core MD,
working memory, and current task state retain their existing behavior.

The initial index respects the saved character budget (**900 characters** by
default, at most 2,000) and a **500-token estimate**. `context_open` preserves the
maximum of two calls per turn and three handles per call, with a shared
**2,000-token estimate** for returned details and a 6,000-character per-call cap.
These are local estimates, not exact provider token counts. Opening evidence can
add normal main-model turns and must be counted in end-to-end cost.

Open-call handles are saved and replayed verbatim, so a later model call sees
the actual handle instead of a redaction placeholder. Expanded evidence remains
request-local, and handles still expire at the end of the turn. Old redacted
call examples are omitted from model replay; if a model submits a placeholder,
Astra directs it to `context_inspect` to obtain current handles before retrying.

Activity recommendation remains read-only: it does not synchronize archives,
capture the desktop, request OS permissions, or load an embedding model on a
query. A missing/busy embedding backend or failed source falls back to available
local results. Only existing explicit Activity operations refresh its history.

Ask what was injected this turn and Astra can use **`context_inspect`** to read
the actual decision, counts, exact current handles, retrieval channels and
character/token budget omissions. It also separates archive errors from semantic
errors and shows phase timings (including vector snapshot, encoding, search and
canonical validation). Activity reserves read time for later channels, so a slow
summary query need not erase event results. Vector snapshots decode float32
buffers directly, avoiding millions of temporary Python objects that could
stall concurrent archive readers. Local acceptance covers simultaneous cold
loads of session, memory and activity vectors. The model chooses when to call
`context_inspect` through normal tool use; Astra does not match self-inspection
phrases in user input. The turn follows the normal recommendation pipeline, and
the tool reads its actual results without rerunning retrieval or changing the
injection. An inspection can therefore report either zero or nonzero entries.
The immediately preceding completed turn has a separate metadata-only summary;
it is never described as this turn's injection. Core MD memory is separate.
The inspection tool itself performs no embedding or database search.

`/context-index why` also reports the selected/displayed counts and omissions.
The default **four recommendations** and **three handles per open call** are
different limits; fewer displayed entries need not mean fewer candidates.
`related` is a ranking label; only a recorded vector channel establishes a
vector match. New local audit events retain these diagnostics without queries
or preview text. Older logs cannot reconstruct omitted diagnostic fields.
Building an index, submitting it to the provider,
and opening evidence are separate events; none automatically adds a positive
reward. Explicit feedback is stored locally, scoped to the workspace, query
intent, and evidence revision, with a small smoothed ranking adjustment.
Repeating feedback for the same recommendation is idempotent. A new record
version starts neutral. Feedback changes ranking; existing memory commands
continue to manage facts.

For architecture, feedback storage, and reproducible regression checks, see
[Native memory recommendation](docs/native-memory-recommendation.md). These
checks establish behavior and resource limits, not a measured answer-quality
advantage over other memory systems.

Very old Activity archives that have not yet been opened by the normal Activity
Store writer may lack the canonical UTC epoch columns. In that read-only case,
Context Index keeps compatible search results but omits recency and inferred
habit suggestions; only normal Activity Store writer initialization or an
explicit `activity_search` performs the one-time archive migration.

The readers default to Astra's canonical process archives: Session Recall's
repository-local `.astra/sessions.db` and the Activity Store archive selected
by `ASTRA_ACTIVITY_DB` (or its repository-local default). They do not switch
archives merely because a turn is running in another workspace. For a separate
read-only recommendation archive, set `ASTRA_CONTEXT_INDEX_SESSIONS_DB` and/or
`ASTRA_CONTEXT_INDEX_ACTIVITY_DB` in `.env`; these reader-specific overrides
do not turn the feature on. `ASTRA_SESSIONS_DB` is an importer option, not a
Session Recall runtime override.

## Skills and Learning Review

The built-in agent contract lives in [`agent/runtime/astra.md`](agent/runtime/astra.md).
Persona text is preserved; the learning section uses direct skill saving. With `ASTRA_CORE_RULES_MODE=skill`
(default), persona/basic rules and a short usage guide stay in the stable prompt.
The model decides when to read the built-in `astra-core` Skill, listed first:
ordinary chat needs no read; engineering, file, browser and desktop work require
`skill_view(name="astra-core")` before specialist skills and execution. In Code
Mode, call `tools.skill_view` through `run_code` and print the complete result.
There is no runtime intent classifier, automatic core-body injection, or forced
replanning. The model can reuse the complete current-version result in context;
after compaction removes it, work requires another read. Once read, it still
occupies history tokens during later chat.

Set `ASTRA_CORE_RULES_MODE=full` and restart to restore the original complete core
prompt. An unrecognized value also retains the full contract. The retired
`ASTRA_LAZY_WORK_RULES` flag has no effect. Changing prompt configuration needs a
new cache prefix; ordinary chat/work transitions retain the same system prompt.
The packaged core Skill is read-only and available independently of project
trust; it does not replace project `AGENTS.md` discovery. Model compliance must
be measured separately from runtime tests. See
[core-rule loading and cache boundaries](docs/runtime/core-rules.md).

Reusable procedures live under `.astra/skills/<name>/SKILL.md`, with
optional `references/`, `templates/`, `scripts/`, and `assets/` files. Only the
skill catalog is injected into the prompt; the model uses `skill_view` to load
full instructions on demand.

```text
/skills
/skills show <name> [file]
/skills create <name> <description>
```

The model saves its own reusable summaries through `skill_manage(origin="auto")`
into `.astra/skills/learned/<name>/`. The skill is immediately available through the
existing catalog and `skill_view`. It should describe when to use it, the steps,
source context, and known limitations. Personal preferences and project facts
continue to use the memory tools. There is no learning quota or extra review
request after an ordinary conversation turn.

`/skills` labels each skill's origin independently of its topic/category:

| Origin | Created by | Included in `/learn review` |
| --- | --- | --- |
| `auto` | The model's own summaries, registered by the automatic-learning writer | Yes, subject to scope and edit protection |
| `user` | `/skills create`, copied/imported files, or `skill_manage(origin="user")` at the user's request | No |
| `builtin` | Packaged Astra rules | No |

When the model adds or installs content supplied/requested by the user, it must
choose `origin="user"`; new user additions go in `.astra/skills/user/`. Existing
user directories keep their paths. An unknown or missing origin is protected as
user content. Placing a file in `learned/`, or writing `origin: auto` inside its
frontmatter, does not make it an automatic review target. Disabling automatic
learning does not prevent an explicitly requested user-skill installation.

**Quality review is manual.** Enter `/learn review` to ask the active model to
keep, rewrite, merge, or archive a batch of automatic skills. User-skill content
and catalog descriptions are excluded from that review request. It checks at most 4
skills and 24,000 characters of full files plus source excerpts in stable name
order, with a persisted cursor for the next batch. Catalog summaries are context
only: an unread skill cannot be edited. One bounded request uses up to 4,096
output tokens and 90 seconds; failure is reported without retrying or advancing
the cursor. Oversized, pinned, other-workspace, or externally edited skills are
reported as not checked. A content review is not a claim that the procedures
have been executed or proven correct.

There is **no timer, idle review, startup catch-up, or background learning job**.
Ctrl+C cancels a running review; a new message cancels it before starting normal
work. Closing the terminal does not schedule future work. If cancellation meets
a short file commit already in progress, that commit settles before cancellation
returns; its result is available in history. Restart never restarts a model review.
The old `LEARNING_REVIEW_AUTO`, interval, idle-delay, and candidate-reminder
settings no longer activate those paths.

Automatic writes and curation are restricted to model-owned `learned/` skills in
the current workspace/platform. Core rules, manual skills, pinned skills, and
files subsequently edited by the user are protected. Each change retains its
source, reason and original files under `.astra/skills-learning/`, outside the
skill catalog. A cross-process lock prevents simultaneous maintenance. A journal
recovers interrupted multi-file writes; undo refuses to overwrite later changes.
Archived skills leave the catalog, while their files remain in the history.
If maintenance records cannot be read, normal chat remains available; the LEARN
row shows `? / CHECK` and `/learn` reports the error without replacing those files.

```text
/learn                       # status, learned count, last review, legacy count
/learn review                # manually organize the next batch
/learn migrate               # import historical automatic summaries, once
/learn history               # recent changes and reasons
/learn history <run-id>      # one change in detail
/learn undo <run-id>         # restore unchanged affected files
/learn mode off              # stop new automatic skill saves
/learn mode review           # enable direct saving again
/learn legacy pending       # inspect historical candidates
/learn legacy show <id>
/learn legacy rollback <id>  # undo a historical applied proposal
```

Legacy candidates and their original evidence remain in `.astra/learning.db`.
They are history, not a queue that must be cleared, and are not automatically
installed or converted into facts. `/learn pending` and `/learn show` still read
that history. The old summarize, repair and approve commands explain their
retirement instead of silently applying candidates. Old trial tools and keyword
reminders are no longer registered in normal runtime sessions. Existing manual
skills and memory are retained. The startup LEARN count now means learned skills,
with **MANUAL REVIEW** indicating how quality maintenance is triggered.
For legacy data, explicitly run `/learn migrate`. It backs up the original
SQLite database, imports procedural candidates as automatic skills, and records
standalone observations as history-only without converting them into memory.
The original rows remain queryable. If a candidate conflicts with a user skill,
the importer creates a separately named automatic copy; a historical patch must
still match its original target exactly. Unsupported/invalid patches remain
unmigrated and are reported, with their originals intact. The command records
each candidate's destination, so repeating it does not create duplicates or
bring back skills later archived by review. `/learn` reports imported,
history-only and not-yet-migrated counts; `/learn undo <run-id>` can undo the
migration. File import does not call a model or certify the historical content.

An empty automatic library is normal before saving or migrating any summaries.
`/learn review` then reports zero checked without calling a model. User skills
remain available for normal tasks regardless of whether the automatic library is empty.

## MCP

MCP servers are loaded from `.astra/mcp.json`. Both stdio and
streamable HTTP transports are supported, servers can be disabled individually,
and `${ENV_NAME}` placeholders are resolved without storing credentials in the
JSON file. Start from `config/mcp.example.json`, then use `/mcp` or `/doctor` to
inspect connection and tool-registration status.

## Tool policy and tracing

`AGENT_TOOL_POLICY` accepts `permissive`, `safe`, or `locked`. In safe/locked
mode a denied tool can be approved for the current process with
`/permissions allow <tool-name>`.

Tool execution has three production guards enabled by default:

- `TOOL_MAX_INLINE_CHARS=12000` keeps a bounded head/tail preview in durable
  history and writes the complete result to `TOOL_RESULT_DIR`. Fresh text up to
  `TOOL_MAX_FRESH_RESULT_CHARS=100000` is shown in full to the immediately
  following model iteration, then falls back to the durable preview. Opaque
  base64/data-URL payloads are never inlined as text noise.
- `TOOL_FAILURE_THRESHOLD=3` opens a per-tool circuit after equivalent
  consecutive failures, then performs one final synthesis with tools disabled.
- `PROMPT_CACHE_STABLE_TOOLS=1` keeps one provider-visible tool manifest for
  the work session. Tool budgets and circuits remain enforced at execution
  time without deleting schemas between ReAct iterations.
- With `PROMPT_CACHE_STABLE_TOOLS=0`, `TOOL_PROGRESSIVE_EXPOSURE=1` restores
  the legacy core/request-relevant group routing and `activate_tool_group`
  behavior. This reduces the first uncached prompt but causes more prefix-cache
  invalidations.
- `AGENT_MAX_REACT_ITERATIONS=50` sets a coarse total-turn safety ceiling. Set
  it to `0` for no fixed ceiling; repeated-call, failure-circuit, prompt-budget,
  and cancellation guards remain active.

Set `AGENT_TRACE_ENABLED=1` to emit OpenTelemetry spans. When the SDK and OTLP
exporter are installed, `OTEL_EXPORTER_OTLP_ENDPOINT` selects a Phoenix or other
OTLP-compatible collector.

## Deterministic replay evaluations

Persona and context invariants can be checked without starting a model server:

```powershell
python -m agent.evals.replay
python -m agent.evals.replay --category context-compression
python -m agent.evals.replay --case legacy-work-session-migrates --json
```

Editable JSONL cases live in `evals/persona_invariants.jsonl`. Every run writes
the replayed session artifacts and a structured `report.json` below
`.astra/evals/runs/`. An editable install also exposes the same runner as
`agent-lab-eval`.

## Read-only 163 mail

The optional 163 integration incrementally caches mail for local queries. Put
your account and a 163 client authorization code (not the web password) in the
repository `.env`:

```dotenv
ASTRA_163_EMAIL=your-account@163.com
ASTRA_163_AUTH_CODE=your-client-authorization-code
# ASTRA_163_MAX_BODY_BYTES=5242880
```

With no subcommand, `checkmail` refreshes the default `INBOX` and `已发送`
folders and then shows the 30 newest Inbox messages. Subcommands provide an
explicit `sync --json`, cached `search`, stable-UID `read`, folder listing, and
status. Add `--offline` to `recent`, `search`, `read`, or `folders` to avoid a
server connection and use only an existing cache. A failed refresh returns
clearly labelled cached results when available; offline access to a missing
cache fails without creating a database.

```powershell
.\scripts\checkmail.bat
.\scripts\checkmail.bat sync --json
.\scripts\checkmail.bat search "invoice" --from billing@example.com --window 200 --json
.\scripts\checkmail.bat read 352 --folder INBOX --json
.\scripts\checkmail.bat recent --offline --recent 10 --json
```

Explicit attachment download currently fails closed on Windows because secure
local attachment writes are available only on POSIX. Windows synchronization,
search, reading, folder listing, status, and offline cache queries remain
supported. A direct Windows attachment request returns the stable
`unsupported_platform` classification with exit code `2` before contacting the
mail server for attachment data.

```bash
./scripts/checkmail.sh
./scripts/checkmail.sh sync --json
./scripts/checkmail.sh search "invoice" --from billing@example.com --window 200 --json
./scripts/checkmail.sh read 352 --folder INBOX --json
./scripts/checkmail.sh recent --offline --recent 10 --json
./scripts/checkmail.sh attachment --folder INBOX --uidvalidity 77 --uid 352 --part 2 --json
```

Synchronization stores headers and decoded text in `.astra/mail/163.sqlite3`.
It does not download attachment payloads during synchronization; only the
explicit `attachment` command writes one requested payload below
`.astra/mail/attachments/`. The integration selects mailboxes read-only and
does not mark messages read, move, delete, reply to, or send mail.

## Maintenance entry points

Windows wrappers remain supported alongside their POSIX counterparts:

| Operation | Windows | macOS/Linux |
| --- | --- | --- |
| Start Astra | `astra.bat` | `./astra.sh` |
| Migrate legacy state | `.\scripts\astra-migrate.bat` | `./scripts/astra-migrate.sh` |
| Run release gate | `scripts\phase_t_gate.cmd` | `./scripts/phase_t_gate.sh` |
| Build sandbox image | `scripts\build-sandbox-image.ps1` | `./scripts/build-sandbox-image.sh` |

Inside the TUI, `/maintenance preview 30` lists expired generated artifacts;
`/maintenance apply 30` removes eligible files and performs passive SQLite WAL
checkpoints. `/maintenance checkpoint` only checkpoints configured databases.
Nested temporary files are considered only inside explicitly configured
generated artifact directories. Root-level `.astra/*.tmp` files retain the
24-hour stale-file policy. Sessions, memory, skills, tasks and checkpoints are
excluded, including misleading `.tmp` names. Symlinks are not followed; files
changed since planning are skipped. Windows additionally verifies file content,
because its creation timestamp does not detect all rewrites. SQLite migration,
backup and checkpoint connections are closed before replacing or removing files.
This is artifact maintenance, not deletion of conversation or activity history.

### Activity summary catch-up and health

Mac and Windows activity summaries default to `deepseek-flash` with thinking
explicitly disabled, independently of the chat model. Set
`ASTRA_ACTIVITY_SUMMARY_MODEL=deepseek-flash` in the local `.env`; dedicated
`ASTRA_ACTIVITY_SUMMARY_BASE_URL` and `ASTRA_ACTIVITY_SUMMARY_API_KEY` overrides
remain supported (the default key is `DEEPSEEK_API_KEY`). Existing official V4
model overrides migrate at the transport boundary. Each scheduled summary
subprocess reloads this configuration on its next run; existing summaries are
not regenerated just because the model changes.

The summarizer maintains a durable queue in the activity database. New, changed
or late events enqueue their ten-minute windows; failures remain retryable after
restart and preserve the last valid summary. Only closed windows with at least
three retained events are eligible. Existing summaries use input revisions to
avoid repeat model calls. Retention still determines which historical evidence
can be summarized. Overlapping batches cannot overwrite a newer generation:
summary text, its input revision and queue acknowledgement commit together.

```text
uv run --locked python -m agent.runtime.activity_recorder.summarizer --lookback 120 --max-windows 24
```

This command can call the configured summary model. `--lookback` prioritizes
recent windows; it no longer excludes older backlog. `--max-windows` limits
window checks and model calls per run (default 24), with persisted fair ordering
and one in four slots preferring older work. `--max-windows 0` skips summary
generation and retries vector indexing. Existing scheduler intervals are
unchanged; running the CLI once does not install a scheduler.

JSON output includes `windows_processed`, `llm_calls`, `written`,
`pending_windows`, `oldest_pending_at`, `catchup_pending_windows`, `vector_index`,
`last_success_at`, `last_summary_at` and `consecutive_failures`. Pending windows
are queued checks, not necessarily missing summaries: the first run also audits
retained windows that already have summaries. `last_success_at` records a
successful batch, including one with no new summary; `last_summary_at` records
the last batch that wrote a summary. A model, parse, storage or vector-index
failure produces a nonzero exit status; a remaining bounded backlog alone does
not. Setting `ASTRA_CONTEXT_INDEX_EMBEDDING=off` intentionally skips vector work
and reports `vector_index="disabled"` without counting a failure. Health fields
contain aggregate status, not activity text or provider
error payloads.

## Verify

Install the locked development environment above first. The release gate runs
Ruff, Pyright, all TUI tests, TUI typecheck/build and the Python suite. Ruff uses
an explicit correctness-focused rule set in `pyproject.toml`; formatting rules
are not a release requirement. Optional acceptance gates are separate:

```text
uv run --locked --extra dev --extra mcp --extra tracing --extra server --extra notebook python scripts/phase_t_gate.py --wheel-smoke
```

During development, run focused checks locally. Before the final cloud run,
complete the local macOS acceptance gate:

```bash
./scripts/phase_t_gate.sh --keep-going --wheel-smoke --native --context-index-performance
```

This includes the Swift suite and the production-sized lexical Context Index
latency/coverage fixture. The latter disables embedding, isolates the vector
database path, runs separately from ordinary tests and retains its original
thresholds.
`--provider-smoke` explicitly enables real model requests. A green automated
gate does not establish live desktop action effects, Appshot capture permission,
or acceptance against a real provider; those still need targeted manual checks.
For final cross-platform acceptance, push the completed batch and start one
manual run from **Actions → Maintenance → Run workflow**, selecting the final
branch and verifying the run's commit. All three platform jobs must pass; if a
check fails, fix and verify it locally before rerunning. This full Maintenance
workflow is manual. It checks the normal gate and wheel smoke on Ubuntu, macOS
and Windows, with native/performance acceptance on macOS.

The separate [Launcher compatibility workflow](.github/workflows/launcher.yml)
runs automatically when its listed launcher files change in a push or pull
request, and also supports **Run workflow**. It exercises source discovery,
native command forwarding, real Git updates, process exclusion and recovery on
all three platforms. A passing local macOS run does not establish Windows CMD,
PowerShell or Linux acceptance; verify the target commit's completed jobs.

Private-repository runs still consume
the account's [GitHub Actions allowance](https://docs.github.com/en/billing/concepts/product-billing/github-actions).
The Maintenance gate uses `--keep-going` to collect
independent check failures in one run and still exits unsuccessfully if any
check fails; local invocations stop at the first failure by default. Native
macOS/POSIX permission tests require those host capabilities; portable rejection
and protocol checks still run on Windows. The sandbox image tests require a
Linux Docker engine. Appshot integration separates Swift dependency/build time
from the bounded end-to-end test run.

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m ruff check agent tests scripts
.\.venv\Scripts\python.exe -m pyright agent scripts/phase_t_provider_smoke.py
npm --prefix ui-tui run build
```

```bash
.venv/bin/python -m pytest -q
.venv/bin/python -m ruff check agent tests scripts
.venv/bin/python -m pyright agent scripts/phase_t_provider_smoke.py
npm --prefix ui-tui run build
```

## Appshot shortcut (macOS and Windows)

Appshot requires an interactive Astra TUI. On Windows x64, build and install its
native helper with:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/build_windows_computer_helper.ps1 -Configuration release -Install
```

Restart Astra and run `/appshot enable`; the shortcut is disabled by default.
The Windows bundle contains its runtime DLLs and uses WGC/UI Automation plus
Windows-specific private storage. It does not enable other Computer Use tools.
See [Windows Appshot setup, tests and limitations](native/windows-computer-helper/README.md).
Mac helper paths, v1 and POSIX storage remain unchanged.

On macOS, Appshot requires macOS 14+ and the signed native
helper built with `./scripts/build_macos_computer_helper.sh`. Grant the helper
Screen Recording and Accessibility in macOS Privacy & Security. Recheck these
permissions after rebuilding an ad-hoc signed helper; an older grant may not
apply to its new code identity.

Use the destination TUI once with actual keyboard input before the first
capture. Merely opening a TUI or querying background status does not make it an
eligible recipient. Then switch to the source app and press **Control+Shift+Z**.
The newest eligible TUI receives a draft placeholder; capture does not send a
message. Add your question and press Enter explicitly. With multiple TUIs,
identical most-recent activity is refused rather than broadcast.

Appshot captures an exact visible window selected by your shortcut. System Settings,
Activity Monitor and dialog windows are not excluded by application or page type.
On macOS, Screen Recording permission is required; Accessibility adds available UI text.
If AX root/tree reading fails, the attachment falls back to the screenshot with
an explicit “仅截图” label and an unavailable-text notice for the model. Without
AX window evidence, capture requires one unique visible window in the foreground
app. Ambiguous windows are refused; there is no full-display fallback or source
activation. Available AX coverage is `reported_ax_subtree`, not a full-page guarantee.
Masked/secure text fields are never expanded into hidden plaintext. Ordinary static
text may omit its optional AX subrole without losing its value. Appshot traverses
up to 64 levels (2,000 nodes / 256 KiB / existing time budget), including offscreen
content exposed by the app. It reports truncation instead of promising a complete
page. Restart all TUIs after updating the native helper and recapture old attachments
to obtain text that was previously omitted.

Commands stay local: `/appshot status`, `/appshot enable`, `/appshot disable`,
and `/appshot shortcut Control+Shift+Z`. Editable and pending attachments share
a four-item cap. Removing an unsent placeholder releases its files. While the
backend is busy, captures can enter the draft, but submitting them returns
`backend_busy` and retains the content. Lost acknowledgments use status lookup;
`/appshot pending status` reconciles a pending submission and
`/appshot pending discard` explicitly discards its retained local segment.
Discard does not cancel work already admitted by the backend. A backend restart
can report `unknown`; it never automatically resends the message.

Common refusals include `no_receiving_session` (use a destination TUI first),
`receiving_session_ambiguous` (use the intended TUI again), `shortcut_conflict`
(choose another chord), `permission_unavailable` (check helper permissions),
`source_window_unavailable` (cannot identify one visible window),
`protected_ui` (the console session is locked or unavailable),
and `attachment_limit_reached` (send or remove existing attachments).
On macOS Apple Terminal, focusing an Astra tab selects it as the Appshot
recipient without typing. A bounded read-only probe checks the front tab's TTY
against the TUI process TTY every 400 ms; switching to the source app preserves
the last selected recipient. Repeated observations and background output never
claim activity. This requires macOS Automation access to Terminal; if unavailable
(or on other terminals), press an arrow key in the intended TUI before capturing.
A background window starting up does not select itself.
Appshot reconnects use exponential backoff and stop after five consecutive
short-lived connections; repeated disconnect notices are coalesced. Input in a
disconnected TUI retries the connection. A connection must remain established
for ten seconds to reset the retry budget. Resizing or changing display modes
does not recreate the client or discard its recipient activity.
`context_budget_exceeded` and `context_budget_unavailable` retain the draft.
Appshot checks the resolved model context window minus output reserve; the status
bar percentage and ordinary proactive compression use the separate 50% threshold.
The two checks also use different accounting: Appshot budgets the fresh complete
request, while the status bar shows recent usage or an estimate.
`appshot_vision_unavailable` also retains the draft when the selected profile lacks
`vision`; select an image-capable profile before submitting a new Appshot. Existing
Appshot history does not block ordinary text-only follow-ups: AX text remains,
with an explicit notice that pixels are unavailable to this model. Original
images stay in session storage and become visible again on switching back.

Appshot accepts every profile declaring `vision`, including local and custom
endpoints. Official Qwen and GPT-4o/4.1 endpoints use their existing image bounds;
DeepSeek Vision uses the [published 1,024-token image maximum](https://api-docs.deepseek.com/guides/vision/#token-usage).
All models use Astra's text/schema token estimator with 25% slack; serialized
UTF-8 bytes are used only for transport-size checks, never as text token counts.
Other models' image allowance is at least 4096 tokens, increasing with a 32-pixel
patch grid for large images. Text accounting and generic image allowances are
estimates, not exact billing or guaranteed provider bounds. Model names and
endpoint domains choose image rules, not text accounting or admission rights.
Qwen reserves 16,386 tokens per image using its
[published maximum](https://help.aliyun.com/zh/model-studio/vision), not its default resize cost.
Native PNG bounds are
10 MiB, 1–16384 pixels per dimension and 32 million pixels; Qwen additionally
requires dimensions above 10 pixels and aspect ratio no greater than 200:1.

`./scripts/test_appshot_e2e.sh` runs private Swift broker → Node client/draft →
Python admission/media/provider-projection fixtures. It does not register a
real hotkey or capture the desktop. Automated fixture evidence, signed bundle
identity, and real-machine acceptance are tracked separately; real-machine
acceptance remains pending.

### Notebook execution

The optional `notebook_execute` code tool runs selected notebook cells in a fresh
Jupyter kernel, saves outputs to a new notebook, and supports background progress
through `process_poll` / `process_read`. Install `.[notebook]` in the execution
environment first. Cell indices are 1-based and include markdown cells; earlier
cells are not run automatically. See the [usage and execution boundaries](docs/notebook-execution.md).

## Documentation

The [documentation index](docs/README.md) groups current guides by task:

- [Installation, startup, updates and recovery](docs/launcher-update.md)
- [Activity history](docs/activity-history.md) and [Context Index platforms](docs/context-index-platforms.md)
- [Browser interaction](docs/browser-interaction.md)
- [macOS Computer Use](docs/macos-computer-use.md) and its [real-machine runbook](docs/macos-computer-use-real-machine-test-runbook.md)
- [Notebook execution](docs/notebook-execution.md)
- [Skill learning, review, migration and undo](docs/skill-learning.md)
- [Local history retrieval](docs/local-history-retrieval.md)
- [Appshot architecture](docs/appshot-architecture.md) and [runtime profiling](docs/runtime-responsiveness.md)

Dated implementation plans, task reports, personal handoffs, experiment outputs,
and retired artwork are local artifacts excluded from version control. Keep new
run reports under `output/` or `.astra/artifacts/`; planning tools may still use
the ignored `docs/superpowers/` directory. Do not force-add ignored planning or
validation files. Source code, regression fixtures, runtime skills, architectural
decisions and operational contracts remain part of the repository. Dated
compatibility matrices retained by regression checks describe only their recorded
builds and test conditions. Older implementation reports remain in Git history.

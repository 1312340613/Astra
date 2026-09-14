# Astra installation, startup and updates

This is the pre-release source workflow. It also establishes package-installation
ownership and private data paths, without claiming that a public release bundle
or automatic stable-release download is available.

## First setup

For an existing checkout with older launchers, follow the README's
[one-time migration steps](../README.md#upgrade-an-older-checkout) first.

Install Python 3.11 or newer, Git and Node.js 18 or newer. A supported Node LTS is
recommended; the maintenance workflow tests its pinned Python/Node versions.

From the cloned source directory:

| Platform | Prepare dependencies and install the command |
| --- | --- |
| Windows CMD or PowerShell | `.\astra.bat setup --install-command` |
| macOS or Linux | `./astra.sh setup --install-command` |

Setup uses `uv.lock` and `ui-tui/package-lock.json`, validates installed Python
dependencies, builds the interface, and records the selected optional extras.
An existing `.env` is preserved. A missing `.env` is created from the example;
configure the intended model provider there. Management commands do not require
an API key or a model connection.

If uv is unavailable, setup installs the maintenance workflow's pinned uv into
a private bootstrap environment. Python packages are never installed globally.
Node remains an explicit prerequisite for source development.

The command shim lives in `%LOCALAPPDATA%\Astra\bin` on Windows and
`~/.local/bin` on POSIX. PATH changes affect subsequent terminals. POSIX setup
adds one identifiable PATH line to the current shell's profile. Windows setup
prepends the dedicated bin directory to the user's PATH without replacing other
entries. Restart the terminal after setup. If an old command still wins, use
`where.exe astra` in CMD/PowerShell or `command -v astra` on POSIX, and inspect
`astra doctor`.

`setup --command-only` registers the command without changing dependencies.
`--bin-dir PATH --no-path` is available for CI or a manually managed PATH.
An existing command owned by another installer is never overwritten.

## Daily use

Run `astra` from the project you want to work on. The current directory becomes
the workspace, subject to existing filesystem permissions and explicit
`SANDBOX_WORKDIR` configuration. It does not select the Astra installation.

All source wrappers and the installed `astra` entrypoint route through the same
standard-library command parser. `astra` selects Ink. `astra --cli` selects the
legacy text CLI explicitly; `astra --tui` retains the legacy Textual entry.
`agent-lab` keeps its existing legacy compatibility behavior.

Normal startup checks the recorded environment and starts it. It does not fetch
code or run package installation. After manually changing lockfiles or pulling
code outside the updater, run `astra setup` when dependencies are unverified.
First setup is explicit so an ordinary invocation does not silently install
software or modify the environment.

## Update from a source checkout

```text
astra update --check
astra update
```

The update follows the current branch's configured Git upstream. This supports
the Mac-push / Windows-update development workflow without assuming that every
user should follow an arbitrary branch. A source archive without `.git` cannot
perform a Git update; clone the repository to use this workflow.

`--check` fetches into a temporary Git ref, compares the pinned commit and
removes that ref. It does not replace working files, synchronize dependencies or
restart a session. Git object storage may change. Checks can run while Astra is
active. `--json` provides structured results.

Applying an update requires a fast-forward upstream. Local edits to files
untouched by that upstream change are kept automatically, as are local files
already identical to the incoming content. When both sides change a file,
interactive `astra update` lists the local files and offers **Keep local files**,
**Back up and overwrite local files**, or **Cancel**. Keep is the default.

Keep retains each locally changed file whole; it does not merge separate edits
inside the file. Its original bytes, deletion and executable mode are preserved,
and originally staged and unstaged contents stay separate. Other incoming files
are updated normally. Overwrite replaces all listed locally changed files after
backing them up. A successful keep-local update may still have `dirty: True`.

```text
astra update --keep-local
astra update --overwrite-local
```

These explicit policies are useful for scripts. `--json` does not prompt, and
noninteractive overlapping changes require a policy before mutation. Policy
flags cannot be combined with `--check`, `--repair` or `--recover`. An
already-current healthy checkout retains local edits even if overwrite was
requested; there is no incoming update to apply.

Unrelated untracked/ignored files are retained. A local file, including an ignored
one, that an incoming file would replace is included in the choice. Private
configuration/state and generated environments are excluded from source-file
overwrites. File/directory and submodule conflicts that cannot be preserved
unambiguously require manual resolution before mutation.
Resolve intent-to-add entries and hidden index flags on affected paths explicitly;
the updater does not convert them into ordinary staged files or silently drop them.

There is no automatic stash, reset of local commits, or branch switch. Shared or
symlinked generated environments are not mutated by another worktree.

Close interactive Astra sessions before applying an update. New launcher/backend
processes hold process-lifetime leases; source setup/update uses a separate
installation lock to prevent simultaneous startup or another update. The updater
identifies installed companion services and manages their pause/restart itself.
Unknown processes holding the installation remain blockers; no broad Python/Node
termination is used.

### Companion service lifecycle

Local-file decisions and tooling checks happen before services are paused. The
enabled service set is saved in `services.json`; stop intent is durable before
each native call. After pausing, the updater checks again that the installation
is idle before changing code or generated directories. A cancelled choice,
`--check`, or healthy already-current checkout does not restart services.

| Platform | Automatically managed services | Restored state |
| --- | --- | --- |
| macOS | Browser bridge, activity summarizer and activity sync LaunchAgents whose executable/module/working directory identify this checkout; the stable native activity recorder when this checkout owns the activity pipeline | Previously loaded jobs, with unchanged plists, recording settings, exclusions and pairing data. Continuous jobs must be running; periodic jobs need a loaded schedule. |
| Windows | Opt-in Python activity recorder launched from this installation, including its owned summary child | Graceful stop marker and bounded exit wait, then hidden restart with the same validated arguments. Existing pause state stays in place; a fresh running heartbeat is required. |
| Linux | No companion service is currently installed by Astra | No service-manager operations. |

The macOS service set also includes shared MLX embedding workers launched by this
checkout. Native argument inspection identifies their exact interpreter, module
and runtime directory, including paths containing spaces. The updater waits for
active encoding to finish and uses the authenticated loopback stop endpoint; it
does not kill an arbitrary Python process. Original worker directories are
restored before periodic clients restart. A restored worker must expose its
authenticated control endpoint; model preparation can continue in the background.
Rendezvous tokens and embedding contents never appear in update receipts.

Unloaded/disabled services remain off. A LaunchAgent belonging to another checkout
is not modified. The updater compares the loaded identity and saved definition,
then checks its fingerprint again before restarting; edits made during maintenance
are preserved and reported for inspection. Existing stable native executables
are restarted as installed; source maintenance does not silently rebuild or
re-sign them. Appshot daemons belong to sessions and are started/reconnected on
the next session launch. External model and MCP servers are outside this set.

Successful updates print `services_status: restored` and per-service results.
If the code was installed but a service failed to restart, the result remains
`outcome: applied`, with `services_status: restart_failed`, an actionable message
and a nonzero exit code. Other services still get their restoration attempt.
Run `astra update --recover` to retry the remaining service restoration. This
does not roll back successfully installed code or restart services already
restored by the same operation. JSON stdout remains separate from stderr progress.

The updater runs from a temporary copy outside the checkout on a base Python
interpreter. Windows source batch entrypoints finish in a pre-parsed command
block, so changing a batch file during an update cannot change its continuation.
If a Windows command was invoked from the very virtual environment being
replaced, it directs the user to `astra.bat` instead.

Only affected generated directories are backed up: Python for Python-lock or
package metadata changes, Node dependencies for npm-lock changes, and the build
output for interface changes. Unverified environments and explicit repair run
the full synchronization. Existing extras are recorded; the initial adoption
also detects common extras already present in an older environment. `--inexact`
retains additional installed Python packages, while dependency checks still
reject incompatible combinations.

Every changed environment is preserved before mutation. The updater fast-forwards
to the pinned commit, performs the needed locked synchronization, checks Python
imports and syntax, verifies the interface, and writes a receipt. A successful
`applied` result means that the installation is ready for the **next launch**.
It does not claim that an existing process is already running the new code.

## Recovery and repair

```text
astra doctor
astra setup --repair
astra update --repair
astra update --recover
```

- `setup --repair` synchronizes the current source configuration, including
  deliberately edited development metadata.
- `update --repair` repairs the current clean commit without fetching a new one.
- `update --recover` resolves an interrupted setup/update before another launch.

Service restoration follows runtime recovery: after a successful rollback, services
restart against the restored old runtime; if rollback is incomplete they remain
paused. The service journal also covers interruption before a code transaction
exists and after one has committed. When only services need recovery,
`outcome: services_recovered` reports that retry. `astra doctor` exposes
`pending_services` and identifies this recovery requirement.

Failed updates restore the recorded previous source commit, original staged and
unstaged local files, and every generated directory they changed. They do not
restore an old conversation database over new user data. Virtual-environment
snapshots are restored to the original path
on the same machine; they are not portable environments or cross-platform backups.

If another process has edited the checkout or changed HEAD, recovery stops with
an actionable error and retains its journal. It does not force-reset that work.
Likewise, failure to replace a Windows-locked directory keeps the recovery record
instead of reporting success. Inspect the error and close the identified holder,
then retry recovery.

For source installs, control files are under `.astra/launcher/`:

| File/directory | Purpose |
| --- | --- |
| `installation.json` | Installation identity and selected extras |
| `environment.json` | Last verified lockfile fingerprint and runtime versions |
| `pending.json` | Authoritative interrupted-operation journal |
| `services.json` | Original enabled companion services and durable pause/restore intent; retained until restoration completes |
| `transactions/` | Generated directories retained until completion/recovery |
| `receipts/latest.json` | Most recent applied/current/recovered result |
| `instances/` | Live process leases and versions captured at process startup |
| `local-changes/<id>/` | Retained original file contents, index snapshot and manifest for a local-file choice |

Do not delete `pending.json` or `services.json` to bypass recovery. If manual
recovery is necessary, preserve the journals and generated-directory snapshots
before changing anything. A service definition changed during maintenance must
be inspected and reconciled with the recorded definition before automatic retry.

Successful local-file updates retain their private backup and print its path.
The `files/` subdirectory contains original file contents under their relative
source paths; `manifest.json` describes types, permissions and local deletions.
To recover a file after a successful overwrite, inspect that saved copy before
restoring it over any newer work. The saved `index` belongs to transaction
recovery at the original commit; do not copy it into an updated Git checkout.

## Data and installed distributions

| Location | Source checkout | Package-installed distribution |
| --- | --- | --- |
| Program files | The actual source root | The installer-owned package location |
| Private state | `<source>/.astra` | Platform user-data directory |
| Sessions | `<source>/.sessions` | `<user data>/sessions` |
| Provider configuration | `<source>/.env` | `<user data>/.env` |

`ASTRA_HOME` overrides the private-state directory. Existing specific database,
settings and session overrides keep their precedence. Known legacy `.astra/...`
settings in `.env` resolve against the selected data directory. Workspace-owned
files, such as project rules and file checkpoints, remain in their own project.
The source launcher keeps `.env` beside the source checkout.

Default installed-distribution data locations are `%LOCALAPPDATA%\Astra` on
Windows, `~/Library/Application Support/Astra` on macOS, and
`$XDG_DATA_HOME/astra` (normally `~/.local/share/astra`) on Linux. Each installation
has a separate control identity, even when a shared data profile is selected.

`astra version` and `astra doctor` distinguish source and package ownership.
Package-owned installations are not modified with Git, uv project sync, or npm.
Upgrade them through their original installer. The current Python wheel lacks
the complete Ink assets and native helper; default startup reports that missing
component rather than entering the legacy CLI silently.

A future formal release can supply bundled resources and a versioned runtime
activation adapter without changing command names or private data ownership.
This work does not publish that release, create a stable-release feed, silently
upgrade external model/MCP servers, or implement in-chat restart scheduling.

## Acceptance boundary

Automated coverage includes isolated management commands without site packages,
native command forwarding tests per host, real temporary Git remotes, dirty and
divergent repositories, file collisions, update failure and interruption,
state preservation, process leases, and installed-wheel import isolation.
The existing maintenance gate additionally runs Python/TUI tests, typing, lint,
the interface build and optional wheel smoke on its supported OS matrix.

Tests executed on macOS do not count as native Windows acceptance. Windows CMD
and PowerShell, platform-specific helper behavior, and real provider interactions
must be reported separately from the portable automated checks.

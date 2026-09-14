# macOS browser URL recording

Edge and Chrome can enrich activity events with the active page URL through the
existing MV3 extension. Safari retains its existing recorder behavior; this
bridge does not add Safari support. Ordinary `activity install` does not enable
the browser bridge.

## Enable explicitly

From the stable Astra checkout, using its Python environment:

```sh
.venv/bin/python -m agent.cli.main activity browser-install
.venv/bin/python -m agent.cli.main activity browser-status
.venv/bin/python -m agent.cli.main activity browser-token
```

The final command copies the pairing token to the clipboard without printing it.
In Edge, open `edge://extensions`, enable developer mode and load the repository's
`browser-extension` directory as an unpacked extension. Open its options, paste
the token, enable recording and save. The browser extension continues to use the
local bridge on port 8089. Install the updated native recorder with
`scripts/deploy_activity_recorder.sh`; macOS may require Accessibility and Input
Monitoring permission again after the signed binary changes.

The dedicated launch agent is `com.astra.activity-browser-bridge`. Its generated
plist uses the Python interpreter and checkout used by `browser-install`; do not
install from a temporary worktree that will later be removed. Re-run installation
after relocating the checkout or environment. Logs discard captured content;
`browser-status` reports the launchd state, last exit code when available and
snapshot freshness without exposing URLs or the token. A loaded process alone
does not prove extension pairing or recorder acceptance.

Optional bridge exclusions:

```sh
.venv/bin/python -m agent.cli.main activity browser-install --exclude-domain private.example
```

Repeat the option for multiple domains. Without options, installation reads
`ASTRA_ACTIVITY_EXCLUDE_DOMAINS` as a comma-separated list. Keep the extension's
exclusions aligned. Changing the environment afterwards requires reinstalling
the launch agent to update its arguments.

## Storage and capture boundary

State is stored under `ASTRA_ACTIVITY_ROOT/browser-bridge`, defaulting to
`~/Library/Application Support/Astra/activity/browser-bridge`. This directory is
private (0700); the token, opt-in marker and atomic snapshot are private (0600).
The recorder requires matching browser identity/title and a snapshot no older
than eight seconds. Newer private, unfocused, invalid or excluded updates remove
the corresponding URL; restarting or stopping the bridge clears its snapshot.
An enabled Edge/Chrome bridge with no valid match supplies no URL, while title
and existing AX recording continue.

URLs retain only HTTP(S) scheme, host and path: credentials, query and fragment
are removed. Paths may still contain sensitive information and can be included
in activity summaries sent to the configured summary model. Private-mode URL
suppression is not suppression of window titles or AX content.

## Disable

```sh
.venv/bin/python -m agent.cli.main activity browser-uninstall
```

This revokes the recorder's opt-in marker before stopping the bridge, removes its
snapshot and launch agent, and retains the pairing token and existing history.
The recorder then resumes its previous URL behavior, including any native AXURL
available to it. This command is not deletion of recorded activity. Disable the
extension separately if it should stop attempting local updates.

## Acceptance

Check `browser-status` for a running process and a fresh snapshot while an
ordinary Edge tab is focused. Confirm a newly recorded event carries its
sanitized URL and that sync preserves it. Also check same-title navigation,
private-window invalidation and disabled behavior. Unit and harness tests cover
the protocol but do not establish these live-browser results.

# Windows computer activity (opt-in)

Double-click `activity-control.bat` at the repository root. Start runs a hidden
recorder; Pause suspends new sampling and stops the current summary subprocess
within the polling interval; Resume clears the pause; Stop shuts it down.
Closing the menu does not stop recording. No login task is installed.
Pause persists across recorder restarts until explicitly resumed.

The lightweight recorder samples the foreground executable name and window title
every five seconds. It emits transitions and one-minute heartbeats through the
same event normalizer and SQLite store used by macOS. It does not collect keys,
clipboard, screenshots, selected text, or page contents. Browser URLs require
the optional paired extension described below.
Durations count only consecutive observed samples, not idle, sleep, or lock gaps.
After 120 seconds without input, or when the input desktop is inaccessible or
not the normal desktop, sampling stops. Sub-poll activity cannot be measured.

Password-manager executables are excluded by default. Add exact executable names
using `ASTRA_ACTIVITY_EXCLUDE_APPS=example.exe,another.exe` in the local `.env`.
`ASTRA_ACTIVITY_EXCLUDE_TITLES` is a comma-separated list of case-insensitive
substrings (default: InPrivate, Incognito, 隐身, 无痕). Title filtering cannot
guarantee detection of every private browser window; exclude the browser entirely
if its titles should not be recorded. Configuration reloads on recorder restart.

Every ten minutes the recorder starts the existing summary worker for completed
ten-minute windows. Windows and Mac share summary, vector indexing, and retrieval
code. With default model settings, app names and window titles are sent to
DeepSeek for summaries; dedicated `ASTRA_ACTIVITY_SUMMARY_MODEL`,
`ASTRA_ACTIVITY_SUMMARY_BASE_URL`, and `ASTRA_ACTIVITY_SUMMARY_API_KEY` overrides
apply. Defaults are `deepseek-flash` (V4.1 Flash) with thinking disabled and `DEEPSEEK_API_KEY`;
interactive `LLM_MODEL` / `LLM_BASE_URL` / `LLM_API_KEY` do not affect summaries.
This configuration is shared by macOS and Windows. Windows GGUF embeddings
remain local. Start `embedding-control.bat` for semantic indexing. Missing
embedding service defers indexing without losing recorded activity.

The initial summary is not immediate: a window must finish, have at least three
events, and be picked up by the next summary run. Use `/context-index all` in
Astra to include activity recommendations and `/context-index why` to inspect
actual source results. Recorder status is separate from summary/vector health.

Records go to `.astra/activity-history.sqlite3` (or `ASTRA_ACTIVITY_DB`). Recorder
heartbeats and summary logs are under `.astra/windows-activity/`. Metadata status
contains counts and errors, not window titles. The recorder retains 30 days of
activity, pruning hourly. Menu option 7 permanently clears the local activity
archive and the current model's vector index after the recorder is stopped and
you type CLEAR. This includes any imported activity in that same database, but
does not delete conversation memory. Older model-specific vector files may remain;
without canonical evidence their entries cannot be opened as recommendations.

For custom settings run the Python module directly:

```console
.venv\Scripts\python.exe -m agent.runtime.activity_recorder.windows --poll 5 --idle 120 --retention-days 30
```

Use `--no-summaries` for local recording without LLM requests. One project-wide
Windows file lock prevents duplicate writers. Normal stop waits for cleanup;
if it cannot stop promptly the controller reports that state instead of killing
unrelated Python processes. This module rejects non-Windows execution; the Mac
Swift recorder and LaunchAgents remain unchanged.

## Edge / Chrome foreground URLs

1. Start the recorder and open `edge://extensions` (or `chrome://extensions`).
2. Enable Developer mode, choose Load unpacked, and select this repository's
   `browser-extension` directory. No store publishing or browser policy changes
   are required. Install only in the profile you want to record.
3. In `activity-control.bat`, choose **8** to copy the local pairing code.
4. Open the extension's Options, paste the code, check Enable, and Save.
5. Visit a normal HTTP(S) page. Options should show Connected. Use recorder
   Status to confirm `browser_bridge: listening`.

The extension has tabs/storage/alarms permissions and access only to the local
receiver at `127.0.0.1:8089`. It has no content scripts and cannot run in incognito
mode. It sends only a currently focused, fully loaded normal tab. Disabled,
unfocused, loading, private or excluded pages send an empty snapshot. Parameters,
fragments and URL credentials are stripped before transmission and again before
storage. Paths and titles are retained and may be sent to the summary model.

Add excluded domains in extension Options (subdomains are also excluded), or set
`ASTRA_ACTIVITY_EXCLUDE_DOMAINS=example.com,another.example` in `.env` and restart
the recorder. Supported browser windows are skipped unless a paired snapshot is
under eight seconds old and matches the OS foreground window title. Thus browser
titles are also skipped until pairing. Other applications continue recording.
If a browser suspends the extension worker, sampling skips its stale snapshot;
tab/focus events or the alarm revive it. Very brief pages can be missed by the
five-second OS sampling interval.

The local token stays in ignored `.astra/windows-activity/browser-token.txt` and
the browser profile's extension storage. Do not publish it. Uninstalling/disabling
the extension stops URL updates without changing the base recorder. This first
bridge is used only by the Windows recorder; Mac's native capture remains unchanged.

Protocol references: [Chrome tabs](https://developer.chrome.com/docs/extensions/reference/api/tabs),
[Chrome windows](https://developer.chrome.com/docs/extensions/reference/api/windows),
[Chrome alarms](https://developer.chrome.com/docs/extensions/reference/api/alarms).

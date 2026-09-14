# Everyday use

[Home](../README.md) · [Documentation](README.md)

Choose a model, adjust the interface and manage work in the terminal.

[Model connections](#model-connections) · [Model time context](#model-time-context) · [Reasoning intensity](#reasoning-intensity) · [Structured clarification](#structured-clarification) · [TUI themes](#tui-themes) · [Durable tasks](#durable-tasks)

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

## TUI themes

Use `/theme` to list the Ink TUI themes. `/theme hermes`, `/theme classic`,
`/theme nord`, `/theme dracula`, `/theme solarized`, and `/theme gruvbox`
switch immediately. `hermes` is the default warm gold-and-cream skin modeled
after Hermes CLI; `classic` preserves the original bright ANSI styling. The
selection is saved separately in `.astra/tui-settings.json`.

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

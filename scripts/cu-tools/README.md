# Standalone macOS helper commands

These small shell/Swift utilities operate the real desktop directly. They are
separate from Astra's target-bound Computer Use API and do not inherit its
approval, target identity or unknown-outcome handling. Prefer the
[Computer Use API](../../docs/macos-computer-use.md) for agent workflows.

## Install

Requires macOS, the Swift command-line toolchain and the relevant Accessibility
and Screen Recording grants. OCR also requires Tesseract with `chi_sim` and `eng`
language data. From the repository root, run:

```sh
bash scripts/cu-tools/install.sh
```

This compiles/copies the tools into `~/.astra/bin`; invoke them by that path or
add it to the shell's `PATH`. Installation does not enable permissions.

## Interfaces

| Tool | Interface and effect |
| --- | --- |
| `cuclick` | `x y [left\|right\|double] [-f app]`; click, optionally activate an app |
| `cushot` | `x y w h [out.png]`; capture a screen region, default output `/tmp/cushot.png` |
| `cuocr` | `x y w h [--raw]` or `image.png`; OCR as tab-separated word/confidence/rectangle |
| `cuwin` | `-l` to list; `keyword [--first]` to activate a matching window |
| `cuclip` | `path` or `-t text`; replace clipboard contents with a file reference or text |
| `cuwait` | `text [-x X -y Y -w W -h H] [--absent] [--timeout N] [--interval I]` |

`cuwait` reports success as exit 0 and timeout as exit 1. It uses a whitespace-
insensitive OCR substring check; it does not prove an app saved or submitted data.
Its default region is 1512 × 982, so pass the actual region on other displays.

Region inputs use logical screen coordinates. The current `cuocr` region path
assumes a 2× image and converts pixel rectangles by dividing by two; this must be
checked on the actual display. `--raw` and existing-image input report pixel
coordinates relative to the image. Do not treat those as verified click targets.

`test.sh` can capture the screen and exercise desktop utilities. Read it and use
a controlled fixture before running it; a successful local smoke check is not
general application compatibility evidence.

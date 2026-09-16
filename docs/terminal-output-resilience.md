# Terminal output resilience

Approved scope: reduce live rendering pressure, then isolate stalled terminal
output. Keep Ink 5 and React 18; an IME/cursor migration is a separate change.

## Problem and evidence

Ink 5 reprints all static history when its mutable output reaches the terminal
height. The current fixed preview allowance does not count expanding controls,
menus or wrapped input. Separately, synchronous POSIX TTY writes can block the
Node event loop and eventually back up the backend event pipe. These are
reproduced application weaknesses, not proof of a native terminal crash cause.

## A. Bound the live layout

- Allocate preview space from the actual visible control height, with room for
  Ink's trailing newline. Constrain the live area on the first render and resize.
- Give input and pending interactions priority. Bound menu, input and optional
  detail views without changing canonical input or stored conversation text.
- Batch Apple Terminal text previews at 80 ms. Completion, cancellation and
  other control events remain ordered immediate barriers.
- Preserve ordinary layout, theme and full static history.

## B. Isolate terminal writes

- Use one ordered asynchronous output transport, including Ink's global stderr
  cursor writes and application setup/cleanup sequences.
- Bound pending bytes and detect lack of write progress only while work is
  pending. Preserve partial-write ordering; never drop relative ANSI commands
  while continuing to draw as though they were delivered.
- On output failure, stop rendering and request backend shutdown through its
  existing cancellation/persistence path. Continue draining backend IPC during
  shutdown; do not replay tools. Bound terminal drain and backend writer close.
- Expose content-free output metrics and fault codes. Normal idle waits are not
  output stalls. Do not restart or signal unrelated user processes.

The transport uses an owned Node subprocess with acknowledged partial writes.
Using `fs.write` asynchronously kept timers responsive but still left Node exit
waiting for a blocked libuv worker in the stopped-reader reproduction. A process
boundary lets the parent terminate that writer without waiting for its syscall.
The queue limit is 4 MiB and the no-progress deadline is five seconds. Backend
shutdown has a ten-second grace period; its daemon event writer has a bounded
two-second join and writes through the descriptor without a TextIOWrapper lock.

## Verification

First reproduce overflowing menus, long CJK input and expanded activity in real
Ink output (`debug: false`, `CI=0`), then verify no overflow history replay at
143x36, 80x24 and small/resized windows, including pending interactions. Check
cursor editing and complete submission text, not just a cropped screenshot.

Use owned PTYs for stalled/slow output and disconnect tests. Check event-loop
responsiveness, byte bounds, ordering, bounded shutdown and recovery boundaries.
Run focused tests, the full TUI suite, relevant Python shutdown tests, type/lint
checks and production build. Native Terminal/IME acceptance remains separate.

Deliver A and B as separate commits and integrate on local main while preserving
unrelated work. No dependency upgrade or public release is part of this change.

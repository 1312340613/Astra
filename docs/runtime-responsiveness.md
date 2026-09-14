# Runtime profiling and diagnosis

[Home](../README.md) · [Documentation](README.md) · [简体中文](zh-CN/runtime-responsiveness.md)

Astra records optional timings to help distinguish model wait, tool execution,
storage and interface delivery. Enable `ASTRA_PROFILE_QUERY=1`, restart Astra,
reproduce the operation, then exit normally so accepted records can drain.

Run these commands from the installation checkout using its Python interpreter
(`.venv/bin/python` on macOS/Linux, `.venv\Scripts\python.exe` on Windows):

```text
python -m agent.runtime.latency
python -m agent.runtime.query_profiler
python scripts/benchmark_runtime_responsiveness.py --samples 20 --lock-ms 150
python scripts/benchmark_runtime_responsiveness.py --samples 50 --lock-ms 0
```

## Profile files

`ASTRA_PROFILE_QUERY=1` writes `.astra/query-profile.jsonl` for model-request
stages and `.astra/runtime-profile.jsonl` for tool, storage and frontend stages.
With `ASTRA_HOME` set, both files live there. Runtime records use a bounded queue
and a 5 MiB file with two rotated backups. Dropped records, write errors and
unacknowledged samples appear in the final summary; an incomplete run reports
`complete: false`. Use `--generation ID` to inspect an older backend lifetime.
Profiling is off by default and does not change model or reasoning settings.

## Reading the results

| Stage | What it helps explain |
| --- | --- |
| Request preparation and first model event | Local preparation versus provider waiting |
| First reasoning and first answer | When different output channels become available |
| Tool queue, approval and execution | Waiting for admission versus work inside the tool |
| Result processing and persistence | Serialization, storage and delivery overhead |
| Frontend handling and React commit | UI processing after an event arrives |

Reports normally summarize the latest backend lifetime with sample counts,
median, p95, maximum and dropped-record counts. Each process uses its own
monotonic clock. A React commit is not a measurement of when terminal pixels
became visible. Timings are bounded, use sanitized identifiers and do not need
conversation text or raw tool output.

## Runtime behavior

Task and approval writes, session saves and Session Recall persistence run off
the event loop. A bounded, ordered event writer preserves delivery order and
applies backpressure. Tool admission still obtains a durable execution claim
before dispatch. Cancellation drains already accepted writes and retains unknown
outcomes so incomplete records cannot trigger automatic action replay.

This keeps slow storage from blocking unrelated controls. It does not make a
locked database finish its write sooner or reduce a model's reasoning budget.
Normal shutdown waits for accepted writes and worker cleanup.

## Repeatable measurements

The benchmark uses temporary SQLite stores and controlled local workloads; it
makes no model requests. Compare the same workload with and without injected
lock pressure and retain failures and long-tail samples. It measures local
scheduling/persistence behavior, not network latency or general answer quality.

Save individual reports under `output/` or `.astra/artifacts/` with the commit,
platform and relevant configuration. Use a fresh fixed-condition measurement to
choose further optimizations; historical pass counts and one machine's timings
are not current release guarantees.

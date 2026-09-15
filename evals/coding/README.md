# Coding evaluation

**English** · [简体中文](README.zh-CN.md)

The maintained entry point is `python -m agent.evals.swebench`. It runs a
10-instance subset of SWE-bench Verified from public Requests, pytest and Flask
repositories. Task checkouts use those upstream repositories, independently of
Astra's Git history.

## Run

Use a separate checkout and virtual environment with Astra's development
requirements installed. The runner clones task repositories and, unless
`--skip-install` is set, installs them into the interpreter's environment.

```text
# Prepare one baseline and run its judge, without calling a model.
python -m agent.evals.swebench --instances psf__requests-1142 --dry-run

# Run the agent, then apply the hidden tests and judge the result.
python -m agent.evals.swebench --instances psf__requests-1142 --model deepseek-flash

# Run all bundled instances; a configured model and API credentials are required.
python -m agent.evals.swebench --all --model deepseek-flash --rounds 3
```

`--dry-run` still needs network access for uncached repositories and dependencies.
It makes no model request. Baseline test failures (pytest exit 1) are expected
for bug-fix tasks; collection, checkout and installation failures are pipeline
errors and cause a nonzero command exit. A completed dry run is not an agent
score or proof that the task was fixed.

## Results and limits

Reports are written to `evals/coding/results/swebench_<model>_<timestamp>.json`.
They record each instance's status, judge output, elapsed time and model usage.
Only a real agent run whose judge succeeds is counted as resolved.

This is Astra's lightweight runner over a small selected subset, not the
standard SWE-bench harness or a full SWE-bench Verified leaderboard result.
Use the recorded configuration and exact instance list when comparing runs.

The old `agent.evals.coding_bench` entry point and its single local-history task
have been retired. Use the SWE-bench entry point above; no unpublished Astra
commits are required.

## Sources and licenses

The manifest is adapted from
[SWE-bench Verified](https://huggingface.co/datasets/princeton-nlp/SWE-bench_Verified).
The original problems and test patches are third-party evaluation material;
Astra's MIT license does not replace their upstream notices. See
[third-party notices](THIRD_PARTY_NOTICES.md). Repositories cloned for evaluation
retain their own license files.

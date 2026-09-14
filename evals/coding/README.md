# Astra Coding Benchmark（Step 0 · 立秤）

用真实任务量化 Astra 的编码能力，作为后续一切改进的对照基准。

SWE-bench Verified 子集跑法：

```powershell
python -m agent.evals.swebench --all --model deepseek-flash
python -m agent.evals.swebench --instances psf__requests-1142 --dry-run
```

子集 manifest 位于 `evals/coding/swebench_verified_subset.json`，只包含低依赖
Python 仓库（requests / pytest / flask）。`--dry-run` 只验证 checkout、依赖安装和
判分管线，不调用模型。


## 为什么

在没有评测基准前，"变强了没"无法回答。这个基准从 agent-system 自己的 git 历史
挑选真实 bug 修复、小功能与跨文件重构，用 pytest 硬判分（pass/fail 是唯一标准），
并记录回合数、token 成本与墙钟耗时。同一题库 × 多模型 × 多次运行形成基线矩阵，
模型路由（Step 1）与执行效率改造（Step 2）都以此为准回归。

## 任务结构

```
evals/coding/tasks/<task-id>/
  setup.json        # {id, category, start_commit, target_commit, judge, timeout_min}
  task.md           # 任务描述（agent 可见）
  verify/test_task.py  # 判定测试（agent 不可见，pytest 判分）
```

每道题 = 一个真实 commit 及其 parent：

- `start_commit`：工作区从该提交检出（缺陷/缺失功能状态）
- `target_commit`：参考修复（仅用于选题与人工复核，不给 agent）
- 判定测试断言修复后的行为

## 运行

```powershell
# 需要 DEEPSEEK_API_KEY / QWEN38_API_KEY（按所选模型）
python -m agent.evals.coding_bench --task llm-chat-max-tokens --model qwen3.8-max
python -m agent.evals.coding_bench --all --model deepseek-flash --rounds 3
# 不调用模型，只验证工作区构造与判分管线
python -m agent.evals.coding_bench --all --dry-run
```

结果写入 `evals/coding/results/report_<model>_<stamp>.json`，每轮一条：

```json
{"task": "...", "model": "...", "round": 1, "status": "pass",
 "wall_seconds": 318.4, "iterations": 9,
 "prompt_tokens": 12430, "completion_tokens": 3851, "judge_stdout": "..."}
```

## 指标口径

- `status`：`pass`（判定测试全过）/ `fail`（至少一个失败）/ `error`（任务中断）
- 成功率 = pass / 总轮次；方差由 `--rounds N`（N≥2）观察
- token 成本从 `AgentContext.total_prompt_tokens / total_completion_tokens` 读取；
  后续接入前缀缓存后应另记 cache hit/miss（报告结构可扩展）

## 选题原则

- 从 git 历史挑**真实发生**的 bug/改造，三类均衡：bugfix+测试、小功能+测试、跨文件重构
- 判分必须确定性：pytest 硬判，不用 LLM-as-judge
- 任务描述模拟真实场景（给出症状与验收），不透露判定测试内容
- 起始状态必须是可复现的 commit

## 添加新题

1. 在 tasks/ 下建目录，写 setup.json / task.md / verify/test_task.py
2. 确认 `start_commit` 可 checkout、判分测试在起始状态失败、在 target 状态通过
3. 用 `--dry-run` 验证管线，再真实跑一轮校准难度

## 当前题库

| 题 ID | 类别 | 起始 commit | 说明 |
|---|---|---|---|
| llm-chat-max-tokens | bugfix | 3eca2bb | LLMClient.chat 不透传 max_tokens 导致 delegate 调用 TypeError |

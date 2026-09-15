# 编码评测

[English](README.md) · **简体中文**

当前维护的入口是 `python -m agent.evals.swebench`。题库选取 SWE-bench Verified
中 Requests、pytest 和 Flask 公开仓库的 10 道题。每题从对应上游仓库检出起始代码，
不依赖 Astra 自身的 Git 历史。

## 运行

使用单独的源码副本和虚拟环境，并安装 Astra 的开发依赖。评测会克隆题目仓库；
除非指定 `--skip-install`，还会把题目依赖安装到当前解释器环境。

```text
# 准备一道题的基线并运行判定测试，不调用模型。
python -m agent.evals.swebench --instances psf__requests-1142 --dry-run

# 让 Agent 解题，然后应用隐藏测试并判分。
python -m agent.evals.swebench --instances psf__requests-1142 --model deepseek-flash

# 运行全部题目，需要配置对应模型和 API 凭据。
python -m agent.evals.swebench --all --model deepseek-flash --rounds 3
```

`--dry-run` 不发送模型请求，但未缓存的仓库与依赖仍需联网。修复类题目的基线断言失败
（pytest 退出码 1）属于预期结果；检出、安装或测试收集失败属于评测流程错误，命令会
以非零状态退出。完成 dry run 不能当作 Agent 得分，也不表示问题已修复。

## 结果与限制

结果写入 `evals/coding/results/swebench_<model>_<timestamp>.json`，记录每题状态、
判定输出、耗时和模型用量。只有真实 Agent 解题后通过判定测试，才计为解决。

这里使用 Astra 的轻量执行器和小规模自选题目，不等于标准 SWE-bench harness 的
完整 Verified 榜单结果。比较多次运行时应保留实际配置与准确的题目列表。

旧入口 `agent.evals.coding_bench` 及其唯一的本地历史题已退役。
请使用上述 SWE-bench 入口，不需要获取任何未公开的 Astra 提交。

<a id="sources-and-licenses"></a>

## 来源与许可

题库取自 [SWE-bench Verified](https://huggingface.co/datasets/princeton-nlp/SWE-bench_Verified)。
原始问题与测试补丁属于第三方评测资料，Astra 的 MIT 许可证不替代其上游声明，见
[第三方声明（英文）](THIRD_PARTY_NOTICES.md)。评测时克隆的仓库保留自己的许可证文件。

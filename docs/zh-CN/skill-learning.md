# 技能学习与手动检查

[首页](../../README.zh-CN.md) · [文档导航](README.md) · [English](../skill-learning.md)

[使用技能库](#use-the-library) · [技能归属](#skill-ownership) · [分批检查](#review-a-batch) · [迁移旧技能库](#migrate-an-older-library) · [维护参考](#maintenance-references)

Astra 可在工作中直接把可复用的方法保存为技能，无需每条经验都经过候选、实验和激活流程。由模型判断是否值得保存，并不要求每轮对话都产生技能。

<a id="use-the-library"></a>

## 使用技能库

```text
/skills
/skills show <name> [file]
/skills create <name> <description>
```

技能包含 `SKILL.md`，可附带 `references/`、`templates/`、`scripts/` 或 `assets/`。模型自己的总结使用 `skill_manage(origin="auto")`；用户要求加入的内容使用 `origin="user"`。提示中只注入目录，完整指令按需读取。内置规则有单独的[加载约定（英文）](../runtime/core-rules.md)。

<a id="skill-ownership"></a>

## 技能归属

| 来源 | 含义 | `/learn review` 是否检查 |
| --- | --- | --- |
| `auto` | 模型总结并维护的方法 | 是 |
| `user` | 用户要求加入、安装或编辑的技能 | 否 |
| `builtin` | 随 Astra 打包的核心规则 | 否 |

自动技能通常位于 `.astra/skills/learned/<name>/`；新用户技能位于 `user/`，已有分类保持原路径。`AGENT_SKILLS_PATH` 可指定其他技能库。归属以写入方的来源记录为准，不能靠正文声明或目录名称伪造。无法确认归属的内容按用户技能保护，`/skills` 显示归属和分类。

技能应说明何时使用、步骤、限制和来源。它会进入普通技能目录，相关任务通过 `skill_view` 读取。一次记录的经验不保证命令在其他机器或版本仍能运行。

<a id="review-a-batch"></a>

## 分批检查

```text
/learn
/learn review
/learn history
/learn history <run-id>
/learn undo <run-id>
```

`/learn review` 只检查自动技能，可保留、重写、合并或归档。用户技能正文和目录描述不进入检查请求。检查只审核文字和提供的来源材料，不执行技能中的命令，也不声称流程已通过实测。

每次只有一个受限的模型请求：最多 4 个技能、24,000 字符的正文和来源，超时 90 秒，输出上限 4,096 token。持久游标按稳定顺序记录进度。再次运行同一命令会继续剩余技能，换会话也能接着做；完整一轮结束后才允许重新遍历。超大条目会明确报告未检查。

固定保护、其他工作区、其他平台和被外部编辑过的技能不会被改动。只有完整放入请求的自动技能才能被修改或合并。输出无效、不完整或被截断时，整批不变，游标也不前进。空库不调用模型；没有自动重试、计时器、空闲检查或启动补跑。

原始版本和原因保存在目录之外的 `.astra/skills-learning/`，归档技能仍能从历史找到。日志恢复中断的多文件写入，库锁防止并发维护，内容校验保护审核期间发生的新编辑，撤销拒绝覆盖后续修改。记录不可读时 `/learn` 报错，LEARN 行显示 `? / CHECK`；普通聊天可继续，不会覆盖损坏记录。

关掉终端会停止未完成的维护，已提交的变更仍留在历史。`Ctrl+C` 或新消息会取消检查；若短暂的文件提交已经开始，会等它完成后返回取消，应在历史中确认结果。

`/learn mode off` 关闭新自动总结的直接保存；`/learn mode review` 重新开启。这里的旧名称 `review` 不代表定时维护。即使关闭自动保存，仍可明确执行 `/learn review`。

<a id="migrate-an-older-library"></a>

## 迁移旧技能库

```text
/learn legacy pending
/learn migrate
```

迁移会备份旧 SQLite 库，使用现有文件事务和撤销机制导入符合条件的自动技能总结。记录原 ID 和目标位置，重复执行不会重复导入。名称冲突和旧补丁不能覆盖用户技能，原数据库记录继续作为历史保留。

观察和环境笔记保留在历史库，不转成技能，可通过[本地历史检索](local-history-retrieval.md)读取。因此旧候选数量、自动技能数量和本批检查数量可能不同。

<a id="maintenance-references"></a>

## 维护参考

实现位于 `agent/runtime/skill_learning.py`、`skill_curation.py`、`skill_provenance.py`、`skill_migration.py` 和 `agent/cli/learning_commands.py`。测试覆盖归属、游标、无效输出、并发修改、迁移和撤销。可选 `scripts/skill_curation_acceptance.py` 使用历史数据副本做验收；原始记录和模型输出不要提交到 Git。

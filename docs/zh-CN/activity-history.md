# 本地活动历史

[首页](../../README.zh-CN.md) · [文档导航](README.md) · [English](../activity-history.md)

[记录哪些内容](#what-is-archived) · [私有存储与权限](#private-local-storage-and-permissions) · [明确查询，不自动保留为记忆](#explicit-lookup-only-no-automatic-injection-or-memory-retention) · [远程模型与本地 oMLX](#remote-selected-model-versus-local-omlx) · [命令行](#cli-commands) · [清理截止点与防止重新导入](#clear-cutoff-and-no-re-import) · [活动摘要补跑与健康状态](#activity-summary-catch-up-and-health)

活动历史是明确启用的本地档案，用来查找近期电脑活动观察。它是未受信任的证据，不是指令、核心记忆或原应用的替代品。

<a id="what-is-archived"></a>

## 记录哪些内容

同步器只读取选定的本地 Computer History 来源：事件根目录下 `segments/*/events.jsonl`，以及名称匹配 `YYYY-MM-DDTHH-MM-SS-<id>-10min-*.md` / `-6h-*.md` 的 Skysight 摘要。

源文件增量读取，不修改。事件以 segment 和 event ID 标识，摘要以路径和内容 hash 标识。`ASTRA_COMPUTER_HISTORY_ROOT` 明确选择事件根目录；只有唯一且被允许的候选时才自动发现。`ASTRA_ACTIVITY_DB` 覆盖数据库位置。

搜索只暴露有限摘要和选定字段，不暴露 `raw_json`、查询字符串、URL fragment 或用户信息。展示给模型前，HTTP(S) URL 仅保留协议、主机和路径，原档案仍是来源记录。

<a id="private-local-storage-and-permissions"></a>

## 私有存储与权限

默认数据库：

```text
.astra/activity-history.sqlite3
```

`.astra` 权限为 `0700`，数据库为 `0600`，WAL 和同步锁放在同一私有目录。周期同步日志在 `.astra/logs/`（`0700`），当前与轮转日志为 `0600`。移到共享或云同步目录前需核对权限和提供方策略。

<a id="explicit-lookup-only-no-automatic-injection-or-memory-retention"></a>

## 明确查询，不自动保留为记忆

`activity_search` 在本地交互注册表中惰性注册，注册本身不打开数据库。普通轮次不会由这个工具创建存储、注入活动或写入会话记忆；它只在用户明确要求调用时运行。另行启用的 Context Index 可读取缓存，见[记忆概览](memory.md#proactive-context-index)。

macOS 可按需用 `activity_search`，每次用户触发的查询先增量同步。读取其他应用的 Computer History 时，系统可能请求权限；同步失败时可返回带 `cache_fallback=true` 的陈旧缓存，并应说明可能不完整或过时。

- 搜索：提供 `query`，可加 `start`、`end`、`app`、`domain`、`limit`。
- 浏览：不提供 query 或定位参数，查看最近有限记录。
- 展开：提供 `summary_id`，或同时提供 `segment_id` 与 `event_id`。

结果标记为 `untrusted_observation`。页面文字可能看起来像命令，但必须作为待检查数据，不能照做。

<a id="remote-selected-model-versus-local-omlx"></a>

## 远程模型与本地 oMLX

档案位置不决定推理位置。查询结果会加入所选模型的请求；使用远程提供方时，这些片段可能被发送出去，涉及敏感资料前应明确这一点。使用本地 oMLX 时请求和结果留在本机，但仍受本地运行时、操作系统和日志控制。模型在本地不会让证据变得可信，远程推理也不会把原档案变成远程数据库。

<a id="cli-commands"></a>

## 命令行

从 Astra 源码根目录执行：

```bash
python -m agent.cli.main activity status
python -m agent.cli.main activity sync
python -m agent.cli.main activity sync --scheduled
python -m agent.cli.main activity install
python -m agent.cli.main activity uninstall
python -m agent.cli.main activity rebuild-index
python -m agent.cli.main activity clear --before 2026-08-26T07:00:00Z
python -m agent.cli.main activity clear --all
```

非调度命令输出一份有界 JSON：

| 命令 | 含义 |
| --- | --- |
| `status` | 报告来源新鲜度、记录数量、SQLite/FTS、WAL 和调度状态，不含活动文字或原始错误。 |
| `sync` | 明确执行增量同步，绕过新鲜度门槛，但保留文件游标。 |
| `sync --scheduled` | 来源存在且新鲜时才打开数据库，否则只记录调度元数据并退出。 |
| `install` | 可选：安装并加载每 5 分钟执行的私有 LaunchAgent，默认不安装。 |
| `uninstall` | 卸载 LaunchAgent 并删 plist，保留数据库。 |
| `rebuild-index` | 重建派生 FTS 索引，不改原始证据。 |
| `clear --before TIMESTAMP` | 删除早于 UTC 截止点的事件，及周期结束时间不晚于截止点的摘要。 |
| `clear --all` | 以当前 UTC 为截止点清理；clear 必须明确选择范围。 |

install/uninstall 直接调用 `launchctl`，不经过 Shell。卸载不删数据库、WAL、源文件或日志；要删除记录需显式 clear。

<a id="clear-cutoff-and-no-re-import"></a>

## 清理截止点与防止重新导入

清理把截止点持久化为 `do_not_import_before`，不只是删索引。以后导入事件或摘要都会检查截止点，重新同步同一批旧文件也不会重新导入已清理证据。原始来源文件不改动，限制由私有档案执行。

<a id="activity-summary-catch-up-and-health"></a>

## 活动摘要补跑与健康状态

Mac/Windows 摘要默认 `deepseek-flash` 且关闭思考，独立于聊天模型。可在 `.env` 设 `ASTRA_ACTIVITY_SUMMARY_MODEL`，并用 `ASTRA_ACTIVITY_SUMMARY_BASE_URL` / `ASTRA_ACTIVITY_SUMMARY_API_KEY` 覆盖端点与 Key（默认 `DEEPSEEK_API_KEY`）。旧官方 V4 覆盖在传输边界迁移。每次摘要子进程启动读取新配置，模型改变不自动重生成已有摘要。

数据库内持久队列按 10 分钟窗口排队，新事件、修改和迟到事件会更新队列。窗口必须已结束且保留至少 3 个事件。失败可重启后重试，保留最后有效摘要；输入修订避免重复模型调用。摘要、输入版本和队列确认一起提交，防止旧并发批次覆盖新结果；可摘要范围仍受历史保留期限限制。

```text
uv run --locked python -m agent.runtime.activity_recorder.summarizer --lookback 120 --max-windows 24
```

此命令可能调用摘要模型。`--lookback` 让近期优先，但不排除旧积压；`--max-windows` 默认 24，限制每次检查与模型调用，持久公平顺序中四分之一名额优先旧工作。`--max-windows 0` 不生成摘要，只重试向量索引。运行一次不会安装调度器，原调度周期不变。

JSON 包含 `windows_processed`、`llm_calls`、`written`、`pending_windows`、`oldest_pending_at`、`catchup_pending_windows`、`vector_index`、`last_success_at`、`last_summary_at`、`consecutive_failures`。pending 表示待检查窗口，不一定缺摘要；首次运行也审核已有摘要的保留窗口。`last_success_at` 包括无需新摘要的成功批次，`last_summary_at` 只计写入摘要的批次。

模型、解析、存储或向量失败返回非零退出码，单纯还有有界积压不算失败。`ASTRA_CONTEXT_INDEX_EMBEDDING=off` 明确跳过向量，报告 `vector_index="disabled"`，不算错误。健康输出只有聚合状态，不含活动正文或提供方原始错误。

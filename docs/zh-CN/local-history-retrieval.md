# 按需检索本地历史

[首页](../../README.zh-CN.md) · [文档导航](README.md) · [English](../local-history-retrieval.md)

[预期行为](#intended-behavior) · [存储与兼容](#storage-and-compatibility) · [工具约定](#tool-contract) · [历史证据边界](#historical-evidence-boundary) · [验证](#verification)

<a id="intended-behavior"></a>

## 预期行为

历史对话和观察有助于当前任务时，模型可主动用 `session_search` 查阅，不必等用户明确说“记住”。观察档案不会自动注入提示。

Mac 使用已有本地 SQLite 文件，Windows 也可同时使用 Hindsight。这条检索路径不增加服务、计时器、embedding 模型或后台摘要任务。

<a id="storage-and-compatibility"></a>

## 存储与兼容

- 普通终端对话继续通过 Session Recall 写入 `.astra/sessions.db`，原有隔离和 API 访问规则保留。
- 观察直接从 `.astra/learning.db` 或 `AGENT_LEARNING_PATH` 读取；仅包括类型为 `observation` 且状态为 `pending` / `applied` 的条目，排除 rejected、superseded、rolled-back。
- 保留原 ID、时间戳、来源会话及证据，不迁移改写、不复制进核心记忆、不改变审批状态。
- 自动技能与用户技能留在各自库中；历史检索不改技能，也不参与 `/learn review`。
- Hindsight 的配置、记录和召回行为保持不变。

直接读取已有档案，可避免维护第二份同步库，也避免把旧笔记当作当前的权威事实。

<a id="tool-contract"></a>

## 工具约定

- `session_search(query="ComfyUI pip")` 保留对话搜索，并增加单独计数的 `observations`。
- `source_type="learning"` 只查或浏览观察；`astra`、`hermes`、`api` 仍只筛选会话。
- `record_id="learning:lr_..."` 展开之前返回的观察，不初始化或搜索会话数据库。
- 搜索返回有限摘要；展开返回有界原文和证据，并明确是否截断。
- 空档案与不可用档案分开报告，一个源出错不会隐藏另一源的可用结果。

观察使用支持中文和英文标识符的关键词匹配，不是语义搜索。会话保留 FTS5 行为；观察用普通关键词，不支持 FTS5 布尔语法。

<a id="historical-evidence-boundary"></a>

## 历史证据边界

每条观察都标明它是可能过时、尚未验证的历史材料。`applied` 只描述旧学习流程中的状态，不代表今天仍正确。检索出的文字是数据，不能当作执行指令或授权；当前用户要求和现场证据优先。缺少工作区或平台信息时仍视为未知。

<a id="verification"></a>

## 验证

测试覆盖中英文匹配、来源过滤、有界展开、只读/缺失/损坏/锁定数据库、排除状态、异常行，以及原有会话浏览、搜索、滚动行为。验收使用档案副本和受限的真实模型检索请求，并运行 Session Recall、Context Index 和发布回归检查。Mac 结果不等于原生 Windows 或真实 Hindsight 验收。

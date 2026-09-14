# 日常使用

[首页](../../README.zh-CN.md) · [文档导航](README.md) · [English](../usage.md)

[连接模型](#model-connections) · [模型时间上下文](#model-time-context) · [推理强度](#reasoning-intensity) · [结构化澄清](#structured-clarification) · [终端主题](#tui-themes) · [持久任务](#durable-tasks)

选择模型、调整终端界面，并管理正在进行的工作。

<a id="model-connections"></a>

## 连接模型

内置配置位于 `config/models.yaml`。本机专用配置写入 `.astra/models.yaml`，可覆盖同名内置项；配置文件记录凭据的环境变量名，不保存 API Key 值。

```text
/model
/connect my-local http://127.0.0.1:8084/v1 LLM_API_KEY
/doctor
```

`/connect` 的第三个参数是保存 API Key 的环境变量名，默认使用 `LLM_API_KEY`。

<a id="deepseek-model-migration"></a>

### DeepSeek 模型迁移

内置 DeepSeek 配置使用 `deepseek-flash`（DeepSeek V4.1 Flash），支持原生图片输入和思考控制，工作代理的 `flash` / `fast` 别名也指向它。在官方 `api.deepseek.com` 端点上，已保存的 V4 Flash、Vision Exp 和临时 V4.1 名称会解析到这个配置；退休名称不再单独显示。`deepseek-v4-pro` 仍是独立可选的纯文本配置。自定义和本地端点保留各自的模型 ID；1M 上下文与 384K 输出额度保持不变。

[9 月 10 日更新公告](https://api-docs.deepseek.com/zh-cn/updates/)停用了 V4 Flash 和 Vision Exp；原定 9 月 14 日执行的 V4 Pro 重定向后来撤回，官方继续提供 `deepseek-v4-pro`，计费不变。已有 Astra 进程需重启才能加载新版适配器。

<a id="deepseek-vision-tiling"></a>

### DeepSeek 图片切块

`deepseek-flash` 可通过原始像素切块保留大型本地图片或 base64 图片的细节。此选项默认开启，修改后立即生效，并保存到 `.astra/settings.json` 的 `vision_tiles_enabled`：

```text
/vision-tiles
/vision-tiles on
/vision-tiles off
```

启用时，Astra 生成一张概览和若干无损 768 × 768 细节块，相邻块重叠 64 px。单次请求最多 32 张图片；放不下全部切块时，模型通过当前请求的切块工具选择补充区域。缓存位于 `.astra/image-cache/tiles`，并有容量清理限制。

原始像素保证仅适用于本地路径和 base64 数据。外部图片 URL 直接发送，Astra 不主动下载；小型动态 GIF 也直接发送。需要切块的动态 GIF 会返回可恢复错误，避免悄悄改为服务端缩小后的图片。`/vision-tiles off` 恢复直接发送，此时 DeepSeek 可能缩小大图并损失细节。其他模型配置不受影响。

<a id="model-time-context"></a>

## 模型时间上下文

用户消息带有仅供模型读取的时间戳，例如 `<message_time>2026-09-14T00:30:00+08:00 周一</message_time>`。周几按同一个本地日期计算；历史回放保留原日期，相对日期锚点也使用保存日期对应的周几。

界面时间栏不变，输出和历史过滤器同时识别旧版 ISO 标记和新版带周几标记。现有会话无需迁移。需要精确当前时间或其他时区时，使用 `current_time`。

<a id="reasoning-intensity"></a>

## 推理强度

`/mode` 只控制推理强度，与人格、工具和输出预算分开：

```text
/mode
/mode low
/mode high
/mode max
```

默认 `high`。选定的 `reasoning_effort` 保存到 `.astra/settings.json`，从下一次模型请求起应用。目前 DeepSeek 适配器发送该参数；其他适配器保留原行为，并说明该偏好未生效。输出预算仍由模型配置控制，`/think` 单独控制是否显示思考内容。

旧 `agent_mode` 的 `coding` 迁移到 `max`，`chat` 迁移到 `high`。旧 coding/chat 命令和 `/mode code` 已退休。普通会话使用原生工具，工具暴露方式不再作为用户模式选项。

状态栏中的 `TOOLS NATIVE` 是只读运行状态，位于推理强度之后，随后显示模型、上下文占用和最近工具结果。工具执行时仍显示模型。`Ctrl+L` 展开会话和上下文详情；窄窗口会缩短次要标签，优先保留模型、上下文和展开快捷键，极窄时优先显示 Computer Use 控制状态。

每次成功的模型请求结束后，状态栏显示该次平均输出速度 `tok/s`（紧凑布局为 `t/s`）。它等于提供方报告的 `completion_tokens` 除以请求总耗时，包含首 token 等待，不包含工具执行；DeepSeek 的计数包含思考 token。展开后的 `SPEED` 行显示计数与时长。这是最近完成请求的平均值，不是实时估计；缺少用量时不猜测速度，切换模型或会话会清除旧值。

<a id="structured-clarification"></a>

## 结构化澄清

较复杂的编码任务可能出现带选项和自定义回答的问题卡。回答后继续同一轮；需求明确且原请求已授权实施时，Astra 可填写 Working Plan、建立 Goal Mode 并开始执行。

选择答案不等于工具授权。文件、Shell、MCP、工作流和主机执行仍遵循原有权限检查。消息渠道无法显示交互卡时，工具返回可恢复错误，模型可改用普通文字提问，避免等待不可见的界面。

<a id="tui-themes"></a>

## 终端主题

`/theme` 列出 Ink TUI 主题。`/theme hermes`、`classic`、`nord`、`dracula`、`solarized` 和 `gruvbox` 会立即切换。默认 `hermes` 是参考 Hermes CLI 的暖金与奶油色主题；`classic` 保留鲜明的 ANSI 配色。选择单独保存到 `.astra/tui-settings.json`。

<a id="durable-tasks"></a>

## 持久任务

每轮用户请求通过 SQLite WAL 记入 `.astra/tasks.db`。模型和工具边界保存检查点；重启后可复用已经完成的工具结果，执行状态不确定的工具不会自动重放。

```text
/tasks
/tasks <task-id>
/resume <task-id>
/cancel [task-id]
```

任务运行时终端仍接收命令。第一次 `Ctrl+C` 请求持久化取消，再按一次强制退出进程。恢复仅限原会话，以保证对应的对话检查点可用。

回复流或工具执行期间也可切换 YOLO：`/yolo` 切换，`/yolo on` / `off` 明确设定，`/yolo status` 查询。`Ctrl+Y` 在授权面板、问题卡和工具详情中同样有效，不会提交草稿。启用后输入栏显示 `YOLO`，支持此机制的待审批操作获得一次性放行；强制权限边界仍然有效。关闭后，后续工具边界恢复检查，包括运行中的 Team 成员；已经授权的工具不会被取消，单独授予的会话权限也不会被抹掉。Work 和 Minimal 模式均支持 YOLO，不能在回复期间执行的命令会说明原因。

较长工具结果默认折叠。`/tool <id>` 打开指定结果，`Ctrl+O` 打开最近一次结果；方向键和 PageUp/PageDown 滚动，Escape 或 `Ctrl+O` 关闭。`TUI_TOOL_COLLAPSE_CHARS`、`TUI_TOOL_COLLAPSE_LINES` 和 `TUI_MAX_TOOL_RESULTS` 控制折叠阈值和详情保留数量。命令建议使用固定高度滚动窗口，`TUI_COMMAND_MENU_ROWS` 默认 8，方向键仍可遍历全部匹配项。

向输入框粘贴图片路径时，光标前后的已有文字会保留。路径转换成 `[Image #N]` 占位符并与问题分开处理；粘贴本身不会发送，确认草稿后再按 Enter。

# Windows 电脑活动记录（可选）

[首页](../../README.zh-CN.md) · [文档导航](README.md) · [English](../windows-activity.md)

双击 `.\scripts\activity-control.bat`：Start 后台启动；Pause 在轮询周期内停止新采样和当前摘要子进程；Resume 解除暂停；Stop 停止记录器。关闭菜单不停止记录，不安装开机登录任务。暂停状态跨记录器重启保留，直到明确恢复。

轻量记录器每 5 秒采样前台可执行程序名和窗口标题，将变化及每分钟心跳写入与 macOS 相同的归一化器和 SQLite。它不采集按键、剪贴板、截图、选中文字或页面正文，URL 需要下面的可选扩展。时长只累计连续观察，不计空闲、睡眠或锁屏间隙；无输入 120 秒、输入桌面不可访问或不是普通桌面时停止采样，轮询间更短的活动无法准确测量。

默认排除密码管理器。`.env` 中 `ASTRA_ACTIVITY_EXCLUDE_APPS=example.exe,another.exe` 使用准确程序名；`ASTRA_ACTIVITY_EXCLUDE_TITLES` 使用忽略大小写的逗号分隔子串，默认包括 InPrivate、Incognito、隐身、无痕。标题过滤不能保证识别所有隐私窗口，不应记录标题时可排除整个浏览器。重启记录器读取新配置。

每 10 分钟启动摘要 worker，处理已结束的十分钟窗口。Mac/Windows 共享摘要、向量和检索代码。默认把程序名与标题发给 DeepSeek；摘要配置为 `deepseek-flash`、关闭思考、使用 `DEEPSEEK_API_KEY`，可通过 `ASTRA_ACTIVITY_SUMMARY_MODEL`、`ASTRA_ACTIVITY_SUMMARY_BASE_URL`、`ASTRA_ACTIVITY_SUMMARY_API_KEY` 覆盖。聊天的 `LLM_MODEL` / `LLM_BASE_URL` / `LLM_API_KEY` 不控制摘要。

Windows GGUF 向量保持本地，用 `.\scripts\embedding-control.bat` 启动服务。缺少服务只延迟索引，不丢活动。首次摘要需等窗口结束、至少 3 个事件及下一次摘要调度，并非立即出现。Astra 中 `/context-index all` 启用活动推荐，`/context-index why` 查看实际来源；记录器状态与摘要/向量健康分开。

数据在 `.astra/activity-history.sqlite3` 或 `ASTRA_ACTIVITY_DB`，心跳/摘要日志在 `.astra/windows-activity/`。状态只含数量和错误，不含标题。默认保留 30 天，每小时清理。停止记录器后，菜单选项 7 并输入 CLEAR，会永久清空同库全部本地/导入活动和当前模型向量，不删除对话记忆。旧模型向量文件可能仍在，但失去原始证据的记录不能展开成推荐。

自定义启动：

```console
.venv\Scripts\python.exe -m agent.runtime.activity_recorder.windows --poll 5 --idle 120 --retention-days 30
```

`--no-summaries` 只本地记录，不请求模型。项目级 Windows 文件锁避免重复写入。正常停止等待清理，无法及时结束时报告状态，不终止无关 Python。该模块拒绝非 Windows 执行，不改变 Mac Swift 记录器或 LaunchAgent。

<a id="edge--chrome-foreground-urls"></a>

## Edge / Chrome 前台 URL

1. 启动记录器，打开 `edge://extensions` 或 `chrome://extensions`。
2. 开启开发者模式，加载仓库 `browser-extension`，只在要记录的浏览器配置中安装，无需上架或修改浏览器策略。
3. 在 `.\scripts\activity-control.bat` 选 **8** 复制本地配对码。
4. 扩展 Options 中粘贴、勾选 Enable 并 Save。
5. 打开普通 HTTP(S) 页面，确认 Options 显示 Connected，记录器 Status 显示 `browser_bridge: listening`。

扩展有 tabs/storage/alarms 权限，只访问 `127.0.0.1:8089`，不注入内容脚本，不能在隐私模式运行。只发送当前聚焦且加载完成的普通标签；禁用、失焦、加载中、隐私或排除页面发送空快照。参数、fragment、URL 凭据在传输和落盘前分别剥离；路径与标题保留，可能发给摘要模型。

可在扩展 Options 排除域名（含子域），或设置 `ASTRA_ACTIVITY_EXCLUDE_DOMAINS=example.com,another.example` 并重启记录器。受支持的浏览器窗口只有配对快照小于 8 秒且匹配前台标题时才记录，因此未配对前浏览器标题也跳过，其他应用继续。扩展 worker 被挂起后跳过旧快照，标签/聚焦事件或 alarm 可唤醒；5 秒轮询可能漏掉极短页面。

令牌存在忽略的 `.astra/windows-activity/browser-token.txt` 和浏览器扩展存储中，不应发布。卸载/停用扩展只停止 URL 更新，不改基础记录器。这条 Windows 桥接不改变 Mac 原生捕获。

协议参考（英文）：[tabs](https://developer.chrome.com/docs/extensions/reference/api/tabs)、[windows](https://developer.chrome.com/docs/extensions/reference/api/windows)、[alarms](https://developer.chrome.com/docs/extensions/reference/api/alarms)。

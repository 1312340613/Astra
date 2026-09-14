---
name: wechat-mac-automation
description: Use when inspecting or operating WeChat (微信 macOS), including welcome-to-main window transitions, chats and file transfer. Start with exact native window observation and distinguish unavailable capture content from input failure.
---

# WeChat macOS 自动化

## 入口：先确认当前窗口

同时遵守 `using-computer-use`。默认使用 `computer_*` 原生链路：

1. `computer_apps` 找到 `com.tencent.xinWeChat`，从最新目录选择 `bindable=true` 的目标窗口。
2. `computer_get_app_state(app_ref, window_ref)` 读取目标窗口本体及 AX 树；普通读取不激活应用。
3. 以本次快照确认欢迎页、主界面、聊天窗口或弹窗。不要按名字相同、`cuwin --first`、固定窗口大小或旧坐标选目标。
4. 欢迎页关闭、进入主界面或新窗口出现后，优先采用 `window_transition.next_observation` 的新 refs；否则重新 `computer_apps` → `computer_get_app_state`。欢迎页和主界面可有不同 window ID，隐藏的旧欢迎页仍可能存在。
5. 读取已返回的 AX 树，再决定动作。有的界面暴露按钮，有的聊天内容不暴露；不要把历史“自绘、无 AX”当作所有微信窗口的规则。

## 截图缺失：停止猜测

`window_content_unavailable` 表示目标窗口截图全透明，没有可读取像素；不生成可操作快照。
窗口共享状态为 `kCGWindowSharingNone` 时可能出现此现象，但全透明错误本身不证明具体保护设置。

- 窗口 `onscreen=true`、应用 `frontmost=true` 都不能证明截图可读。
- 截图里没有微信，不证明它被 Codex/终端挡住、跑到另一个 Space、移出屏幕，或上次点击失败。
- 不反复激活、移动窗口、重复截图或点击；不改用 `cushot` / `screencapture -R` / 全屏裁剪。桌面矩形的像素不是指定窗口内容的证据。
- 说明缺少可读取的窗口内容，交给用户检查应用的捕获/隐私状态。状态变化后再刷新目录并读取；不要擅自修改隐私设置。
- `snapshot_failed` 是较宽的旧错误，不能据此宣称遮挡、权限不足或鼠标通道不兼容。

## 点击与焦点

- 先使用本次快照确实提供的控件 ref 和动作。`AXRaise` 只升窗，不等于 `AXPress`。
- 需要坐标时，使用快照里的尺寸/缩放换算窗口相对坐标及精确 `target_element_ref`；不能用历史坐标猜目标。
- `foreground_takeover_required` 说明后台请求在投递前被拒绝；按返回指引对同一批次申请显式接管。审批与投递前激活由原生执行链管理。
- 在 shell 中事先 `set frontmost`，或在动作结束后看到 Terminal 在前台，都不能证明投递瞬间的焦点。接管可能正常恢复原前台应用。
- `action_acknowledged` / `effect_verification=unverified` 不能证明操作成功或无效；核验目标界面变化。截图失败时不重放点击。
- 缺少 AXPress、单次点击无变化，不足以断言所有微信鼠标事件都被忽略。兼容性结论需要具体版本、动作、执行路径和效果证据。
- 不把 shell、剪贴板或全局 HID 输入当作错误/审批的自动兜底。System Events 快捷键依赖前台，`cuclick` 会移动真实鼠标；两者都不是“无感、不抢焦点”的路线。

## 发送文件、消息与删除

先确认用户要求的收件人、文件或消息及当前界面，再按正常授权执行。诊断截图/点击不授权发送测试消息或文件。

- 收件人、文件名、文件路径均须准确；若流程使用剪贴板，核对确实是目标文件而非目录。
- 输入焦点需要当前可验证证据；不要机械双击旧输入框坐标或无条件发送 Return。
- 发送后核验正确会话中出现对应消息/文件；删除后核验目标消息状态。
- 菜单、确认框、输入结果不明时停止。菜单消失或截图缺失不授权再次删除、发送或重放上一批动作。
- 登录凭据、验证码、权限等敏感界面交给用户。历史成功步骤不覆盖当前工具或审批边界。

## 坐标和诊断数据

- 坐标比例使用快照返回的 `logical_size`、`pixel_size`、`backing_scale`；不硬编码 Retina 2x 或窗口 1084×684。
- 物理像素先除以实际缩放得到窗口相对逻辑坐标；不要把窗口原点再次加到 `computer_act` 的窗口相对坐标。
- 旧截图、旧 ref、旧欢迎页尺寸不是当前目标证据。
- AppleScript 列表转字符串可能拼接数字：`{30,56}` 变成 `3056`。诊断时显式输出每个分量或结构化数据，不能据此认定 x=3056。

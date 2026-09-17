# macOS 电脑操作（Computer Use）

[首页](../../README.zh-CN.md) · [文档导航](README.md) · [English](../macos-computer-use.md)

[构建与就绪检查](#build-and-readiness) · [模型工具与操作流程](#model-facing-operation) · [审批与受保护界面](#approvals-and-protected-ui) · [本次请求的数据与清理](#request-local-data) · [Appshot 的来源绑定](#appshot-source-binding) · [环境变量](#environment) · [真实机器验收](#real-acceptance) · [批量设置表单选择状态：第一阶段](#checked-form-batches-phase-one) · [定位子树观察](#targeted-subtree-observations) · [输入派发契约](#input-delivery-contracts)

Computer Use 是 macOS 14 及以上的本地集成，一次控制一个选定应用窗口。应遵守用户指定的操作通道：原生 CU 可通过 Accessibility（AX）操作浏览器表单；用户没有指定原生 CU 时，也可使用[浏览器专用集成](browser-interaction.md)。

<a id="build-and-readiness"></a>

## 构建与就绪检查

在仓库根目录构建并签名正式辅助程序：

```bash
bash scripts/build_macos_computer_helper.sh
```

默认程序位于 `.astra/bin/AstraMacComputerHelper.app/Contents/MacOS/AstraMacComputerHelper`。程序及所在目录必须归当前用户所有，不能以符号链接替代。

候选构建使用 `--output-root /absolute/private/directory`，再将 `ASTRA_COMPUTER_HELPER_PATH` 指向生成的可执行文件。路径应为规范路径，例如 macOS 的 `/private/tmp`，而非符号链接 `/tmp`。签名辅助程序集成测试在私有 pytest 目录构建一次，并核对已部署程序未变；测试不能顺便替换正在使用的辅助程序。

签名辅助程序需要“辅助功能”和“屏幕录制”权限。几个入口的行为不同：

- `/computer status` 读取缓存，显示最近已知能力、权限、目标和 handoff 状态，不启动辅助程序、不弹窗、不打开系统设置。
- 模型工具 `computer_status` 实际查询辅助程序并刷新缓存，同样不请求权限或打开系统设置。
- `/computer setup` 是唯一会打开隐私设置页的命令，只针对报告缺失的权限。
- `/computer stop` 关闭会话，删除本次请求的捕获文件并清理审批。

会话存在时，TUI 显示 Computer Use 状态，并在可用时显示应用名称，区分自动操作和已交给用户接管。

<a id="model-facing-operation"></a>

## 模型工具与操作流程

| 工具 | 用途 |
| --- | --- |
| `computer_status` | 查询就绪和会话状态。 |
| `computer_apps` | 列出可用 GUI 应用及窗口，提供不透明引用。 |
| `computer_get_app_state` | 绑定最新目录中的精确应用/窗口，获取首个已验证观察，不激活应用。 |
| `computer_focus` | 为高级操作显式锁定应用/窗口。 |
| `computer_snapshot` | 只刷新绑定目标，返回新图像和有界 AX 树。 |
| `computer_act` | 对最新快照执行一批受保护的动作。 |
| `computer_handoff` | 暂停自动输入，让用户完成受保护步骤。 |
| `computer_resume` | 接管后重新验证目标并返回新观察。 |
| `computer_close` | 结束会话，清理捕获、引用和授权。 |

<a id="transactional-app-state-read"></a>

### 一次一致的应用状态读取

普通读取顺序为 `computer_apps` → `computer_get_app_state`，使用最新目录中的精确 app/window ref。这条读取路径不聚焦应用、不移动真实鼠标。`computer_focus` 是显式高级操作，不是观察前置步骤；显式 focus 后用 `computer_snapshot` 刷新已绑定目标。

只选择 `bindable=true` 的窗口。`ax_window_unmatched` 表示窗口仍在，但没有唯一匹配的 AX 身份；它不表示引用过期或系统禁止交互。窗口状态未变化时停止反复列目录和绑定；面板状态变化后再刷新目录获取新引用，不复用或隐式改绑旧引用，也不改用被面板遮挡的父窗口。每个新 ref 只绑定它对应的当前目标。

部分 `AXSheet` 文件面板在 AXTitle 缺失时，用 AXDescription 暴露名称。目录、绑定和前台焦点校验共用这个有界补充规则；其他窗口、读取失败和歧义匹配不会因此放行。操作恢复结果中的 `unmatched_window_observed` 只表示可能存在遮挡目标的未匹配窗口，不授予输入权限，也不能据此重复之前的动作。

“前往文件夹”等嵌套面板，需要从当前焦点所在窗口逐层证明面板的进程归属和包含关系。无标题面板还必须唯一对应到同进程、同位置、同身份的系统窗口。读取面板时优先处理操作按钮和经归属核验的焦点分支，再遍历其他目录分支，避免“打开/取消”和当前文件列被挤出预算。纯文本输入会清除前一个快捷键遗留的修饰键；仍须读回字段值及所选文件名，派发回执不能代替结果核验。

面板截图从已核验归属的根窗口画面中裁出，只返回所选面板的像素及其坐标系；父窗口身份有歧义或位置变化时拒绝这次观察。这样可避免系统把整个父窗口缩进面板尺寸，导致截图与点击坐标错位。真实 Edge 本地验收覆盖元素引用和截图坐标两种文件选择路径。

PNG、普通 AX 树和可选 Smart 详情作为同一次事务验证。目标或动作失效会清理快照、元素、计划和审批权限。Smart 的 `text_detail=on` 审批仅一次，绑定精确目标、范围、模式和焦点代际。错误时不猜当前 ref、不保留旧文件充当新观察、不把局部观察当成功。遇到 `unsafe_artifact`，先 `computer_close`，再开启新会话并重新运行 apps/get_app_state，不能复用旧引用或恢复结果。

普通 AX 投影按父子元素边计深度，默认 19 条边，覆盖原生树的 20 层；`children` 列表和属性不额外占元素层。网页分支可再深入最多 48 层，总计不超过 64 层；`form_controls` 单独暴露深层控件。类型化 AX 树上限为 2,000 元素、64,000 个值，工具栏属性不消耗元素名额。通用元数据仍限 2,000 个值、12 条边，字符串 512 字符、映射 128 字段、最终 JSON 512 KiB。

定位子树从新根起限 24 条边，按角色筛选的树可到 64 条边，同时保留元素和字节上限。安全角色/子角色的值独立于深度进行脱敏。若原生或详情数据存在而普通树缺失，先检查投影截断，不直接判断应用尚未就绪。确定性测试只证明工具契约；真实机器仍需独立记录当前辅助程序、应用、AX、PNG、鼠标和清理证据。

快照默认 `target_window`。`display` 需要一次性精确审批，仅通过 ScreenCaptureKit 捕获完整包含目标窗口的单个受限显示器，随后失效；下一次默认仍为窗口截图。显示器不明确、跨多个显示器或尺寸超限时拒绝，不支持任意区域或持续全局捕获。显示器截图不授权输入，AX 仍只来自选定应用/窗口。

捕获前后核对 PID、窗口 ID、边界、前台应用和聚焦 AX 身份。坐标相对于目标窗口，过期或超出 `capture_bounds` 时拒绝。同应用模态 sheet 仅在原生窗口与 AX 几何严格包含于原目标时接受。快照记录选用了覆盖层还是选定窗口，每次动作前核对同一选择；覆盖层消失、替换或移动会让快照失效。单独枚举的同应用对话框使用普通深度，只有聚焦 AX 根不同于选定原生窗口时才使用浅层覆盖限制。

<a id="keyboard-failure-evidence"></a>

### 键盘失败与观察边界

键盘失败可附 `input_diagnostics`：`stage` 为 `before_key_down`、`key_down`、`before_key_up`、`key_up` 或 `after_key_up`，另含原始受限 `cause`、布尔 `input_may_have_started` 和 `cleanup_failed`。它们描述执行过程，不证明应用效果；`cleanup_failed=false` 不证明应用处理了释放事件。文本动作中的“可能开始”也包含之前已输入的字符。旧版无诊断结果仍有效，但更新原生与 Python 组件时应一起交付。

前序动作可能已改变应用时，整批与失败动作均报告 `unknown_outcome`，诊断保留底层原因。不能据此猜测某个按键被禁止，也不能盲目重放整批；先读取新状态。

匹配的 key-up 使用精确令牌和活动代际完成已授权的按住输入，再执行释放后的检查。若确认按键后发生普通元素焦点变化，但仍在同一精确窗口内，结果附 `observation_required: true`。还有后续动作时，批次在此停止，只返回成功前缀，不把未执行后缀记作失败或已派发；若是最后一步，批次成功但仍带该标记。

公开工具在原绑定下获取新状态，并返回从零开始的 `next_action_index`；没有剩余动作时等于批次长度。只评估已发送前缀，不自动继续后缀。窗口/根变化、安全或不确定焦点、用户活动、派发失败和超时仍会停止。多字符输入和显式文本目标保留严格焦点匹配。这是观察边界，不是应用处理事件或延迟变化已稳定的证明；旧运行时不理解这类回执，应同步更新组件。

后台观察会区分位于目标后面的普通同级窗口和覆盖层。独立映射的 `AXWindow` / `AXStandardWindow` 需有明确视觉顺序；`AXWindow` / `AXDialog` 还必须明确非模态且处于目标后方。此例外只用于观察，不改变焦点或输入权限。

同尺寸窗口标题不足以逐一映射时，可用完整 AX、ScreenCaptureKit、CG 清单的数量/几何一致性证明不遮挡：原生 ID 和 AX 对象均唯一，所有 AX 成员为普通非模态窗口，已知映射互不重叠，余下所有候选都在唯一绑定的目标后面。这不会给未解析的同级窗口分配 ID 或输入权限，每次验证重新建立该证据。清单不完整、顺序未知、组内含未解析对话框或可能遮挡时仍拒绝；已映射但模态为 true/未知或前后关系不明的对话框也拒绝。诊断使用固定 `observation_validation` 阶段名。

前台覆盖检测同样使用原生顺序，已证明位于目标后面的包含窗口不会改变焦点根选择或缩短观察深度；不确定或前台覆盖层继续原有检查。

按键别名 `ArrowLeft`、`ArrowRight`、`ArrowUp`、`ArrowDown`、`Esc`、`Enter` 分别对应 `left`、`right`、`up`、`down`、`escape`、`return`。修饰键单独传入，别名不改变兼容或审批检查，不支持的键名在派发前拒绝。

<a id="smart-snapshot-text-detail"></a>

### Smart Snapshot 文本详情

`computer_snapshot` 接受 `text_detail=off|on`，默认 `off`，保持原请求/响应形式且不生成 `.ax.json`。`on` 增加经过验证的文件形式 AX 详情、受限元数据和精简内联 `text_summary`，不会把整棵详情树内联。`text_detail_path`、`text_detail_metadata`、`text_summary` 与图片和普通 AX 观察一样只供本次请求使用；原有有界 `ax_tree` 独立保留，不被扩大或替换。

详情只包含应用通过 Accessibility children 报告的子树，因此 `coverage=reported_ax_subtree`。可能含应用报告的屏幕外节点，但不保证完整文档、虚拟化/延迟创建节点或滚动后才出现的内容。`truncated=false` 只代表没有触及 Astra 自己的上限。捕获不会为发现更多文字而滚动、改焦点或修改目标。

| 限制 | 上限 |
| --- | --- |
| 遍历 | 深度 20、4,000 节点、5 秒 |
| 文本 | 单个结构字符串 4 KiB、单个值 256 KiB、累计文本 4 MiB |
| AX JSON | 最终 8 MiB |
| 内联摘要 | 200 条、32 KiB |
| 单会话保留 | 8 个详情文件、累计 64 MiB |

受限部分树标记 `truncated=true` 并列出有序的精确原因。权限、目标身份、脱敏、序列化、发布或验证失败时，整个 Smart Snapshot 失败。

角色/子角色安全、缺失、不可读、截断或不确定时，原生层在序列化前去掉值、选中文字、范围和富文本；字面 `AXUnknown` 同样视为不确定。角色不完整时输出 `AXUnknown`，子角色不完整时省略，两种情况均标记 `redacted=true`，不调用敏感访问器。每次 AX 读取都对精确元素设置剩余时间上限，再恢复继承的超时；无法设置或恢复就失败。Python 严格验证 schema 并复查脱敏，是第二道检查。详情节点不提供可执行 `element_ref`。

每次 `text_detail=on` 都需绑定当前会话、精确应用/窗口、范围、详情模式和焦点代际的一次性审批。`scope=display,text_detail=on` 使用一次组合审批，在等待捕获前同步消费。目标、会话或焦点失效会清理授权；该观察审批不授权鼠标/键盘，也不改变兼容注册表。

普通 AX 观察可含目标应用自身菜单栏，但应用元素和菜单栏都必须报告锁定 PID，不暴露其他应用或系统 UI。自动文本插入只使用完整、非安全聚焦元素的可设置 selected-text 属性；不支持时拒绝，不转合成按键、不替换整个 value、不读取安全值、不用剪贴板。

每个动作消费一份快照。输入可能已开始后，不能自动重试或重放；未知结果、目标变化、handoff 或刷新失败后需要新快照。

`verified` 只覆盖明确类型：AXIncrement/AXDecrement 数值方向、下述滚动条回退后的所属滚动条方向变化，或在保留的精确 AX 元素上观察到 checkbox/radio/disclosure 的 AXPress 状态变化。它不证明更大任务完成。`noop` 表示限时观察内可比较状态未变，应重新观察再选择不同的显式动作；`unknown_outcome` 禁止重放。Return/默认按钮恢复也必须是新观察后的新显式动作。

无坐标垂直 `scroll` 首选 AXIncrement/AXDecrement。若目标子树中恰有一个方向匹配、支持 AXPress 的 `AXIncrementPage`/`AXDecrementPage`，只按一次；没有有效 Page 候选时才考虑一个匹配 Arrow 按钮。Finder 在 macOS 26.4 的这些子按钮可能不报告 `AXEnabled`，缺失按未知接受，明确 false 则拒绝。动作前重新解析并核对滚动条及按钮身份，歧义拒绝，delta 大小不会导致重复点击，未知 AXPress 不转 CGEvent 重放。

该路径针对 Finder 风格滚动条验证，不解决 Safari/WebKit 滚动。PID 定向滚轮 CGEvent 仍需独立兼容许可，不能自动升级全局 HID。`scrollbar_not_found` 表示没有滚动条，`scrollbar_control_not_found` 表示没有可用方向按钮，`scrollbar_identity_not_unique` 表示多候选或前后身份不唯一；这些均为拒绝继续的未知结果，不证明页面移动。

默认是后台观察和 AX 原生动作。真实鼠标动作需要先规划一个有界前台片段，再取得绑定精确快照和动作列表的审批，激活并验证精确 PID/AX 窗口，执行至多该片段后恢复先前前台应用。派发前检测到用户鼠标/键盘活动则取消，执行中检测到活动则在下个动作边界停止并释放按住输入。计划中的覆盖光标是虚拟的；已批准 PID 点击、双击、滚动、拖动须将真实鼠标恢复到记录的起点。

输入保护还独立证明聚焦 AX 窗口对应精确原生窗口。同 PID 同边界的堆叠窗口需要完整、非空 AX/原生标题唯一匹配，标题缺失或冲突仍拒绝。每个输入边界都核对原生 ID、几何和 AX 身份；只有前台 PID 不够，切到同位置同级窗口会停止剩余输入。

前台鼠标兼容按精确 `bundle_id + CFBundleShortVersionString + action` 默认拒绝。启用需要当前实机记录证明鼠标恢复、前/中/后前台 PID、准确接收窗口、新鲜目标效果、无目标外效果和按住输入清理。一款应用/版本/动作的证据不扩展到其他项，不接受版本范围或通配符。证据缺失时可继续支持后台 AX，但鼠标请求按情形返回 `foreground_takeover_required`、`background_action_unsupported`、`target_not_frontmost`、`stale_snapshot` 或 `unknown_outcome`，不升级全局 HID。

<a id="approvals-and-protected-ui"></a>

## 审批与受保护界面

普通动作可按当前会话/应用/窗口批准。Save、Export、Return/Enter、Finder 复制/重命名/废纸篓、Terminal 输入等高影响动作使用绑定精确批次和快照的一次性审批。此 Computer Use 审批在 locked、safe、permissive、YOLO 下均有效，通用工具策略审批不能替代、抑制或扩大其应用、窗口、路径、效果范围。拒绝时不会调用辅助程序执行动作。可信本地 UI 提供确认路径时，审批显示规范化的精确文件路径，不扩展到目录或后续批次。

安全或不完整 AX 身份拒绝继续；安全字段值不进入快照、事件、审批或日志。认证、付款及其他受保护目标交给 `computer_handoff`。接管期间应用枚举、焦点操作、快照和动作均暂停，直到 `computer_resume` 重新验证。

安全字段、IME/组合候选、系统/管理员授权、权限弹窗，以及无法证明精确聚焦窗口身份的目标，不支持自动前台输入，应交给用户，不点击绕过或从其他应用推断成功。

UI 事件只持久化有限 handoff 状态：是否活跃、是否接管、权限状态和可选应用名。不保存窗口标题、AX 树、辅助诊断、截图路径或图片字节。普通输入参数的历史策略见下文。

<a id="request-local-data"></a>

## 本次请求的数据与清理

截图和 AX 观察是本次请求的工具结果。文件权限 `0600`，会话目录权限 `0700`，发布前核验。活跃会话持有目录和租约描述符，并持有共享 `flock`；清理前取得独占锁。启动清理由当前用户所有、权限 `0600` 的根锁串行化，保留租约仍活跃的其他会话。

即使进程被 `SIGKILL`，只有目录/文件所有者、类型、权限、链接数和描述符身份全部核验后才移除死租约目录。无租约旧目录需超过 24 小时，且完整清单只含精确 `snapshot-<32-lowercase-hex>.png` 旧文件才可自动清理。近期目录或含详情、临时、隔离、未知文件的目录保留并阻止自动清理。符号链接、非普通文件、所有者不符或身份竞争均拒绝。

这些原生 CU 捕获不作为远程 API 输入、持久对话历史或可复用产物。运行时不用剪贴板粘贴；审批按字符数概述输入。普通键入文本会准确保存在工具调用历史、事件、任务和本地诊断中，供后续请求/恢复会话使用。可识别的凭据赋值（含 JSON 字段）、认证头、URL 凭据和私钥块被替换为可重复处理的 `[redacted N chars]`。检测基于语法，不能识别任意未标注密码；执行路径仍接收原始且验证过的文本。完整脱敏标记（含旧形式）在规划/输入前拒绝。新 `raw-tool-calls.jsonl` 条目使用同一参数保存策略并记录策略，历史档案不重写。

启用详情时，PNG 与 `.ax.json` 使用同一个 32 位小写十六进制 token 和同一会话目录权限。两个临时文件均完成并同步后才重命名，目录同步成功后才发布；Python 核对所有者、类型、权限、链接数、inode、大小、SHA-256、严格 JSON schema 和快照绑定。

清理权由目录和 `flock` 租约持有。这里信任同 UID 的本地进程并假定 Astra 组件遵守租约，不防御恶意同 UID 进程绕过锁。正常失效会在首次 unlink 前预检完整 PNG/JSON 对；缺失、替换、链接/权限/大小改变等会导致零删除并标记会话不可复用。预检通过后仍逐个重新验证、删除；后续失败可能留下一个文件，需阻止复用直到最终清理或显式恢复。临时/隔离/未知条目和身份不明残留保留，需独立审核后手动恢复。第二次重命名、目录同步、回滚或隔离失败同样阻止发布，直到持有目录的清理流程证明安全。

读取应用树前，只有 `AXEnhancedUserInterface` 为 false 且可设置时才协商增强辅助功能，使 Chromium 等提供方暴露更多原生树内容。每个保留的应用实例只请求一次，setter 报错也回读；确认启用后在原观察时限内等待 2.25 秒稳定。已启用或不支持时不重复写。关闭辅助程序不关闭应用共享模式，不改焦点、不发按键、不切换全局 VoiceOver。此准备不证明所有页面元素都已出现。

<a id="appshot-source-binding"></a>

## Appshot 的来源绑定

Appshot 用进程身份和精确几何绑定前台应用的聚焦窗口。多个原生窗口同边界时，AX/原生标题必须非空，并通过精确或应用后缀匹配唯一候选；标题不足或歧义则拒绝。原生 ID、进程、几何和 AX 根都会复核，用于消歧的标题在读取期间也不能改变，不选择首个同尺寸窗口。

<a id="environment"></a>

## 环境变量

| 变量 | 用途 |
| --- | --- |
| `ASTRA_COMPUTER_HELPER_PATH` | 开发/测试用签名辅助程序绝对路径；拒绝不安全所有权、符号链接、相对路径和不可执行目标。 |
| `ASTRA_COMPUTER_CACHE_ROOT` | 私有会话目录的本地根路径，运行时创建并验证私有子目录。 |
| `ASTRA_MACOS_COMPUTER_E2E` | 只有精确为 `1` 才运行真实桌面测试；未设置或 `0` 明确跳过。 |
| `ASTRA_MACOS_APPSHOT_E2E` | 只有精确为 `1` 才运行原生 Edge Appshot 测试，用户需事先把目标 Edge 窗口放到前台。 |
| `ASTRA_COMPUTER_DEBUG_RETAIN` | 尚未实现，设置任何值均无效；捕获始终清理，直到可在保留模式下维持请求数据契约。 |

<a id="real-acceptance"></a>

## 真实机器验收

测试只在新建、权限 `0700` 的 pytest 目录创建样本，不打开或修改已有用户文档。WPS 另存为/PDF 导出、Finder 复制/重命名/废纸篓拒绝和 Terminal `printf` 都限制在该目录。

原生单元/测试框架检查直接运行 Swift Testing，零测试发现视为失败：

```bash
bash scripts/test_macos_computer_helper.sh
```

显式启用真实桌面测试：

```bash
bash scripts/build_macos_computer_helper.sh
ASTRA_MACOS_COMPUTER_E2E=1 .venv/bin/python -m pytest tests/macos_computer_e2e -v
```

未设置开关时会说明跳过原因；启用后缺应用或权限是带诊断的失败，不能当通过。AppKit 控件/画布、WPS、Electron、Chromium 分别报告，缺失或失败不能折算成另一项通过。

仓库的确定性测试覆盖协议、审批、覆盖层、缓存和显示器捕获。真实 WPS 通过必须是当前签名辅助程序在本轮通过精确审批生成新的测试 DOCX 和可搜索 PDF。跳过、旧产物或安全拒绝停止均不算验收。[脱敏兼容摘要（英文）](../macos-computer-compatibility-evidence.md) 保留历史结果的边界，确定性和实机结果分别报告。原始测量记录只保存在被 Git 忽略的本地证据目录。

[Smart Snapshot 只读 WPS 测量说明（英文）](../macos-smart-snapshot-runbook.md) 衡量当前 AX 子树对可见/屏幕外文本是否有用，不滚动、不改变焦点、不输入。它不是实现验证，也不能把 `coverage=reported_ax_subtree` 提升为完整性保证。

当前兼容注册表按精确 bundle/版本/后端/动作声明能力，应读取文件并与签名辅助程序资源哈希比对。旧的空注册表报告只是历史。配置了某项不证明此构建已完成新实机流程；按[证据与路由指南（英文）](../computer-use-evidence.md) 区分测试框架结果、配置路径和真实应用效果。一次动作通过不能推断版本或启用整个应用。

<a id="unavailable-target-window-pixels"></a>

### 目标窗口没有可用像素

`window_content_unavailable` 表示精确窗口图像完全透明，事务不发布快照或动作权限。它不同于一般 `snapshot_failed`，也不证明遮挡、失焦或输入失败；部分应用即使窗口可见也会隐藏捕获内容。

不要切换桌面区域截图、反复激活/移动目标或重放输入。让用户检查应用捕获/隐私状态，相关状态改变后刷新 `computer_apps` 并重新绑定当前窗口。欢迎页转主窗口可能替换 OS 窗口却保留标题，应使用返回的下一次观察引用或重新从目录选定；shell `--first`、历史尺寸和旧坐标均不能绑定新窗口。接管清理后看到的焦点不证明输入曾正确送达。

<a id="checked-form-batches-phase-one"></a>

## 批量设置表单选择状态：第一阶段

使用 `computer_apps` → `computer_get_app_state` → 一次 `computer_act`，传入相互独立的 `{type:"click", element_index:currentIndex, checked:true}` 目标。默认 `auto`：新辅助程序在派发前内部协商并规划前台接管；旧程序保留后台行为，也支持显式模式。后台计划不能传给 takeover_begin。

首次打开原生文件选择、保存或模态面板时，在 `computer_act` 传 `opens_dialog:true`，也可显式指定 `foreground_takeover`。Chromium 可能把文件按钮暴露成普通 AXButton；后台 AXPress 成功不代表面板获得焦点或可绑定。对话框标记让 auto 在输入前走现有接管审批，不替代窗口身份，也不允许重放。显式 background 与此标记组合会被拒绝。普通网页上传优先用 `browser_upload`。

协议仍为 v4，`subtree_v1`、`checked_click_v1`、`auto_takeover_v1` 位于可扩展 AX 树中。旧客户端可读取普通快照，新客户端不会给旧辅助程序发送不支持的可选请求。原生 `snapshot_subtree` 将精确旧快照和 AX 对象绑定同一窗口后重读。网页表单截断时，首次观察和批次验证各可触发一次定位子树读取，不无界展开或按标题恢复。

每个 checked 目标先读状态，已满足则跳过，否则只派发一次，最多等 400 ms 观察 AX 状态。最终 `choice_verification` 用进程内 AX 身份、角色、标签/标题关联新观察，可处理布局变化和文本上下文截断。关联 token 不授权动作。未知状态不等于 false，更不允许重放。数值 0.0/1.0 和布尔值均识别，长选择标签仍受现有字节上限限制。

坐标默认窗口内逻辑单位；`coordinate_space="image_pixels"` 使用最新目标窗口截图的精确 `published_image_size`。显示器截图和旧几何不能授权这类动作。可持久化回执只含固定派发/验证枚举和数量，不含页面文字、路径、ref、截图或输入。

macOS `WindowSharingSessionButton` 徽标通过完整非模态 AX 形状和标题栏位置识别，只从对话框分类中排除；普通对话框处理和原始鼠标命中检测保留。

<a id="targeted-subtree-observations"></a>

## 定位子树观察

大 AX 树隐藏所需控件时，用新快照的 `subtree_ref` 展开一个已观察子树。`role_filter` 缩小返回角色，不使未见目标可执行。新快照产生新 ref；角色无匹配、遍历限制或预算耗尽都需明确报告。展开是只读操作，不证明已观察应用全部内容。

<a id="input-delivery-contracts"></a>

## 输入派发契约

兼容项可用 `requires_active` 声明应用要求激活后才能接收合成鼠标输入。规划器在输入前拒绝后台鼠标路径并请求现有前台接管；激活被拒绝不能回退后台。该字段缺失或 false 也不会授权本来不支持的动作。

键盘焦点必须证明精确目标窗口和合格的非安全输入元素。前台接管应在激活后、紧邻派发前获取并验证焦点，不能把未来活跃焦点作为激活前提。前台 PID 不足以证明目标；变化、歧义或受保护目标均停止派发。

纯文本和物理组合键使用不同计划。Unicode 文本避免把普通字符当快捷键，物理按键保留真实语义。输入法组合需独立证据，不能因为某应用文本样本通过就宣称通用支持。部分派发或结果未知的动作不能换输入通道重放，必须先观察新状态再决定新的显式动作。

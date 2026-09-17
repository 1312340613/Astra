# 浏览器文件选择能力

日期：2026-09-17。状态：已实现；自动测试、本地原生面板和 Canvas 后台实机验收通过。

## 目标与范围

用户希望 Astra 在已授权、已登录的 Edge/Chrome 网页中选择本地文件，无需打开 macOS 文件面板、切换桌面焦点或让用户手动点击。首个真实验收页面是 Canvas 作业上传页，只验证文件选择，不点击 Submit。

新增 `browser_upload` 专用工具。支持单个或多个普通文件、替换已选文件、通过空数组清空选择，并返回网页实际选中的文件元数据。最终表单提交仍是单独的 `browser_click` 操作，按用户指令执行。网站可以在文件选择事件后自动开始上传，因此该工具属于向指定网站提供文件的写操作，不能宣称所有站点在 Submit 前都不会收到文件。

首版不增加下载工具、目录递归上传、拖放专用上传组件、跨域 iframe 穿透、原生应用上传或新的常驻服务。用户追加批准一并修复 CU 焦点问题；两条通道分别验收，不放松原有目标校验。

## 方案比较

1. **现有 Browser Control 扩展加 Native Messaging 文件通道，推荐。** 沿用已授权标签、登录状态、来源校验和 Stop/handoff 语义。Python 读取已批准的文件，扩展把文件内容交给目标上传控件；无需调试端口、桌面激活或新增浏览器权限。
2. **CDP/Playwright 文件接口。** `setInputFiles` 是成熟实现，但当前用户的日常 Edge 通过扩展连接，直接采用此方案还需要已配置的调试实例及对应登录状态。后续可作为同一工具的独立后端；首版遇到 CDP 明确报告不支持，不偷偷换会话。
3. **网站专用上传 API。** 能完全避开 UI，但需要网站专用认证及请求实现，无法覆盖任意网页。当前不选。

## 工具契约

`browser_upload(tab_id, selector, paths, frame_ref?)`

- `tab_id` 必须是已绑定的 Astra 逻辑标签。
- `selector` 使用最新快照中指向 `input[type=file]` 的 `ref:`；首版要求引用，不接受猜测的 CSS。`frame_ref` 如提供必须与引用一致。
- `paths` 是明确的本地文件路径数组；相对路径按工作区解析。空数组清空当前文件选择。多文件要求控件声明 `multiple`。
- 首版最多 10 个文件，单文件 32 MiB，总计 64 MiB。超限在任何页面文件选择发生前返回明确错误。
- 不读取目录、设备、管道、socket 等非普通文件。解析路径并保留真实路径和文件身份用于授权；拒绝读取过程中发生替换或变更的文件。权限遵循现有文件读取和浏览器写操作机制，不因 YOLO 或已授权标签扩大可传文件范围。
- 通用 `browser_fill`/`browser_type` 继续拒绝文件输入框，避免把文本路径误当已上传文件。

## 发现与返回值

浏览器快照增加专门的文件控件元数据：当前引用、frame、label、`accept`、`multiple`、`webkitdirectory`、disabled、可见性，以及已选文件的名称、大小、MIME 类型。文件控件不作为普通可编辑文本；不返回 `value` 中的伪路径，也不在快照中暴露本地绝对路径或文件内容。隐藏的原生 file input 可被发现并获得有效引用，以覆盖“可见按钮代理隐藏 input”的常见网页结构。

结果包含 `dispatch_state`、`verified`、`files` 和目标身份。`verified=true` 仅表示重新读取的 `input.files` 与请求的名称、大小、类型及顺序一致；不表示服务器已收到内容或表单已提交。控件在事件中被替换、导航或连接中断时，返回实际能够证明的状态，不能把回执成功当作网页结果成功。

## 传输与页面操作

沿用现有 Python → native host → 扩展 service worker → 隔离世界页面代码链路。Native Messaging 单条消息受 1 MiB 限制，因此将每块原始文件字节限制在 256 KiB，编码后仍在现有帧限制内，不通过模型消息或日志传递二进制内容。

内部采用 prepare/chunk/commit/abort 四阶段，模型只调用一次 `browser_upload`：

1. 校验绑定标签、当前 origin、扩展与页面 upload 能力、当前引用及文件控件类型；取得有界的临时传输标识。旧扩展缺少能力时，在读取或传送文件内容前失败。
2. Python 完成明确文件列表的授权及有界读取；任何文件读取失败或中途变更均在 commit 前终止。传输会话绑定连接代次、授权代次、origin、frame 文档及确切元素对象。
3. 按文件索引、偏移和声明长度逐块传输。拒绝越界、重复、乱序及数量不符的块。每个标签最多一个待提交传输；空闲 60 秒或总时长 5 分钟自动清理。
4. commit 再次验证整个目标和文件清单，构造目标 Document 对应的 `File`/`DataTransfer`，一次替换 `input.files` 并触发必要的 input/change 事件。首版不调用 focus、click、showPicker、requestSubmit 或 submit。
5. 有界读回 `input.files` 并返回验证结果。连接撤销、Stop、handoff、frame 导航或目标替换会废弃待提交缓冲区及旧传输授权。

`accept` 是网页提示而非服务器接受承诺。明确不能匹配的文件在 commit 前报错；空或无法判定的 MIME 不声称已通过网站验证。`webkitdirectory` 首版明确不支持。

## 不确定结果与恢复

commit 之前失败：页面文件选择未派发，清理传输缓冲区，不影响已选文件。commit 派发之后失去回执：返回 `unknown_outcome`，不得自动重发 commit 或切换到 CU；先用文件元数据读回核对。清空也属于一次明确写操作。文件字节和 base64 不进入模型可见回执、异常文本或持久工具日志。

能力协商分别检查 controller 和 page，增加显式 upload 标志并更新扩展版本；旧版本返回 `unsupported_operation/not_dispatched`。继续使用当前的标签授权、origin 绑定和写操作队列。不会为文件功能增加任意 JavaScript 执行或读取任意页面隐藏数据的接口。

## 改动挂载点

- `agent/runtime/tools/browser.py`：专用工具注册、参数、权限及简洁回执。
- 新增专门的 Python 文件准备模块：路径、普通文件身份、数量、大小和有界分块；避免继续膨胀浏览器工具文件。
- `agent/runtime/extension_browser_backend.py`、`browser_backend_router.py`、`browser_control_transport.py`：能力判断、上传事务及已绑定后端路由。
- `browser-control-extension/control.mjs`、`worker.mjs`、`page.js` 及专用文件传输模块：事务授权、文件控件发现、字节重组、commit 和读回。
- 浏览器中英文使用文档及专项 skill：网页文件选择优先采用专用工具，明确最终提交为独立行为。

不增加外部依赖；保持开发库现有未提交改动，使用独立 worktree 完成设计和实现。

## 验收

1. Python：普通/缺失/目录/特殊文件、文件替换、超限、多文件、空列表、能力缺失、绑定错位、传输失败、commit 不确定结果均有针对性测试。
2. 扩展：页面 DOM 元素和 frame 的实际身份校验，文件名/大小/内容正确，超过 1 MiB 的文件经分块完整还原，目标替换、导航、Stop、超时和重复 commit 不会误发。
3. 本地真实浏览器页面：包含普通及隐藏文件控件、单/多文件、同源 iframe、替换/清空、change 自动处理；比较接收字节摘要。开始及结束时另一应用保持前台，网页不需要文档焦点，并确认全过程没有打开原生文件面板。
4. Canvas 实机：使用明确的 notebook 文件，通过 Astra 新工具选择并读回文件名，不点击 Submit；保留新工具回执与页面结果。若其他 Astra 实例占有浏览器通道，按已有交接机制处理，不删锁或抢占。
5. 运行相关 Python/扩展测试、静态检查及受影响构建；记录实际验收范围和未测后端。安装更新需要重新加载扩展和重启对应 Astra 实例时明确说明。

## 依据

- Chrome Native Messaging 单消息限制：https://developer.chrome.com/docs/extensions/develop/concepts/native-messaging#native-messaging-protocol
- 文件控件 API：https://developer.mozilla.org/en-US/docs/Web/API/HTMLInputElement/files
- FileList 构造载体：https://developer.mozilla.org/en-US/docs/Web/API/DataTransfer/files
- 对照的成熟接口：https://playwright.dev/docs/input#upload-files

## 设计检查

- 已核对现有通道、近期提交、授权与帧长度限制。
- 文件选择、网站自动上传及最终表单提交的语义已区分。
- CU 焦点修复已追加授权；下载和其他上传后端保持独立范围。
- 无待填占位项；设计已获批准；实现和验收结果在交付时记录。

### 清空回执补充

实际会话里选择与替换均为 verified，清空后的独立快照已显示 files=[]，但旧控件读回失效使回执成为 unknown_outcome。清空时允许一次只读结果核验：原授权代次、文档、frame、DOM root 和 form 均未变化；原控件已断开；操作前唯一的非空 id 在原 root 中仍只对应一个 file input，原生 name、accept、multiple 和目录属性一致且控件可用。此时只读取替代控件的 files，空列表才算清空验证成功，并标记 verification_source=replacement_input。不会对新控件发送任何事件，也不复用旧 ref。

这项补充只适用于 paths=[]。文件选择/替换仍验证原元素；授权撤销、导航、重复 id、跨表单、缺失控件等均不满足回退条件。测试覆盖同步及异步重建、身份不一致、清空结果不符，并验证事件只派发一次且不提交表单。

## 已完成验证

- 最终 Python 浏览器/CU/技能相关回归 1,226 项通过、20 项跳过，包含配置工作目录与启动目录不同的相对路径上传用例。Ruff 和修改模块的 Pyright 通过。
- 扩展控制器、页面与兼容性回归 96 项通过。表单页新增能力后仍满足 10K 观察预算，分页提示也计入预算。
- 真实隔离 Edge 页面验证 1,300,017 字节文件的 SHA-256 一致；隐藏、多文件、零字节、清空、同源 iframe、精确事件次数及不提交均通过。文件/控件变化、导航、撤销、重复/乱序块、空闲和总时限均有拒绝验证。
- 原生 Swift 串行套件 747 项通过。真实 Edge 文件面板元素点击、图片坐标点击、从 Finder 前台主动接管三种场景全部通过。补测普通 AXButton 的 `auto + opens_dialog` 路径，从 Finder 前台自行接管、绑定面板、输入路径和选中文件均通过，无人工焦点点击。
- 用户释放原 Astra 的浏览器端点并重载扩展后，Canvas 原有授权标签经 Browser Control 0.4.0 → Native Messaging → 注册的 browser_upload 工具完成真实文件选择。工具返回 verified=true，读回 123,817 字节及确切文件名；操作前后的独立前台查询均为 Finder，未打开原生文件面板。页面保留原来的提交记录，没有点击 Submit。验收连接已正常释放。
- Canvas 验收补齐了控件重排后的读回：派发前仍要求有效 ref；派发后只读核对保留的同一元素、文档和授权代次，不因旧操作 ref 失效而丢弃可证明的文件结果。除上述严格限定的清空核验外，真正替换/断开/撤销仍返回 unknown_outcome。快照失败与已完成的文件读回分开报告，异常仅暴露固定阶段名。
- 清空回执修复通过真实 Chromium 19 项测试，包含同步/异步重建、控件移位和身份不一致等情况；文件字节、单次派发和不提交仍有验证。后续 Python 浏览器上传/CU/技能回归 446 项通过。
- 当前扩展版本为 0.4.1，需要重新加载扩展并重启 Astra，才能使用清空核验及 `opens_dialog` 路由。已运行实例不自动替换通道或 helper；构建与发布状态在交付记录中列出。

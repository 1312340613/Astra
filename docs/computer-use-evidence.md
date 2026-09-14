# Computer Use：路由与验收证据

本约定把“代码允许哪条路径”和“哪次运行证明了效果”分别记录。它不修改
`config/macos_computer_compatibility.json`，也不扩大任何应用、版本或输入后端的授权。

## 能力与证据

| 维度 | 能证明什么 | 不能推导什么 |
| --- | --- | --- |
| `configured` | 指明来源的 registry 对精确 bundle/version/backend/action 的配置 | 源码 registry 与运行中的 bundle 一致，或此 build 的输入效果已验收 |
| `harness_passed` | fake、Fixture 或协议测试已通过指明的 contract | 真实 WPS、微信、Outlook 等应用的效果 |
| `live_accepted` | 记录的 build、系统、应用版本与测试专属工作流获得了完整后置条件 | 同应用其他动作、其他版本或用户当前会话 |
| `unknown` / `not_run` | 缺少该维度的证据 | 失败或成功 |

历史报告保留其运行时结论，不随当前 registry 重写。当前 registry 非空不能把旧的
`NOT_SUPPORTED` 改写为 PASS；旧报告 registry 为空也不能作为今天的 default-deny 前提。
具体配置以源文件及 bundle resource hash 为准，不在文档中维护第二份许可名单。
`computer_apps.routing_advice` 查询 Python 的 source registry，只是路由建议；必须对比 helper
bundle hash 才能证明配置同步，实际 helper plan/approval 仍有最终决定权。

`computer_status` 的 build manifest 若可用，应记录 `build_id`、`helper_git_revision`、
`helper_source_dirty`、`compatibility_registry_sha256`。这些值在打包时写入 signed bundle；
旧 helper 缺少 manifest 应报告 unknown，不用运行时工作目录的 HEAD 填空。
`helper_source_dirty=true` 时，revision 只是构建所基于的提交，不能声称二进制精确等于该提交。
未安装新 helper 时，新源码或新 manifest 测试通过不代表正在运行的 helper 已更新。

## 路由建议

1. 普通网页任务优先考虑已有 browser/DOM 工具；先确认它实际连接的浏览器、profile、tab 和登录态。
   CDP、独立测试浏览器与用户当前 Safari/Edge 窗口可能属于不同会话。建议路由不自动复制 cookies、
   打开新 profile、接管现有会话或修改 remote-debugging 设置。无法绑定用户指定会话时，明确保留当前
   Computer Use 路径，或让用户选择可访问的会话。
2. 原生界面先取 `computer_apps` → `computer_get_app_state` 的精确目标。AX-native 可达的动作
   按现有安全策略执行；“应用名字相同”不能替代 element_ref 或精确版本配置。
3. registry 明确 `requires_active=true` 的指针路径，以及明确的 foreground keyboard 路径，可以
   建议首次就选择 `foreground_takeover`，减少必然失败的 background 往返。建议本身不是 approval：
   仍需 exact plan、once-only 授权、焦点/secure-field/user-activity 检查。
4. 对未知 cell 不猜测兼容性；保留 default-deny 与原有 HID 人工工作流。此优化没有改变 HID skill、
   生产兼容 cell、输入分块默认值或未知结果禁止重放的规则。

## 可复现的测量行

每个 bundle/version/backend/action/workflow 单独一行；不能用一次点击的成功代替整个应用成功率。
下面是未执行的 JSONL 模板，`null` 表示没有测量，不是零延迟：

```json
{"schema_version":1,"run_id":"example-not-run","build_id":null,"helper_git_revision":null,"helper_source_dirty":null,"registry_sha256":null,"macos_version":null,"bundle_id":"example.fixture","app_version":null,"backend":"pid_pointer","action":"click","workflow":"test-owned-button-effect","configured":null,"harness_result":"NOT_RUN","live_result":"NOT_RUN","attempts":0,"input_started":false,"acknowledged":false,"effect_verified":false,"no_replay_preserved":true,"latency_ms":{"queue":null,"start":null,"write":null,"read":null,"observation":null,"encoding":null,"total":null},"evidence_path":null}
```

测量步骤：

- 固定相同测试专属内容、确切应用版本和 build/registry identity；冷启动、warm 与恢复路径分别统计。
- 只在用户授权的实机验收中采样。记录样本数、失败数及 p50/p95，保留 timeout/cancel/unknown 行，
  不从分母剔除失败后声称成功率提高。
- 回执与效果分别记录：`acknowledged=true` 不能代替 `effect_verified=true`。后置条件必须具体，
  如唯一测试文件内容、目标控件值或测试专属 URL 的状态；不要把原始用户文本/标题/URL写入 timing 日志。
- timing 只记录 operation/stage/count/elapsed；证据文件保存在测试专属目录，未知结果不重放。
- 用相同 workload 比较优化前后。确定性计数减少可以由 fake 证明；端到端实机延迟与 app success rate
  必须等新 signed helper 的新一轮测试，不能从源码、编译或旧产物推断。

## 运行时边界

- Python helper 请求的排队、启动、写入和读取共享 15 秒预算；失败后的进程清理有独立预算。
  原生动作批次上限 12 秒；显式 wait/drag 合计超过 10 秒的批次在输入前拒绝。长工作流应拆为
  有新观察和确切授权的批次，不能把超时后的未知输入当作未执行并重放。
- 动作后的只读观察重试共享 15 秒预算，只重试已分类的临时失败；权限、产物身份和会话错误立即结束。
  返回 `unknown_outcome` 时保留输入回执，并要求检查当前状态。
- 图片编码最多同时运行两个 worker。取消调用不会提前释放仍在编码的线程名额；编码结束后重新检查
  快照、目标代次和恢复发布的所有者。接管在取得并校验 PNG 后结束，编码期间无需继续占用前台。
- 图片仍采用原有压缩顺序和 1600px 长边缩放下限；小 PNG 字节保持原样，只对最终候选做 base64。
  `CU_IMAGE_MAX_DATA_URL_BYTES` 是已有的传输预算覆盖项，可按已验收的 provider/gateway 设置；
  当前没有可靠的 provider 能力协商，因此不会猜测模型名称并自动降低分辨率。预算无法满足时仍保留
  最小候选并返回 `fits_budget=false`，不是强行丢弃图片。
- 当前没有加入跨请求 AX 缓存或 observer 生命周期管理。缓存仅限同一次观察的固定大小几何/布尔值，
  role/subrole 和文本重新读取。未来跨请求缓存必须先覆盖失效通知、窗口/PID 复用和 secure 状态变化。

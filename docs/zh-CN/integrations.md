# 可选集成

[首页](../../README.zh-CN.md) · [文档导航](README.md) · [English](../integrations.md)

[OpenAI 兼容 API](#optional-openai-compatible-api) · [原生消息渠道](#native-messaging-channels) · [MCP](#mcp) · [网页搜索](#web-search) · [Conclave 多专家研究](#conclave-multi-expert-research) · [ComfyUI 绘图](#comfyui-image-generation) · [163 邮件只读集成](#read-only-163-mail)

按需启用服务。Shell 示例从安装目录执行，提供方凭据放在本地 `.env`。

<a id="optional-openai-compatible-api"></a>

## OpenAI 兼容 API

API 与 TUI 分开运行，需要 `server` 扩展：

```text
uv run --locked --extra server python -m agent.cli.api_server
```

默认监听 `127.0.0.1:8900`，通过 `ASTRA_API_HOST` / `ASTRA_API_PORT` 覆盖。环境或安装目录 `.env` 中的 `ASTRA_API_KEY` 为所有端点（含 `/health`、`/v1/models`）启用 `Authorization: Bearer <key>`，与上游模型 Key 分开。

绑定非回环地址必须设置此 Key，否则启动被拒绝。携带浏览器 `Origin` 的请求即使访问回环地址，也要求配置 bearer 认证。

`POST /v1/chat/completions` 支持流式和非流式响应。已消费字段格式错误时，在创建会话或执行 Agent 前返回 HTTP 400。该桥接把 Astra 本地工具开放给认证调用方，远程访问应使用可信网络或 TLS 反向代理。原生消息渠道是另一项独立集成。

<a id="native-messaging-channels"></a>

## 原生消息渠道

Ink 后端在同进程中管理可选消息适配器。消息直接进入现有 ReAct Agent，不经过 AstrBot 或实验性 API 桥接。每个私聊和群聊在 `.sessions/channel_*` 中有独立持久会话；渠道与 TUI 共享生命周期锁，避免有状态 Agent 同时使用两个上下文。

QQ 目前采用原生 OneBot v11 反向 WebSocket。NapCat 仍负责 QQ 协议并直接连接 Astra。启用前复制示例：

Windows：

```powershell
# Windows
New-Item -ItemType Directory -Force .astra | Out-Null
Copy-Item config/channels.example.json .astra/channels.json
```

macOS/Linux：

```bash
# macOS/Linux
mkdir -p .astra
cp config/channels.example.json .astra/channels.json
```

设置 `qq.enabled=true`，默认监听 `127.0.0.1:2280`，NapCat 反向 WebSocket 地址为 `ws://127.0.0.1:2280`。若 NapCat 配有 access token，将相同值放入 `ASTRA_QQ_ACCESS_TOKEN`，不写入 JSON。默认接收私聊，群消息需 @提及或配置的唤醒前缀（默认 `/`）。

2280 端口只能有一个服务占用，启用前先停用或改配 AstrBot。适配器启动失败会报告但不关闭 TUI；后端退出时关闭全部适配器。消息渠道中需要交互授权的操作会被拒绝，不等待不可见提示。

QQ 图片经公开 URL 检查与大小限制后下载，再作为原生多模态内容传给视觉模型。发本地文件需启用 `send_files_enabled`、用 `file_allow_users`（或 `allow_users`）列出可信 QQ ID，并把 `send_file_roots` 限定为可分享的输出目录。`channel_send_file` 拒绝越界路径和常见凭据文件。

渠道接口不绑定特定协议，将来可接入 Weixin iLink 而不改 Agent 路由和隔离；当前仅实现 QQ/OneBot，不接管已有 Hermes 微信登录。

<a id="mcp"></a>

## MCP

安装 `mcp` 扩展，并创建 `.astra/mcp.json`：

```json
{
  "servers": {
    "filesystem": {
      "transport": "stdio",
      "command": "python",
      "args": ["path/to/server.py"]
    }
  }
}
```

工具名为 `mcp__<server>__<tool>`。`/mcp` 查看服务，`/doctor` 排查连接错误。支持 stdio 和 streamable HTTP，可分别停用服务；`${ENV_NAME}` 从环境解析凭据，避免将密钥存入 JSON。见[完整配置示例](../../config/mcp.example.json)。

<a id="web-search"></a>

## 网页搜索

`search_web` 支持 `provider=auto|exa|searxng`。`/search` 查看默认值，`/search auto|exa|searxng` 持久切换。auto 对新闻、研究、模型发布和基准问题优先 Exa，普通问题先用 SearXNG；结果为空或失败时，同一次工具调用内相互回退。不设固定每轮搜索次数，但重复调用和 ReAct 上限仍防止无限循环。不使用 DuckDuckGo。

直接配置 `EXA_API_KEY`，或用 `EXA_ENV_FILE` 指向另一份包含该 Key 的 dotenv，以复用凭据而不复制。`/doctor` 只报告是否配置和来源，不打印密钥。

`web_extract` 有独立回退顺序。`WEB_EXTRACT_PROVIDER=auto` 依次尝试已配置的 `Tavily -> Exa -> Parallel`，再到本地/云 Firecrawl，最后 HTTP 与正文 HTML-to-Markdown 提取。逐 URL 回退，成功页面不重复获取。单次工具 `provider` 或 `WEB_EXTRACT_PROVIDER=tavily|exa|parallel|firecrawl|http` 可明确首选。超过 15,000 字符时返回 75/25 首尾窗口，完整 Markdown 保存到 `.astra/cache/web`。

浏览器提取器优先使用 `BROWSER_EXTRACT_ARGV` JSON 数组，将可执行文件和固定参数分开，例如 `["C:\\Program Files\\Browser Extract\\extract.exe","--render"]`。兼容 Shell 字符串时 `BROWSER_EXTRACT_CMD` 优先于 `WSL_EXTRACT_CMD`，旧变量在所有平台仍作为回退。

Windows 通过 `cmd.exe`，POSIX 通过 Bash 执行旧字符串。Windows 给每个子进程独立 URL 环境值，使用带引号的 `%ASTRA_BROWSER_EXTRACT_URL%` 且关闭延迟展开；POSIX 将 URL 作为独立位置参数。Windows 旧模式拒绝 URL 中原始双引号、CR、LF、NUL，应百分号编码或使用 argv。`$HOME` 等 Shell 展开仍可用，但不把 URL 文字拼进 Shell 程序。

`BROWSER_EXTRACT_STATUS_CMD` 可配置独立就绪检查；未设时只查可执行文件或包装器，不伪造 URL 或 status 参数。没有自定义设置时，仅 Windows Python（`sys.platform=win32`）走内置 WSL 提取；macOS 和所有 Linux Python（含 WSL 中 Python）走原生 Bash。macOS 能发现标准 Chrome、Edge 和 Chromium 安装位置。

把 Windows 源码拷到 macOS/Linux 后，应取消 `WSL_EXTRACT_CMD`，或改成原生有效的 argv/命令。旧变量在每个平台都会读取，残留以 `wsl` 开头的值会被错误尝试。

代理动态读取 `HTTP_PROXY`、`HTTPS_PROXY`、`ALL_PROXY`。`ASTRA_PROXY_MODE=auto` 只在端点可达时使用代理，重启代理无需重启 Astra；启动器默认选 `off`，需明确选 `auto` 才探测，`always` 跳过探测。私有/本地 URL 及 `NO_PROXY` 主机始终直连，可同时使用本地 SearXNG/Firecrawl 与需代理的 API。

<a id="conclave-multi-expert-research"></a>

## Conclave 多专家研究

Conclave 编排 12 类领域专家的并行 SearXNG 搜索，再综合结构化报告。可直接提问：

```text
/conclave Compare current options for running LLMs locally
```

配置命令支持终端补全：

```text
/conclave config
/conclave config chairperson <profile>
/conclave config expert_list
/conclave config sources 5
```

`chairperson` 选择综合模型，`active` 使用当前模型；`sources` 限制每位专家的来源数量。专家菜单支持多选：Tab 追加，Enter 确认，使用菜单提供的名称。配置自动保存到 `~/.config/hermes/conclave.json`。

<a id="comfyui-image-generation"></a>

## ComfyUI 绘图

工具组保留 NoobAI/JANKU 路线，并提供独立 Anima/Qwen 路线：

- `comfyui_draw`：旧 NoobAI 工作流，等待并下载结果。
- `comfyui_start` / `comfyui_stop`：管理所选 WSL/原生实例，或说明外部服务所需操作；通过真实 HTTP API 验证状态。
- `comfyui_anima_status`：检查 Anima 模型、Qwen CLIP/VAE 和锁定 LoRA。
- `comfyui_anima_draw`：提交 Anima v2 signature-lineart 工作流，立即返回 `prompt_id`，不轮询。
- `comfyui_result`：单次查询已提交任务，完成后下载并附图供实际查看。

Anima 优先使用 `COMFYUI_ANIMA_SERVER`，否则共享 `COMFYUI_SERVER`。锁定默认参数为 Anima v2、Qwen 0.6B CLIP、Qwen Image VAE、Turbo 1.0、Aesthetic 0.6、Lyra lineart v2 0.8、16 步、CFG 1.0。日常请求优先用专用工具，未支持流程和诊断仍可用 Shell/API；只有真实 `prompt_id` 才代表提交成功。

`COMFYUI_LIFECYCLE` 支持 `auto`、`wsl`、`native`、`external`。auto 在 Windows 保留 WSL；macOS/Linux 只有同时配置 `COMFYUI_NATIVE_ROOT` 和 `COMFYUI_NATIVE_PYTHON` 才选 native，否则视为外部管理。

native 将 `main.py` 放进独立进程组，在工作区 `.astra` 保存 Astra 拥有的 PID 元数据，停止前核对命令和身份。`COMFYUI_NATIVE_LOG` 可覆盖日志路径。所有模式都以 `/system_stats` 为就绪和停止检查依据。

自行在 macOS 启动的 ComfyUI 可这样连接：

```dotenv
COMFYUI_SERVER=http://127.0.0.1:8188
COMFYUI_LIFECYCLE=external
```

需要 Astra 管理时，把 `COMFYUI_NATIVE_ROOT` 设为 ComfyUI 目录，`COMFYUI_NATIVE_PYTHON` 设为绝对解释器路径或相对该目录的路径。Windows 保留默认 WSL 生命周期时不要填写原生路径。

<a id="read-only-163-mail"></a>

## 163 邮件只读集成

可选邮件集成增量缓存邮件，用于本地检索。在安装目录 `.env` 填写账号和客户端授权码（不是网页登录密码）：

```dotenv
ASTRA_163_EMAIL=your-account@163.com
ASTRA_163_AUTH_CODE=your-client-authorization-code
# ASTRA_163_MAX_BODY_BYTES=5242880
```

不带子命令时，`checkmail` 刷新默认 `INBOX` 和 `已发送`，然后显示收件箱最近 30 封。还支持明确同步、缓存搜索、稳定 UID 读取、文件夹列表和状态。对 `recent`、`search`、`read`、`folders` 加 `--offline` 可只读已有缓存；刷新失败且缓存存在时返回明确标注的缓存结果，离线访问缺失缓存会失败且不新建数据库。

Windows：

```powershell
.\scripts\checkmail.bat
.\scripts\checkmail.bat sync --json
.\scripts\checkmail.bat search "invoice" --from billing@example.com --window 200 --json
.\scripts\checkmail.bat read 352 --folder INBOX --json
.\scripts\checkmail.bat recent --offline --recent 10 --json
```

安全的本地附件写入目前只支持 POSIX；Windows 明确下载附件会在联系服务器获取附件前返回 `unsupported_platform` 和退出码 `2`。同步、搜索、读取、文件夹、状态和离线缓存功能仍可用。

macOS/Linux：

```bash
./scripts/checkmail.sh
./scripts/checkmail.sh sync --json
./scripts/checkmail.sh search "invoice" --from billing@example.com --window 200 --json
./scripts/checkmail.sh read 352 --folder INBOX --json
./scripts/checkmail.sh recent --offline --recent 10 --json
./scripts/checkmail.sh attachment --folder INBOX --uidvalidity 77 --uid 352 --part 2 --json
```

同步把头部和解码文字存入 `.astra/mail/163.sqlite3`，不下载附件载荷；只有明确的 `attachment` 命令把选定附件写入 `.astra/mail/attachments/`。邮箱以只读方式选择，不标记已读，不移动、删除、回复或发送邮件。

源码安装附有[163 邮件技能](../../.astra/skills/operations/163-email-sync/SKILL.md)，通过项目信任检查发现，Setup 不负责安装或更新该技能。凭据、缓存和其他私有 `.astra` 状态保持本地且被 Git 忽略。

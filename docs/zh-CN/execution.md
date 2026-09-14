# 工具执行与文件访问

[首页](../../README.zh-CN.md) · [文档导航](README.md) · [English](../execution.md)

[Minimal 模式的 Bash 环境](#minimal-bash-environment) · [主机文件访问](#host-filesystem-access) · [工具策略与追踪](#tool-policy-and-tracing)

配置工具运行位置、文件访问范围及审批方式。

<a id="minimal-bash-environment"></a>

## Minimal 模式的 Bash 环境

Minimal Mode 的 `bash` 工具保留一个持久交互式 Shell，工作目录和导出的变量会跨调用保留。Windows 使用 WSL Bash，macOS/Linux 使用原生 Bash，并报告中性的 `posix` 环境。

每条命令默认超时 300 秒。跨平台配置 `ASTRA_PERSISTENT_BASH_TIMEOUT` 优先；只有未设置它时才读取旧 `ASTRA_WSL_PERSISTENT_TIMEOUT`。后者虽然名称含 WSL，仍兼容 WSL 和原生主机。

<a id="host-filesystem-access"></a>

## 主机文件访问

Python 和 Shell 默认使用 Docker 隔离。`/sandbox` 查看当前后端，`/sandbox off` 切到受保护的主机执行（如运行 `wsl.exe`），`/sandbox on` 切回 Docker。设置立即生效并保存到 `.astra/settings.json`。主机执行仍有超时、输出限额和危险命令检查。

`read_file`、`search_files`、`write_file`、`edit_file` 使用单独的主机文件策略，可访问明确允许的 Windows 盘符和 WSL UNC 路径，无需将这些路径暴露给任意沙箱命令。

当前工作区始终可读写。将 `config/filesystem.example.json` 复制为 `.astra/filesystem.json`，把 `shared-files` 换成实际要开放的主机路径。额外根目录设为 `ro` 或 `rw`，外部目录通常保持只读。`AGENT_FILESYSTEM_CONFIG` 可指定另一份策略文件。

默认 Docker 镜像为精简的 `python:3.12-slim`。需要项目依赖或 `pytest` 的 Minimal `run_code` 检查可构建并启用开发镜像。

Windows：

```powershell
.\scripts\build-sandbox-image.ps1
$env:ASTRA_DOCKER_IMAGE = "astra-sandbox:agent-system-dev"
```

macOS/Linux：

```bash
./scripts/build-sandbox-image.sh
export ASTRA_DOCKER_IMAGE=astra-sandbox:agent-system-dev
```

开发镜像包含 `pyproject.toml` 的基础依赖、`mcp`、`server`、`tracing`、`dev` 扩展，以及 Git、ripgrep 和 curl。不提供 Docker CLI、Node.js/npm 命令、浏览器或 GUI；Pyright 单独使用 Python 包提供的私有 Node 运行时，TUI 检查仍在主机执行。包含 curl 不代表开放网络，容器默认仍使用 `--network none`。

根目录 `.dockerignore` 仅让沙箱 Dockerfile 和依赖文件进入构建上下文，配置、状态、环境和无关源码不会传给 Docker。镜像首次构建需要联网；运行容器时，除非明确配置，否则仍断网。源码挂载在 `/workspace`，检查使用当前工作树，不是烘焙进镜像的旧副本。镜像设置也可写入 `.env`。

Windows 文件根目录示例：

```json
{
  "roots": [
    {"path": "D:\\shared", "mode": "ro"},
    {"path": "\\\\wsl.localhost\\Ubuntu\\home\\user", "mode": "ro"}
  ]
}
```

macOS 示例：

```json
{
  "roots": [
    {"path": "/Users/your-name/shared-files", "mode": "ro"}
  ]
}
```

`.astra/channels.json` 的 `send_file_roots` 遵循同样原则，例如 Windows 的 `D:\\allowed\\outputs` 或 macOS 的 `/Users/your-name/allowed/outputs`。仓库示例使用可移植的相对目录 `outputs`。

配置范围之外的路径会被拒绝，文件工具不会写入或编辑只读根目录。

<a id="tool-policy-and-tracing"></a>

## 工具策略与追踪

`AGENT_TOOL_POLICY` 支持 `permissive`、`safe` 和 `locked`。在 safe/locked 模式下，可用 `/permissions allow <tool-name>` 为当前进程批准被拒绝的工具。

- `TOOL_MAX_INLINE_CHARS=12000` 限制持久历史中的首尾预览，完整结果保存到 `TOOL_RESULT_DIR`。新结果在紧接着的一次模型迭代中最多完整展示 `TOOL_MAX_FRESH_RESULT_CHARS=100000` 字符，之后回到预览。base64/data URL 等不透明载荷不会作为大段文字内联。
- `TOOL_FAILURE_THRESHOLD=3` 在等价错误连续出现后打开单工具熔断，并禁用工具做最后一次综合回答。
- `PROMPT_CACHE_STABLE_TOOLS=1` 在工作会话中保持一份稳定的模型工具清单；预算和熔断仍在执行阶段检查，不在 ReAct 迭代间删除 schema。
- 设为 `PROMPT_CACHE_STABLE_TOOLS=0` 后，可用 `TOOL_PROGRESSIVE_EXPOSURE=1` 恢复旧核心/相关工具组及 `activate_tool_group`，以缩小首次未缓存提示，但会增加前缀缓存失效。
- `AGENT_MAX_REACT_ITERATIONS=50` 为整轮设置粗粒度上限；`0` 表示无固定次数上限，重复调用、熔断、提示预算和取消检查仍有效。

`AGENT_TRACE_ENABLED=1` 启用 OpenTelemetry spans。安装 SDK 和 OTLP exporter 后，`OTEL_EXPORTER_OTLP_ENDPOINT` 指向 Phoenix 或其他兼容收集器。

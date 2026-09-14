# macOS 浏览器 URL 记录

[首页](../../README.zh-CN.md) · [文档导航](README.md) · [English](../macos-browser-activity.md)

[明确启用](#enable-explicitly) · [存储与捕获范围](#storage-and-capture-boundary) · [停用](#disable) · [验收](#acceptance)

Edge 和 Chrome 可通过已有 MV3 扩展把活动页 URL 加入记录。Safari 保留原行为，此桥接不增加 Safari 支持。普通 `activity install` 不会启用浏览器桥接。

<a id="enable-explicitly"></a>

## 明确启用

在稳定的 Astra 源码安装目录，使用其 Python 环境：

```sh
.venv/bin/python -m agent.cli.main activity browser-install
.venv/bin/python -m agent.cli.main activity browser-status
.venv/bin/python -m agent.cli.main activity browser-token
```

最后一个命令把配对令牌复制到剪贴板，不打印。Edge 打开 `edge://extensions`，启用开发者模式，将 `browser-extension` 加载为解压扩展。打开 Options、粘贴令牌、启用记录并保存。扩展连接本地 8089 端口。用 `scripts/deploy_activity_recorder.sh` 安装新版原生记录器；签名身份变化后可能需重新授予辅助功能和输入监控权限。

专用 LaunchAgent 为 `com.astra.activity-browser-bridge`，plist 绑定安装时的源码与解释器，不要从将被删除的临时工作树安装。移动目录/环境后重新安装。日志不保留捕获内容；`browser-status` 报告 launchd 状态、可用退出码和快照新鲜度，不暴露 URL/令牌。进程加载不代表配对或记录链路已验收。

可选域名排除：

```sh
.venv/bin/python -m agent.cli.main activity browser-install --exclude-domain private.example
```

多域名可重复参数；不传时读取逗号分隔的 `ASTRA_ACTIVITY_EXCLUDE_DOMAINS`。扩展端也应保持一致。以后改环境变量时要重装 LaunchAgent，才能更新参数。

<a id="storage-and-capture-boundary"></a>

## 存储与捕获范围

状态存于 `ASTRA_ACTIVITY_ROOT/browser-bridge`，默认 `~/Library/Application Support/Astra/activity/browser-bridge`。目录 `0700`，令牌、启用标记和原子快照 `0600`。

记录器要求浏览器身份/标题匹配且快照不超过 8 秒。新的隐私、失焦、无效或排除更新会移除对应 URL；桥接停止/重启会清空快照。已启用但没有有效匹配时不提供 URL，窗口标题和现有 AX 记录仍继续。

URL 只保留 HTTP(S) 协议、主机和路径，移除凭据、查询和 fragment。路径仍可能敏感，并可能进入发给摘要模型的活动摘要。隐藏隐私模式 URL 不等于停止记录标题或 AX 内容。

<a id="disable"></a>

## 停用

```sh
.venv/bin/python -m agent.cli.main activity browser-uninstall
```

先撤销启用标记，再停止桥接并移除快照和 LaunchAgent；配对令牌和已有历史保留。记录器恢复此前 URL 行为，包括可用的原生 AXURL。这不是删除历史；若扩展也应停止尝试更新，需另行关闭扩展。

<a id="acceptance"></a>

## 验收

普通 Edge 页面聚焦时检查进程和新鲜快照，确认新事件包含清理后的 URL，且同步后保留。还应检查同标题导航、隐私窗口失效及关闭后的行为。单元/测试框架通过不代表这些真实浏览器结果已经通过。

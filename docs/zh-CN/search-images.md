# 搜索响应与图片画廊

[首页](../../README.zh-CN.md) · [文档导航](README.md) · [English](../search-images.md)

[搜索图片](#find-images) · [质量与限制](#result-quality-and-limitations) · [验证](#verification)

Astra 保留配置的搜索提供方和类型。`search_web` / `web_extract` 明确启用有界并行调度，但风险仍为 `network`；单项完成即发给前端，模型工具消息仍按原调用 ID 配对排序。其他网络操作保持原顺序，除非单独审查并启用并行。

<a id="find-images"></a>

## 搜索图片

可以直接说：“帮我找几张日式庭院的参考图”。

`search_images(query, max_results=6)` 复用 `EXA_API_KEY` 或 `EXA_ENV_FILE` 以及 `EXA_API_URL`，不需要新服务 Key。它从相关网页提取图片链接和代表图，不是以图搜图或视觉相似度搜索。

需要看图确认时可说：“实际看图后选出有池塘和石灯笼的图片”。模型通过 `read_search_images(image_ids, question)` 读取搜索返回的 ID。候选初始为 `not_inspected`，每次并发加载 1–4 张，把真实像素、ID 和来源标签送入已有图片附件/视觉预处理流程。下载成功仅表示 `awaiting_model_inspection`，不代表匹配要求。视觉检查使用当前模型和正常图片预算，无需额外 Key 或提供方。

下载上限为 8 MiB、1,600 万像素，每次跳转均检查公开 URL，并限制重定向和超时。实际解码后才附图；AVIF 原尺寸转成 PNG，其他支持格式保留原字节。文件保存到工具产物目录 `search-images`，存在时可复用。失败按 ID 单独报告，不阻止其他图片；ID 是有界的进程内映射，重启或过期后重新搜索。纯文本模型若无外部视觉工具，必须说明不能查看像素。

最多返回 10 张，含稳定 ID、来源网页和图片链接，以及 `image-galleries` 下的 HTML 画廊。终端提供：

- `/gallery`：当前 TUI 最近可用的搜索画廊。
- `/gallery 4`：工具结果 4 的画廊。
- `/tool 4`：该工具结果的详情。

这些本地命令在其他工具工作时仍可用。更新 Python 并构建 `ui-tui` 后重启以加载新版运行时、菜单和界面。画廊是独立本地文件，正常使用不需要常驻 Web 服务器。

<a id="result-quality-and-limitations"></a>

## 质量与限制

候选按 URL 去重、分散网页来源，并过滤明显的 SVG、头像、Logo 和登录素材。公开 URL 与有界 HEAD 检查排除不可达或极小文件，跳转前先检查目标。HEAD 有效不保证稍后浏览器能显示，源站可能改权限或禁止嵌入。过滤后可能少于请求数量，不推断原图分辨率或授权许可。

画廊转义外部文字，使用严格 CSP、无脚本和不发 referrer 的懒加载预览，打开时从源站加载图片。仅搜索不把像素放入模型上下文，需明确读取选中的候选。视觉内容判断必须来自这些像素；看过部分图片不会自动把全部画廊标成通过。

Exa 参考：[内容获取（英文）](https://exa.ai/docs/reference/contents-retrieval)。

<a id="verification"></a>

## 验证

测试覆盖候选过滤、有界下载、附件和画廊命令。真实模型验收需先查看像素，再描述内容；下载或渲染成功本身不证明相关性。每次保留时长和选图观察，区分模型/网络波动与工具调度开销。

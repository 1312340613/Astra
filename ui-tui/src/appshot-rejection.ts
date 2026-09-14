/** Only fixed identifiers/messages may enter terminal output. */
const recapture = [
  "artifact_missing", "artifact_path", "artifact_directory", "artifact_name",
  "artifact_authority", "artifact_identity_changed", "artifact_hash",
  "binding_mismatch", "recipient_identity_changed", "recipient_identity_unavailable",
  "invalid_png", "invalid_png_dimensions", "pixel_limit", "invalid_ax", "ax_size",
  "invalid_json", "invalid_fields", "unsupported_schema", "duplicate_key", "json_depth",
  "frame_size", "invalid_submission", "duplicate_manifest",
];
const retry: Record<string, string> = {
  backend_busy: "当前任务忙，请等任务结束后重试。",
  context_budget_exceeded: "请压缩会话或减少附件后重试。",
  context_budget_unavailable: "暂时无法计算输入预算，请稍后重试。",
  appshot_vision_unavailable: "当前模型配置不支持图片，请切换支持图片的模型。",
  session_media_unavailable: "会话附件无法保存，请检查磁盘空间和目录权限。",
  submission_size: "附件过大，请减少附件或重新截取较小窗口。",
  submission_capacity: "本会话的提交记录已达上限，请新建会话。",
  submission_payload_conflict: "提交标识冲突，请重新截图后发送。",
};
export function appshotRejectionNotice(value: unknown): string {
  const code = typeof value === "string" && (recapture.includes(value) || Object.hasOwn(retry, value))
    ? value : "appshot_admission_failed";
  const recovery = recapture.includes(code)
    ? "附件已失效或未通过校验，请删除该 Appshot 并重新截图；重复发送同一附件不会修复。"
    : retry[code] ?? "请查看 .logs/backend.log 中的 appshot_rejected 记录定位原因。";
  return `Appshot 发送失败 (${code})。草稿已恢复。${recovery}`;
}

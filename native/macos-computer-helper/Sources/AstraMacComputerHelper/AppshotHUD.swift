import AppKit
import Foundation
import AstraAppshotCore

/// Canonical codes shared with the versioned cross-language fixture. Never expose Error text.
public enum AppshotErrorCode: String, Error, Codable, CaseIterable, Sendable {
  case shortcutConflict = "shortcut_conflict"
  case noReceivingSession = "no_receiving_session"
  case receivingSessionAmbiguous = "receiving_session_ambiguous"
  case attachmentLimitReached = "attachment_limit_reached"
  case captureInProgress = "capture_in_progress"
  case permissionUnavailable = "permission_unavailable"
  case protectedUI = "protected_ui"
  case sourceWindowUnavailable = "source_window_unavailable"
  case sourceWindowChanged = "source_window_changed"
  case captureFailed = "capture_failed"
  case captureTimeout = "capture_timeout"
  case captureCancelled = "capture_cancelled"
  case axObservationFailed = "ax_observation_failed"
  case artifactUnsafe = "artifact_unsafe"
  case recipientDisconnected = "recipient_disconnected"
  case attachmentRejected = "attachment_rejected"
  case backendBusy = "backend_busy"
  case contextBudgetExceeded = "context_budget_exceeded"
  case submissionUnknown = "submission_unknown"
  case submissionConflict = "submission_conflict"
  case mediaUnavailable = "media_unavailable"
  case resourceLimitReached = "resource_limit_reached"
  case settingsInvalid = "settings_invalid"
  case settingsWriteFailed = "settings_write_failed"
  case protocolInvalid = "protocol_invalid"
  case brokerUnavailable = "broker_unavailable"

  static func map(_ error: Error) -> Self {
    if let code = error as? Self { return code }
    if let error = error as? AppshotCaptureError {
      switch error {
      case .permissionDenied: return .permissionUnavailable
      case .protectedContext: return .protectedUI
      case .indeterminateTarget: return .sourceWindowUnavailable
      case .sourceWindowChanged: return .sourceWindowChanged
      case .captureFailed: return .captureFailed
      case .resourceLimit: return .resourceLimitReached
      case .timedOut: return .captureTimeout
      case .cancelled: return .captureCancelled
      case .busy: return .captureInProgress
      }
    }
    if let error = error as? AppshotBrokerError {
      switch error {
      case .unsafeRuntime: return .artifactUnsafe
      case .systemFailure, .notOwner, .stopped: return .brokerUnavailable
      case .unauthorized, .invalidActivity: return .protocolInvalid
      case .tooManyClients, .quotaExceeded: return .resourceLimitReached
      case .noReceivingSession: return .noReceivingSession
      case .receivingSessionAmbiguous: return .receivingSessionAmbiguous
      case .attachmentLimitReached: return .attachmentLimitReached
      case .recipientDisconnected: return .recipientDisconnected
      case .captureBusy: return .captureInProgress
      case .unknownAttachment: return .attachmentRejected
      case .captureExpired: return .captureTimeout
      }
    }
    if let error = error as? AppshotError {
      switch error {
      case .invalidShortcut, .insecureSettingsPath: return .settingsInvalid
      case .shortcutConflict: return .shortcutConflict
      case .hotKeyRegistrationFailed: return .brokerUnavailable
      case .settingsIO: return .settingsWriteFailed
      }
    }
    if error is AXTextDetailSerializationError { return .axObservationFailed }
    if error is AppshotProtocolError { return .protocolInvalid }
    if let error = error as? ArtifactBundleError {
      return error == .quotaExceeded ? .resourceLimitReached : .artifactUnsafe
    }
    return .captureFailed
  }
}

enum AppshotHUDMessage: Equatable {
  case success
  case failure(AppshotErrorCode)
  var text: String {
    switch self {
    case .success: return "✓ Appshot 已附加到 Astra"
    case .failure(let code):
      switch code {
      case .shortcutConflict: return "Appshot 快捷键已被占用"
      case .noReceivingSession: return "没有可接收 Appshot 的 Astra 会话"
      case .receivingSessionAmbiguous: return "无法确定接收 Appshot 的会话"
      case .attachmentLimitReached: return "Appshot 附件已达上限"
      case .captureInProgress: return "Appshot 正在采集"
      case .permissionUnavailable: return "Appshot 所需权限不可用"
      case .protectedUI: return "无法采集受保护的界面"
      case .sourceWindowUnavailable: return "Appshot 来源窗口不可用"
      case .sourceWindowChanged: return "Appshot 来源窗口已改变"
      case .captureFailed: return "Appshot 采集失败"
      case .captureTimeout: return "Appshot 操作超时"
      case .captureCancelled: return "Appshot 操作已取消"
      case .axObservationFailed: return "Appshot 界面文本读取失败"
      case .artifactUnsafe: return "Appshot 文件验证失败"
      case .recipientDisconnected: return "Appshot 接收会话已断开"
      case .attachmentRejected: return "Appshot 附件未被接收"
      case .backendBusy: return "Astra 正忙，请稍后提交"
      case .contextBudgetExceeded: return "Appshot 超出上下文容量"
      case .submissionUnknown: return "Appshot 提交状态尚未确认"
      case .submissionConflict: return "Appshot 提交状态冲突"
      case .mediaUnavailable: return "Appshot 媒体不可用"
      case .resourceLimitReached: return "Appshot 资源已达上限"
      case .settingsInvalid: return "Appshot 设置无效"
      case .settingsWriteFailed: return "Appshot 设置保存失败"
      case .protocolInvalid: return "Appshot 通信验证失败"
      case .brokerUnavailable: return "Appshot 服务不可用"
      }
    }
  }
}

@MainActor protocol AppshotHUDPresenting: AnyObject {
  func show(_ message: AppshotHUDMessage)
  func hide()
}

@MainActor final class AppshotHUD: AppshotHUDPresenting {
  typealias Schedule = (TimeInterval, @escaping @MainActor () -> Void) -> Void
  private let render: (AppshotHUDMessage?) -> Void
  private let schedule: Schedule
  private var generation: UInt64 = 0
  init(render: @escaping (AppshotHUDMessage?) -> Void, schedule: @escaping Schedule) {
    self.render = render
    self.schedule = schedule
  }
  convenience init() {
    let surface = AppshotHUDSurface()
    self.init(
      render: { surface.render($0) },
      schedule: { delay, action in
        DispatchQueue.main.asyncAfter(deadline: .now() + delay) { action() }
      })
  }
  func show(_ message: AppshotHUDMessage) {
    generation &+= 1
    let expected = generation
    render(message)
    schedule(1.5) { [weak self] in
      guard let self, self.generation == expected else { return }
      self.hide()
    }
  }
  func hide() {
    generation &+= 1
    render(nil)
  }
}

@MainActor private final class AppshotHUDPanel: NSPanel {
  override var canBecomeKey: Bool { false }
  override var canBecomeMain: Bool { false }
}

/// Lazily created only on a terminal outcome, never while binding or capturing a source.
@MainActor private final class AppshotHUDSurface {
  private var panel: AppshotHUDPanel?
  func render(_ message: AppshotHUDMessage?) {
    guard let message else {
      panel?.orderOut(nil)
      return
    }
    if panel == nil {
      let panel = AppshotHUDPanel(
        contentRect: NSRect(x: 0, y: 0, width: 420, height: 56),
        styleMask: [.borderless, .nonactivatingPanel], backing: .buffered, defer: false)
      panel.hidesOnDeactivate = false
      panel.becomesKeyOnlyIfNeeded = false
      panel.isReleasedWhenClosed = false
      panel.ignoresMouseEvents = true
      panel.level = .floating
      panel.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary]
      self.panel = panel
    }
    guard let panel else { return }
    let label = NSTextField(labelWithString: message.text)
    label.alignment = .center
    label.font = .systemFont(ofSize: 15, weight: .medium)
    label.frame = NSRect(x: 12, y: 17, width: 396, height: 24)
    panel.contentView?.subviews.forEach { $0.removeFromSuperview() }
    panel.contentView?.addSubview(label)
    if let frame = NSScreen.main?.visibleFrame {
      panel.setFrameOrigin(NSPoint(x: frame.midX - 210, y: frame.minY + 64))
    }
    panel.orderFrontRegardless()
  }
}

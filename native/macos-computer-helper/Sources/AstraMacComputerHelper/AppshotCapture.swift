import AppKit
@preconcurrency import ApplicationServices
import CoreGraphics
import Darwin
import Foundation
import AstraAppshotCore
import ImageIO
import ScreenCaptureKit
import UniformTypeIdentifiers

extension AppshotCaptureDeadline {
  func axBudget() throws -> AXObservationBudget {
    _ = try remaining()
    return AXObservationBudget(remainingBudget: { (try? self.remaining()) ?? 0 })
  }
}

struct AppshotCaptureTarget {
  let source: AppshotSource
  let axRoot: AXUIElement?
  let window: SCWindow?
  func sameIdentity(as other: Self) -> Bool {
    source.pid == other.source.pid && source.processStart == other.source.processStart
      && source.windowId == other.source.windowId && source.bounds == other.source.bounds
      && source.bundleId == other.source.bundleId
      && (axRoot == nil || other.axRoot == nil || CFEqual(axRoot!, other.axRoot!))
  }
}
struct AppshotCaptureAuthority {
  let recipient: AppshotRecipientBinding
  let target: AppshotCaptureTarget
  let nonce: UUID
}
struct AppshotCaptureResult {
  let authority: AppshotCaptureAuthority
  let png: Data
  let width: Int
  let height: Int
  let deadline: AppshotCaptureDeadline
  var source: AppshotSource { authority.target.source }
  var axRoot: AXUIElement? { authority.target.axRoot }
}
protocol AppshotTargetProviding {
  /// Uncancellable framework work survives a caller returning on timeout/cancellation.
  var hasOutstandingRead: Bool { get }
  func read(deadline: AppshotCaptureDeadline) throws -> AppshotCaptureTarget
}
extension AppshotTargetProviding {
  var hasOutstandingRead: Bool { false }
}
protocol AppshotWindowImageProviding {
  /// Callback may arrive after cancellation; callers must reject it. No fallback operation exists.
  func capture(target: AppshotCaptureTarget, completion: @escaping (Result<CGImage, Error>) -> Void)
}

enum AppshotCaptureImage {
  static func validateDimensions(width: Int, height: Int) throws {
    guard (1...16384).contains(width), (1...16384).contains(height), width * height <= 32_000_000
    else { throw AppshotCaptureError.resourceLimit }
  }
  static func validatePNGSize(_ bytes: Int) throws {
    guard bytes > 0, bytes <= 10 * 1024 * 1024 else { throw AppshotCaptureError.resourceLimit }
  }
  static func png(_ image: CGImage) throws -> Data {
    try validateDimensions(width: image.width, height: image.height)
    try validateWindowImageContent(image)
    let data = NSMutableData()
    guard
      let destination = CGImageDestinationCreateWithData(
        data, UTType.png.identifier as CFString, 1, nil)
    else { throw AppshotCaptureError.captureFailed }
    CGImageDestinationAddImage(destination, image, nil)
    guard CGImageDestinationFinalize(destination) else { throw AppshotCaptureError.captureFailed }
    try validatePNGSize(data.length)
    return data as Data
  }
}

/// There is exactly one filter operation at the injectable construction boundary.
func appshotWindowFilter<Window, Filter>(
  _ window: Window, desktopIndependentWindow: (Window) -> Filter
) -> Filter {
  desktopIndependentWindow(window)
}

/// Concrete filter construction is shared with the injectable screenshot seam.
struct AppshotExactWindowImageProvider: AppshotWindowImageProviding {
  typealias Screenshot = (
    SCContentFilter, SCStreamConfiguration, @escaping (CGImage?, Error?) -> Void
  ) -> Void
  var screenshot: Screenshot = { filter, configuration, completion in
    SCScreenshotManager.captureImage(
      contentFilter: filter, configuration: configuration, completionHandler: completion)
  }
  func capture(target: AppshotCaptureTarget, completion: @escaping (Result<CGImage, Error>) -> Void)
  {
    guard let window = target.window, Int(window.windowID) == target.source.windowId,
      Int(window.owningApplication?.processID ?? -1) == target.source.pid
    else {
      completion(.failure(AppshotCaptureError.sourceWindowChanged))
      return
    }
    let filter = appshotWindowFilter(
      window, desktopIndependentWindow: SCContentFilter.init(desktopIndependentWindow:))
    let configuration = SCStreamConfiguration()
    let width = ceil(window.frame.width * CGFloat(filter.pointPixelScale))
    let height = ceil(window.frame.height * CGFloat(filter.pointPixelScale))
    guard width.isFinite, height.isFinite, width > 0, height > 0, width <= 16384, height <= 16384
    else {
      completion(.failure(AppshotCaptureError.resourceLimit))
      return
    }
    do { try AppshotCaptureImage.validateDimensions(width: Int(width), height: Int(height)) } catch
    {
      completion(.failure(error))
      return
    }
    configuration.width = Int(width)
    configuration.height = Int(height)
    configuration.showsCursor = false
    screenshot(filter, configuration) { image, error in
      if let error {
        completion(.failure(error))
      } else if let image {
        do {
          _ = try resolvedWindowImageGeometry(
            requested: WindowGeometry(
              bounds: window.frame, backingScale: CGFloat(filter.pointPixelScale)),
            imageWidth: image.width, imageHeight: image.height)
          completion(.success(image))
        } catch { completion(.failure(error)) }
      } else {
        completion(.failure(AppshotCaptureError.captureFailed))
      }
    }
  }
}

/// Single delivery without a structured task-group waiting for an uncooperative framework callback.
private final class AppshotCallbackClaim: @unchecked Sendable {
  private let lock = NSLock()
  private var claimed = false
  func take() -> Bool {
    lock.lock()
    defer { lock.unlock() }
    if claimed { return false }
    claimed = true
    return true
  }
}
private final class AppshotCaptureDelivery: @unchecked Sendable {
  private let lock = NSLock()
  private var completion: ((Result<AppshotCaptureResult, Error>) -> Void)?
  init(_ completion: @escaping (Result<AppshotCaptureResult, Error>) -> Void) {
    self.completion = completion
  }
  func finish(_ result: Result<AppshotCaptureResult, Error>) {
    lock.lock()
    let callback = completion
    completion = nil
    lock.unlock()
    callback?(result)
  }
}
final class AppshotCaptureCoordinator: @unchecked Sendable {
  private let targets: any AppshotTargetProviding
  private let images: any AppshotWindowImageProviding
  private let worker = DispatchQueue(label: "astra.appshot.capture")
  private let lock = NSLock()
  private var inFlight = false
  init(
    targets: any AppshotTargetProviding = AppshotSystemTargetProvider(),
    images: any AppshotWindowImageProviding = AppshotExactWindowImageProvider()
  ) {
    self.targets = targets
    self.images = images
  }
  /// Completion runs on a worker/timer queue. Task6 must hop to MainActor for broker delivery.
  /// The cancellation closure also invalidates successful results awaiting AX/publication.
  @discardableResult func capture(
    for recipient: AppshotRecipientBinding,
    deadline: AppshotCaptureDeadline = AppshotCaptureDeadline(),
    completion: @escaping (Result<AppshotCaptureResult, Error>) -> Void
  ) -> () -> Void {
    let delivery = AppshotCaptureDelivery(completion)
    lock.lock()
    let busy = inFlight || targets.hasOutstandingRead
    if !busy { inFlight = true }
    lock.unlock()
    guard !busy else {
      delivery.finish(.failure(AppshotCaptureError.busy))
      return {}
    }
    let timer = DispatchWorkItem {
      delivery.finish(.failure(AppshotCaptureError.timedOut))
      deadline.cancel()
    }
    DispatchQueue.global().asyncAfter(
      deadline: .now() + ((try? deadline.remaining()) ?? 0), execute: timer)
    worker.async {
      do {
        _ = try deadline.remaining()
        let target = try self.targets.read(deadline: deadline)
        _ = try deadline.remaining()
        let claim = AppshotCallbackClaim()
        self.images.capture(target: target) { outcome in
          guard claim.take() else { return }
          self.worker.async {
            let completed: Result<AppshotCaptureResult, Error>
            do {
              _ = try deadline.remaining()
              let image = try outcome.get()
              let png = try AppshotCaptureImage.png(image)
              let result = AppshotCaptureResult(
                authority: .init(recipient: recipient, target: target, nonce: UUID()), png: png,
                width: image.width, height: image.height, deadline: deadline)
              try self.validateIdentity(result)
              completed = .success(result)
            } catch { completed = .failure(error) }
            self.lock.lock()
            self.inFlight = false
            self.lock.unlock()
            timer.cancel()
            // Callers may immediately enter final validation from this completion.
            delivery.finish(completed)
          }
        }
      } catch {
        self.lock.lock()
        self.inFlight = false
        self.lock.unlock()
        timer.cancel()
        delivery.finish(.failure(error))
      }
    }
    return {
      deadline.cancel()
      timer.cancel()
      delivery.finish(.failure(AppshotCaptureError.cancelled))
    }
  }
  /// Must be called after AX serialization and immediately before handing bytes to publication.
  func revalidate(_ result: AppshotCaptureResult) throws {
    _ = try result.deadline.remaining()
    // Final validation participates in the same admission lock as capture. The provider's
    // pending callback remains owned even after this synchronous scope has unwound.
    lock.lock()
    let busy = inFlight || targets.hasOutstandingRead
    if !busy { inFlight = true }
    lock.unlock()
    guard !busy else { throw AppshotCaptureError.busy }
    defer {
      lock.lock()
      inFlight = false
      lock.unlock()
    }
    try validateIdentity(result)
  }
  private func validateIdentity(_ result: AppshotCaptureResult) throws {
    _ = try result.deadline.remaining()
    let current: AppshotCaptureTarget
    do { current = try targets.read(deadline: result.deadline) } catch AppshotCaptureError
      .indeterminateTarget
    {
      _ = try result.deadline.remaining()
      throw AppshotCaptureError.sourceWindowChanged
    }
    guard result.authority.target.sameIdentity(as: current) else {
      throw AppshotCaptureError.sourceWindowChanged
    }
    _ = try result.deadline.remaining()
  }
}

struct AppshotFrontmostApplication {
  let pid: Int32
  let bundle: String
  let label: String
}
struct AppshotAXWindowEvidence {
  let root: AXUIElement
  let bounds: CGRect
  var title: String? = nil
}
struct AppshotNativeWindow {
  let id: UInt32
  let pid: Int32
  let bounds: CGRect
  let onScreen: Bool
  let layer: Int
  let alpha: Double
  var title: String = ""
  var scWindow: SCWindow? = nil
}
struct AppshotShareableWindows {
  let windows: [AppshotNativeWindow]
  let displays: [CGRect]
}

/// Production observation only. Tests replace observations, while running the same safety and binding code.
struct AppshotSystemTargetProvider: AppshotTargetProviding {
  var accessibility: () -> Bool = AXIsProcessTrusted
  var screenRecording: () -> Bool = CGPreflightScreenCaptureAccess
  var session: () -> [String: Any]? = { CGSessionCopyCurrentDictionary() as? [String: Any] }
  var frontmost: () -> AppshotFrontmostApplication? = {
    guard let app = NSWorkspace.shared.frontmostApplication, !app.isTerminated,
      let bundle = app.bundleIdentifier
    else { return nil }
    return .init(pid: app.processIdentifier, bundle: bundle, label: app.localizedName ?? bundle)
  }
  var processes: any AppshotProcessProviding = AppshotSystemProcesses()
  var focusedWindow: (Int32, AppshotCaptureDeadline) throws -> AppshotAXWindowEvidence = Self.readAX
  var nativeWindows: () throws -> [AppshotNativeWindow] = Self.readCG
  var enumerator = AppshotShareableWindowEnumerator()
  var hasOutstandingRead: Bool { enumerator.hasOutstandingRead }
  func read(deadline: AppshotCaptureDeadline) throws -> AppshotCaptureTarget {
    _ = try deadline.remaining()
    try checkSafety()
    guard let app = frontmost(), !app.bundle.isEmpty,
      let process = processes.identity(pid: app.pid)
    else { throw AppshotCaptureError.indeterminateTarget }
    // AX is optional context, never a prerequisite for a screenshot. A bounded
    // root read may improve binding; without it require one unique visible window.
    let ax = accessibility() ? try? focusedWindow(app.pid, deadline.limited(to: 0.3)) : nil
    _ = try deadline.remaining()
    var matches = try nativeWindows().filter {
      $0.pid == app.pid && $0.onScreen && $0.alpha > 0 && $0.id != 0
        && !$0.bounds.isEmpty && !$0.bounds.isInfinite && !$0.bounds.isNull
        && (ax == nil || $0.bounds == ax!.bounds)
    }
    let needsTitle = matches.count > 1
    if needsTitle, let title = ax?.title, !title.isEmpty,
      matches.allSatisfy({ !$0.title.isEmpty }) {
      matches = matches.filter {
        !$0.title.isEmpty && windowTitlesMatch(screenCaptureTitle: $0.title, accessibilityTitle: title)
      }
    }
    guard matches.count == 1, let native = matches.first
    else { throw AppshotCaptureError.indeterminateTarget }
    let content = try enumerator.read(deadline: deadline)
    let windows = content.windows.filter { $0.id == native.id }
    guard windows.count == 1, let window = windows.first, window.onScreen,
      window.pid == app.pid, window.bounds == native.bounds,
      content.displays.contains(where: { $0.intersects(native.bounds) }),
      processes.identity(pid: app.pid) == process,
      frontmost()?.pid == app.pid
    else { throw AppshotCaptureError.sourceWindowChanged }
    try checkSafety()
    if let ax {
      let afterAX = try? focusedWindow(app.pid, deadline.limited(to: 0.3))
      if needsTitle {
        guard let afterAX, afterAX.title == ax.title else {
          throw AppshotCaptureError.sourceWindowChanged
        }
      }
      if let afterAX {
        guard CFEqual(ax.root, afterAX.root), ax.bounds == afterAX.bounds
        else { throw AppshotCaptureError.sourceWindowChanged }
      }
    }
    guard frontmost()?.pid == app.pid, processes.identity(pid: app.pid) == process
    else { throw AppshotCaptureError.sourceWindowChanged }
    _ = try deadline.remaining()
    return AppshotCaptureTarget(
      source: .init(
        pid: Int(process.pid), processStart: process.processStart,
        bundleId: app.bundle,
        appLabel: truncateUTF8Linearly(app.label, maximumCharacters: 256, maximumBytes: 256),
        windowTitle: truncateUTF8Linearly(
          window.title, maximumCharacters: 1024, maximumBytes: 1024), windowId: Int(native.id),
        bounds: .init(
          x: native.bounds.minX, y: native.bounds.minY, width: native.bounds.width, height: native.bounds.height)),
      axRoot: ax?.root, window: window.scWindow)
  }
  private func checkSafety() throws {
    guard screenRecording() else { throw AppshotCaptureError.permissionDenied }
    guard let session = session(),
      session[kCGSessionOnConsoleKey as String] as? Bool == true,
      session[kCGSessionLoginDoneKey as String] as? Bool == true,
      session["CGSSessionScreenIsLocked"] as? Bool != true
    else { throw AppshotCaptureError.protectedContext }
  }
  // Window type is structural evidence only; user-selected dialogs/settings
  // are not filtered by application, subrole, modal state or focused text field.
  static func validateAXWindow(role: String?, minimized: Bool?) throws {
    guard (role == kAXWindowRole || role == kAXSheetRole), minimized != true
    else { throw AppshotCaptureError.indeterminateTarget }
  }
  private static func readAX(pid: Int32, deadline: AppshotCaptureDeadline) throws
    -> AppshotAXWindowEvidence
  {
    let budget = try deadline.axBudget()
    func attribute(_ element: AXUIElement, _ name: String) throws -> CFTypeRef {
      var value: CFTypeRef?
      guard
        budget.call(
          element: element, { AXUIElementCopyAttributeValue(element, name as CFString, &value) })
          == .success,
        let value
      else { throw AppshotCaptureError.indeterminateTarget }
      return value
    }
    func element(_ value: CFTypeRef) throws -> AXUIElement {
      guard CFGetTypeID(value) == AXUIElementGetTypeID() else {
        throw AppshotCaptureError.indeterminateTarget
      }
      return unsafeBitCast(value, to: AXUIElement.self)
    }
    let application = AXUIElementCreateApplication(pid)
    let root = try element(attribute(application, kAXFocusedWindowAttribute))
    var rootPID: pid_t = 0
    guard AXUIElementGetPid(root, &rootPID) == .success, rootPID == pid
    else { throw AppshotCaptureError.indeterminateTarget }
    var minimized: CFTypeRef?
    _ = budget.call(element: root) {
      AXUIElementCopyAttributeValue(root, kAXMinimizedAttribute as CFString, &minimized)
    }
    try validateAXWindow(
      role: attribute(root, kAXRoleAttribute) as? String, minimized: minimized as? Bool)
    let position = try attribute(root, kAXPositionAttribute)
    let size = try attribute(root, kAXSizeAttribute)
    guard CFGetTypeID(position) == AXValueGetTypeID(), CFGetTypeID(size) == AXValueGetTypeID()
    else { throw AppshotCaptureError.indeterminateTarget }
    var point = CGPoint.zero
    var dimensions = CGSize.zero
    guard AXValueGetValue(unsafeBitCast(position, to: AXValue.self), .cgPoint, &point),
      AXValueGetValue(unsafeBitCast(size, to: AXValue.self), .cgSize, &dimensions)
    else { throw AppshotCaptureError.indeterminateTarget }
    let bounds = CGRect(origin: point, size: dimensions)
    guard bounds.origin.x.isFinite, bounds.origin.y.isFinite, bounds.width.isFinite,
      bounds.height.isFinite,
      bounds.width > 0, bounds.height > 0
    else { throw AppshotCaptureError.indeterminateTarget }
    let title = (try? attribute(root, kAXTitleAttribute)) as? String
    return .init(root: root, bounds: bounds,
                 title: title.flatMap { !$0.isEmpty && $0.utf8.count <= 4096 ? $0 : nil })
  }
  private static func readCG() throws -> [AppshotNativeWindow] {
    guard
      let records = CGWindowListCopyWindowInfo(
        [.optionOnScreenOnly, .excludeDesktopElements], kCGNullWindowID) as? [[String: Any]]
    else { throw AppshotCaptureError.indeterminateTarget }
    return try records.map { record in
      guard let id = record[kCGWindowNumber as String] as? UInt32,
        let pid = record[kCGWindowOwnerPID as String] as? Int32,
        let dictionary = record[kCGWindowBounds as String] as? [String: Any],
        let bounds = CGRect(dictionaryRepresentation: dictionary as CFDictionary),
        let layer = record[kCGWindowLayer as String] as? Int,
        let onScreen = record[kCGWindowIsOnscreen as String] as? Bool,
        let alpha = record[kCGWindowAlpha as String] as? Double
      else { throw AppshotCaptureError.indeterminateTarget }
      return .init(id: id, pid: pid, bounds: bounds, onScreen: onScreen, layer: layer, alpha: alpha,
                   title: record[kCGWindowName as String] as? String ?? "")
    }
  }
}

/// Injectable framework callback boundary shared by every target read.
final class AppshotShareableWindowEnumerator: @unchecked Sendable {
  typealias Completion = (Result<AppshotShareableWindows, Error>) -> Void
  typealias Enumerate = (@escaping Completion) -> Void
  private let enumerate: Enumerate
  private let lock = NSLock()
  private var pending: AppshotShareableContentBox?
  init(enumerate: @escaping Enumerate = AppshotShareableWindowEnumerator.enumerateSystem) {
    self.enumerate = enumerate
  }
  var hasOutstandingRead: Bool {
    lock.lock()
    defer { lock.unlock() }
    return pending != nil
  }
  func read(deadline: AppshotCaptureDeadline) throws -> AppshotShareableWindows {
    let box = AppshotShareableContentBox()
    // Reserve before invoking the callback API, which may complete synchronously.
    // Expired/cancelled callers cannot create new OS work, including after slow AX/CG reads.
    try lock.withLock {
      _ = try deadline.remaining()
      guard pending == nil else { throw AppshotCaptureError.busy }
      pending = box
    }
    enumerate { [self] outcome in
      lock.lock()
      // Object identity is an operation lease. An old duplicate cannot write another
      // result or release a newer read. Retain self until the terminal callback.
      guard pending === box else {
        lock.unlock()
        return
      }
      box.receive(outcome)
      pending = nil
      lock.unlock()
      box.signal.signal()
    }
    // The framework has no cancellation API. Return promptly while retaining the
    // pending lease, and periodically observe the same absolute cancellation budget.
    while true {
      let remaining = try deadline.remaining()
      if box.signal.wait(timeout: .now() + min(remaining, 0.01)) == .success {
        _ = try deadline.remaining()
        guard let result = box.value else { throw AppshotCaptureError.captureFailed }
        return try result.get()
      }
    }
  }
  private static func enumerateSystem(completion: @escaping Completion) {
    SCShareableContent.getExcludingDesktopWindows(true, onScreenWindowsOnly: true) {
      content, error in
      if let error {
        completion(.failure(error))
        return
      }
      guard let content else {
        completion(.failure(AppshotCaptureError.captureFailed))
        return
      }
      completion(
        .success(
          .init(
            windows: content.windows.map {
              .init(
                id: $0.windowID, pid: $0.owningApplication?.processID ?? -1,
                bounds: $0.frame, onScreen: $0.isOnScreen, layer: Int($0.windowLayer), alpha: 1,
                title: $0.title ?? "", scWindow: $0)
            }, displays: content.displays.map(\.frame))))
    }
  }
}
private final class AppshotShareableContentBox: @unchecked Sendable {
  let signal = DispatchSemaphore(value: 0)
  private let lock = NSLock()
  private var content: Result<AppshotShareableWindows, Error>?
  var value: Result<AppshotShareableWindows, Error>? {
    lock.lock()
    defer { lock.unlock() }
    return content
  }
  func receive(_ value: Result<AppshotShareableWindows, Error>) {
    lock.lock()
    content = value
    lock.unlock()
  }
}

import Foundation

public enum AppshotCaptureError: Error, Equatable {
  case permissionDenied, protectedContext, indeterminateTarget, sourceWindowChanged
  case captureFailed, resourceLimit, timedOut, cancelled, busy
}

/// One absolute deadline, with shared cancellation across child budgets.
public final class AppshotCaptureDeadline: @unchecked Sendable {
  private final class Cancellation: @unchecked Sendable {
    let lock = NSLock()
    var cancelled = false
  }
  private let cancellation: Cancellation
  private let end: TimeInterval
  private let clock: () -> TimeInterval

  public init(duration: TimeInterval = 5,
    clock: @escaping () -> TimeInterval = { ProcessInfo.processInfo.systemUptime }) {
    self.clock = clock
    self.cancellation = Cancellation()
    self.end = clock() + (duration.isFinite ? min(5, max(0, duration)) : 0)
  }
  private init(end: TimeInterval, clock: @escaping () -> TimeInterval, cancellation: Cancellation) {
    self.end = end
    self.clock = clock
    self.cancellation = cancellation
  }
  public func cancel() {
    cancellation.lock.lock()
    cancellation.cancelled = true
    cancellation.lock.unlock()
  }
  public func remaining() throws -> TimeInterval {
    cancellation.lock.lock()
    let stopped = cancellation.cancelled
    cancellation.lock.unlock()
    if stopped { throw AppshotCaptureError.cancelled }
    let value = end - clock()
    guard value.isFinite, value > 0 else { throw AppshotCaptureError.timedOut }
    return value
  }
  public func limited(to duration: TimeInterval) throws -> AppshotCaptureDeadline {
    _ = try remaining()
    let childEnd = clock() + (duration.isFinite ? max(0, duration) : 0)
    return AppshotCaptureDeadline(end: min(end, childEnd), clock: clock, cancellation: cancellation)
  }
}

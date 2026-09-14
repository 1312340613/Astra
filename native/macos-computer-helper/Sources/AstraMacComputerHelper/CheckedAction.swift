import Foundation

/// A bounded state goal. Never toggle an already satisfied choice or retry a
/// dispatched click. The caller retains exact element/window authority.
func performCheckedAction(
    expected: Bool,
    radio: Bool,
    read: () -> Bool?,
    perform: () throws -> ActionPerformance,
    clock: () -> TimeInterval = { ProcessInfo.processInfo.systemUptime },
    sleep: (TimeInterval) -> Void = { Thread.sleep(forTimeInterval: $0) }
) throws -> ActionPerformance {
    guard let before = read() else { throw ActionPerformFailure(error: .helperFailed, inputStarted: false) }
    if before == expected { return ActionPerformance(inputStarted: false, effectVerification: .verified) }
    guard !radio || expected else { throw ActionPerformFailure(error: .invalidAction, inputStarted: false) }
    let performance = try perform()
    let deadline = clock() + 0.4
    repeat {
        if read() == expected {
            return ActionPerformance(inputStarted: performance.inputStarted, effectVerification: .verified)
        }
        sleep(0.01)
    } while clock() < deadline
    throw ActionPerformFailure(error: .unknownOutcome, inputStarted: performance.inputStarted)
}

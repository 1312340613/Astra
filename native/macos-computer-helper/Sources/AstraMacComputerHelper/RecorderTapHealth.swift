import Foundation

/// A tap can exist with keyboard events removed by macOS, or be disabled
/// without its notification reaching the polling loop. Never retry without
/// permission; bound attempts independently of event callbacks.
struct RecorderTapHealth {
    enum Action { case none, install, enable }
    private var lastAttempt = Date.distantPast

    mutating func action(at date: Date, permitted: Bool, installed: Bool,
                         keyboardIncluded: Bool, enabled: Bool) -> Action {
        guard permitted, date.timeIntervalSince(lastAttempt) >= 2 else { return .none }
        let result: Action = !installed || !keyboardIncluded ? .install : (!enabled ? .enable : .none)
        if result != .none { lastAttempt = date }
        return result
    }
}

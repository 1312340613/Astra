import Foundation

/// A real key opens a three-second capture window; polling cannot extend it.
/// Keep the latest bounded snapshot until idle, never an unbounded sequence.
struct RecorderTextBurst<Snapshot> {
    private var scope: Int32?
    private var until = Date.distantPast
    private var pending: Snapshot?

    mutating func keyDown(scope: Int32, at date: Date) {
        if self.scope != scope { cancel() }
        self.scope = scope
        until = date.addingTimeInterval(3)
    }

    mutating func observe(_ snapshot: Snapshot, scope: Int32, at date: Date) {
        guard self.scope == scope else { cancel(); return }
        guard date <= until else { return }
        pending = snapshot
    }

    mutating func cancel() {
        scope = nil
        until = .distantPast
        pending = nil
    }

    mutating func flush(at date: Date, force: Bool = false) -> Snapshot? {
        guard force || date > until else { return nil }
        let result = pending
        cancel()
        return result
    }
}

enum RecorderKeyNames {
    static func name(_ code: Int) -> String { names[code] ?? "k\(code)" }
    private static let names: [Int: String] = [
        0: "a", 1: "s", 2: "d", 3: "f", 4: "h", 5: "g", 6: "z", 7: "x", 8: "c", 9: "v",
        11: "b", 12: "q", 13: "w", 14: "e", 15: "r", 16: "y", 17: "t", 18: "1", 19: "2",
        20: "3", 21: "4", 22: "6", 23: "5", 36: "return", 37: "l", 38: "j", 40: "k", 45: "n",
        46: "m", 47: "period", 48: "tab", 53: "escape", 76: "enter",
        117: "forward-delete", 115: "home", 119: "end", 116: "pageup", 121: "pagedown",
        122: "f1", 120: "f2", 99: "f3", 118: "f4", 96: "f5", 97: "f6", 98: "f7",
        100: "f8", 101: "f9", 109: "f10", 103: "f11", 111: "f12", 105: "f13", 107: "f14", 113: "f15",
        106: "f16", 64: "f17", 79: "f18", 80: "f19", 90: "f20",
        49: "space", 51: "delete", 123: "left", 124: "right", 125: "down", 126: "up",
    ]

}

enum RecorderClickDescriptor {
    private static let containers: Set<String> = ["AXScrollArea", "AXGroup", "AXList"]

    static func refine(_ target: [String: String], children: () -> [[String: String]]) -> [String: String] {
        guard containers.contains(target["role"] ?? ""),
              !SuppressionEngine.suppressesSecureField(role: target["role"], subrole: target["subrole"]),
              (target["description"] ?? "").isEmpty, (target["title"] ?? "").isEmpty else { return target }
        return children().first { child in
            guard let role = child["role"], !containers.contains(role) else { return false }
            return !SuppressionEngine.suppressesSecureField(role: role, subrole: child["subrole"])
        } ?? target
    }
}

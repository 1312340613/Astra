import Foundation

// Astra native activity recorder — pure-logic core (stage 3, M2).
// Recorder maintenance: README.md#activity-summary-catch-up-and-health
// Events/buckets must byte-match what activity_sync.normalize_event consumes;
// golden fixture tests live in tests/test_activity_recorder_contract.py (Python side).
//
// JSONValue comes from Protocol.swift (same module) — one JSON dialect per helper.
// Everything here is internal; only runActivityRecorder() (daemon file) is public.

struct RecorderEvent: Codable {
    var id: Int
    var kind: String
    var timestamp: String
    var app: [String: JSONValue]
    var window: [String: JSONValue]?
    var selection: JSONValue?
    var keyboard: JSONValue?
    var mouse: JSONValue?
    var ax: JSONValue?

    enum CodingKeys: String, CodingKey {
        case id, kind, timestamp, app, window, selection, keyboard, mouse, ax
    }

    init(
        id: Int, kind: String, timestamp: String,
        app: [String: JSONValue], window: [String: JSONValue]?,
        body: (field: String, value: JSONValue)?
    ) {
        self.id = id
        self.kind = kind
        self.timestamp = timestamp
        self.app = app
        self.window = window
        switch body?.field {
        case "selection": self.selection = body?.value
        case "keyboard": self.keyboard = body?.value
        case "mouse": self.mouse = body?.value
        case "ax": self.ax = body?.value
        default: break
        }
    }
}

enum SuppressionEngine {
    /// Bundle ids never recorded (mirrors the user's existing privacy boundary:
    /// WeChat excluded since Skysight days, plus credential surfaces).
    static let defaultBlocklist: Set<String> = [
        "com.tencent.xinWeChat",
        "com.apple.Passwords",
    ]

    static func suppresses(bundleID: String, blocklist: Set<String>) -> Bool {
        blocklist.contains(bundleID)
    }

    /// Secure text fields are always dropped (value would be ciphertext anyway).
    static func suppressesSecureField(role: String?, subrole: String? = nil) -> Bool {
        role?.localizedCaseInsensitiveContains("secure") == true ||
            subrole?.localizedCaseInsensitiveContains("secure") == true
    }
}

/// Reads role metadata first so callers can reject secure controls without
/// touching attributes that may contain user text.
enum RecorderAXCapture {
    static func trimmedValue(_ raw: String) -> String {
        let cap = 2_000
        guard raw.count > cap else { return raw }
        let marker = "…[truncated]…"
        let headCount = 1_200
        return String(raw.prefix(headCount)) + marker + String(raw.suffix(cap - headCount - marker.count))
    }

    static func capture(
        role: String?,
        subrole: String?,
        sensitiveAttributes: [String: () -> String?]
    ) -> [String: String] {
        var result: [String: String] = [:]
        if let role { result["role"] = role }
        if let subrole { result["subrole"] = subrole }
        guard !SuppressionEngine.suppressesSecureField(role: role, subrole: subrole) else {
            return result
        }
        for (name, read) in sensitiveAttributes {
            if let value = read() { result[name] = value }
        }
        return result
    }

    static func captureHitTarget(
        expectedPID: pid_t,
        hitPID: pid_t,
        role: String?,
        subrole: String?,
        sensitiveAttributes: [String: () -> String?]
    ) -> [String: String] {
        guard hitPID == expectedPID else { return [:] }
        return capture(role: role, subrole: subrole, sensitiveAttributes: sensitiveAttributes)
    }
}

final class BucketWriter {
    private let root: URL
    private let bucketWidthSeconds: Int
    private let retentionDays: Int
    private let encoder = JSONEncoder()
    private(set) var currentBucketStart: Date?
    private var eventCount = 0
    private var suppressedCount = 0
    private var nextEventID: Int

    init(root: URL, bucketWidthSeconds: Int = 600, initialEventID: Int = 1, retentionDays: Int = 90) {
        self.root = root
        self.bucketWidthSeconds = bucketWidthSeconds
        self.retentionDays = retentionDays >= 0 ? retentionDays : 90
        self.nextEventID = initialEventID
    }

    /// Zero expires completed buckets immediately; malformed/negative values
    /// retain the safe 90-day default.
    static func retentionDays(environment: [String: String]) -> Int {
        guard let raw = environment["ASTRA_ACTIVITY_RETENTION_DAYS"],
              let days = Int(raw), days >= 0 else { return 90 }
        return days
    }

    private static func isPlainDirectory(_ url: URL) -> Bool {
        guard let values = try? url.resourceValues(forKeys: [.isDirectoryKey, .isSymbolicLinkKey]) else { return false }
        return values.isDirectory == true && values.isSymbolicLink != true
    }

    /// Daemon restart recovery: resume the id sequence across ALL existing
    /// buckets (a fresh 1 would collide inside a still-open segment) and seal
    /// stale unsealed buckets that a previous process died mid-writing.
    func bootstrap(now: Date) throws {
        let segmentsDir = root.appendingPathComponent("segments")
        guard Self.isPlainDirectory(root), Self.isPlainDirectory(segmentsDir),
              let buckets = try? FileManager.default.contentsOfDirectory(at: segmentsDir, includingPropertiesForKeys: nil) else { return }
        var maxID = nextEventID - 1
        let currentStart = bucketStart(for: now)
        let openStart = currentBucketStart
        let cutoff = now.addingTimeInterval(-Double(retentionDays) * 86_400)
        for dir in buckets {
            guard Self.isPlainDirectory(dir),
                  let started = Self.bucketFormat.date(from: dir.lastPathComponent),
                  Self.bucketFormat.string(from: started) == dir.lastPathComponent else { continue }
            // Do not follow event/metadata links during recovery either.
            let linkedFiles = ["events.jsonl", "metadata.json"].contains { name in
                (try? dir.appendingPathComponent(name).resourceValues(forKeys: [.isSymbolicLinkKey]))?.isSymbolicLink == true
            }
            guard !linkedFiles else { continue }
            if let data = try? Data(contentsOf: dir.appendingPathComponent("events.jsonl")),
               let lastLine = data.split(separator: 0x0A).last,
               let object = try? JSONSerialization.jsonObject(with: lastLine) as? [String: Any],
               let lastID = object["id"] as? Int {
                maxID = max(maxID, lastID)
            }
            // Only completed buckets age out. Preserve both the wall-clock
            // bucket and any bucket still open in this writer, even at age zero.
            if started.addingTimeInterval(Double(bucketWidthSeconds)) < cutoff,
               started != currentStart, started != openStart {
                try FileManager.default.removeItem(at: dir)
                continue
            }
            let hasMetadata = FileManager.default.fileExists(atPath: dir.appendingPathComponent("metadata.json").path)
            if !hasMetadata, let started = Self.bucketFormat.date(from: dir.lastPathComponent),
               started < currentStart {
                try seal(started)
            }
        }
        nextEventID = max(nextEventID, maxID + 1)
    }

    private static let bucketFormat: DateFormatter = {
        let f = DateFormatter()
        f.locale = Locale(identifier: "en_US_POSIX")
        f.timeZone = TimeZone(identifier: "UTC")
        f.dateFormat = "yyyy-MM-dd'T'HH-mm-ss'Z'"
        return f
    }()

    private static let eventTimestampFormat: ISO8601DateFormatter = {
        let f = ISO8601DateFormatter()
        f.formatOptions = [.withInternetDateTime]
        f.timeZone = TimeZone(identifier: "UTC")
        return f
    }()

    static func timestamp(for date: Date) -> String {
        eventTimestampFormat.string(from: date)
    }

    static func bucketName(for date: Date, widthSeconds: Int) -> String {
        let seconds = Int(date.timeIntervalSince1970)
        let aligned = Date(timeIntervalSince1970: TimeInterval(seconds - seconds % widthSeconds))
        return bucketFormat.string(from: aligned)
    }

    func allocateEventID() -> Int {
        defer { nextEventID += 1 }
        return nextEventID
    }

    func suppressOne() { suppressedCount += 1 }

    private func bucketStart(for date: Date) -> Date {
        let seconds = Int(date.timeIntervalSince1970)
        return Date(timeIntervalSince1970: TimeInterval(seconds - seconds % bucketWidthSeconds))
    }

    /// Append one event, rotating+sealing on bucket boundary crossing.
    @discardableResult
    func write(_ event: RecorderEvent, at date: Date) throws -> URL? {
        let start = bucketStart(for: date)
        var sealed: URL?
        if let current = currentBucketStart, current != start {
            sealed = try seal(current)
        }
        currentBucketStart = start
        eventCount += 1
        let dir = root.appendingPathComponent("segments").appendingPathComponent(Self.bucketFormat.string(from: start))
        try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        encoder.outputFormatting = [.sortedKeys]
        let line = try encoder.encode(event) + Data("\n".utf8)
        let file = dir.appendingPathComponent("events.jsonl")
        if !FileManager.default.fileExists(atPath: file.path) {
            FileManager.default.createFile(atPath: file.path, contents: nil)
        }
        let handle = try FileHandle(forWritingTo: file)
        defer { try? handle.close() }
        handle.seekToEndOfFile()
        handle.write(line)
        return sealed
    }

    /// metadata.json appearance = bucket sealed (safe for sync import).
    func seal(_ start: Date) throws -> URL {
        let dir = root.appendingPathComponent("segments").appendingPathComponent(Self.bucketFormat.string(from: start))
        let bucketName = Self.bucketFormat.string(from: start)
        let metadata: [String: JSONValue] = [
            "id": .string(bucketName),
            "startedAt": .string(bucketName),
            "endedAt": .string(Self.bucketFormat.string(from: start.addingTimeInterval(TimeInterval(bucketWidthSeconds)))),
            "eventsPath": .string(dir.appendingPathComponent("events.jsonl").path),
            "eventCount": .number(Double(eventCount)),
            "suppressedEventCount": .number(Double(suppressedCount)),
        ]
        encoder.outputFormatting = [.prettyPrinted, .sortedKeys]
        let data = try encoder.encode(metadata)
        try (data + Data("\n".utf8)).write(to: dir.appendingPathComponent("metadata.json"))
        currentBucketStart = nil
        eventCount = 0
        suppressedCount = 0
        return dir
    }

    /// Flush the open bucket regardless of boundary (daemon shutdown path).
    @discardableResult
    func sealCurrentIfNeeded(now: Date) throws -> URL? {
        guard let current = currentBucketStart else { return nil }
        _ = now
        return try seal(current)
    }
}

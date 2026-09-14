import Foundation
import Darwin

/// Main-run-loop-only reader. No browser credentials or HTTP enter the recorder.
final class RecorderBrowserSnapshot {
    private struct Row: Decodable {
        let observedAt: Double
        let title: String
        let url: String
    }
    private struct Snapshot: Decodable {
        let version: Int
        let browsers: [String: Row]
    }
    private struct Identity: Equatable {
        let device: dev_t
        let inode: ino_t
        let size: off_t
        let modifiedSeconds: Int
        let modifiedNanos: Int
        let changedSeconds: Int
        let changedNanos: Int
        init(_ value: stat) {
            device = value.st_dev; inode = value.st_ino; size = value.st_size
            modifiedSeconds = value.st_mtimespec.tv_sec; modifiedNanos = value.st_mtimespec.tv_nsec
            changedSeconds = value.st_ctimespec.tv_sec; changedNanos = value.st_ctimespec.tv_nsec
        }
    }
    private let root: URL
    private var cached: Snapshot?
    private var identity: Identity?
    private var readAt = Date.distantPast
    private static let maximumBytes = 16_384
    private static let suffix = try! NSRegularExpression(pattern: #"^(?: 和另外 \d+ 个页面| and \d+ more pages?)? - "#)

    init(root: URL) { self.root = root }

    func enrich(window: [String: JSONValue]?, bundleIdentifier: String, at date: Date) -> [String: JSONValue]? {
        let browser: String
        switch bundleIdentifier {
        case "com.microsoft.edgemac": browser = "edge"
        case "com.google.Chrome": browser = "chrome"
        default: return window
        }
        let state = load(at: date)
        guard state.enabled else { return window }
        guard var window else { return nil }
        // Opt-in Chromium contexts never inherit raw AXURL on bridge failures.
        window.removeValue(forKey: "url")
        if case let .string(title)? = window["title"], let row = state.snapshot?.browsers[browser],
           row.observedAt.isFinite, date.timeIntervalSince1970.isFinite {
            let age = date.timeIntervalSince1970 - row.observedAt
            if age >= 0, age <= 8, Self.matches(windowTitle: title, pageTitle: row.title),
               let url = Self.sanitizedURL(row.url) { window["url"] = .string(url) }
        }
        return window.isEmpty ? nil : window
    }

    func tracksURLChanges(bundleIdentifier: String, at date: Date) -> Bool {
        guard bundleIdentifier == "com.microsoft.edgemac" || bundleIdentifier == "com.google.Chrome" else { return false }
        return load(at: date).enabled
    }

    private static func matches(windowTitle: String, pageTitle: String) -> Bool {
        guard !pageTitle.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty,
              windowTitle.hasPrefix(pageTitle) else { return false }
        let suffix = String(windowTitle.dropFirst(pageTitle.count))
        return suffix.isEmpty || self.suffix.firstMatch(in: suffix, range: NSRange(suffix.startIndex..., in: suffix)) != nil
    }

    private static func sanitizedURL(_ raw: String) -> String? {
        guard !raw.isEmpty, raw.utf8.count <= 8_192,
              !raw.unicodeScalars.contains(where: { CharacterSet.whitespacesAndNewlines.contains($0) || CharacterSet.controlCharacters.contains($0) }),
              var parts = URLComponents(string: raw),
              let scheme = parts.scheme?.lowercased(), ["http", "https"].contains(scheme),
              let host = parts.host, !host.isEmpty else { return nil }
        parts.scheme = scheme
        parts.user = nil; parts.password = nil; parts.query = nil; parts.fragment = nil
        return parts.url?.absoluteString
    }

    private func clear() { cached = nil; identity = nil; readAt = .distantPast }

    private func load(at date: Date) -> (enabled: Bool, snapshot: Snapshot?) {
        let directory = open(root.path, O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC)
        guard directory >= 0 else {
            let absent = errno == ENOENT
            clear()
            return (!absent, nil)
        }
        defer { close(directory) }
        // Check opt-in every time, even when the parsed snapshot is cached.
        let marker = openat(directory, "enabled", O_RDONLY | O_NONBLOCK | O_NOFOLLOW | O_CLOEXEC)
        guard marker >= 0 else {
            let absent = errno == ENOENT
            clear()
            return (!absent, nil)
        }
        defer { close(marker) }
        var markerStat = stat()
        var directoryStat = stat()
        guard fstat(directory, &directoryStat) == 0, directoryStat.st_uid == getuid(),
              directoryStat.st_mode & 0o077 == 0,
              fstat(marker, &markerStat) == 0, Self.privateRegular(markerStat) else {
            clear(); return (true, nil)
        }
        let descriptor = openat(directory, "snapshot.json", O_RDONLY | O_NONBLOCK | O_NOFOLLOW | O_CLOEXEC)
        guard descriptor >= 0 else { clear(); return (true, nil) }
        defer { close(descriptor) }
        var metadata = stat()
        guard fstat(descriptor, &metadata) == 0, Self.privateRegular(metadata),
              metadata.st_size > 0, metadata.st_size <= Self.maximumBytes else {
            clear(); return (true, nil)
        }
        let currentIdentity = Identity(metadata)
        let elapsed = date.timeIntervalSince(readAt)
        if currentIdentity == identity, elapsed >= 0, elapsed < 0.2 { return (true, cached) }
        var bytes = [UInt8](repeating: 0, count: Self.maximumBytes + 1)
        var count = 0
        while count < bytes.count {
            let capacity = bytes.count - count
            let readCount = bytes.withUnsafeMutableBytes { buffer in
                read(descriptor, buffer.baseAddress!.advanced(by: count), capacity)
            }
            if readCount < 0 {
                if errno == EINTR { continue }
                clear(); return (true, nil)
            }
            if readCount == 0 { break }
            count += readCount
        }
        var after = stat()
        guard count <= Self.maximumBytes, fstat(descriptor, &after) == 0,
              Self.privateRegular(after), Identity(after) == currentIdentity,
              let snapshot = try? JSONDecoder().decode(Snapshot.self, from: Data(bytes.prefix(count))),
              snapshot.version == 1 else { clear(); return (true, nil) }
        cached = snapshot; identity = currentIdentity; readAt = date
        return (true, snapshot)
    }

    private static func privateRegular(_ metadata: stat) -> Bool {
        metadata.st_mode & S_IFMT == S_IFREG && metadata.st_uid == getuid() &&
            metadata.st_mode & 0o077 == 0 && metadata.st_mode & 0o400 != 0
    }
}

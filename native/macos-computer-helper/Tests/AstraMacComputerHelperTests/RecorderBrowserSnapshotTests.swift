import Foundation
import CoreGraphics
import Testing
@testable import AstraMacComputerHelperCore

private func browserFixture(_ body: (URL, RecorderBrowserSnapshot) throws -> Void) throws {
    let root = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
    try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true, attributes: [.posixPermissions: 0o700])
    defer { try? FileManager.default.removeItem(at: root) }
    try Data().write(to: root.appendingPathComponent("enabled"))
    try FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: root.appendingPathComponent("enabled").path)
    try body(root, RecorderBrowserSnapshot(root: root))
}

private func publish(_ root: URL, _ json: String) throws {
    let path = root.appendingPathComponent("snapshot.json")
    try Data(json.utf8).write(to: path, options: .atomic)
    try FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: path.path)
}
private let browserJSON = #"{"version":1,"browsers":{"edge":{"observedAt":100,"title":"Page","url":"https://user:secret@example.org/path?q=secret#hash"},"chrome":{"observedAt":100,"title":"Page","url":"https://chrome.org/"}}}"#
private func enriched(_ reader: RecorderBrowserSnapshot, title: String = "Page", bundle: String = "com.microsoft.edgemac", time: Double = 100) -> [String: String]? {
    reader.enrich(window: ["title": .string(title), "url": .string("ax-unsafe")], bundleIdentifier: bundle, at: Date(timeIntervalSince1970: time))?.compactMapValues { if case let .string(value) = $0 { return value }; return nil }
}

@Test func browserSnapshotMatchingAndSanitation() throws {
    try browserFixture { root, reader in
        try publish(root, browserJSON)
        for title in ["Page", "Page - Microsoft Edge", "Page and 2 more pages - Profile", "Page 和另外 2 个页面 - Microsoft Edge"] {
            #expect(enriched(reader, title: title)?["url"] == "https://example.org/path")
        }
        for title in ["", "Other", "Page spoof", "Pages - Edge"] { #expect(enriched(reader, title: title)?["url"] == nil) }
        #expect(enriched(reader, bundle: "com.google.Chrome")?["url"] == "https://chrome.org/")
        #expect(enriched(reader, bundle: "com.apple.Safari")?["url"] == "ax-unsafe")
        #expect(enriched(reader, bundle: "com.microsoft.edgemac.beta")?["url"] == "ax-unsafe")
        #expect(reader.enrich(window: nil, bundleIdentifier: "com.microsoft.edgemac", at: Date(timeIntervalSince1970: 100)) == nil)
    }
}

@Test func browserSnapshotFreshnessRecheckedInsideCache() throws {
    try browserFixture { root, reader in
        try publish(root, browserJSON)
        #expect(enriched(reader, time: 108)?["url"] != nil)
        #expect(enriched(reader, time: 108.01)?["url"] == nil)
        #expect(enriched(reader, time: 99.99)?["url"] == nil)
        #expect(enriched(reader, time: .nan)?["url"] == nil)
    }
}

@Test func browserSnapshotFailClosedAndClear() throws {
    try browserFixture { root, reader in
        try publish(root, browserJSON)
        #expect(enriched(reader)?["url"] != nil)
        for invalid in [#"{"version":1,"browsers":{}}"#, "corrupt", browserJSON + String(repeating: " ", count: 16385), #"{"version":1,"browsers":{"edge":{"observedAt":NaN,"title":"Page","url":"https://example.org"}}}"#, browserJSON.replacingOccurrences(of: "https://", with: "file://"), browserJSON.replacingOccurrences(of: "\"version\":1", with: "\"version\":2")] {
            try publish(root, invalid)
            #expect(enriched(reader)?["url"] == nil)
        }
        try publish(root, browserJSON)
        try FileManager.default.setAttributes([.posixPermissions: 0], ofItemAtPath: root.appendingPathComponent("snapshot.json").path)
        #expect(enriched(reader)?["url"] == nil)
        try FileManager.default.removeItem(at: root.appendingPathComponent("snapshot.json"))
        try FileManager.default.createSymbolicLink(at: root.appendingPathComponent("snapshot.json"), withDestinationURL: root.appendingPathComponent("enabled"))
        #expect(enriched(reader)?["url"] == nil)
        try FileManager.default.removeItem(at: root.appendingPathComponent("enabled"))
        #expect(enriched(reader)?["url"] == "ax-unsafe")
    }
}

@Test func browserSnapshotDaemonEventContextsAndURLTransitions() throws {
    try browserFixture { root, reader in
        try publish(root, browserJSON)
        let activity = root.appendingPathComponent("activity")
        let writer = BucketWriter(root: activity)
        let edge = RecorderApplication(pid: 42, bundleIdentifier: "com.microsoft.edgemac", name: "Edge")
        let chrome = RecorderApplication(pid: 43, bundleIdentifier: "com.google.Chrome", name: "Chrome")
        var front = edge
        let window: [String: JSONValue] = ["title": .string("Page"), "url": .string("ax-unsafe")]
        let daemon = RecorderDaemon(writer: writer, blocklist: [],
            dependencies: RecorderDependencies(frontmostApplication: { front }, windowInfo: { _ in window }, focusedTarget: { _ in [:] }, clickTarget: { _, _ in [:] }),
            browserSnapshot: reader)
        let now = Date(timeIntervalSince1970: 100)
        let first = reader.enrich(window: window, bundleIdentifier: edge.bundleIdentifier, at: now)
        daemon.recordWindowChange(application: edge, window: first, appChanged: true, at: now)
        let click = try #require(CGEvent(mouseEventSource: nil, mouseType: .leftMouseDown, mouseCursorPosition: .zero, mouseButton: .left))
        daemon.handleCGEvent(type: .leftMouseDown, event: click, at: now)
        let key = try #require(CGEvent(keyboardEventSource: nil, virtualKey: 0, keyDown: true))
        key.flags = .maskCommand
        daemon.handleCGEvent(type: .keyDown, event: key, at: now)
        daemon.recordFocusedValue("typed", role: "AXTextArea", subrole: nil, application: edge, window: window, contextChanged: false, at: now)
        // A bridge-only change emits a window event but never manufactures a key.
        try publish(root, browserJSON.replacingOccurrences(of: "example.org/path", with: "example.org/next"))
        let next = reader.enrich(window: window, bundleIdentifier: edge.bundleIdentifier, at: now)
        daemon.recordWindowChange(application: edge, window: next, appChanged: false, at: now)
        daemon.recordWindowChange(application: edge, window: next, appChanged: false, at: now)
        try publish(root, #"{"version":1,"browsers":{}}"#)
        let cleared = reader.enrich(window: window, bundleIdentifier: edge.bundleIdentifier, at: now)
        daemon.recordWindowChange(application: edge, window: cleared, appChanged: false, at: now)
        front = chrome
        daemon.finishTextInput(at: now.addingTimeInterval(4))
        daemon.recordFocusedValue("scroll", role: "AXTextArea", subrole: nil, application: chrome, window: window, contextChanged: false, at: now.addingTimeInterval(5))
        daemon.finishTextInput(at: now.addingTimeInterval(9))
        let bucket = try #require(try writer.sealCurrentIfNeeded(now: now.addingTimeInterval(30)))
        let lines = try String(contentsOf: bucket.appendingPathComponent("events.jsonl"), encoding: .utf8).split(separator: "\n")
        let events = try lines.map { try JSONSerialization.jsonObject(with: Data($0.utf8)) as! [String: Any] }
        #expect(events.count == 6)
        #expect(events.filter { $0["kind"] as? String == "window.changed" }.count == 3)
        #expect(events.filter { $0["kind"] as? String == "keyboard.text_input" }.count == 1)
        for event in events {
            #expect((event["app"] as? [String: Any])?["bundleIdentifier"] as? String == edge.bundleIdentifier)
            let url = (event["window"] as? [String: Any])?["url"] as? String
            #expect(url == "https://example.org/path" || url == "https://example.org/next" || (url == nil && event["kind"] as? String == "window.changed"))
            if event["kind"] as? String == "keyboard.text_input" { #expect(url == "https://example.org/path") }
        }
    }
}

@Test func browserSnapshotDisabledAndSafariKeepTransitionBehavior() throws {
    try browserFixture { root, reader in
        let now = Date(timeIntervalSince1970: 100)
        let writer = BucketWriter(root: root.appendingPathComponent("activity"))
        let daemon = RecorderDaemon(writer: writer, blocklist: [], browserSnapshot: reader)
        let safari = RecorderApplication(pid: 1, bundleIdentifier: "com.apple.Safari", name: "Safari")
        let edge = RecorderApplication(pid: 2, bundleIdentifier: "com.microsoft.edgemac", name: "Edge")
        let first: [String: JSONValue] = ["title": .string("Page"), "url": .string("https://example.org/first")]
        let next: [String: JSONValue] = ["title": .string("Page"), "url": .string("https://example.org/next")]
        daemon.recordWindowChange(application: safari, window: first, appChanged: true, at: now)
        daemon.recordWindowChange(application: safari, window: next, appChanged: false, at: now)
        try FileManager.default.removeItem(at: root.appendingPathComponent("enabled"))
        daemon.recordWindowChange(application: edge, window: first, appChanged: true, at: now)
        daemon.recordWindowChange(application: edge, window: next, appChanged: false, at: now)
        let bucket = try #require(try writer.sealCurrentIfNeeded(now: now.addingTimeInterval(30)))
        let lines = try String(contentsOf: bucket.appendingPathComponent("events.jsonl"), encoding: .utf8).split(separator: "\n")
        #expect(lines.count == 2)
    }
}

@Test func browserSnapshotMarkerAndDirectorySymlinksFailClosed() throws {
    try browserFixture { root, reader in
        try publish(root, browserJSON)
        #expect(enriched(reader)?["url"] != nil)
        let marker = root.appendingPathComponent("enabled")
        try FileManager.default.removeItem(at: marker)
        try FileManager.default.createSymbolicLink(at: marker, withDestinationURL: root.appendingPathComponent("snapshot.json"))
        #expect(enriched(reader)?["url"] == nil)
        try FileManager.default.removeItem(at: marker)
        try Data().write(to: marker)
        try FileManager.default.setAttributes([.posixPermissions: 0], ofItemAtPath: marker.path)
        #expect(enriched(reader)?["url"] == nil)
        let alias = root.appendingPathComponent("alias")
        try FileManager.default.createSymbolicLink(at: alias, withDestinationURL: root)
        #expect(enriched(RecorderBrowserSnapshot(root: alias))?["url"] == nil)
    }
}

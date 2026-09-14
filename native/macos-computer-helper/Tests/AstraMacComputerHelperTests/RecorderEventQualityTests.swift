import Foundation
import CoreGraphics
@testable import AstraMacComputerHelperCore
import Testing

@Test func recorderHeadTailPreservesBottomInput() {
    let raw = "HEADER" + String(repeating: "汉🙂", count: 2_000) + "牛逼"
    let result = RecorderAXCapture.trimmedValue(raw)
    #expect(result.count == 2_000)
    #expect(result.hasPrefix("HEADER"))
    #expect(result.hasSuffix("牛逼"))
    #expect(result.contains("[truncated]"))
    #expect(RecorderAXCapture.trimmedValue("short") == "short")
}

@Test func recorderBurstKeepsLastKeyGatedSnapshot() {
    var burst = RecorderTextBurst<String>()
    let now = Date(timeIntervalSince1970: 100)
    burst.keyDown(scope: 1, at: now)
    for i in 0..<14 { burst.observe("value \(i)", scope: 1, at: now.addingTimeInterval(Double(i) / 10)) }
    #expect(burst.flush(at: now.addingTimeInterval(2)) == nil)
    #expect(burst.flush(at: now.addingTimeInterval(3.1)) == "value 13")
    #expect(burst.flush(at: now.addingTimeInterval(4)) == nil)
    burst.observe("scroll", scope: 1, at: now.addingTimeInterval(4))
    #expect(burst.flush(at: now.addingTimeInterval(8)) == nil)
}

@Test func recorderBurstRejectsAppSwitchAndCancellation() {
    var burst = RecorderTextBurst<String>()
    let now = Date(timeIntervalSince1970: 100)
    burst.keyDown(scope: 1, at: now)
    burst.observe("wrong app", scope: 2, at: now)
    #expect(burst.flush(at: now.addingTimeInterval(4)) == nil)
    burst.keyDown(scope: 1, at: now)
    burst.observe("allowed", scope: 1, at: now)
    burst.cancel()
    #expect(burst.flush(at: now.addingTimeInterval(4)) == nil)
}

@Test func recorderCommonKeyNames() {
    let expected = [48: "tab", 53: "escape", 76: "enter", 117: "forward-delete", 115: "home", 119: "end", 116: "pageup", 121: "pagedown", 122: "f1", 120: "f2", 99: "f3", 118: "f4", 96: "f5", 97: "f6", 98: "f7", 100: "f8", 101: "f9", 109: "f10", 103: "f11", 111: "f12", 105: "f13", 107: "f14", 113: "f15", 106: "f16", 64: "f17", 79: "f18", 80: "f19", 90: "f20"]
    for (code, name) in expected { #expect(RecorderKeyNames.name(code) == name) }
    #expect(RecorderKeyNames.name(999) == "k999")
}

@Test func recorderContainerDescriptorUsesOnlySpecificSafeChild() {
    let generic = ["role": "AXScrollArea"]
    #expect(RecorderClickDescriptor.refine(generic, children: { [["role": "AXButton", "title": "Save"]] })["title"] == "Save")
    var reads = 0
    let secure = ["role": "AXTextField", "subrole": "AXSecureTextField"]
    #expect(RecorderClickDescriptor.refine(secure, children: { reads += 1; return [] }) == secure)
    #expect(reads == 0)
    #expect(RecorderClickDescriptor.refine(generic, children: { [secure] }) == generic)
}

@Test func recorderNamedContainerDoesNotDescend() {
    let target = ["role": "AXGroup", "description": "toolbar"]
    var reads = 0
    #expect(RecorderClickDescriptor.refine(target, children: { reads += 1; return [] }) == target)
    #expect(reads == 0)
}

@Test func recorderBurstExtendsForRealKeysAndExpiresWithoutKeys() {
    var burst = RecorderTextBurst<String>()
    let now = Date(timeIntervalSince1970: 100)
    burst.keyDown(scope: 1, at: now)
    burst.observe("first", scope: 1, at: now)
    burst.keyDown(scope: 1, at: now.addingTimeInterval(2))
    burst.observe("last", scope: 1, at: now.addingTimeInterval(2.2))
    #expect(burst.flush(at: now.addingTimeInterval(4)) == nil)
    // A poll arriving after the real-key window must not replace the pending input.
    burst.observe("later scroll", scope: 1, at: now.addingTimeInterval(5.1))
    #expect(burst.flush(at: now.addingTimeInterval(5.1)) == "last")
}

private func recorderQualityFixture(_ run: (RecorderDaemon, Date, RecorderApplication) throws -> Void) throws -> [[String: Any]] {
    let root = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
    defer { try? FileManager.default.removeItem(at: root) }
    let writer = BucketWriter(root: root)
    let app = RecorderApplication(pid: 42, bundleIdentifier: "com.apple.Terminal", name: "Terminal")
    let daemon = RecorderDaemon(writer: writer, blocklist: SuppressionEngine.defaultBlocklist,
        dependencies: RecorderDependencies(frontmostApplication: { app }, windowInfo: { _ in nil }, focusedTarget: { _ in [:] }))
    let now = Date(timeIntervalSince1970: 1_788_573_600)
    try run(daemon, now, app)
    guard let bucket = try writer.sealCurrentIfNeeded(now: now.addingTimeInterval(30)) else { return [] }
    let contents = try String(contentsOf: bucket.appendingPathComponent("events.jsonl"), encoding: .utf8)
    return try contents.split(separator: "\n").map { try JSONSerialization.jsonObject(with: Data($0.utf8)) as! [String: Any] }
}

@Test func recorderDaemonBurstContractAndBottomText() throws {
    let events = try recorderQualityFixture { daemon, now, app in
        let key = try #require(CGEvent(keyboardEventSource: nil, virtualKey: 0, keyDown: true))
        daemon.handleCGEvent(type: .keyDown, event: key, at: now)
        for i in 0..<14 {
            daemon.recordFocusedValue(String(repeating: "screen", count: 500) + "牛逼 \(i)", role: "AXTextArea", subrole: nil,
                application: app, window: ["title": .string("TUI")], contextChanged: i == 0,
                at: now.addingTimeInterval(Double(i) / 10))
        }
        daemon.recordFocusedValue("scroll after expiry", role: "AXTextArea", subrole: nil,
            application: app, window: nil, contextChanged: false, at: now.addingTimeInterval(4))
    }
    #expect(events.count == 1)
    let event = try #require(events.first)
    #expect(event["kind"] as? String == "keyboard.text_input")
    let keyboard = try #require(event["keyboard"] as? [String: Any])
    let target = try #require(keyboard["target"] as? [String: Any])
    #expect((target["value"] as? String)?.hasSuffix("牛逼 13") == true)
    #expect((event["window"] as? [String: Any])?["title"] as? String == "TUI")
}

@Test func recorderDaemonScrollOnlyIsSilent() throws {
    let events = try recorderQualityFixture { daemon, now, app in
        for i in 0..<10 {
            daemon.recordFocusedValue("scroll \(i)", role: "AXTextArea", subrole: nil,
                application: app, window: nil, contextChanged: false, at: now.addingTimeInterval(Double(i)))
        }
    }
    #expect(events.isEmpty)
}

@Test func recorderDaemonTabChordContract() throws {
    let events = try recorderQualityFixture { daemon, now, _ in
        let key = try #require(CGEvent(keyboardEventSource: nil, virtualKey: 48, keyDown: true))
        key.flags = .maskCommand
        daemon.handleCGEvent(type: .keyDown, event: key, at: now)
    }
    #expect((events.first?["keyboard"] as? [String: Any])?["keyEquivalent"] as? String == "cmd+tab")
}

@Test func recorderDaemonAllowedContextSwitchFlushesOriginalContext() throws {
    let events = try recorderQualityFixture { daemon, now, app in
        let key = try #require(CGEvent(keyboardEventSource: nil, virtualKey: 0, keyDown: true))
        daemon.handleCGEvent(type: .keyDown, event: key, at: now)
        daemon.recordFocusedValue("typed", role: "AXTextArea", subrole: nil,
            application: app, window: ["title": .string("old")], contextChanged: false, at: now)
        let other = RecorderApplication(pid: 43, bundleIdentifier: "com.apple.finder", name: "Finder")
        daemon.recordFocusedValue("unrelated", role: "AXTextArea", subrole: nil,
            application: other, window: ["title": .string("new")], contextChanged: true, at: now.addingTimeInterval(1))
    }
    #expect(events.count == 1)
    #expect((events.first?["app"] as? [String: Any])?["name"] as? String == "Terminal")
    #expect((events.first?["window"] as? [String: Any])?["title"] as? String == "old")
}

@Test func recorderDaemonSecureAndBlockedContextsCancelPending() throws {
    for blocked in [true, false] {
        let events = try recorderQualityFixture { daemon, now, app in
            let key = try #require(CGEvent(keyboardEventSource: nil, virtualKey: 0, keyDown: true))
            daemon.handleCGEvent(type: .keyDown, event: key, at: now)
            daemon.recordFocusedValue("typed", role: "AXTextArea", subrole: nil,
                application: app, window: nil, contextChanged: false, at: now)
            let next = blocked ? RecorderApplication(pid: 43, bundleIdentifier: "com.apple.Passwords", name: "Passwords") : app
            daemon.recordFocusedValue("secret", role: "AXTextField", subrole: blocked ? nil : "AXSecureTextField",
                application: next, window: nil, contextChanged: true, at: now.addingTimeInterval(1))
            daemon.finishTextInput(at: now.addingTimeInterval(4))
        }
        #expect(events.isEmpty)
    }
}

@Test func recorderDaemonFreshKeyInChangedWindowIsNotDiscarded() throws {
    let events = try recorderQualityFixture { daemon, now, app in
        daemon.recordFocusedValue("old window", role: "AXTextArea", subrole: nil,
            application: app, window: nil, contextChanged: false, at: now)
        let key = try #require(CGEvent(keyboardEventSource: nil, virtualKey: 0, keyDown: true))
        daemon.handleCGEvent(type: .keyDown, event: key, at: now.addingTimeInterval(1))
        daemon.recordFocusedValue("new typed value", role: "AXTextArea", subrole: nil,
            application: app, window: ["title": .string("new")], contextChanged: true, at: now.addingTimeInterval(1.1))
        daemon.finishTextInput(at: now.addingTimeInterval(2))
    }
    #expect(events.count == 1)
    #expect((events.first?["window"] as? [String: Any])?["title"] as? String == "new")
}

@Test func recorderSuppressedKeyCannotRearmOnReturnToAllowedApp() throws {
    let root = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
    defer { try? FileManager.default.removeItem(at: root) }
    let writer = BucketWriter(root: root)
    let allowed = RecorderApplication(pid: 42, bundleIdentifier: "com.apple.Terminal", name: "Terminal")
    var current = allowed
    let daemon = RecorderDaemon(writer: writer, blocklist: SuppressionEngine.defaultBlocklist,
        dependencies: RecorderDependencies(frontmostApplication: { current }))
    let now = Date(timeIntervalSince1970: 1_788_573_600)
    daemon.recordFocusedValue("baseline", role: "AXTextArea", subrole: nil, application: allowed,
        window: nil, contextChanged: false, at: now)
    let key = try #require(CGEvent(keyboardEventSource: nil, virtualKey: 0, keyDown: true))
    daemon.handleCGEvent(type: .keyDown, event: key, at: now.addingTimeInterval(1))
    current = RecorderApplication(pid: 43, bundleIdentifier: "com.apple.Passwords", name: "Passwords")
    daemon.handleCGEvent(type: .keyDown, event: key, at: now.addingTimeInterval(1.1))
    current = allowed
    daemon.recordFocusedValue("scroll without a new key", role: "AXTextArea", subrole: nil,
        application: allowed, window: nil, contextChanged: true, at: now.addingTimeInterval(1.2))
    daemon.finishTextInput(at: now.addingTimeInterval(4))
    let bucket = try writer.sealCurrentIfNeeded(now: now.addingTimeInterval(4))
    if let bucket {
        let contents = try String(contentsOf: bucket.appendingPathComponent("events.jsonl"), encoding: .utf8)
        #expect(!contents.contains("keyboard.text_input"))
    }
}

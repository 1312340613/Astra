import CoreGraphics
import Foundation
@testable import AstraMacComputerHelperCore
import Testing

private func tempRoot() -> URL {
    let dir = FileManager.default.temporaryDirectory
        .appendingPathComponent("astra-recorder-test-\(UUID().uuidString)")
    try! FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
    return dir
}

private func makeEvent(_ writer: BucketWriter, kind: String = "window.changed", date: Date) -> RecorderEvent {
    RecorderEvent(
        id: writer.allocateEventID(), kind: kind,
        timestamp: BucketWriter.timestamp(for: date),
        app: ["bundleIdentifier": .string("com.apple.Terminal"), "name": .string("Terminal")],
        window: ["title": .string("test — window")],
        body: ("ax", .object(["mode": .string("fullTree"), "text": .string("0 窗口 test")]))
    )
}

@Test("bucket names are UTC 10-minute aligned")
func bucketAlignment() {
    let writer = BucketWriter(root: tempRoot())
    let date = Date(timeIntervalSince1970: 1_788_573_600) // 2026-09-05T02:00:00Z
    #expect(BucketWriter.bucketName(for: date, widthSeconds: 600) == "2026-09-05T02-00-00Z")
    let later = date.addingTimeInterval(601)
    #expect(BucketWriter.bucketName(for: later, widthSeconds: 600) == "2026-09-05T02-10-00Z")
}

@Test("event timestamps are ISO8601 UTC seconds")
func eventTimestamp() {
    let date = Date(timeIntervalSince1970: 1_788_573_661)
    #expect(BucketWriter.timestamp(for: date) == "2026-09-05T02:01:01Z")
}

@Test("writing rotates at bucket boundary and seals the old bucket")
func rotationSealsOldBucket() throws {
    let root = tempRoot()
    let writer = BucketWriter(root: root)
    let start = Date(timeIntervalSince1970: 1_788_573_600)
    try writer.write(makeEvent(writer, date: start), at: start)
    try writer.write(makeEvent(writer, kind: "mouse.click", date: start.addingTimeInterval(599)), at: start.addingTimeInterval(599))
    #expect(writer.currentBucketStart != nil)
    // crossing into the next 10-minute bucket seals the previous one
    let sealed = try writer.write(makeEvent(writer, date: start.addingTimeInterval(700)), at: start.addingTimeInterval(700))
    #expect(sealed != nil)
    let metadata = try #require(sealed).appendingPathComponent("metadata.json")
    let parsed = try JSONSerialization.jsonObject(with: Data(contentsOf: metadata)) as? [String: Any]
    #expect(parsed?["eventCount"] as? Int == 2)
    #expect(parsed?["suppressedEventCount"] as? Int == 0)
    let events = try String(contentsOf: sealed!.appendingPathComponent("events.jsonl"), encoding: .utf8)
    #expect(events.split(separator: "\n").count == 2)
}

@Test("sealCurrentIfNeeded flushes on shutdown")
func shutdownFlush() throws {
    let root = tempRoot()
    let writer = BucketWriter(root: root)
    let start = Date(timeIntervalSince1970: 1_788_573_600)
    try writer.write(makeEvent(writer, date: start), at: start)
    let sealed = try writer.sealCurrentIfNeeded(now: start.addingTimeInterval(30))
    let dir = try #require(sealed)
    #expect(FileManager.default.fileExists(atPath: dir.appendingPathComponent("metadata.json").path))
    #expect(writer.currentBucketStart == nil)
}

@Test("suppressed events are counted into metadata")
func suppressedCounting() throws {
    let root = tempRoot()
    let writer = BucketWriter(root: root)
    let start = Date(timeIntervalSince1970: 1_788_573_600)
    writer.suppressOne()
    writer.suppressOne()
    try writer.write(makeEvent(writer, date: start), at: start)
    let dir = try #require(try writer.sealCurrentIfNeeded(now: start))
    let parsed = try JSONSerialization.jsonObject(with: Data(contentsOf: dir.appendingPathComponent("metadata.json"))) as? [String: Any]
    #expect(parsed?["suppressedEventCount"] as? Int == 2)
}

@Test("suppression: wechat and password surfaces blocked, secure fields dropped")
func suppressionRules() {
    #expect(SuppressionEngine.suppresses(bundleID: "com.tencent.xinWeChat", blocklist: SuppressionEngine.defaultBlocklist))
    #expect(SuppressionEngine.suppresses(bundleID: "com.apple.Passwords", blocklist: SuppressionEngine.defaultBlocklist))
    #expect(!SuppressionEngine.suppresses(bundleID: "com.microsoft.edgemac", blocklist: SuppressionEngine.defaultBlocklist))
    #expect(SuppressionEngine.suppressesSecureField(role: "AXSecureTextField"))
    #expect(SuppressionEngine.suppressesSecureField(role: "AXTextField", subrole: "AXSecureTextField"))
    #expect(!SuppressionEngine.suppressesSecureField(role: "AXTextArea"))
}

@Test("secure recorder fields are identified before sensitive attributes are read")
func secureRecorderFieldsDoNotReadSensitiveAttributes() {
    var reads: [String] = []
    let captured = RecorderAXCapture.capture(
        role: "AXTextField",
        subrole: "AXSecureTextField",
        sensitiveAttributes: [
            "description": { reads.append("description"); return "secret description" },
            "title": { reads.append("title"); return "secret title" },
            "value": { reads.append("value"); return "secret value" },
            "selection": { reads.append("selection"); return "secret selection" },
            "url": { reads.append("url"); return "secret url" },
        ]
    )
    #expect(reads.isEmpty)
    #expect(captured == ["role": "AXTextField", "subrole": "AXSecureTextField"])

    let allowed = RecorderAXCapture.capture(
        role: "AXTextArea",
        subrole: nil,
        sensitiveAttributes: ["value": { "allowed" }, "selection": { "selected" }]
    )
    #expect(allowed["value"] == "allowed")
    #expect(allowed["selection"] == "selected")
    #expect(RecorderAXCapture.trimmedValue(String(repeating: "x", count: 2_001)).count == 2_000)
}

@Test("click target rejects a cross-process hit before sensitive AX reads")
func clickTargetBindsHitToAllowedApplication() {
    var sensitiveReads = 0
    let rejected = RecorderAXCapture.captureHitTarget(
        expectedPID: 42,
        hitPID: 99,
        role: "AXButton",
        subrole: nil,
        sensitiveAttributes: ["title": { sensitiveReads += 1; return "blocked app title" }]
    )
    #expect(rejected.isEmpty)
    #expect(sensitiveReads == 0)

    let allowed = RecorderAXCapture.captureHitTarget(
        expectedPID: 42,
        hitPID: 42,
        role: "AXButton",
        subrole: nil,
        sensitiveAttributes: ["title": { sensitiveReads += 1; return "allowed app title" }]
    )
    #expect(allowed["title"] == "allowed app title")
    #expect(sensitiveReads == 1)
}

@Test("blocked callback routes stop before AX reads while allowed shortcuts are recorded")
func callbackRoutesApplyBlocklistBeforeAXReads() throws {
    let root = tempRoot()
    let writer = BucketWriter(root: root)
    var windowReads = 0
    var focusReads = 0
    var hitTestPoints: [CGPoint] = []
    var app = RecorderApplication(pid: 7, bundleIdentifier: "com.apple.Passwords", name: "Passwords")
    let dependencies = RecorderDependencies(
        frontmostApplication: { app },
        windowInfo: { _ in windowReads += 1; return ["title": .string("secret")] },
        focusedTarget: { _ in focusReads += 1; return ["value": .string("secret")] },
        clickTarget: { point, _ in hitTestPoints.append(point); return ["title": .string("secret")] }
    )
    let daemon = RecorderDaemon(writer: writer, blocklist: SuppressionEngine.defaultBlocklist, dependencies: dependencies)
    let shortcut = try #require(CGEvent(keyboardEventSource: nil, virtualKey: 8, keyDown: true))
    shortcut.flags = .maskCommand
    daemon.handleCGEvent(type: .keyDown, event: shortcut)
    let click = try #require(CGEvent(mouseEventSource: nil, mouseType: .leftMouseDown, mouseCursorPosition: CGPoint(x: 123, y: 456), mouseButton: .left))
    daemon.handleCGEvent(type: .leftMouseDown, event: click)
    #expect(windowReads == 0)
    #expect(focusReads == 0)
    #expect(hitTestPoints.isEmpty)

    app = RecorderApplication(pid: 8, bundleIdentifier: "com.apple.Terminal", name: "Terminal")
    daemon.handleCGEvent(type: .keyDown, event: shortcut)
    let dir = try #require(try writer.sealCurrentIfNeeded(now: Date()))
    let events = try String(contentsOf: dir.appendingPathComponent("events.jsonl"), encoding: .utf8)
    #expect(events.contains("keyboard.shortcut"))
    #expect(focusReads == 1)
    #expect(windowReads == 1)
}

@Test("click hit testing receives CGEvent top-left coordinates unchanged")
func clickCoordinatesAreNotFlipped() throws {
    let writer = BucketWriter(root: tempRoot())
    var received: CGPoint?
    let dependencies = RecorderDependencies(
        frontmostApplication: { RecorderApplication(pid: 8, bundleIdentifier: "com.apple.Terminal", name: "Terminal") },
        clickTarget: { point, _ in received = point; return [:] }
    )
    let daemon = RecorderDaemon(writer: writer, blocklist: [], dependencies: dependencies)
    let event = try #require(CGEvent(mouseEventSource: nil, mouseType: .leftMouseDown, mouseCursorPosition: CGPoint(x: 123, y: 456), mouseButton: .left))
    daemon.handleCGEvent(type: .leftMouseDown, event: event)
    #expect(received == CGPoint(x: 123, y: 456))
}

@Test("event tap disabled notifications re-enable the installed tap")
func disabledEventTapIsReenabled() throws {
    var enableCount = 0
    let daemon = RecorderDaemon(
        writer: BucketWriter(root: tempRoot()),
        blocklist: [],
        dependencies: RecorderDependencies(enableEventTap: { enableCount += 1 })
    )
    let event = try #require(CGEvent(source: nil))
    daemon.handleCGEvent(type: .tapDisabledByTimeout, event: event)
    daemon.handleCGEvent(type: .tapDisabledByUserInput, event: event)
    #expect(enableCount == 2)
}

@Test("bootstrap resumes id watermark and seals stale orphan buckets")
func bootstrapRecovery() throws {
    let root = tempRoot()
    let start = Date(timeIntervalSince1970: 1_788_573_600) // 2026-09-05T02:00:00Z
    // 模拟前一个进程：留下未封的 02:00 桶（写到 id=7 后崩了）
    let previous = BucketWriter(root: root)
    for i in 0..<7 {
        try previous.write(makeEvent(previous, date: start.addingTimeInterval(Double(i) * 30)), at: start.addingTimeInterval(Double(i) * 30))
    }
    // 新进程启动：bootstrap 应封旧桶并把 id 续到 8
    let revived = BucketWriter(root: root)
    try revived.bootstrap(now: start.addingTimeInterval(900)) // 02:15
    let orphanDir = root.appendingPathComponent("segments/2026-09-05T02-00-00Z")
    #expect(FileManager.default.fileExists(atPath: orphanDir.appendingPathComponent("metadata.json").path))
    let event = makeEvent(revived, date: start.addingTimeInterval(960))
    #expect(event.id == 8)
}

@Test("event JSON keeps the contract's top-level keys")
func eventEncodingShape() throws {
    let writer = BucketWriter(root: tempRoot())
    let event = makeEvent(writer, date: Date(timeIntervalSince1970: 1_757_047_200))
    let encoder = JSONEncoder()
    encoder.outputFormatting = [.sortedKeys]
    let object = try JSONSerialization.jsonObject(with: encoder.encode(event)) as? [String: Any]
    let keys = Set(object?.keys ?? Dictionary<String, Any>().keys)
    #expect(keys.isSuperset(of: ["id", "kind", "timestamp", "app", "window", "ax"]))
}

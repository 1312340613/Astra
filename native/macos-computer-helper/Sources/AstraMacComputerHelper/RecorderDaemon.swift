import AppKit
import ApplicationServices

// Recorder: listen-only CGEventTap (Input Monitoring) plus AX polling
// (Accessibility). Both grants and the actual tap mask must be checked.
//
// Env: ASTRA_ACTIVITY_ROOT (default ~/Library/Application Support/Astra/activity)
//      ASTRA_ACTIVITY_SMOKE=<seconds>  bounded run for development/self-test.

private func axCopy(_ element: AXUIElement, _ attribute: String) -> AnyObject? {
    var value: AnyObject?
    guard AXUIElementCopyAttributeValue(element, attribute as CFString, &value) == .success else {
        return nil
    }
    return value
}

private func axString(_ element: AXUIElement, _ attribute: String) -> String? {
    axCopy(element, attribute) as? String
}

struct RecorderApplication {
    let pid: pid_t
    let bundleIdentifier: String
    let name: String
}

struct RecorderDependencies {
    var frontmostApplication: () -> RecorderApplication? = {
        guard let app = NSWorkspace.shared.frontmostApplication else { return nil }
        return RecorderApplication(
            pid: app.processIdentifier,
            bundleIdentifier: app.bundleIdentifier ?? "",
            name: app.localizedName ?? "unknown"
        )
    }
    var windowInfo: (pid_t) -> [String: JSONValue]? = { pid in
        let axApp = AXUIElementCreateApplication(pid)
        guard let focusedWindow = axCopy(axApp, kAXFocusedWindowAttribute) else { return nil }
        let element = focusedWindow as! AXUIElement
        var window: [String: JSONValue] = [:]
        if let title = axString(element, kAXTitleAttribute) { window["title"] = .string(title) }
        if let url = axString(element, "AXURL") { window["url"] = .string(String(url.prefix(500))) }
        return window.isEmpty ? nil : window
    }
    var focusedTarget: (pid_t) -> [String: JSONValue] = { pid in
        let axApp = AXUIElementCreateApplication(pid)
        guard let focused = axCopy(axApp, kAXFocusedUIElementAttribute) else { return [:] }
        let element = focused as! AXUIElement
        let role = axString(element, kAXRoleAttribute)
        let subrole = axString(element, kAXSubroleAttribute)
        let captured = RecorderAXCapture.capture(
            role: role,
            subrole: subrole,
            sensitiveAttributes: [
                "description": { axString(element, kAXDescriptionAttribute).map { String($0.prefix(120)) } },
                "title": { axString(element, kAXTitleAttribute).map { String($0.prefix(120)) } },
                "value": { axString(element, kAXValueAttribute).map(RecorderAXCapture.trimmedValue) },
            ]
        )
        return captured.mapValues(JSONValue.string)
    }
    var clickTarget: (CGPoint, pid_t) -> [String: JSONValue] = { point, expectedPID in
        var element: AXUIElement?
        guard AXUIElementCopyElementAtPosition(
            AXUIElementCreateSystemWide(), Float(point.x), Float(point.y), &element
        ) == .success, let hit = element else { return [:] }
        var hitPID = pid_t()
        guard AXUIElementGetPid(hit, &hitPID) == .success else { return [:] }
        let role = axString(hit, kAXRoleAttribute)
        let subrole = axString(hit, kAXSubroleAttribute)
        let captured = RecorderAXCapture.captureHitTarget(
            expectedPID: expectedPID,
            hitPID: hitPID,
            role: role,
            subrole: subrole,
            sensitiveAttributes: [
                "description": { axString(hit, kAXDescriptionAttribute).map { String($0.prefix(120)) } },
                "title": { axString(hit, kAXTitleAttribute).map { String($0.prefix(120)) } },
            ]
        )
        let refined = RecorderClickDescriptor.refine(captured) {
            // One bounded level, and only children actually containing the hit.
            // Reject cross-process and secure children before any content reads.
            guard hitPID == expectedPID,
                  let children = axCopy(hit, kAXChildrenAttribute) as? [AXUIElement] else { return [] }
            return children.prefix(20).compactMap { child in
                var childPID = pid_t()
                guard AXUIElementGetPid(child, &childPID) == .success, childPID == expectedPID,
                      let position = axCopy(child, kAXPositionAttribute),
                      let size = axCopy(child, kAXSizeAttribute),
                      CFGetTypeID(position) == AXValueGetTypeID(),
                      CFGetTypeID(size) == AXValueGetTypeID() else { return nil }
                var origin = CGPoint.zero
                var dimensions = CGSize.zero
                guard AXValueGetValue(position as! AXValue, .cgPoint, &origin),
                      AXValueGetValue(size as! AXValue, .cgSize, &dimensions),
                      CGRect(origin: origin, size: dimensions).contains(point) else { return nil }
                return RecorderAXCapture.captureHitTarget(
                    expectedPID: expectedPID, hitPID: childPID,
                    role: axString(child, kAXRoleAttribute), subrole: axString(child, kAXSubroleAttribute),
                    sensitiveAttributes: [
                        "description": { axString(child, kAXDescriptionAttribute).map { String($0.prefix(120)) } },
                        "title": { axString(child, kAXTitleAttribute).map { String($0.prefix(120)) } },
                    ])
            }
        }
        return refined.mapValues(JSONValue.string)
    }
    var enableEventTap: () -> Void = {}
}

final class RecorderDaemon {
    private let writer: BucketWriter
    private let blocklist: Set<String>
    private let dependencies: RecorderDependencies
    private let browserSnapshot: RecorderBrowserSnapshot?
    private(set) var stopping = false
    private var lastAppPID: pid_t = -1
    private var lastWindowTitle: String?
    private var lastWindowURL: String?
    private var lastFocusedValue: String?
    private var lastSelection: String?
    // Keystroke-driven text_input: a burst marks "user typed"; the value
    // snapshot is emitted only while a burst is fresh, killing the old
    // screen-scroll false positives (202/10min → target ~12 like upstream).
    private struct TextSnapshot {
        let app: [String: JSONValue]
        let window: [String: JSONValue]?
        let target: [String: JSONValue]
    }
    private var textBurst = RecorderTextBurst<TextSnapshot>()
    private var burstAppPID: pid_t?
    private var lastKeyDownAt = Date.distantPast
    private var lastInputPollAt = Date.distantPast
    private var lastFocusedElement: AXUIElement?
    private var lastShortcutKey: String?
    private var lastShortcutAt = Date.distantPast

    init(writer: BucketWriter, blocklist: Set<String>, dependencies: RecorderDependencies = RecorderDependencies(),
         browserSnapshot: RecorderBrowserSnapshot? = nil) {
        self.writer = writer
        self.blocklist = blocklist
        self.dependencies = dependencies
        self.browserSnapshot = browserSnapshot
    }

    func stop() { stopping = true }

    // MARK: emission

    private func emit(kind: String, app: [String: JSONValue], window: [String: JSONValue]?, body: (field: String, value: JSONValue)?, at date: Date) {
        let event = RecorderEvent(
            id: writer.allocateEventID(), kind: kind,
            timestamp: BucketWriter.timestamp(for: date),
            app: app, window: window, body: body
        )
        try? writer.write(event, at: date)
    }

    func emitSession(kind: String, at date: Date, app: [String: JSONValue]) {
        emit(kind: kind, app: app, window: nil, body: nil, at: date)
    }

    private func appInfo(_ application: RecorderApplication) -> [String: JSONValue] {
        var info: [String: JSONValue] = ["name": .string(application.name)]
        if !application.bundleIdentifier.isEmpty {
            info["bundleIdentifier"] = .string(application.bundleIdentifier)
        }
        return info
    }

    private func appInfo(_ application: NSRunningApplication) -> [String: JSONValue] {
        appInfo(RecorderApplication(
            pid: application.processIdentifier,
            bundleIdentifier: application.bundleIdentifier ?? "",
            name: application.localizedName ?? "unknown"
        ))
    }

    private func allowedFrontmostApplication() -> RecorderApplication? {
        guard let application = dependencies.frontmostApplication() else { return nil }
        guard !SuppressionEngine.suppresses(bundleID: application.bundleIdentifier, blocklist: blocklist) else {
            writer.suppressOne()
            cancelTextInput()
            return nil
        }
        return application
    }

    private func trimmedValue(_ raw: String) -> JSONValue {
        .string(RecorderAXCapture.trimmedValue(raw))
    }

    // MARK: monitors (M3) — CGEventTap, NOT NSEvent global monitors.
    //
    // addGlobalMonitorForEvents silently never fires in a launchd-run command
    // line tool with no NSApplication event dispatch. A listen-only session
    // event tap is the canonical daemon-side observer; keyboard events require
    // Input Monitoring (already granted), and the tap needs the main run loop,
    // which the poll loop pumps.

    private var eventTap: CFMachPort?
    private var eventTapSource: CFRunLoopSource?
    private var tapHealth = RecorderTapHealth()
    private var lastTapCheck = Date.distantPast
    private var lastTapDiagnostic: String?

    private func removeEventTap() {
        if let source = eventTapSource {
            CFRunLoopRemoveSource(CFRunLoopGetCurrent(), source, .commonModes)
            CFRunLoopSourceInvalidate(source)
        }
        if let tap = eventTap { CFMachPortInvalidate(tap) }
        eventTapSource = nil
        eventTap = nil
    }

    private func tapIncludesKeyboard() -> Bool {
        var count: UInt32 = 0
        guard CGGetEventTapList(0, nil, &count) == .success, count > 0 else { return false }
        var taps = [CGEventTapInformation](repeating: CGEventTapInformation(), count: Int(count))
        guard CGGetEventTapList(count, &taps, &count) == .success else { return false }
        return taps.prefix(Int(count)).contains {
            $0.tappingProcess == getpid() && $0.tapPoint == .cgSessionEventTap &&
                ($0.eventsOfInterest & (1 << CGEventType.keyDown.rawValue)) != 0
        }
    }

    func maintainEventTap(at date: Date, axTrusted: Bool) {
        guard !stopping, date.timeIntervalSince(lastTapCheck) >= 2 else { return }
        lastTapCheck = date
        let listening = CGPreflightListenEventAccess()
        if !axTrusted || !listening { cancelTextInput() }
        let installed = eventTap.map { CFMachPortIsValid($0) } ?? false
        let keyboard = installed && tapIncludesKeyboard()
        let enabled = eventTap.map { CGEvent.tapIsEnabled(tap: $0) } ?? false
        let diagnostic = "inputMonitoring=\(listening) tapInstalled=\(installed) keyboardIncluded=\(keyboard) tapEnabled=\(enabled)"
        if diagnostic != lastTapDiagnostic {
            FileHandle.standardError.write(Data("recorder: \(diagnostic)\n".utf8))
            lastTapDiagnostic = diagnostic
        }
        switch tapHealth.action(at: date, permitted: axTrusted && listening,
                                installed: installed, keyboardIncluded: keyboard, enabled: enabled) {
        case .none: break
        case .install:
            cancelTextInput()
            installMonitors(axTrusted: axTrusted)
        case .enable:
            cancelTextInput()
            if let tap = eventTap { CGEvent.tapEnable(tap: tap, enable: true) }
        }
    }

    func installMonitors(axTrusted: Bool) {
        guard axTrusted else { return }
        removeEventTap()
        let mask: CGEventMask =
            (1 << CGEventType.keyDown.rawValue) |
            (1 << CGEventType.flagsChanged.rawValue) |
            (1 << CGEventType.leftMouseDown.rawValue) |
            (1 << CGEventType.rightMouseDown.rawValue) |
            (1 << CGEventType.otherMouseDown.rawValue)
        let refcon = Unmanaged.passUnretained(self).toOpaque()
        guard let tap = CGEvent.tapCreate(
            tap: .cgSessionEventTap, place: .headInsertEventTap,
            options: .listenOnly, eventsOfInterest: mask,
            callback: { _, type, event, refcon in
                if let refcon {
                    Unmanaged<RecorderDaemon>.fromOpaque(refcon).takeUnretainedValue()
                        .handleCGEvent(type: type, event: event)
                }
                return Unmanaged.passUnretained(event)
            }, userInfo: refcon
        ) else {
            FileHandle.standardError.write("recorder: event tap create failed (needs Input Monitoring)\n".data(using: .utf8)!)
            return
        }
        eventTap = tap
        let source = CFMachPortCreateRunLoopSource(kCFAllocatorDefault, tap, 0)
        eventTapSource = source
        CFRunLoopAddSource(CFRunLoopGetCurrent(), source, .commonModes)
        CGEvent.tapEnable(tap: tap, enable: true)
    }

    func handleCGEvent(type: CGEventType, event: CGEvent, at now: Date = Date()) {
        guard !stopping else { return }
        if type == .tapDisabledByTimeout || type == .tapDisabledByUserInput {
            cancelTextInput()
            if let eventTap { CGEvent.tapEnable(tap: eventTap, enable: true) }
            dependencies.enableEventTap()
            return
        }
        switch type {
        case .keyDown:
            guard let application = allowedFrontmostApplication() else {
                cancelTextInput()
                return
            }
            flushTextInput(at: now, force: burstAppPID != application.pid)
            burstAppPID = application.pid
            lastKeyDownAt = now
            textBurst.keyDown(scope: application.pid, at: now)
            let flags = event.flags.intersection([.maskCommand, .maskControl, .maskAlternate])
            if flags.contains(.maskCommand), event.getIntegerValueField(.keyboardEventKeycode) != 56 {
                recordShortcut(event: event, flags: flags, application: application, now: now)
            }
        case .leftMouseDown, .rightMouseDown, .otherMouseDown:
            recordClick(type: type, event: event, now: now)
        default:
            break
        }
    }

    private func recordShortcut(event: CGEvent, flags: CGEventFlags, application: RecorderApplication, now: Date) {
        let keyCode = Int(event.getIntegerValueField(.keyboardEventKeycode))
        let key = RecorderKeyNames.name(keyCode)
        if key == lastShortcutKey, now.timeIntervalSince(lastShortcutAt) <= 2 { return }
        lastShortcutKey = key
        lastShortcutAt = now
        var chord = ""
        if flags.contains(.maskCommand) { chord += "cmd+" }
        if flags.contains(.maskControl) { chord += "ctrl+" }
        if flags.contains(.maskAlternate) { chord += "opt+" }
        chord += key
        let body: JSONValue = .object([
            "keyEquivalent": .string(chord),
            "target": .object(dependencies.focusedTarget(application.pid)),
        ])
        emit(kind: "keyboard.shortcut", app: appInfo(application), window: enrichedWindow(dependencies.windowInfo(application.pid), application: application, at: now), body: ("keyboard", body), at: now)
    }

    private func recordClick(type: CGEventType, event: CGEvent, now: Date) {
        guard let application = allowedFrontmostApplication() else { return }
        let point = event.location
        let target = dependencies.clickTarget(point, application.pid)
        let button: String
        switch type {
        case .rightMouseDown: button = "right"
        case .otherMouseDown: button = "other"
        default: button = "left"
        }
        let body: JSONValue = .object(["button": .string(button), "target": .object(target)])
        emit(kind: "mouse.click", app: appInfo(application), window: enrichedWindow(dependencies.windowInfo(application.pid), application: application, at: now), body: ("mouse", body), at: now)
    }

    private func cancelTextInput() {
        textBurst.cancel()
        // Suppression revokes the real-key eligibility as well as its snapshot.
        // A later context transition must wait for a new allowed key.
        lastKeyDownAt = .distantPast
        burstAppPID = nil
    }

    private func flushTextInput(at date: Date, force: Bool = false) {
        guard let snapshot = textBurst.flush(at: date, force: force) else { return }
        // Use flush time so a burst crossing a bucket boundary cannot reopen
        // an already sealed bucket. Context is from the captured snapshot.
        emit(kind: "keyboard.text_input", app: snapshot.app, window: snapshot.window,
             body: ("keyboard", .object(["target": .object(snapshot.target)])), at: date)
    }

    func finishTextInput(at date: Date) {
        guard allowedFrontmostApplication() != nil else { cancelTextInput(); return }
        flushTextInput(at: date, force: true)
    }

    /// Shared by live AX polling and deterministic event-stream tests.
    func recordFocusedValue(_ value: String?, role: String?, subrole: String?,
                            application: RecorderApplication, window: [String: JSONValue]?,
                            contextChanged: Bool, at date: Date) {
        defer { lastInputPollAt = date }
        guard !SuppressionEngine.suppresses(bundleID: application.bundleIdentifier, blocklist: blocklist),
              !SuppressionEngine.suppressesSecureField(role: role, subrole: subrole) else {
            writer.suppressOne()
            cancelTextInput()
            lastFocusedValue = nil
            return
        }
        if contextChanged {
            flushTextInput(at: date, force: true)
            // A key can arrive in the new window before its first AX poll.
            // Re-open only for that real key, retaining its original deadline.
            if lastKeyDownAt > lastInputPollAt, burstAppPID == application.pid {
                textBurst.keyDown(scope: application.pid, at: lastKeyDownAt)
            }
            lastFocusedValue = nil
        }
        if let value, value != lastFocusedValue {
            textBurst.observe(TextSnapshot(app: appInfo(application), window: enrichedWindow(window, application: application, at: date),
                target: ["role": .string(role ?? "unknown"), "value": trimmedValue(value)]),
                scope: application.pid, at: date)
        }
        lastFocusedValue = value
        flushTextInput(at: date)
    }

    private func enrichedWindow(_ window: [String: JSONValue]?, application: RecorderApplication,
                                at date: Date) -> [String: JSONValue]? {
        guard let browserSnapshot else { return window }
        return browserSnapshot.enrich(window: window, bundleIdentifier: application.bundleIdentifier, at: date)
    }

    /// URL transitions are independent of the text burst's key/focus state.
    func recordWindowChange(application: RecorderApplication, window: [String: JSONValue]?,
                            appChanged: Bool, at date: Date, axBody: () -> JSONValue? = { nil }) {
        guard !SuppressionEngine.suppresses(bundleID: application.bundleIdentifier, blocklist: blocklist) else { return }
        let title: String? = { if case let .string(value)? = window?["title"] { return value }; return nil }()
        let url: String? = { if case let .string(value)? = window?["url"] { return value }; return nil }()
        let bridgeURLChanged = browserSnapshot?.tracksURLChanges(bundleIdentifier: application.bundleIdentifier, at: date) == true && url != lastWindowURL
        guard appChanged || title != lastWindowTitle || bridgeURLChanged else { return }
        lastWindowTitle = title
        lastWindowURL = url
        emit(kind: "window.changed", app: appInfo(application), window: window,
             body: axBody().map { ("ax", $0) }, at: date)
    }

    // MARK: polling

    func pollOnce(at date: Date, axTrusted: Bool) {
        guard let front = NSWorkspace.shared.frontmostApplication else { return }
        let bundleID = front.bundleIdentifier ?? ""
        if SuppressionEngine.suppresses(bundleID: bundleID, blocklist: blocklist) {
            writer.suppressOne()
            cancelTextInput()
            lastFocusedValue = nil
            lastAppPID = front.processIdentifier
            return
        }
        let app = appInfo(front)
        let appChanged = front.processIdentifier != lastAppPID
        lastAppPID = front.processIdentifier

        guard axTrusted else {
            cancelTextInput()
            if appChanged {
                emit(kind: "window.changed", app: app, window: nil, body: nil, at: date)
            }
            return
        }

        let axApp = AXUIElementCreateApplication(front.processIdentifier)
        var window: [String: JSONValue]?
        var currentTitle: String?
        var focused: AXUIElement?
        var focusedRole: String?
        var focusedSubrole: String?
        var focusedValue: String?
        var selectedText: String?

        if let focusedWindow = axCopy(axApp, kAXFocusedWindowAttribute) {
            let element = focusedWindow as! AXUIElement
            currentTitle = axString(element, kAXTitleAttribute)
            var info: [String: JSONValue] = [:]
            if let title = currentTitle { info["title"] = .string(title) }
            if let url = axString(element, "AXURL") { info["url"] = .string(String(url.prefix(500))) }
            if !info.isEmpty { window = info }
        }
        if let focusedElement = axCopy(axApp, kAXFocusedUIElementAttribute) {
            let element = focusedElement as! AXUIElement
            focused = element
            focusedRole = axString(element, kAXRoleAttribute)
            focusedSubrole = axString(element, kAXSubroleAttribute)
            if !SuppressionEngine.suppressesSecureField(role: focusedRole, subrole: focusedSubrole) {
                focusedValue = axString(element, kAXValueAttribute)
                selectedText = axString(element, kAXSelectedTextAttribute)
            }
        }

        let application = RecorderApplication(pid: front.processIdentifier, bundleIdentifier: bundleID,
                                              name: front.localizedName ?? "unknown")
        window = enrichedWindow(window, application: application, at: date)
        // Bridge URL updates affect window events, but not real-key eligibility.
        let contextChanged = appChanged || currentTitle != lastWindowTitle ||
            (lastFocusedElement != nil && focused != lastFocusedElement)
        lastFocusedElement = focused

        recordWindowChange(application: application, window: window, appChanged: appChanged, at: date) {
            .object(["mode": .string("fullTree"), "text": .string(self.axDump(focused ?? axApp))])
        }

        recordFocusedValue(focusedValue, role: focusedRole, subrole: focusedSubrole,
            application: application,
            window: window, contextChanged: contextChanged, at: date)

        if let selection = selectedText, !selection.isEmpty, selection != lastSelection {
            lastSelection = selection
            let body: JSONValue = .object([
                "selectedItems": .array([.object([
                    "description": .string(String(selection.prefix(500))),
                    "role": .string(focusedRole ?? "unknown"),
                ])]),
                "target": .object(["role": .string(focusedRole ?? "unknown")]),
            ])
            emit(kind: "selection.changed", app: app, window: window, body: ("selection", body), at: date)
        } else {
            lastSelection = selectedText
        }
    }

    // MARK: AX tree dump (bounded, mirrors upstream collapsed-style sizing)

    private func axDump(_ root: AXUIElement) -> String {
        var lines: [String] = []
        var budget = 300
        func walk(_ element: AXUIElement, depth: Int) {
            guard budget > 0 else { return }
            budget -= 1
            let role = axString(element, kAXRoleAttribute) ?? "?"
            let subrole = axString(element, kAXSubroleAttribute)
            if SuppressionEngine.suppressesSecureField(role: role, subrole: subrole) {
                lines.append("\(depth) \(role)")
                return
            }
            let description = axString(element, kAXDescriptionAttribute)
            let title = axString(element, kAXTitleAttribute)
            let url = axString(element, "AXURL")
            var label = "\(depth) \(role)"
            if let title, !title.isEmpty { label += " " + String(title.prefix(80)) }
            if let description, !description.isEmpty { label += " (\(String(description.prefix(80))))" }
            if let url, !url.isEmpty { label += " URL: \(String(url.prefix(120)))" }
            if depth > 6 {
                label += " (collapsed)"
                lines.append(label)
                return
            }
            lines.append(label)
            if let children = axCopy(element, kAXChildrenAttribute) as? [AXUIElement] {
                for child in children.prefix(20) { walk(child, depth: depth + 1) }
            }
        }
        walk(root, depth: 0)
        return lines.joined(separator: "\n")
    }
}

private let sharedDaemonBox = DaemonBox()
final class DaemonBox { var daemon: RecorderDaemon? }

public func runActivityRecorder() -> Int32 {
    let root: URL
    if let configured = ProcessInfo.processInfo.environment["ASTRA_ACTIVITY_ROOT"], !configured.isEmpty {
        root = URL(fileURLWithPath: (configured as NSString).expandingTildeInPath)
    } else {
        root = FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent("Library/Application Support/Astra/activity")
    }
    try? FileManager.default.createDirectory(at: root.appendingPathComponent("segments"), withIntermediateDirectories: true)

    let writer = BucketWriter(root: root, retentionDays: BucketWriter.retentionDays(environment: ProcessInfo.processInfo.environment))
    try? writer.bootstrap(now: Date())
    let daemon = RecorderDaemon(writer: writer, blocklist: SuppressionEngine.defaultBlocklist,
        browserSnapshot: RecorderBrowserSnapshot(root: root.appendingPathComponent("browser-bridge")))
    sharedDaemonBox.daemon = daemon

    let options = [kAXTrustedCheckOptionPrompt.takeUnretainedValue() as String: true] as CFDictionary
    let trusted = AXIsProcessTrustedWithOptions(options)
    FileHandle.standardError.write("recorder: root=\(root.path) accessibility=\(trusted)\n".data(using: .utf8)!)

    let started = Date()
    let sessionApp: [String: JSONValue] = ["name": .string("session")]
    daemon.emitSession(kind: "session.started", at: started, app: sessionApp)
    daemon.pollOnce(at: started, axTrusted: trusted)
    daemon.installMonitors(axTrusted: trusted)

    for sig in [SIGTERM, SIGINT] {
        signal(sig) { _ in sharedDaemonBox.daemon?.stop() }
    }

    let smokeSeconds = Int(ProcessInfo.processInfo.environment["ASTRA_ACTIVITY_SMOKE"] ?? "")
    let deadline = smokeSeconds.map { started.addingTimeInterval(TimeInterval($0)) }

    while !daemon.stopping {
        RunLoop.current.run(mode: .default, before: Date().addingTimeInterval(0.2))
        let now = Date()
        let currentlyTrusted = AXIsProcessTrusted()
        daemon.maintainEventTap(at: now, axTrusted: currentlyTrusted)
        daemon.pollOnce(at: now, axTrusted: currentlyTrusted)
        if let deadline, now >= deadline { break }
    }
    daemon.finishTextInput(at: Date())
    daemon.emitSession(kind: "session.ended", at: Date(), app: sessionApp)
    _ = try? writer.sealCurrentIfNeeded(now: Date())
    return 0
}

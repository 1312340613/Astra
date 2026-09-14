@testable import AstraMacComputerHelperCore
import AstraVirtualCursorProtocol
import CoreGraphics
import Darwin
import Foundation
import Testing

@Test func cursorProtocolRoundTripsOnlyFiniteBoundedMessages() throws {
    let messages: [CursorMessage] = [
        .show(CGPoint(x: 12, y: 34)),
        .move(CGPoint(x: -500, y: 800)),
        .click(CGPoint(x: 0, y: 0)),
        .hide,
    ]

    for message in messages {
        #expect(try CursorMessageCodec.decode(CursorMessageCodec.encode(message)) == message)
    }

    for coordinate in [Double.nan, .infinity, -.infinity, maximumVirtualCursorCoordinate + 1] {
        #expect(throws: CursorProtocolError.self) {
            try CursorMessageCodec.encode(.move(CGPoint(x: coordinate, y: 0)))
        }
    }
}

@Test func cursorProtocolRejectsOversizedUnknownMalformedAndExtraFields() {
    let invalid = [
        Data(repeating: 0x61, count: maximumCursorMessageBytes + 1),
        Data(#"{"command":"dance","x":1,"y":2}"#.utf8),
        Data(#"{"command":"move","x":1}"#.utf8),
        Data(#"{"command":"hide","extra":true}"#.utf8),
        Data(#"{"command":"move","x":true,"y":2}"#.utf8),
        Data(#"{"command":"move","x":1,"y":2"#.utf8),
        Data(#"{"command":"hide","command":"move","x":1,"y":2}"#.utf8),
        Data(#"{"command":"move","x":1,"x":2,"y":3}"#.utf8),
        Data(#"{"comm\u0061nd":"hide","command":"hide"}"#.utf8),
    ]

    for frame in invalid {
        #expect(throws: CursorProtocolError.self) {
            try CursorMessageCodec.decode(frame)
        }
    }
}

@Test func cursorProtocolAcceptsEscapedUniqueKeysAfterStandardJSONUnescaping() throws {
    #expect(try CursorMessageCodec.decode(Data(#"{"comm\u0061nd":"hide"}"#.utf8)) == .hide)

    let hello = try CursorSidecarHelloCodec.decode(Data(
        #"{"type":"ready","window_\u0069d":73,"panel":{"borderless_panel":true,"transparent":true,"nonactivating_panel":true,"ignores_mouse_events":true,"can_become_key":false,"can_become_main":false}}"#.utf8
    ))
    #expect(hello.windowID == 73)
}

@Test func cursorSessionRemovesOverlayAndExitsOnMalformedFrameOrEOF() throws {
    let malformedRenderer = CursorRendererSpy()
    let malformedSession = CursorProtocolSession(renderer: malformedRenderer)
    #expect(malformedSession.consume(Data(#"{"command":"unknown"}"#.utf8)) == .exit)
    #expect(malformedRenderer.removeCount == 1)

    let eofRenderer = CursorRendererSpy()
    let eofSession = CursorProtocolSession(renderer: eofRenderer)
    #expect(eofSession.consume(try CursorMessageCodec.encode(.show(CGPoint(x: 1, y: 2)))) == .continue)
    #expect(eofSession.endOfFile() == .exit)
    #expect(eofRenderer.hideCount == 1)
    #expect(eofRenderer.removeCount == 1)
}

@Test func cursorPanelHandshakeMustProveNonactivatingClickThroughProperties() throws {
    let expected = CursorPanelProperties(
        borderlessPanel: true,
        transparent: true,
        nonactivatingPanel: true,
        ignoresMouseEvents: true,
        canBecomeKey: false,
        canBecomeMain: false
    )
    let hello = CursorSidecarHello(windowID: 73, panel: expected)

    #expect(try CursorSidecarHelloCodec.decode(CursorSidecarHelloCodec.encode(hello)) == hello)
    #expect(hello.isSafeForCooperativePresentation)
    #expect(!CursorSidecarHello(
        windowID: 73,
        panel: CursorPanelProperties(
            borderlessPanel: false,
            transparent: true,
            nonactivatingPanel: false,
            ignoresMouseEvents: true,
            canBecomeKey: false,
            canBecomeMain: false
        )
    ).isSafeForCooperativePresentation)
    #expect(!CursorSidecarHello(
        windowID: 73,
        panel: CursorPanelProperties(
            borderlessPanel: true,
            transparent: false,
            nonactivatingPanel: true,
            ignoresMouseEvents: true,
            canBecomeKey: false,
            canBecomeMain: false
        )
    ).isSafeForCooperativePresentation)
    #expect(throws: CursorProtocolError.self) {
        try CursorSidecarHelloCodec.decode(Data(
            #"{"type":"ready","window_id":72,"window_\u0069d":73,"panel":{"borderless_panel":true,"transparent":true,"nonactivating_panel":true,"ignores_mouse_events":true,"can_become_key":false,"can_become_main":false}}"#.utf8
        ))
    }
    #expect(throws: CursorProtocolError.self) {
        try CursorSidecarHelloCodec.decode(Data(
            #"{"type":"ready","window_id":73,"panel":{"borderless_panel":true,"transparent":true,"nonactivating_panel":true,"ignores_mouse_events":true,"ignores_mouse_\u0065vents":true,"can_become_key":false,"can_become_main":false}}"#.utf8
        ))
    }
}

@Test func cursorHelloWindowIDUsesBoundedExactDecimalIntegerParsing() throws {
    let maximum = try CursorSidecarHelloCodec.decode(cursorHelloFrame(windowIDToken: "4294967295"))
    #expect(maximum.windowID == UInt32.max)

    let rejected = [
        "9223372036854775807",
        "9223372036854775808",
        "4294967296",
        "0",
        "-1",
        "-0",
        "1.0",
        "1e0",
        String(repeating: "9", count: 200),
    ]
    for token in rejected {
        #expect(throws: CursorProtocolError.self) {
            try CursorSidecarHelloCodec.decode(cursorHelloFrame(windowIDToken: token))
        }
    }
}

@Test func virtualCursorStateIsBoundedPresentationMetadataOnly() throws {
    let presenter = InMemoryVirtualCursorPresenter()
    try presenter.show(at: CGPoint(x: 12, y: 34))
    #expect(presenter.presentation == VirtualCursorPresentation(
        virtualPointer: CGPoint(x: 12, y: 34),
        cursorVisible: true
    ))
    presenter.hide()
    #expect(presenter.presentation == VirtualCursorPresentation(
        virtualPointer: CGPoint(x: 12, y: 34),
        cursorVisible: false
    ))
    #expect(throws: CursorProtocolError.self) {
        try presenter.move(to: CGPoint(x: maximumVirtualCursorCoordinate + 1, y: 0))
    }
}

@Test func captureScopeExcludesSidecarOnlyForDisplay() {
    let presenter = InMemoryVirtualCursorPresenter()
    #expect(cursorOverlayExclusionWindowID(scope: "target_window", presenter: presenter, availableWindows: []) == nil)
    #expect(cursorOverlayExclusionWindowID(scope: "display", presenter: presenter, availableWindows: []) == nil)
}

@Test func displayExclusionRequiresLiveSameChildPIDAndExactWindowOwner() throws {
    let validChild = CursorChildProcessSpy(processIdentifier: 4_242)
    let validClient = try launchIdentityCursor(child: validChild, windowID: 73)
    defer { validClient.close() }
    let authority = try #require(validClient.windowAuthority)
    #expect(authority.windowID == 73)
    #expect(authority.childPID == 4_242)
    #expect(validClient.displayExclusionWindowID(availableWindows: [
        VirtualCursorWindowRecord(windowID: 73, ownerPID: 4_242),
    ]) == 73)

    let wrongOwnerClient = try launchIdentityCursor(child: CursorChildProcessSpy(processIdentifier: 4_243), windowID: 74)
    #expect(wrongOwnerClient.displayExclusionWindowID(availableWindows: [
        VirtualCursorWindowRecord(windowID: 74, ownerPID: 9_999),
        VirtualCursorWindowRecord(windowID: 75, ownerPID: 4_243),
    ]) == nil)
    #expect(wrongOwnerClient.windowAuthority == nil)
    #expect(wrongOwnerClient.presentation == VirtualCursorPresentation(virtualPointer: nil, cursorVisible: false))

    let exitedChild = CursorChildProcessSpy(processIdentifier: 4_244)
    let reusedClient = try launchIdentityCursor(child: exitedChild, windowID: 76)
    exitedChild.finish()
    #expect(reusedClient.displayExclusionWindowID(availableWindows: [
        VirtualCursorWindowRecord(windowID: 76, ownerPID: 4_244),
    ]) == nil)
    #expect(reusedClient.windowAuthority == nil)

    let changedPID = CursorChildProcessSpy(processIdentifier: 4_245)
    let mismatchedClient = try launchIdentityCursor(child: changedPID, windowID: 77)
    changedPID.processIdentifier = 4_246
    #expect(mismatchedClient.displayExclusionWindowID(availableWindows: [
        VirtualCursorWindowRecord(windowID: 77, ownerPID: 4_246),
    ]) == nil)
    #expect(mismatchedClient.windowAuthority == nil)
}

@Test func eachCursorLaunchGetsANewWindowSessionIdentity() throws {
    let first = try launchIdentityCursor(child: CursorChildProcessSpy(processIdentifier: 5_001), windowID: 81)
    let second = try launchIdentityCursor(child: CursorChildProcessSpy(processIdentifier: 5_002), windowID: 82)
    defer { first.close(); second.close() }
    #expect(first.windowAuthority?.sessionID != second.windowAuthority?.sessionID)
}

@Test func virtualCursorClientSpawnsTheExactSiblingAndUsesInheritedSocketpair() throws {
    let launcher = CursorLauncherSpy(hello: safeCursorHello(windowID: 73))
    let helper = URL(fileURLWithPath: "/private/tmp/AstraMacComputerHelper.app/Contents/MacOS/AstraMacComputerHelper")
    let client = try VirtualCursorClient.launch(
        helperExecutableURL: helper,
        launcher: launcher,
        startupTimeout: 0.25
    )
    defer { client.close() }

    #expect(launcher.executableURL?.path == "/private/tmp/AstraMacComputerHelper.app/Contents/MacOS/AstraVirtualCursorSidecar")
    #expect(launcher.socketDescriptor.map { $0 > STDERR_FILENO } == true)
    #expect(client.sidecarWindowID == 73)
    #expect(launcher.shellArguments == nil)
}

@Test func virtualCursorClientSendsBoundedCommandsAndParentCloseYieldsEOF() throws {
    let launcher = CursorLauncherSpy(hello: safeCursorHello(windowID: 91))
    let client = try VirtualCursorClient.launch(
        helperExecutableURL: URL(fileURLWithPath: "/tmp/AstraMacComputerHelper"),
        launcher: launcher,
        startupTimeout: 0.25
    )

    try client.show(at: CGPoint(x: 12, y: 34))
    try client.move(to: CGPoint(x: 50, y: 60))
    try client.click(at: CGPoint(x: 50, y: 60))
    client.hide()
    client.close()

    #expect(launcher.receivedMessages == [
        .show(CGPoint(x: 12, y: 34)),
        .move(CGPoint(x: 50, y: 60)),
        .click(CGPoint(x: 50, y: 60)),
        .hide,
        .hide,
    ])
    #expect(launcher.observedEOF)
    #expect(launcher.launchedChild?.reaped == true)
    #expect(launcher.launchedChild?.terminateCount == 0)
    #expect(launcher.launchedChild?.killCount == 0)
    #expect(client.presentation == VirtualCursorPresentation(
        virtualPointer: CGPoint(x: 50, y: 60),
        cursorVisible: false
    ))
}

@Test func closeEscalatesFromEOFToSIGTERMToSIGKILLAndReapsWithinDeadline() throws {
    let child = CursorChildProcessSpy(processIdentifier: 6_001, ignoresTerminate: true)
    let launcher = NonReadingCursorLauncher(hello: safeCursorHello(windowID: 101), child: child)
    let client = try VirtualCursorClient.launch(
        helperExecutableURL: URL(fileURLWithPath: "/tmp/AstraMacComputerHelper"),
        launcher: launcher,
        startupTimeout: 0.05
    )
    let started = ProcessInfo.processInfo.systemUptime
    client.close()
    let elapsed = ProcessInfo.processInfo.systemUptime - started

    #expect(elapsed < 1)
    #expect(client.windowAuthority == nil)
    #expect(child.terminateCount == 1)
    #expect(child.killCount == 1)
    #expect(child.reaped)
    #expect(!child.isRunning)
}

@Test func nonReadingSidecarCannotBlockSendBeyondTheMonotonicDeadline() throws {
    let child = CursorChildProcessSpy(processIdentifier: 6_002, ignoresTerminate: true)
    let launcher = NonReadingCursorLauncher(hello: safeCursorHello(windowID: 102), child: child)
    let client = try VirtualCursorClient.launch(
        helperExecutableURL: URL(fileURLWithPath: "/tmp/AstraMacComputerHelper"),
        launcher: launcher,
        startupTimeout: 0.05
    )
    let finished = DispatchSemaphore(value: 0)
    let observedFailure = LockedBoolean()
    let started = ProcessInfo.processInfo.systemUptime
    DispatchQueue.global().async {
        for offset in 0 ..< 20_000 {
            do { try client.move(to: CGPoint(x: offset % 1_000, y: 10)) }
            catch {
                observedFailure.setTrue()
                break
            }
        }
        finished.signal()
    }
    let result = finished.wait(timeout: .now() + 0.8)
    if result == .timedOut {
        launcher.releasePeer()
        _ = finished.wait(timeout: .now() + 0.5)
    }
    let elapsed = ProcessInfo.processInfo.systemUptime - started

    #expect(result == .success)
    #expect(elapsed < 1)
    #expect(observedFailure.value)
    #expect(client.windowAuthority == nil)
    #expect(child.killCount == 1)
    #expect(child.reaped)
    client.close()
}

@Test func virtualCursorClientFailsClosedOnUnsafeHelloAndStartupEOF() {
    let unsafe = CursorSidecarHello(
        windowID: 7,
        panel: CursorPanelProperties(
            borderlessPanel: true,
            transparent: true,
            nonactivatingPanel: true,
            ignoresMouseEvents: false,
            canBecomeKey: false,
            canBecomeMain: false
        )
    )
    #expect(throws: VirtualCursorClientError.self) {
        try VirtualCursorClient.launch(
            helperExecutableURL: URL(fileURLWithPath: "/tmp/AstraMacComputerHelper"),
            launcher: CursorLauncherSpy(rawHello: try unsafeHelloFrame(unsafe)),
            startupTimeout: 0.25
        )
    }
    #expect(throws: VirtualCursorClientError.self) {
        try VirtualCursorClient.launch(
            helperExecutableURL: URL(fileURLWithPath: "/tmp/AstraMacComputerHelper"),
            launcher: CursorLauncherSpy(rawHello: nil),
            startupTimeout: 0.05
        )
    }
}

@Test func invalidHandshakeTerminatesAChildThatIgnoresParentEOF() throws {
    let child = CursorChildProcessSpy()
    let launcher = StubbornCursorLauncher(
        frame: try unsafeHelloFrame(CursorSidecarHello(
            windowID: 7,
            panel: CursorPanelProperties(
                borderlessPanel: true,
                transparent: true,
                nonactivatingPanel: true,
                ignoresMouseEvents: false,
                canBecomeKey: false,
                canBecomeMain: false
            )
        )),
        child: child
    )
    #expect(throws: VirtualCursorClientError.self) {
        try VirtualCursorClient.launch(
            helperExecutableURL: URL(fileURLWithPath: "/tmp/AstraMacComputerHelper"),
            launcher: launcher,
            startupTimeout: 0.05
        )
    }
    #expect(child.terminateCount == 1)
}

@Test func sidecarDisconnectAfterHandshakeClearsPresentationAuthority() throws {
    let launcher = CursorLauncherSpy(hello: safeCursorHello(windowID: 99), disconnectAfterHello: true)
    let client = try VirtualCursorClient.launch(
        helperExecutableURL: URL(fileURLWithPath: "/tmp/AstraMacComputerHelper"),
        launcher: launcher,
        startupTimeout: 0.25
    )

    #expect(throws: VirtualCursorClientError.self) {
        try client.show(at: CGPoint(x: 1, y: 2))
    }
    #expect(client.sidecarWindowID == nil)
    #expect(client.presentation == VirtualCursorPresentation(virtualPointer: nil, cursorVisible: false))
    client.close()
}

@Test func snapshotMetadataContainsOnlyRequestLocalCursorPresentation() {
    let visible = virtualCursorPresentationJSON(VirtualCursorPresentation(
        virtualPointer: CGPoint(x: 12, y: 34),
        cursorVisible: true
    ))
    guard case let .object(pointer)? = visible["virtual_pointer"],
          case let .number(x)? = pointer["x"],
          case let .number(y)? = pointer["y"],
          case let .bool(cursorVisible)? = visible["cursor_visible"]
    else {
        Issue.record("visible cursor metadata must use the bounded public shape")
        return
    }
    #expect(x == 12)
    #expect(y == 34)
    #expect(cursorVisible)
    #expect(Set(visible.keys) == ["virtual_pointer", "cursor_visible"])
    let absent = virtualCursorPresentationJSON(VirtualCursorPresentation(
        virtualPointer: nil,
        cursorVisible: false
    ))
    guard case let .bool(absentVisible)? = absent["cursor_visible"] else {
        Issue.record("hidden cursor metadata must include cursor_visible")
        return
    }
    #expect(!absentVisible)
    #expect(Set(absent.keys) == ["cursor_visible"])
}

@Test func freshEvidenceSnapshotKeepsPresentationUntilExplicitEndOrTargetInvalidation() {
    #expect(!cursorShouldHide(for: .newSnapshot))
    #expect(cursorShouldHide(for: .focus))
    #expect(cursorShouldHide(for: .appsRefresh))
    #expect(cursorShouldHide(for: .failedRefresh))
    #expect(cursorShouldHide(for: .close))
    #expect(!cursorShouldHideAfterSnapshot(succeeded: true))
    #expect(cursorShouldHideAfterSnapshot(succeeded: false))
}

@Test func unavailableCursorFailsClosedWithoutInventingPresentation() {
    let presenter = UnavailableVirtualCursorPresenter()
    #expect(throws: VirtualCursorClientError.self) { try presenter.show(at: CGPoint(x: 1, y: 2)) }
    #expect(throws: VirtualCursorClientError.self) { try presenter.move(to: CGPoint(x: 1, y: 2)) }
    #expect(throws: VirtualCursorClientError.self) { try presenter.click(at: CGPoint(x: 1, y: 2)) }
    #expect(presenter.presentation == VirtualCursorPresentation(virtualPointer: nil, cursorVisible: false))
    #expect(presenter.sidecarWindowID == nil)
}

private final class CursorRendererSpy: CursorRendering {
    var hideCount = 0
    var removeCount = 0

    func show(at _: CGPoint) throws {}
    func move(to _: CGPoint) throws {}
    func click(at _: CGPoint) throws {}
    func hide() { hideCount += 1 }
    func remove() { removeCount += 1 }
}

private func safeCursorHello(windowID: UInt32) -> CursorSidecarHello {
    CursorSidecarHello(
        windowID: windowID,
        panel: CursorPanelProperties(
            borderlessPanel: true,
            transparent: true,
            nonactivatingPanel: true,
            ignoresMouseEvents: true,
            canBecomeKey: false,
            canBecomeMain: false
        )
    )
}

private func cursorHelloFrame(windowIDToken: String) -> Data {
    Data(
        ("{\"type\":\"ready\",\"window_id\":" + windowIDToken +
            ",\"panel\":{\"borderless_panel\":true,\"transparent\":true," +
            "\"nonactivating_panel\":true,\"ignores_mouse_events\":true," +
            "\"can_become_key\":false,\"can_become_main\":false}}")
            .utf8
    )
}

private func unsafeHelloFrame(_ hello: CursorSidecarHello) throws -> Data {
    try JSONSerialization.data(withJSONObject: [
        "type": "ready",
        "window_id": Int(hello.windowID),
        "panel": [
            "borderless_panel": hello.panel.borderlessPanel,
            "transparent": hello.panel.transparent,
            "nonactivating_panel": hello.panel.nonactivatingPanel,
            "ignores_mouse_events": hello.panel.ignoresMouseEvents,
            "can_become_key": hello.panel.canBecomeKey,
            "can_become_main": hello.panel.canBecomeMain,
        ],
    ], options: [.sortedKeys])
}

private final class CursorLauncherSpy: VirtualCursorSidecarLaunching {
    private let rawHello: Data?
    private let disconnectAfterHello: Bool
    private let lock = NSLock()
    private(set) var executableURL: URL?
    private(set) var socketDescriptor: Int32?
    private(set) var shellArguments: [String]?
    private(set) var receivedMessages: [CursorMessage] = []
    private(set) var observedEOF = false
    private(set) var launchedChild: CursorChildProcessSpy?
    private var reader: Thread?

    init(hello: CursorSidecarHello, disconnectAfterHello: Bool = false) {
        rawHello = try? CursorSidecarHelloCodec.encode(hello)
        self.disconnectAfterHello = disconnectAfterHello
    }

    init(rawHello: Data?) {
        self.rawHello = rawHello
        disconnectAfterHello = false
    }

    func launch(executableURL: URL, inheritedSocket: FileHandle) throws -> any VirtualCursorChildProcess {
        self.executableURL = executableURL
        socketDescriptor = inheritedSocket.fileDescriptor
        if let rawHello {
            inheritedSocket.write(rawHello + Data([0x0A]))
        }
        let child = CursorChildProcessSpy()
        launchedChild = child
        let descriptor = Darwin.dup(inheritedSocket.fileDescriptor)
        if disconnectAfterHello {
            Darwin.close(descriptor)
            return child
        }
        let thread = Thread { [weak self, weak child] in
            var buffer = Data()
            var byte: UInt8 = 0
            while Darwin.read(descriptor, &byte, 1) == 1 {
                if byte == 0x0A {
                    if let message = try? CursorMessageCodec.decode(buffer) {
                        self?.lock.lock()
                        self?.receivedMessages.append(message)
                        self?.lock.unlock()
                    }
                    buffer.removeAll(keepingCapacity: true)
                } else if buffer.count <= maximumCursorMessageBytes {
                    buffer.append(byte)
                }
            }
            self?.lock.lock()
            self?.observedEOF = true
            self?.lock.unlock()
            child?.finish()
        }
        reader = thread
        thread.start()
        return child
    }
}

private final class CursorChildProcessSpy: VirtualCursorChildProcess {
    private let lock = NSLock()
    private var running = true
    private(set) var terminateCount = 0
    private(set) var killCount = 0
    private(set) var reaped = false
    private let ignoresTerminate: Bool
    var processIdentifier: pid_t

    init(processIdentifier: pid_t = 9001, ignoresTerminate: Bool = false) {
        self.processIdentifier = processIdentifier
        self.ignoresTerminate = ignoresTerminate
    }
    var isRunning: Bool {
        lock.lock()
        defer { lock.unlock() }
        return running
    }
    func terminate() {
        lock.lock()
        terminateCount += 1
        if !ignoresTerminate { running = false }
        lock.unlock()
    }
    func kill() {
        lock.lock()
        killCount += 1
        running = false
        lock.unlock()
    }
    func waitUntilExit(deadline: TimeInterval) -> Bool {
        while isRunning, ProcessInfo.processInfo.systemUptime < deadline { usleep(1_000) }
        lock.lock()
        defer { lock.unlock() }
        guard !running else { return false }
        reaped = true
        return true
    }
    func finish() {
        lock.lock()
        running = false
        lock.unlock()
    }
}

private final class NonReadingCursorLauncher: VirtualCursorSidecarLaunching {
    private let hello: CursorSidecarHello
    private let child: CursorChildProcessSpy
    private let lock = NSLock()
    private var peerDescriptor: Int32 = -1

    init(hello: CursorSidecarHello, child: CursorChildProcessSpy) {
        self.hello = hello
        self.child = child
    }

    deinit { releasePeer() }

    func launch(executableURL _: URL, inheritedSocket: FileHandle) throws -> any VirtualCursorChildProcess {
        let descriptor = Darwin.dup(inheritedSocket.fileDescriptor)
        guard descriptor >= 0 else { throw VirtualCursorClientError.launchFailed }
        var receiveBuffer: Int32 = 1_024
        _ = setsockopt(descriptor, SOL_SOCKET, SO_RCVBUF, &receiveBuffer, socklen_t(MemoryLayout.size(ofValue: receiveBuffer)))
        let frame = try CursorSidecarHelloCodec.encode(hello) + Data([0x0A])
        frame.withUnsafeBytes { raw in
            if let base = raw.baseAddress { _ = Darwin.write(descriptor, base, raw.count) }
        }
        lock.lock()
        peerDescriptor = descriptor
        lock.unlock()
        return child
    }

    func releasePeer() {
        lock.lock()
        let descriptor = peerDescriptor
        peerDescriptor = -1
        lock.unlock()
        if descriptor >= 0 { Darwin.close(descriptor) }
    }
}

private final class StubbornCursorLauncher: VirtualCursorSidecarLaunching {
    private let frame: Data
    private let child: CursorChildProcessSpy

    init(frame: Data, child: CursorChildProcessSpy) {
        self.frame = frame
        self.child = child
    }

    func launch(executableURL _: URL, inheritedSocket: FileHandle) throws -> any VirtualCursorChildProcess {
        inheritedSocket.write(frame + Data([0x0A]))
        return child
    }
}

private final class LockedBoolean: @unchecked Sendable {
    private let lock = NSLock()
    private var storedValue = false

    var value: Bool {
        lock.lock()
        defer { lock.unlock() }
        return storedValue
    }

    func setTrue() {
        lock.lock()
        storedValue = true
        lock.unlock()
    }
}

private func launchIdentityCursor(child: CursorChildProcessSpy, windowID: UInt32) throws -> VirtualCursorClient {
    try VirtualCursorClient.launch(
        helperExecutableURL: URL(fileURLWithPath: "/tmp/AstraMacComputerHelper"),
        launcher: StubbornCursorLauncher(frame: CursorSidecarHelloCodec.encode(safeCursorHello(windowID: windowID)), child: child),
        startupTimeout: 0.05
    )
}

@Test func secondForegroundTakeoverLaunchesANewSidecarAfterFirstFragmentCloses() throws {
    var launched: [RestartableCursorChild] = []
    let presenter = RestartableVirtualCursorPresenter {
        let child = RestartableCursorChild()
        launched.append(child)
        return child
    }

    try presenter.show(at: CGPoint(x: 10, y: 20))
    presenter.close()
    try presenter.show(at: CGPoint(x: 30, y: 40))

    #expect(launched.count == 2)
    #expect(launched[0].closeCount == 1)
    #expect(launched[1].presentation == VirtualCursorPresentation(
        virtualPointer: CGPoint(x: 30, y: 40),
        cursorVisible: true
    ))
}

private final class RestartableCursorChild: VirtualCursorPresenting {
    let sidecarWindowID: UInt32? = nil
    let windowAuthority: VirtualCursorWindowAuthority? = nil
    var presentation = VirtualCursorPresentation(virtualPointer: nil, cursorVisible: false)
    var closeCount = 0
    func displayExclusionWindowID(availableWindows _: [VirtualCursorWindowRecord]) -> UInt32? { nil }
    func show(at point: CGPoint) throws { presentation = .init(virtualPointer: point, cursorVisible: true) }
    func move(to point: CGPoint) throws { presentation = .init(virtualPointer: point, cursorVisible: presentation.cursorVisible) }
    func click(at point: CGPoint) throws { presentation = .init(virtualPointer: point, cursorVisible: true) }
    func hide() { presentation = .init(virtualPointer: presentation.virtualPointer, cursorVisible: false) }
    func close() { closeCount += 1; hide() }
}

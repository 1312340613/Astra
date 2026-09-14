import AstraVirtualCursorProtocol
import CoreGraphics
import Darwin
import Foundation

struct VirtualCursorPresentation: Equatable {
    let virtualPointer: CGPoint?
    let cursorVisible: Bool
}

struct VirtualCursorWindowRecord: Equatable {
    let windowID: UInt32
    let ownerPID: pid_t
}

struct VirtualCursorWindowAuthority: Equatable {
    let windowID: UInt32
    let childPID: pid_t
    let sessionID: UUID
}

protocol VirtualCursorPresenting: AnyObject {
    var sidecarWindowID: UInt32? { get }
    var windowAuthority: VirtualCursorWindowAuthority? { get }
    var presentation: VirtualCursorPresentation { get }
    func displayExclusionWindowID(availableWindows: [VirtualCursorWindowRecord]) -> UInt32?
    func show(at point: CGPoint) throws
    func move(to point: CGPoint) throws
    func click(at point: CGPoint) throws
    func hide()
    func close()
}

enum VirtualCursorClientError: Error, Equatable {
    case socketCreationFailed
    case launchFailed
    case startupTimedOut
    case startupEOF
    case invalidHandshake
    case writeFailed
    case closed
    case unavailable
}

final class UnavailableVirtualCursorPresenter: VirtualCursorPresenting {
    let sidecarWindowID: UInt32? = nil
    let windowAuthority: VirtualCursorWindowAuthority? = nil
    let presentation = VirtualCursorPresentation(virtualPointer: nil, cursorVisible: false)
    func displayExclusionWindowID(availableWindows _: [VirtualCursorWindowRecord]) -> UInt32? { nil }
    func show(at _: CGPoint) throws { throw VirtualCursorClientError.unavailable }
    func move(to _: CGPoint) throws { throw VirtualCursorClientError.unavailable }
    func click(at _: CGPoint) throws { throw VirtualCursorClientError.unavailable }
    func hide() {}
    func close() {}
}

final class RestartableVirtualCursorPresenter: VirtualCursorPresenting {
    typealias Factory = () throws -> any VirtualCursorPresenting

    private let lock = NSLock()
    private let factory: Factory
    private var current: (any VirtualCursorPresenting)?
    private var storedPresentation = VirtualCursorPresentation(virtualPointer: nil, cursorVisible: false)

    init(factory: @escaping Factory) { self.factory = factory }

    var sidecarWindowID: UInt32? { snapshotCurrent()?.sidecarWindowID }
    var windowAuthority: VirtualCursorWindowAuthority? { snapshotCurrent()?.windowAuthority }
    var presentation: VirtualCursorPresentation {
        lock.lock()
        defer { lock.unlock() }
        return current?.presentation ?? storedPresentation
    }

    func displayExclusionWindowID(availableWindows: [VirtualCursorWindowRecord]) -> UInt32? {
        snapshotCurrent()?.displayExclusionWindowID(availableWindows: availableWindows)
    }

    func show(at point: CGPoint) throws {
        let presenter = try acquire()
        try presenter.show(at: point)
        remember(presenter.presentation)
    }

    func move(to point: CGPoint) throws {
        guard let presenter = snapshotCurrent() else { throw VirtualCursorClientError.closed }
        try presenter.move(to: point)
        remember(presenter.presentation)
    }

    func click(at point: CGPoint) throws {
        guard let presenter = snapshotCurrent() else { throw VirtualCursorClientError.closed }
        try presenter.click(at: point)
        remember(presenter.presentation)
    }

    func hide() {
        guard let presenter = snapshotCurrent() else { return }
        presenter.hide()
        remember(presenter.presentation)
    }

    func close() {
        lock.lock()
        let presenter = current
        current = nil
        if let presenter {
            storedPresentation = VirtualCursorPresentation(
                virtualPointer: presenter.presentation.virtualPointer,
                cursorVisible: false
            )
        }
        lock.unlock()
        presenter?.close()
    }

    private func acquire() throws -> any VirtualCursorPresenting {
        if let presenter = snapshotCurrent() { return presenter }
        let launched = try factory()
        lock.lock()
        if let current {
            lock.unlock()
            launched.close()
            return current
        }
        current = launched
        lock.unlock()
        return launched
    }

    private func snapshotCurrent() -> (any VirtualCursorPresenting)? {
        lock.lock()
        defer { lock.unlock() }
        return current
    }

    private func remember(_ presentation: VirtualCursorPresentation) {
        lock.lock()
        storedPresentation = presentation
        lock.unlock()
    }
}

protocol VirtualCursorChildProcess: AnyObject {
    var processIdentifier: pid_t { get }
    var isRunning: Bool { get }
    func terminate()
    func kill()
    func waitUntilExit(deadline: TimeInterval) -> Bool
}

protocol VirtualCursorSidecarLaunching: AnyObject {
    func launch(executableURL: URL, inheritedSocket: FileHandle) throws -> any VirtualCursorChildProcess
}

final class FoundationVirtualCursorSidecarLauncher: VirtualCursorSidecarLaunching {
    func launch(executableURL: URL, inheritedSocket: FileHandle) throws -> any VirtualCursorChildProcess {
        let process = Process()
        process.executableURL = executableURL
        process.arguments = []
        process.standardInput = inheritedSocket
        process.standardOutput = inheritedSocket
        process.standardError = FileHandle.nullDevice
        do { try process.run() }
        catch { throw VirtualCursorClientError.launchFailed }
        return FoundationVirtualCursorChildProcess(process: process)
    }
}

private final class FoundationVirtualCursorChildProcess: VirtualCursorChildProcess {
    private let process: Process
    init(process: Process) { self.process = process }
    var processIdentifier: pid_t { pid_t(process.processIdentifier) }
    var isRunning: Bool { process.isRunning }
    func terminate() { if process.isRunning { process.terminate() } }
    func kill() {
        let pid = processIdentifier
        if pid > 0, process.isRunning { _ = Darwin.kill(pid, SIGKILL) }
    }
    func waitUntilExit(deadline: TimeInterval) -> Bool {
        while process.isRunning, ProcessInfo.processInfo.systemUptime < deadline { usleep(1_000) }
        guard !process.isRunning else { return false }
        process.waitUntilExit()
        return true
    }
}

final class VirtualCursorClient: VirtualCursorPresenting {
    private let lock = NSLock()
    private let writeLock = NSLock()
    private let socketDescriptor: Int32
    private let child: any VirtualCursorChildProcess
    private var open = true
    private var storedAuthority: VirtualCursorWindowAuthority?
    private var storedPresentation = VirtualCursorPresentation(virtualPointer: nil, cursorVisible: false)

    var sidecarWindowID: UInt32? {
        lock.lock()
        defer { lock.unlock() }
        return storedAuthority?.windowID
    }

    var windowAuthority: VirtualCursorWindowAuthority? {
        lock.lock()
        defer { lock.unlock() }
        return storedAuthority
    }

    var presentation: VirtualCursorPresentation {
        lock.lock()
        defer { lock.unlock() }
        return storedPresentation
    }

    private init(socketDescriptor: Int32, child: any VirtualCursorChildProcess, windowID: UInt32) {
        self.socketDescriptor = socketDescriptor
        self.child = child
        storedAuthority = VirtualCursorWindowAuthority(
            windowID: windowID,
            childPID: child.processIdentifier,
            sessionID: UUID()
        )
    }

    deinit { close() }

    static func launch(
        helperExecutableURL: URL,
        launcher: any VirtualCursorSidecarLaunching = FoundationVirtualCursorSidecarLauncher(),
        startupTimeout: TimeInterval = 1
    ) throws -> VirtualCursorClient {
        var descriptors = [Int32](repeating: -1, count: 2)
        guard socketpair(AF_UNIX, SOCK_STREAM, 0, &descriptors) == 0 else {
            throw VirtualCursorClientError.socketCreationFailed
        }
        let parent = descriptors[0]
        let childDescriptor = descriptors[1]
        var noSigPipe: Int32 = 1
        guard setsockopt(parent, SOL_SOCKET, SO_NOSIGPIPE, &noSigPipe, socklen_t(MemoryLayout.size(ofValue: noSigPipe))) == 0,
              makeNonblocking(parent)
        else {
            Darwin.close(parent)
            Darwin.close(childDescriptor)
            throw VirtualCursorClientError.socketCreationFailed
        }
        let childHandle = FileHandle(fileDescriptor: childDescriptor, closeOnDealloc: true)
        var process: (any VirtualCursorChildProcess)?
        do {
            let sidecarURL = helperExecutableURL.deletingLastPathComponent().appendingPathComponent("AstraVirtualCursorSidecar")
            let launched = try launcher.launch(executableURL: sidecarURL, inheritedSocket: childHandle)
            process = launched
            try childHandle.close()
            let frame = try readCursorLine(descriptor: parent, timeout: startupTimeout)
            let hello: CursorSidecarHello
            do { hello = try CursorSidecarHelloCodec.decode(frame) }
            catch { throw VirtualCursorClientError.invalidHandshake }
            guard hello.isSafeForCooperativePresentation,
                  launched.processIdentifier > 0,
                  launched.isRunning
            else { throw VirtualCursorClientError.invalidHandshake }
            return VirtualCursorClient(socketDescriptor: parent, child: launched, windowID: hello.windowID)
        } catch {
            try? childHandle.close()
            shutdown(parent, SHUT_RDWR)
            Darwin.close(parent)
            if let process { stopCursorChildBounded(process) }
            if let error = error as? VirtualCursorClientError { throw error }
            throw VirtualCursorClientError.launchFailed
        }
    }

    func show(at point: CGPoint) throws {
        try send(.show(point)) { storedPresentation = VirtualCursorPresentation(virtualPointer: point, cursorVisible: true) }
    }

    func move(to point: CGPoint) throws {
        try send(.move(point)) {
            storedPresentation = VirtualCursorPresentation(virtualPointer: point, cursorVisible: storedPresentation.cursorVisible)
        }
    }

    func click(at point: CGPoint) throws {
        try send(.click(point)) { storedPresentation = VirtualCursorPresentation(virtualPointer: point, cursorVisible: true) }
    }

    func hide() {
        try? send(.hide) {
            storedPresentation = VirtualCursorPresentation(virtualPointer: storedPresentation.virtualPointer, cursorVisible: false)
        }
    }

    func displayExclusionWindowID(availableWindows: [VirtualCursorWindowRecord]) -> UInt32? {
        lock.lock()
        guard open,
              let authority = storedAuthority,
              child.isRunning,
              child.processIdentifier == authority.childPID,
              availableWindows.contains(VirtualCursorWindowRecord(
                  windowID: authority.windowID,
                  ownerPID: authority.childPID
              ))
        else {
            let needsTeardown = failClosedLocked()
            lock.unlock()
            if needsTeardown { closeSocketAndStopChild() }
            return nil
        }
        lock.unlock()
        return authority.windowID
    }

    func close() {
        hide()
        if transitionToClosed(preservePointer: true) { closeSocketAndStopChild() }
    }

    private func send(_ message: CursorMessage, update: () -> Void) throws {
        let frame = try CursorMessageCodec.encode(message)
        writeLock.lock()
        lock.lock()
        guard open else {
            lock.unlock()
            writeLock.unlock()
            throw VirtualCursorClientError.closed
        }
        lock.unlock()
        let written = writeCursorFrame(
            frame,
            descriptor: socketDescriptor,
            deadline: ProcessInfo.processInfo.systemUptime + 0.100
        )
        lock.lock()
        if written, open {
            update()
            lock.unlock()
            writeLock.unlock()
            return
        }
        let needsTeardown = failClosedLocked()
        lock.unlock()
        if needsTeardown {
            shutdown(socketDescriptor, SHUT_RDWR)
            Darwin.close(socketDescriptor)
        }
        writeLock.unlock()
        if needsTeardown {
            stopChildBounded()
        }
        throw written ? VirtualCursorClientError.closed : VirtualCursorClientError.writeFailed
    }

    private func stopChildBounded() {
        stopCursorChildBounded(child)
    }

    private func closeSocketAndStopChild() {
        writeLock.lock()
        shutdown(socketDescriptor, SHUT_RDWR)
        Darwin.close(socketDescriptor)
        writeLock.unlock()
        stopChildBounded()
    }

    private func failClosedLocked() -> Bool {
        storedAuthority = nil
        storedPresentation = VirtualCursorPresentation(virtualPointer: nil, cursorVisible: false)
        guard open else { return false }
        open = false
        return true
    }

    private func transitionToClosed(preservePointer: Bool) -> Bool {
        lock.lock()
        storedAuthority = nil
        storedPresentation = VirtualCursorPresentation(
            virtualPointer: preservePointer ? storedPresentation.virtualPointer : nil,
            cursorVisible: false
        )
        guard open else {
            lock.unlock()
            return false
        }
        open = false
        lock.unlock()
        return true
    }
}

private func stopCursorChildBounded(_ child: any VirtualCursorChildProcess) {
    var deadline = ProcessInfo.processInfo.systemUptime + 0.100
    if child.waitUntilExit(deadline: deadline) { return }
    child.terminate()
    deadline += 0.100
    if child.waitUntilExit(deadline: deadline) { return }
    child.kill()
    deadline += 0.300
    _ = child.waitUntilExit(deadline: deadline)
}

func virtualCursorPresentationJSON(_ presentation: VirtualCursorPresentation) -> [String: JSONValue] {
    var result: [String: JSONValue] = ["cursor_visible": .bool(presentation.cursorVisible)]
    if let point = presentation.virtualPointer,
       (try? CursorMessageCodec.validate(point)) != nil
    {
        result["virtual_pointer"] = .object([
            "x": .number(Double(point.x)),
            "y": .number(Double(point.y)),
        ])
    }
    return result
}

private func writeCursorFrame(_ frame: Data, descriptor: Int32, deadline: TimeInterval) -> Bool {
    guard frame.count <= maximumCursorMessageBytes else { return false }
    let bytes = frame + Data([0x0A])
    return bytes.withUnsafeBytes { raw in
        guard let base = raw.baseAddress else { return false }
        var sent = 0
        while sent < raw.count {
            let remaining = deadline - ProcessInfo.processInfo.systemUptime
            guard remaining > 0 else { return false }
            var pollDescriptor = pollfd(fd: descriptor, events: Int16(POLLOUT), revents: 0)
            let milliseconds = Int32(min(remaining * 1_000, Double(Int32.max)).rounded(.up))
            let pollResult = Darwin.poll(&pollDescriptor, 1, milliseconds)
            if pollResult == 0 { return false }
            if pollResult < 0 {
                if errno == EINTR { continue }
                return false
            }
            if pollDescriptor.revents & Int16(POLLERR | POLLHUP | POLLNVAL) != 0 { return false }
            guard pollDescriptor.revents & Int16(POLLOUT) != 0 else { continue }
            let count = Darwin.send(descriptor, base.advanced(by: sent), raw.count - sent, 0)
            if count > 0 { sent += count; continue }
            if count < 0, errno == EINTR { continue }
            if count < 0, errno == EAGAIN || errno == EWOULDBLOCK { continue }
            return false
        }
        return true
    }
}

private func makeNonblocking(_ descriptor: Int32) -> Bool {
    let flags = fcntl(descriptor, F_GETFL)
    guard flags >= 0 else { return false }
    return fcntl(descriptor, F_SETFL, flags | O_NONBLOCK) == 0
}

private func readCursorLine(descriptor: Int32, timeout: TimeInterval) throws -> Data {
    let deadline = ProcessInfo.processInfo.systemUptime + max(0, timeout)
    var result = Data()
    while true {
        let remaining = deadline - ProcessInfo.processInfo.systemUptime
        guard remaining > 0 else { throw VirtualCursorClientError.startupTimedOut }
        var pollDescriptor = pollfd(fd: descriptor, events: Int16(POLLIN | POLLHUP), revents: 0)
        let milliseconds = Int32(min(remaining * 1_000, Double(Int32.max)).rounded(.up))
        let pollResult = Darwin.poll(&pollDescriptor, 1, milliseconds)
        if pollResult == 0 { throw VirtualCursorClientError.startupTimedOut }
        if pollResult < 0 {
            if errno == EINTR { continue }
            throw VirtualCursorClientError.startupEOF
        }
        var byte: UInt8 = 0
        let count = Darwin.read(descriptor, &byte, 1)
        if count == 0 { throw VirtualCursorClientError.startupEOF }
        if count < 0 {
            if errno == EINTR { continue }
            if errno == EAGAIN || errno == EWOULDBLOCK { continue }
            throw VirtualCursorClientError.startupEOF
        }
        if byte == 0x0A { return result }
        guard result.count < maximumCursorMessageBytes else { throw VirtualCursorClientError.invalidHandshake }
        result.append(byte)
    }
}

final class InMemoryVirtualCursorPresenter: VirtualCursorPresenting {
    let sidecarWindowID: UInt32? = nil
    let windowAuthority: VirtualCursorWindowAuthority? = nil
    private(set) var presentation = VirtualCursorPresentation(virtualPointer: nil, cursorVisible: false)
    func displayExclusionWindowID(availableWindows _: [VirtualCursorWindowRecord]) -> UInt32? { nil }

    func show(at point: CGPoint) throws {
        try CursorMessageCodec.validate(point)
        presentation = VirtualCursorPresentation(virtualPointer: point, cursorVisible: true)
    }

    func move(to point: CGPoint) throws {
        try CursorMessageCodec.validate(point)
        presentation = VirtualCursorPresentation(virtualPointer: point, cursorVisible: presentation.cursorVisible)
    }

    func click(at point: CGPoint) throws {
        try CursorMessageCodec.validate(point)
        presentation = VirtualCursorPresentation(virtualPointer: point, cursorVisible: true)
    }

    func hide() {
        presentation = VirtualCursorPresentation(virtualPointer: presentation.virtualPointer, cursorVisible: false)
    }

    func close() { hide() }
}

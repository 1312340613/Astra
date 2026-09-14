import Foundation
import AstraAppshotCore
import AstraWindowsNative

enum WindowsTransportError: Error { case native(Int32), invalidFrame, closed }

func windowsProcess(_ raw: ASProcess) throws -> AppshotWindowsProcess {
    guard raw.pid > 0, raw.pid <= Int32.max else { throw AppshotBrokerError.unauthorized }
    return try AppshotWindowsProcess.decodeStrict(JSONEncoder().encode(AppshotWindowsProcess(
        pid: Int32(raw.pid), processStart: String(raw.created), userSID: WindowsAppshot.string(raw.sid))))
}

/// Exact v2 framing; the legacy v1 decoder is never relaxed for Windows.
struct WindowsFrameDecoder {
    private var pending = Data()
    var hasPartialFrame: Bool { !pending.isEmpty }
    mutating func feed(_ bytes: Data) throws -> [AppshotWindowsMessage] {
        var result: [AppshotWindowsMessage] = []
        for byte in bytes {
            if byte == 10 {
                guard !pending.isEmpty else { throw AppshotProtocolError.invalidJSON }
                result.append(try AppshotWindowsMessage.decodeStrict(pending))
                pending.removeAll(keepingCapacity: true)
            } else {
                guard pending.count < AppshotLimits.maximumFrameBytes - 1 else { throw AppshotProtocolError.frameSize }
                pending.append(byte)
            }
        }
        return result
    }
    func finish() throws { if !pending.isEmpty { throw AppshotProtocolError.incompleteFrame } }
}

final class WindowsPipeClient {
    private var handle: UnsafeMutableRawPointer?
    private var decoder = WindowsFrameDecoder()
    init(scope: String, expected: AppshotWindowsProcess) throws {
        guard !scope.utf8.contains(0), expected.pid > 0 else { throw AppshotBrokerError.unauthorized }
        var proof = ASProcess()
        guard as_process_identity(UInt32(expected.pid), &proof) != 0,
            try windowsProcess(proof) == expected else { throw AppshotBrokerError.unauthorized }
        var error: Int32 = 0
        handle = scope.withCString { as_pipe_client_open($0, &proof, 1000, &error) }
        guard handle != nil else { throw WindowsTransportError.native(error) }
    }
    deinit { close() }
    func close() { if let handle { as_pipe_client_close(handle); self.handle = nil } }
    func send(_ fields: [String: Any]) throws {
        guard let handle else { throw WindowsTransportError.closed }
        let bytes = try AppshotWindowsMessage.decodeStrict(JSONSerialization.data(withJSONObject: fields)).encodeFrame()
        let code = bytes.withUnsafeBytes { as_pipe_client_write(handle,
            $0.bindMemory(to: UInt8.self).baseAddress, bytes.count, 1000, nil, nil) }
        if code != 0 { close(); throw WindowsTransportError.native(code) }
    }
    func poll() throws -> [AppshotWindowsMessage] {
        guard let handle else { throw WindowsTransportError.closed }
        let bytes = as_pipe_client_read(handle, 0, nil, nil)
        defer { as_bytes_free(bytes) }
        guard bytes.error == 0 else { close(); throw WindowsTransportError.native(bytes.error) }
        guard let data = bytes.data, bytes.size > 0 else { return [] }
        do { return try decoder.feed(Data(bytes: data, count: Int(bytes.size))) }
        catch { close(); throw error }
    }
    var hasPartialFrame: Bool { decoder.hasPartialFrame }
    func enqueue(_ message: AppshotWindowsMessage) throws {
        guard let handle else { throw WindowsTransportError.closed }
        let frame = try message.encodeFrame()
        let code = frame.withUnsafeBytes { as_pipe_client_enqueue(handle,
            $0.bindMemory(to: UInt8.self).baseAddress, frame.count) }
        guard code == 0 else { close(); throw WindowsTransportError.native(code) }
    }
    func flush() throws {
        guard let handle else { throw WindowsTransportError.closed }
        let code = as_pipe_client_flush(handle)
        guard code == 0 else { close(); throw WindowsTransportError.native(code) }
    }
}

/// One owner thread, no blocking reads/writes in the broker's event loop.
final class WindowsPipeServer {
    enum Event {
        case connected(UInt64, AppshotWindowsProcess)
        case bytes(UInt64, Data)
        case disconnected(UInt64)
    }
    private var handle: UnsafeMutableRawPointer?
    init(scope: String) throws {
        guard !scope.utf8.contains(0) else { throw WindowsTransportError.invalidFrame }
        var error: Int32 = 0
        handle = scope.withCString { as_pipe_server_open($0, &error) }
        guard handle != nil else { throw WindowsTransportError.native(error) }
    }
    deinit { close() }
    func close() { if let handle { as_pipe_server_close(handle); self.handle = nil } }
    func poll() throws -> Event? {
        guard let handle else { throw WindowsTransportError.closed }
        var event = ASPipeEvent()
        let result = as_pipe_server_poll(handle, &event)
        defer { as_bytes_free(event.bytes) }
        guard result >= 0 else { throw WindowsTransportError.native(-result) }
        guard result > 0 else { return nil }
        switch event.kind {
        case 1: return .connected(event.connection, try windowsProcess(event.peer))
        case 2:
            guard let bytes = event.bytes.data, event.bytes.size > 0, event.bytes.size <= 4096 else {
                throw WindowsTransportError.invalidFrame
            }
            return .bytes(event.connection, Data(bytes: bytes, count: Int(event.bytes.size)))
        case 3: return .disconnected(event.connection)
        default: throw WindowsTransportError.invalidFrame
        }
    }
    func send(_ message: [String: Any], to id: UInt64) throws {
        guard let handle else { throw WindowsTransportError.closed }
        let frame = try AppshotWindowsMessage.decodeStrict(JSONSerialization.data(withJSONObject: message)).encodeFrame()
        let code = frame.withUnsafeBytes { as_pipe_server_send(handle, id,
            $0.bindMemory(to: UInt8.self).baseAddress, frame.count) }
        guard code == 0 else { throw WindowsTransportError.native(code) }
    }
    func parent(of id: UInt64, peer expected: AppshotWindowsProcess) throws -> AppshotWindowsProcess {
        guard let handle else { throw WindowsTransportError.closed }
        var peer = ASProcess(), parent = ASProcess()
        let code = as_pipe_server_peer(handle, id, &peer, &parent)
        guard code == 0, try windowsProcess(peer) == expected else { throw AppshotBrokerError.unauthorized }
        return try windowsProcess(parent)
    }
    func disconnect(_ id: UInt64) { if let handle { as_pipe_server_disconnect(handle, id) } }
}

struct WindowsChord: Equatable, Codable {
    let modifiers: UInt32
    let key: UInt32
    let description: String
    static let standard = try! parse("Ctrl+Shift+Z")
    static func parse(_ text: String) throws -> Self {
        let parts = text.split(separator: "+", omittingEmptySubsequences: false).map(String.init)
        guard (2...4).contains(parts.count), let last = parts.last else { throw WindowsTransportError.invalidFrame }
        var modifiers: UInt32 = 0
        for modifier in parts.dropLast() {
            let value: UInt32
            switch modifier.lowercased() {
            case "ctrl", "control": value = 2
            case "alt": value = 1
            case "shift": value = 4
            default: throw WindowsTransportError.invalidFrame
            }
            guard modifiers & value == 0 else { throw WindowsTransportError.invalidFrame }
            modifiers |= value
        }
        let keyName = last.uppercased()
        let key: UInt32
        if keyName.utf8.count == 1, let value = keyName.unicodeScalars.first?.value,
            (65...90).contains(value) || (48...57).contains(value) { key = value }
        else if keyName.first == "F", let number = UInt32(keyName.dropFirst()), (1...24).contains(number),
            keyName == "F\(number)" { key = 111 + number }
        else { throw WindowsTransportError.invalidFrame }
        let names = [(UInt32(2), "Ctrl"), (UInt32(1), "Alt"), (UInt32(4), "Shift")]
            .filter { modifiers & $0.0 != 0 }.map(\.1)
        return .init(modifiers: modifiers, key: key, description: (names + [keyName]).joined(separator: "+"))
    }
}

/// Calls are confined to the synchronous broker event-loop thread.
final class WindowsShortcut {
    private var handle: UnsafeMutableRawPointer?
    private(set) var enabled = false
    private(set) var chord = WindowsChord.standard
    init() throws {
        var error: Int32 = 0
        handle = as_hotkey_open(&error)
        guard handle != nil else { throw WindowsTransportError.native(error) }
    }
    deinit { close() }
    func close() { if let handle { as_hotkey_close(handle); self.handle = nil }; enabled = false }
    func replace(enabled: Bool, chord: WindowsChord) throws {
        guard let handle else { throw WindowsTransportError.closed }
        let code = as_hotkey_replace(handle, chord.modifiers, chord.key, enabled ? 1 : 0)
        guard code == 0 else { throw WindowsTransportError.native(code) }
        self.enabled = enabled; self.chord = chord
    }
    func poll() throws -> Bool {
        guard let handle else { return false }
        let code = as_hotkey_poll(handle)
        guard code >= 0 else { throw WindowsTransportError.native(-code) }
        return code > 0
    }
}

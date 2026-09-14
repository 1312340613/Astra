import Foundation
import AstraAppshotCore
import AstraWindowsNative

struct WindowsBrokerDescriptor: Codable, Equatable {
    let version: Int
    let platform: String
    let scope: String
    let instance_id: String
    let broker_nonce: String
    let process: AppshotWindowsProcess
    static func parse(_ data: Data, scope: String) throws -> Self {
        // Reuse the strict duplicate-key scanner and v2 field rules. A descriptor
        // is discovery data; the connected pipe must independently prove process.
        guard data.count <= 4096,
            let value = try JSONSerialization.jsonObject(with: data) as? [String: Any],
            Set(value.keys) == ["version", "platform", "scope", "instance_id", "broker_nonce", "process"],
            let process = value["process"] as? [String: Any] else { throw WindowsTransportError.invalidFrame }
        let decoded = try JSONDecoder().decode(Self.self, from: data)
        guard decoded.version == 2, decoded.platform == "windows", decoded.scope == scope else {
            throw WindowsTransportError.invalidFrame
        }
        _ = try AppshotWindowsProcess.decodeStrict(JSONSerialization.data(withJSONObject: process))
        _ = try AppshotWindowsMessage.decodeStrict(JSONSerialization.data(withJSONObject: [
            "type": "hello_ack", "version": 2, "platform": "windows", "session_id": "descriptor",
            "instance_id": decoded.instance_id, "broker_nonce": decoded.broker_nonce,
        ]))
        // The descriptor writer always emits canonical sorted JSON. This also
        // rejects duplicate keys, alternate number spellings and hidden suffixes.
        let encoder = JSONEncoder(); encoder.outputFormatting = [.sortedKeys]
        guard try encoder.encode(decoded) == data else { throw WindowsTransportError.invalidFrame }
        return decoded
    }
    func encode() throws -> Data {
        let encoder = JSONEncoder(); encoder.outputFormatting = [.sortedKeys]
        let data = try encoder.encode(self)
        _ = try Self.parse(data, scope: scope)
        return data
    }
}

struct WindowsAppshotSettings: Equatable {
    var enabled = false
    var chord = WindowsChord.standard
    static func parse(_ data: Data) throws -> Self {
        guard data.count <= 1024, let raw = try JSONSerialization.jsonObject(with: data) as? [String: Any],
            Set(raw.keys) == ["version", "enabled", "shortcut"],
            let enabled = raw["enabled"] as? Bool, let shortcut = raw["shortcut"] as? String else {
            throw WindowsTransportError.invalidFrame
        }
        let value = Self(enabled: enabled, chord: try WindowsChord.parse(shortcut))
        guard try value.encode() == data else { throw WindowsTransportError.invalidFrame }
        return value
    }
    func encode() throws -> Data {
        try JSONSerialization.data(withJSONObject: ["version": 2, "enabled": enabled,
            "shortcut": chord.description], options: [.sortedKeys])
    }
}

/// The named-pipe election must already be held before replacing discovery or
/// settings. Reads/publication/deletion retain exact native file authority.
final class WindowsBrokerRuntime {
    let directory: WindowsPrivateDirectory
    let descriptor: WindowsBrokerDescriptor
    private var discovery: WindowsFileReceipt?
    static var defaultPath: String {
        get throws {
            guard let local = ProcessInfo.processInfo.environment["LOCALAPPDATA"], !local.isEmpty else {
                throw AppshotBrokerError.unsafeRuntime
            }
            return local.replacingOccurrences(of: "\\", with: "/") + "/AstraAppshot"
        }
    }
    static func read(path: String, scope: String) throws -> WindowsBrokerDescriptor {
        let directory = try WindowsPrivateDirectory(path: path)
        let (bytes, proof) = try directory.read(name: "broker.json", maximum: 4096)
        let descriptor = try WindowsBrokerDescriptor.parse(bytes, scope: scope)
        guard try WindowsAppshot.identity(UInt32(descriptor.process.pid)) == descriptor.process else {
            throw AppshotBrokerError.unauthorized
        }
        let (_, again) = try directory.read(name: "broker.json", maximum: 4096)
        guard proof.sameRead(as: again) else { throw AppshotBrokerError.unsafeRuntime }
        return descriptor
    }
    init(path: String, scope: String) throws {
        directory = try WindowsPrivateDirectory(path: path, create: true)
        // Unknown/crash-left capture files are retained, not guessed/deleted.
        // Refuse a new producer until their custody is resolved, so restarts
        // cannot accumulate unaccounted screenshots indefinitely.
        try directory.checkQuota(controlOnly: true)
        descriptor = WindowsBrokerDescriptor(version: 2, platform: "windows", scope: scope,
            instance_id: UUID().uuidString, broker_nonce: UUID().uuidString,
            process: try WindowsAppshot.identity(as_current_pid()))
        do {
            let (data, proof) = try directory.read(name: "broker.json", maximum: 4096)
            let old = try WindowsBrokerDescriptor.parse(data, scope: scope)
            guard old.process.userSID == descriptor.process.userSID else { throw AppshotBrokerError.unsafeRuntime }
            var process = ASProcess()
            // Obtain a structured native expected identity even when its PID is dead.
            process.pid = UInt32(old.process.pid)
            process.created = UInt64(old.process.processStart)!
            let sid = Array(old.process.userSID.utf8) + [0]
            withUnsafeMutableBytes(of: &process.sid) { $0.copyBytes(from: sid) }
            guard as_process_has_exited(&process) == 1, directory.remove(proof) else {
                throw AppshotBrokerError.notOwner
            }
        } catch WindowsArtifactError.system(14) { /* no previous descriptor */ }
        discovery = try directory.publish(name: "broker.json", data: descriptor.encode())
    }
    deinit { close() }
    func close() { if let discovery, directory.remove(discovery) { self.discovery = nil } }
    func settings() throws -> WindowsAppshotSettings {
        do { return try WindowsAppshotSettings.parse(directory.read(name: "settings.json", maximum: 1024).0) }
        catch WindowsArtifactError.system(14) { return WindowsAppshotSettings() }
    }
    func save(_ settings: WindowsAppshotSettings) throws {
        let data = try settings.encode()
        var previous: Data?
        do {
            let (bytes, proof) = try directory.read(name: "settings.json", maximum: 1024)
            _ = try WindowsAppshotSettings.parse(bytes)
            guard directory.remove(proof) else { throw AppshotBrokerError.unsafeRuntime }
            previous = bytes
        } catch WindowsArtifactError.system(14) {}
        do { _ = try directory.publish(name: "settings.json", data: data) }
        catch {
            // Exclusive restoration only; never overwrite an intervening file.
            if let previous { _ = try? directory.publish(name: "settings.json", data: previous) }
            throw error
        }
        // A crash between removal/publication leaves defaults disabled, never an
        // implicitly enabled shortcut. The broker is the sole settings writer.
    }
}

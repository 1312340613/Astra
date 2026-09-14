import Foundation
import AstraAppshotCore
import AstraWindowsNative

@MainActor protocol WindowsCaptureOperation: AnyObject {
    func poll() -> Result<AppshotCapturedArtifact, Error>?
    func cancel()
}

/// Transport/control composition. A synchronous owner loop keeps the hotkey's
/// native thread affinity while all capture I/O belongs to an injected operation.
/// Production uses the bounded capture worker; consumers independently verify
/// draft and durable session custody before their corresponding acknowledgments.
@MainActor final class WindowsBroker {
    typealias Capture = (AppshotRecipient<AppshotWindowsProcess>) throws -> WindowsCaptureOperation
    private final class Connection {
        let peer: AppshotWindowsProcess
        let opened: UInt64
        var decoder = WindowsFrameDecoder()
        var authenticated = false
        var partialSince: UInt64?
        var rateSince: UInt64
        var frames = 0
        init(peer: AppshotWindowsProcess, now: UInt64) { self.peer = peer; opened = now; rateSince = now }
    }
    let runtime: WindowsBrokerRuntime
    private let pipe: WindowsPipeServer
    private let shortcut: WindowsShortcut
    private let registry: AppshotRegistry<AppshotWindowsProcess>
    private let capture: Capture
    private let clock: () -> UInt64
    private let outcome: (String) -> Void
    private var connections: [UInt64: Connection] = [:]
    private var settings: WindowsAppshotSettings
    private var registration = "unavailable"
    private var active: (binding: AppshotRecipient<AppshotWindowsProcess>, operation: WindowsCaptureOperation)?
    private var pending: AppshotRecipient<AppshotWindowsProcess>?
    private var grace: UInt64?
    private(set) var stopped = false
    var connectionCount: Int { registry.connectedCount }
    var liveBytes: Int { registry.liveBytes }

    init(path: String, scope: String, clock: @escaping () -> UInt64 = { as_monotonic_ns() },
        outcome: @escaping (String) -> Void = { _ in }, capture: @escaping Capture) throws {
        self.clock = clock; self.outcome = outcome; self.capture = capture
        pipe = try WindowsPipeServer(scope: scope) // elect first, before changing any runtime files
        runtime = try WindowsBrokerRuntime(path: path, scope: scope)
        shortcut = try WindowsShortcut()
        settings = try runtime.settings()
        let userSID = runtime.descriptor.process.userSID
        registry = AppshotRegistry(instanceID: runtime.descriptor.instance_id, nonce: runtime.descriptor.broker_nonce,
            identityLookup: { pid in pid > 0 ? try? WindowsAppshot.identity(UInt32(pid)) : nil },
            monotonicNS: clock, peerAllowed: { $0.userSID == userSID })
        grace = clock() &+ 2_000_000_000
    }
    func stop() {
        guard !stopped else { return }
        stopped = true
        shortcut.close()
        cancelPending(reason: "stopped")
        for id in Array(connections.keys) { disconnect(id) }
        runtime.close(); pipe.close()
    }
    func poll() throws {
        guard !stopped else { return }
        do {
            for _ in 0..<64 {
                guard let event = try pipe.poll() else { break }
                switch event {
                case .connected(let id, let peer): connections[id] = Connection(peer: peer, now: clock())
                case .disconnected(let id): disconnect(id)
                case .bytes(let id, let bytes):
                    do { try receive(bytes, from: id) } catch { disconnect(id) }
                }
            }
            let now = clock()
            for (id, connection) in Array(connections) {
                if !connection.authenticated && now &- connection.opened >= 3_000_000_000 { disconnect(id); continue }
                if let began = connection.partialSince, now &- began >= 3_000_000_000 { disconnect(id) }
            }
            for id in registry.deadConnections() + registry.confirmationExpiredConnections() {
                if let id = UInt64(id) { disconnect(id) }
            }
            for binding in registry.expire() {
                if pending == binding { cancelPending(reason: "capture_timeout") }
                else { revoke(binding, reason: "expired") }
            }
            if let operation = active, let result = operation.operation.poll() {
                active = nil
                guard pending == operation.binding, !stopped else {
                    if case .success(let artifact) = result { artifact.destroy() }
                    return
                }
                do {
                    let artifact = try result.get()
                    try registry.stage(operation.binding, artifact: artifact)
                    try send(type: "attach_offer", binding: operation.binding, fields: ["manifest_path": artifact.manifestPath])
                } catch { cancelPending(reason: "capture_failed") }
            }
            if try shortcut.poll() { trigger() }
            if let grace, now >= grace, registry.connectedCount == 0 { stop() }
        } catch { stop(); throw error }
    }
    private func receive(_ bytes: Data, from id: UInt64) throws {
        guard let connection = connections[id] else { throw AppshotBrokerError.unauthorized }
        let messages = try connection.decoder.feed(bytes)
        if connection.decoder.hasPartialFrame { if connection.partialSince == nil { connection.partialSince = clock() } }
        else { connection.partialSince = nil }
        if clock() &- connection.rateSince >= 1_000_000_000 { connection.rateSince = clock(); connection.frames = 0 }
        connection.frames += messages.count
        guard connection.frames <= 256 else { throw AppshotBrokerError.quotaExceeded }
        for message in messages {
            guard connections[id] != nil, !stopped else { return }
            let fields = try JSONSerialization.jsonObject(with: message.data) as! [String: Any]
            if !connection.authenticated {
                guard message.type == "hello", let pid = fields["pid"] as? Int else { throw AppshotBrokerError.unauthorized }
                var peer = connection.peer
                if pid != Int(peer.pid) {
                    // Node uses this helper as a private stdio/pipe relay. Only
                    // the same helper image may prove its kernel-derived parent;
                    // client JSON never supplies the authoritative parent proof.
                    guard as_process_same_executable(UInt32(peer.pid)) != 0 else { throw AppshotBrokerError.unauthorized }
                    peer = try pipe.parent(of: id, peer: peer)
                }
                guard fields["user_sid"] as? String == peer.userSID else { throw AppshotBrokerError.unauthorized }
                try registry.authenticate(connectionID: String(id), peer: peer,
                    sessionID: fields["session_id"] as! String, pid: pid,
                    processStart: fields["process_start"] as! String, clientNonce: fields["client_nonce"] as! String)
                connection.authenticated = true; grace = nil
                if registry.connectedCount == 1 { startShortcut() }
                try pipe.send(["type": "hello_ack", "version": 2, "platform": "windows",
                    "instance_id": registry.instanceID, "broker_nonce": registry.nonce,
                    "session_id": fields["session_id"]!], to: id)
                continue
            }
            let allowed = ["client_state", "attach_ack", "release", "command"]
            guard allowed.contains(message.type), let session = fields["session_id"] as? String,
                let broker = fields["broker_id"] as? String, let request = fields["request_id"] as? String else {
                throw AppshotBrokerError.unauthorized
            }
            try registry.validate(connectionID: String(id), brokerID: broker, sessionID: session)
            let binding = responseBinding(id, session: session, request: request)
            switch message.type {
            case "client_state":
                let incorporated = try registry.update(connectionID: String(id), requestID: request,
                    brokerID: broker, sessionID: session, activity: UInt64(fields["activity_ns"] as! String)!,
                    count: fields["appshot_count"] as! Int, canAccept: fields["can_accept"] as! Bool)
                if incorporated, pending?.requestID == request { pending = nil; outcome("incorporated") }
                guard let state = registry.state(connectionID: String(id)) else { throw AppshotBrokerError.unauthorized }
                try send(type: "client_state_ack", binding: binding,
                    fields: ["appshot_count": min(4, state.count), "can_accept": state.canAccept && state.count < 4])
            case "attach_ack":
                if registry.isCommitted(connectionID: String(id), requestID: request) { continue }
                let artifact = try registry.acknowledge(connectionID: String(id), requestID: request,
                    accepted: fields["accepted"] as! Bool)
                if let artifact { try send(type: "attach_commit", binding: binding, fields: ["manifest_path": artifact.manifestPath]) }
                else {
                    if pending?.requestID == request { cancelPending(reason: "rejected") }
                    else { revoke(binding, reason: "rejected") }
                }
            case "release":
                let released = registry.release(connectionID: String(id), requestID: request)
                if released, pending?.requestID == request { cancelPending(reason: "released") }
                try send(type: "release_ack", binding: binding, fields: ["released": released])
            case "command": try command(fields, binding: binding)
            default: throw AppshotBrokerError.unauthorized
            }
        }
    }
    // A response binding's identity is not used as capture/cleanup authority.
    private func responseBinding(_ id: UInt64, session: String, request: String) -> AppshotRecipient<AppshotWindowsProcess> {
        .init(requestID: request, connectionID: String(id), sessionID: session,
            identity: runtime.descriptor.process, instanceID: registry.instanceID)
    }
    private func send(type: String, binding: AppshotRecipient<AppshotWindowsProcess>, fields: [String: Any] = [:]) throws {
        guard let id = UInt64(binding.connectionID), connections[id] != nil else { throw AppshotBrokerError.recipientDisconnected }
        var message: [String: Any] = ["type": type, "version": 2, "platform": "windows",
            "request_id": binding.requestID, "broker_id": binding.instanceID, "session_id": binding.sessionID]
        message.merge(fields) { _, new in new }
        try pipe.send(message, to: id)
    }
    private func command(_ fields: [String: Any], binding: AppshotRecipient<AppshotWindowsProcess>) throws {
        let name = fields["name"] as! String
        guard let state = registry.state(connectionID: binding.connectionID) else { throw AppshotBrokerError.unauthorized }
        if name == "status" {
            try send(type: "status", binding: binding, fields: ["enabled": settings.enabled,
                "chord": settings.chord.description, "registration": registration,
                "connected_tuis": registry.connectedCount, "permission": "unknown",
                "appshot_count": min(4, state.count), "can_accept": state.canAccept && state.count < 4])
            return
        }
        var replacement = settings
        do {
            switch name {
            case "enable": replacement.enabled = true
            case "disable": replacement.enabled = false
            case "shortcut": replacement.chord = try WindowsChord.parse(fields["argument"] as! String)
            default: throw AppshotProtocolError.invalidValue
            }
            try shortcut.replace(enabled: replacement.enabled, chord: replacement.chord)
            do { try runtime.save(replacement) }
            catch {
                // Rollback to the old registration; if rollback itself fails,
                // stop the service instead of reporting contradictory settings.
                do { try shortcut.replace(enabled: settings.enabled, chord: settings.chord) }
                catch { stop() }
                throw error
            }
            settings = replacement
            registration = settings.enabled ? "registered" : "unavailable"
            if !settings.enabled { cancelPending(reason: "disabled") }
            try send(type: "command_result", binding: binding,
                fields: ["ok": true, "code": "ok", "message": "Appshot settings updated."])
        } catch {
            let code = (error as? WindowsTransportError).map { error -> String in
                if case .native(23) = error { return "shortcut_conflict" }
                if case .invalidFrame = error { return "invalid_shortcut" }
                return "settings_failed"
            } ?? "settings_failed"
            try send(type: "command_result", binding: binding, fields: ["ok": false, "code": code, "message": code])
        }
    }
    private func startShortcut() {
        do {
            try shortcut.replace(enabled: settings.enabled, chord: settings.chord)
            registration = settings.enabled ? "registered" : "unavailable"
        } catch {
            registration = (error as? WindowsTransportError).map { if case .native(23) = $0 { return "conflict" }; return "unavailable" } ?? "unavailable"
        }
    }
    func trigger() {
        guard !stopped, settings.enabled, registration == "registered", pending == nil else { return }
        do {
            let binding = try registry.reserveCapture(); pending = binding
            active = (binding, try capture(binding))
        } catch { cancelPending(reason: "capture_unavailable") }
    }
    private func cancelPending(reason: String) {
        let operation = active; active = nil
        let binding = pending; pending = nil
        operation?.operation.cancel()
        if let binding { registry.cancelCapture(binding); revoke(binding, reason: reason) }
        if binding != nil { outcome(reason) }
    }
    private func revoke(_ binding: AppshotRecipient<AppshotWindowsProcess>, reason: String) {
        do { try send(type: "attach_revoke", binding: binding, fields: ["reason": reason]) }
        catch { if let id = UInt64(binding.connectionID) { disconnect(id) } }
    }
    private func disconnect(_ id: UInt64) {
        guard connections.removeValue(forKey: id) != nil else { return }
        if pending?.connectionID == String(id) { cancelPending(reason: "recipient_disconnected") }
        registry.disconnect(connectionID: String(id)); pipe.disconnect(id)
        if registry.connectedCount == 0 {
            try? shortcut.replace(enabled: false, chord: settings.chord)
            registration = "unavailable"
            grace = clock() &+ 2_000_000_000
        }
    }
}

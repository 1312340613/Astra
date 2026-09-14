import Foundation
import AstraAppshotCore
import AstraWindowsNative

private final class WindowsTestWorkerResult: @unchecked Sendable {
    private let lock = NSLock()
    private var value: Result<Data, Error>?
    func put(_ value: Result<Data, Error>) { lock.lock(); defer { lock.unlock() }; self.value = value }
    func get() -> Result<Data, Error>? { lock.lock(); defer { lock.unlock() }; return value }
}

enum WindowsRelayFixture {
    static func run(path: String, scope: String) throws {
        let descriptor = try WindowsBrokerRuntime.read(path: path, scope: scope)
        let parent = try WindowsAppshot.parentIdentity()
        let client = try WindowsPipeClient(scope: scope, expected: descriptor.process)
        defer { client.close() }
        try client.send(["type": "hello", "version": 2, "platform": "windows", "session_id": "relay-fixture",
            "pid": parent.pid, "process_start": parent.processStart, "user_sid": parent.userSID,
            "client_nonce": descriptor.broker_nonce])
        var hello = false
        let until = ProcessInfo.processInfo.systemUptime + 1
        while ProcessInfo.processInfo.systemUptime < until {
            for message in try client.poll() {
                let fields = try JSONSerialization.jsonObject(with: message.data) as! [String: Any]
                if message.type == "hello_ack" {
                    guard fields["instance_id"] as? String == descriptor.instance_id,
                        fields["broker_nonce"] as? String == descriptor.broker_nonce,
                        fields["session_id"] as? String == "relay-fixture" else { throw AppshotBrokerError.unauthorized }
                    hello = true
                    try client.send(["type": "command", "version": 2, "platform": "windows",
                        "session_id": "relay-fixture", "broker_id": descriptor.instance_id,
                        "request_id": "relay-status", "name": "status", "argument": ""])
                } else if message.type == "status", hello {
                    guard fields["request_id"] as? String == "relay-status",
                        fields["broker_id"] as? String == descriptor.instance_id,
                        fields["session_id"] as? String == "relay-fixture" else { throw AppshotBrokerError.unauthorized }
                    return
                } else { throw AppshotBrokerError.unauthorized }
            }
            Thread.sleep(forTimeInterval: 0.002)
        }
        throw AppshotCaptureError.timedOut
    }
}

/// Explicit helper self-tests only. They never inspect a window or invoke the
/// real capture pipeline; native capture/storage acceptance is a separate test.
@MainActor enum WindowsServiceTests {
    final class Counters {
        var captures = 0, cleaned = 0, cancelled = 0, incorporated = 0
        var delay = false
        var offset: UInt64 = 0
    }
    final class FixtureCapture: WindowsCaptureOperation {
        let counters: Counters
        private var artifact: AppshotCapturedArtifact?
        init(path: String, counters: Counters) {
            self.counters = counters; counters.captures += 1
            artifact = AppshotCapturedArtifact(manifestPath: path, byteCount: 100) { counters.cleaned += 1 }
        }
        func poll() -> Result<AppshotCapturedArtifact, Error>? {
            guard !counters.delay, let artifact else { return nil }
            self.artifact = nil
            return .success(artifact)
        }
        func cancel() { counters.cancelled += 1; artifact?.destroy(); artifact = nil }
    }
    struct Failed: Error { let stage: String }
    static func check(_ value: Bool, _ stage: String) throws {
        if !value { throw Failed(stage: stage) }
    }
    static func framing() throws {
        let hello: [String: Any] = ["type": "hello", "version": 2, "platform": "windows",
            "session_id": "fixture", "pid": 1, "process_start": "1", "user_sid": "S-1-5-21-1", "client_nonce": "nonce"]
        let message = try AppshotWindowsMessage.decodeStrict(JSONSerialization.data(withJSONObject: hello))
        let frame = try message.encodeFrame()
        var decoder = WindowsFrameDecoder()
        try check(try decoder.feed(Data(frame.prefix(7))).isEmpty, "split_frame")
        try check(try decoder.feed(Data(frame.dropFirst(7))).count == 1, "split_frame_completion")
        try decoder.finish()
        var padded = message.data
        padded.append(Data(repeating: 32, count: 65535 - padded.count)); padded.append(10)
        try check(try decoder.feed(padded).count == 1, "maximum_frame_inclusive")
        do {
            var large = WindowsFrameDecoder()
            _ = try large.feed(Data(repeating: 32, count: 65536))
            throw Failed(stage: "oversize_accepted")
        } catch AppshotProtocolError.frameSize {}
        do {
            var legacy = hello; legacy["version"] = 1
            var legacyDecoder = WindowsFrameDecoder()
            _ = try legacyDecoder.feed(JSONSerialization.data(withJSONObject: legacy) + Data([10]))
            throw Failed(stage: "legacy_accepted")
        } catch AppshotProtocolError.invalidValue {}
        do {
            var partial = WindowsFrameDecoder()
            _ = try partial.feed(Data("{".utf8)); try partial.finish()
            throw Failed(stage: "partial_accepted")
        } catch AppshotProtocolError.incompleteFrame {}
    }
    static func run(path: String) throws -> [String: Any] {
        try framing()
        let fixtureDirectory = try WindowsPrivateDirectory(path: path, create: true)
        for name in ["broker.json", "settings.json"] {
            do {
                _ = try fixtureDirectory.read(name: name, maximum: 4096)
                throw Failed(stage: "preexisting_runtime_refused")
            } catch WindowsArtifactError.system(14) {}
        }
        let scope = "service-test-" + UUID().uuidString.lowercased()
        let counters = Counters()
        let makeCapture: WindowsBroker.Capture = { _ in
            FixtureCapture(path: path.replacingOccurrences(of: "\\", with: "/")
                + "/appshot-00112233445566778899aabbccddeeff.manifest.json", counters: counters)
        }
        let broker = try WindowsBroker(path: path, scope: scope,
            clock: { as_monotonic_ns() &+ counters.offset },
            outcome: { if $0 == "incorporated" { counters.incorporated += 1 } }, capture: makeCapture)
        defer { broker.stop() }
        let descriptor = try WindowsBrokerRuntime.read(path: path, scope: scope)
        try check(descriptor == broker.runtime.descriptor, "descriptor_roundtrip")
        let identity = try WindowsAppshot.identity(as_current_pid())
        func hello(_ nonce: String, session: String = "fixture") -> [String: Any] {
            ["type": "hello", "version": 2, "platform": "windows", "session_id": session,
             "pid": identity.pid, "process_start": identity.processStart, "user_sid": identity.userSID, "client_nonce": nonce]
        }
        // A bad nonce must close just that connection; it cannot stop the broker.
        let bad = try WindowsPipeClient(scope: scope, expected: descriptor.process)
        try bad.send(hello("wrong-nonce"))
        var rejected = false
        for _ in 0..<100 {
            try broker.poll()
            do { _ = try bad.poll() } catch { rejected = true; break }
            Thread.sleep(forTimeInterval: 0.002)
        }
        bad.close(); try check(rejected && !broker.stopped, "nonce_rejected")
        let client = try WindowsPipeClient(scope: scope, expected: descriptor.process)
        defer { client.close() }
        var received: [[String: Any]] = []
        func pump() throws {
            try broker.poll()
            received += try client.poll().map { try JSONSerialization.jsonObject(with: $0.data) as! [String: Any] }
        }
        func awaitMessage(_ type: String, request: String? = nil) throws -> [String: Any] {
            let until = ProcessInfo.processInfo.systemUptime + 1
            repeat {
                try pump()
                if let index = received.firstIndex(where: { $0["type"] as? String == type
                    && (request == nil || $0["request_id"] as? String == request) }) { return received.remove(at: index) }
                Thread.sleep(forTimeInterval: 0.002)
            } while ProcessInfo.processInfo.systemUptime < until
            throw Failed(stage: "missing_" + type)
        }
        func send(_ type: String, request: String, fields: [String: Any] = [:]) throws {
            var message: [String: Any] = ["type": type, "version": 2, "platform": "windows",
                "session_id": "fixture", "broker_id": descriptor.instance_id, "request_id": request]
            message.merge(fields) { _, new in new }; try client.send(message)
        }
        func state(_ request: String, count: Int = 0) throws {
            try send("client_state", request: request, fields: ["activity_ns": String(as_monotonic_ns()),
                "appshot_count": count, "can_accept": true])
            _ = try awaitMessage("client_state_ack", request: request)
        }
        func command(_ name: String, argument: String = "") throws -> [String: Any] {
            let request = UUID().uuidString
            try send("command", request: request, fields: ["name": name, "argument": argument])
            return try awaitMessage(name == "status" ? "status" : "command_result", request: request)
        }
        try client.send(hello(descriptor.broker_nonce))
        _ = try awaitMessage("hello_ack")
        try check(broker.connectionCount == 1, "handshake")
        try state("ready")
        let initial = try command("status")
        try check(initial["enabled"] as? Bool == false && initial["registration"] as? String == "unavailable", "disabled_default")
        try check(try command("shortcut", argument: "Ctrl+Alt+Shift+F22")["ok"] as? Bool == true, "shortcut_settings")
        try check(try command("enable")["ok"] as? Bool == true, "enable")
        try check(try command("shortcut", argument: "Win+Z")["ok"] as? Bool == false, "invalid_shortcut")
        let status = try command("status")
        try check(status["registration"] as? String == "registered"
            && status["chord"] as? String == "Ctrl+Alt+Shift+F22", "registration_preserved")
        broker.trigger()
        let offer = try awaitMessage("attach_offer")
        let request = offer["request_id"] as! String
        try check(counters.captures == 1 && counters.incorporated == 0, "offer_only")
        try send("attach_ack", request: request, fields: ["accepted": true, "reason": "fixture"])
        _ = try awaitMessage("attach_commit", request: request)
        try state("not-a-commit-receipt", count: 1)
        broker.trigger(); try check(counters.captures == 1, "waits_for_incorporation")
        try state(request, count: 1)
        try check(counters.incorporated == 1 && counters.cleaned == 0, "incorporation_receipt")
        try send("attach_ack", request: request, fields: ["accepted": true, "reason": "duplicate"])
        try pump(); try check(counters.cleaned == 0 && counters.incorporated == 1, "duplicate_ack")
        try send("release", request: request)
        _ = try awaitMessage("release_ack", request: request)
        try check(counters.cleaned == 1 && broker.liveBytes == 0, "release_exact_once")
        try send("release", request: request)
        _ = try awaitMessage("release_ack", request: request)
        try check(counters.cleaned == 1, "release_idempotent")
        try state("ready-again")
        broker.trigger()
        let negative = try awaitMessage("attach_offer")["request_id"] as! String
        try send("attach_ack", request: negative, fields: ["accepted": false, "reason": "fixture"])
        _ = try awaitMessage("attach_revoke", request: negative)
        try check(counters.cleaned == 2 && broker.liveBytes == 0, "negative_ack_cleanup")
        counters.delay = true
        broker.trigger(); try check(counters.captures == 3, "pending_capture")
        try check(try command("disable")["ok"] as? Bool == true, "disable")
        try check(counters.cancelled == 1 && counters.cleaned == 3 && broker.liveBytes == 0, "disable_cancels_capture")
        try check(try command("enable")["ok"] as? Bool == true, "reenable")
        broker.trigger()
        counters.offset = 6_000_000_000
        try pump()
        try check(counters.cancelled == 2 && counters.cleaned == 4 && broker.liveBytes == 0, "deadline_cancels_capture")
        broker.trigger()
        client.close()
        for _ in 0..<100 where broker.connectionCount != 0 { try broker.poll(); Thread.sleep(forTimeInterval: 0.002) }
        try check(broker.connectionCount == 0 && counters.cancelled == 3 && counters.cleaned == 5, "disconnect_cancels_capture")
        // Real helper child connection: hello claims the kernel-proven parent,
        // not the relay's PID and not an arbitrary client-provided identity.
        let result = WindowsTestWorkerResult()
        let workerArguments = "--self-test-broker-relay \"\(path)\" \(scope)"
        DispatchQueue.global().async {
            result.put(Result { try WindowsAppshot.runWorker(workerArguments,
                deadline: AppshotCaptureDeadline(duration: 2), maximum: 4096) })
        }
        let until = ProcessInfo.processInfo.systemUptime + 3
        while result.get() == nil && ProcessInfo.processInfo.systemUptime < until {
            try broker.poll(); Thread.sleep(forTimeInterval: 0.002)
        }
        guard let worker = result.get() else { throw Failed(stage: "relay_worker_deadline") }
        try check(try worker.get() == Data("relay-ok\n".utf8), "relay_parent_handshake")
        broker.stop()
        let restarted = try WindowsBroker(path: path, scope: scope, capture: makeCapture)
        try check(restarted.runtime.descriptor.instance_id != descriptor.instance_id, "fresh_epoch")
        try check(try restarted.runtime.settings().enabled, "settings_survive_restart")
        restarted.stop()
        let directory = try WindowsPrivateDirectory(path: path)
        // Simulated PID reuse: an exact old descriptor is replaced only after
        // native proof that its recorded creation identity is no longer alive.
        let stale = WindowsBrokerDescriptor(version: 2, platform: "windows", scope: scope,
            instance_id: "old-instance", broker_nonce: "old-nonce",
            process: AppshotWindowsProcess(pid: identity.pid,
                processStart: String(UInt64(identity.processStart)! - 1), userSID: identity.userSID))
        _ = try directory.publish(name: "broker.json", data: stale.encode())
        let recovered = try WindowsBroker(path: path, scope: scope, capture: makeCapture)
        try check(recovered.runtime.descriptor.instance_id != stale.instance_id, "stale_descriptor_recovery")
        recovered.stop()
        let (_, settingsProof) = try directory.read(name: "settings.json", maximum: 1024)
        try check(directory.remove(settingsProof), "owned_settings_cleanup")
        let leftover = try directory.publish(name: "unknown-capture.json", data: Data("fixture".utf8))
        do {
            let unsafe = try WindowsBroker(path: path, scope: scope, capture: makeCapture)
            unsafe.stop()
            throw Failed(stage: "stale_artifacts_accepted")
        } catch WindowsArtifactError.system(10) {}
        let (preserved, preservedProof) = try directory.read(name: leftover.name, maximum: 1024)
        try check(preserved == Data("fixture".utf8) && leftover.sameRead(as: preservedProof), "stale_artifacts_preserved")
        try check(directory.remove(leftover), "owned_stale_fixture_cleanup")
        // Startup validation failure must also relinquish the elected pipe.
        let afterRefusal = try WindowsBroker(path: path, scope: scope, capture: makeCapture)
        afterRefusal.stop()
        return ["ok": true, "windows_v2_framing": true, "private_discovery": true, "nonce_rejected": true,
            "hotkey_commands": true, "offer_ack_commit_receipt": true, "exact_once_release": true,
            "negative_ack_cleanup": true, "disable_cancellation": true, "deadline_cancellation": true,
            "disconnect_cancellation": true, "fresh_restart_epoch": true, "settings_persisted": true,
            "kernel_parent_relay": true, "stale_descriptor_recovery": true, "stale_artifacts_preserved": true,
            "failed_start_releases_election": true, "capture_is_synthetic": true]
    }
}

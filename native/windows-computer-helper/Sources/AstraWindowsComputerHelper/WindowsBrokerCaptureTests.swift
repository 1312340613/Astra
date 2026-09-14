import Foundation
import AstraAppshotCore
import AstraWindowsNative

/// Opt-in, real capture composition. The manual fixture click arms only the
/// self-owned HWND; a foreground change cannot redirect capture to another app.
/// The consumer here is a protocol fixture, not the production TUI or backend.
@MainActor enum WindowsBrokerCaptureTests {
    static func run(path: String) throws -> [String: Any] {
        let directory = try WindowsPrivateDirectory(path: path, create: true)
        try directory.checkQuota(controlOnly: true)
        for name in ["broker.json", "settings.json"] {
            do {
                _ = try directory.read(name: name, maximum: 4096)
                throw WindowsServiceTests.Failed(stage: "preexisting_runtime_refused")
            } catch WindowsArtifactError.system(14) {}
        }
        let window = as_fixture_open()
        defer { as_fixture_close(window) }
        guard window > 0 else { throw WindowsServiceTests.Failed(stage: "fixture_window_unavailable") }
        let readyUntil = ProcessInfo.processInfo.systemUptime + 60
        while as_fixture_status(window) != 63 && ProcessInfo.processInfo.systemUptime < readyUntil {
            if as_fixture_status(window) == 0 { throw WindowsServiceTests.Failed(stage: "fixture_closed") }
            Thread.sleep(forTimeInterval: 0.05)
        }
        guard as_fixture_status(window) == 63 else { throw WindowsServiceTests.Failed(stage: "fixture_not_started") }
        let source = try WindowsAppshot.target()
        guard source.handle == window, source.process.pid == as_current_pid() else {
            throw WindowsServiceTests.Failed(stage: "fixture_not_foreground")
        }
        let worker = WindowsCaptureWorker(path: path)
        let scope = "capture-test-" + UUID().uuidString.lowercased()
        var incorporated = 0
        let broker = try WindowsBroker(path: path, scope: scope,
            outcome: { if $0 == "incorporated" { incorporated += 1 } },
            capture: { try worker.start($0, expectedSource: source) })
        defer {
            broker.stop(); worker.stopAccepting()
            let until = ProcessInfo.processInfo.systemUptime + 2
            while !worker.isIdle && ProcessInfo.processInfo.systemUptime < until { Thread.sleep(forTimeInterval: 0.002) }
        }
        let descriptor = try WindowsBrokerRuntime.read(path: path, scope: scope)
        let recipient = try WindowsAppshot.identity(as_current_pid())
        let client = try WindowsPipeClient(scope: scope, expected: descriptor.process)
        defer { client.close() }
        var received: [[String: Any]] = []
        func awaitMessage(_ type: String, request: String? = nil, timeout: Double = 1) throws -> [String: Any] {
            let until = ProcessInfo.processInfo.systemUptime + timeout
            repeat {
                try broker.poll()
                received += try client.poll().map { try JSONSerialization.jsonObject(with: $0.data) as! [String: Any] }
                if received.contains(where: { $0["type"] as? String == "attach_revoke" }) {
                    throw WindowsServiceTests.Failed(stage: "capture_revoked")
                }
                if let index = received.firstIndex(where: { $0["type"] as? String == type
                    && (request == nil || $0["request_id"] as? String == request) }) { return received.remove(at: index) }
                Thread.sleep(forTimeInterval: 0.002)
            } while ProcessInfo.processInfo.systemUptime < until
            throw WindowsServiceTests.Failed(stage: "missing_" + type)
        }
        func send(_ type: String, request: String, fields: [String: Any] = [:]) throws {
            var message: [String: Any] = ["type": type, "version": 2, "platform": "windows",
                "session_id": "capture-fixture", "broker_id": descriptor.instance_id, "request_id": request]
            message.merge(fields) { _, new in new }; try client.send(message)
        }
        func state(_ request: String, count: Int) throws {
            try send("client_state", request: request, fields: ["activity_ns": String(as_monotonic_ns()),
                "appshot_count": count, "can_accept": true])
            _ = try awaitMessage("client_state_ack", request: request)
        }
        try client.send(["type": "hello", "version": 2, "platform": "windows", "session_id": "capture-fixture",
            "pid": recipient.pid, "process_start": recipient.processStart, "user_sid": recipient.userSID,
            "client_nonce": descriptor.broker_nonce])
        _ = try awaitMessage("hello_ack")
        for (name, argument) in [("shortcut", "Ctrl+Alt+Shift+F22"), ("enable", "")] {
            try send("command", request: name, fields: ["name": name, "argument": argument])
            try WindowsServiceTests.check(try awaitMessage("command_result", request: name)["ok"] as? Bool == true,
                "command_" + name)
        }
        try state("ready", count: 0)
        broker.trigger()
        let offer = try awaitMessage("attach_offer", timeout: 6)
        guard let manifestPath = offer["manifest_path"] as? String,
            let manifestName = manifestPath.split(separator: "/").last.map(String.init),
            let request = offer["request_id"] as? String,
            offer["session_id"] as? String == "capture-fixture",
            offer["broker_id"] as? String == descriptor.instance_id,
            manifestPath == directory.path + "/" + manifestName else { throw WindowsArtifactError.unsafe }
        let verified = try WindowsVerifiedArtifact.read(directory: directory, name: manifestName,
            brokerID: descriptor.instance_id, sessionID: "capture-fixture", recipient: recipient)
        let dimensions = try WindowsAppshot.dimensions(verified.png)
        let text = String(decoding: verified.uia, as: UTF8.self)
        try WindowsServiceTests.check(text.contains("Astra fixture: readable UI text 12345")
            && !text.contains("fixture-password-must-not-appear"), "readable_uia_password_redacted")
        let bridged = try WindowsAppshot.runWorker(
            "--appshot-read-artifact \"\(manifestPath)\" \(descriptor.instance_id) capture-fixture",
            deadline: AppshotCaptureDeadline(duration: 1.5), maximum: 16 * 1024 * 1024)
        guard let decoded = try JSONSerialization.jsonObject(with: bridged) as? [String: Any],
            let image = decoded["png_base64"] as? String, Data(base64Encoded: image) == verified.png,
            let uia = decoded["uia_json"] as? String, Data(uia.utf8) == verified.uia else {
            throw WindowsArtifactError.unsafe
        }
        try send("attach_ack", request: request, fields: ["accepted": true, "reason": "verified_fixture"])
        try WindowsServiceTests.check(try awaitMessage("attach_commit", request: request)["manifest_path"] as? String
            == manifestPath && incorporated == 0, "commit_after_verification")
        try state(request, count: 1)
        try WindowsServiceTests.check(incorporated == 1 && broker.liveBytes > 0, "incorporation_receipt")
        try send("release", request: request)
        _ = try awaitMessage("release_ack", request: request)
        let cleanupUntil = ProcessInfo.processInfo.systemUptime + 2
        while !worker.isIdle && ProcessInfo.processInfo.systemUptime < cleanupUntil { Thread.sleep(forTimeInterval: 0.002) }
        try WindowsServiceTests.check(worker.isIdle && broker.liveBytes == 0, "worker_cleanup_converged")
        for name in [manifestName, verified.manifest.png.name, verified.manifest.uia.name] {
            do {
                _ = try directory.read(name: name, maximum: 10 * 1024 * 1024)
                throw WindowsServiceTests.Failed(stage: "owned_artifact_left")
            } catch WindowsArtifactError.system(14) {}
        }
        try directory.checkQuota(controlOnly: true)
        // A real, fully published result that the consumer has not polled must
        // still be reclaimed when cancelled. Do not deliver it after cancellation.
        let lateBinding = AppshotRecipient(requestID: UUID().uuidString, connectionID: "fixture-cancel",
            sessionID: "capture-fixture", identity: recipient, instanceID: descriptor.instance_id)
        let late = try worker.start(lateBinding, expectedSource: source)
        defer { late.cancel() }
        let publishedUntil = ProcessInfo.processInfo.systemUptime + 5
        while !worker.isIdle && ProcessInfo.processInfo.systemUptime < publishedUntil { Thread.sleep(forTimeInterval: 0.002) }
        try WindowsServiceTests.check(worker.isIdle, "late_capture_worker_finished")
        do {
            try directory.checkQuota(controlOnly: true)
            throw WindowsServiceTests.Failed(stage: "late_capture_not_published")
        } catch WindowsArtifactError.system(10) {}
        late.cancel()
        try WindowsServiceTests.check(late.poll() == nil, "late_result_rejected")
        let cancelledUntil = ProcessInfo.processInfo.systemUptime + 2
        while !worker.isIdle && ProcessInfo.processInfo.systemUptime < cancelledUntil { Thread.sleep(forTimeInterval: 0.002) }
        try WindowsServiceTests.check(worker.isIdle, "late_cancel_cleanup_converged")
        try directory.checkQuota(controlOnly: true)
        broker.stop(); worker.stopAccepting()
        let (_, settingsProof) = try directory.read(name: "settings.json", maximum: 1024)
        try WindowsServiceTests.check(directory.remove(settingsProof), "owned_settings_cleanup")
        return ["ok": true, "capture_is_synthetic": false, "consumer_is_fixture": true,
            "width": dimensions.0, "height": dimensions.1, "real_broker_capture": true,
            "verified_child_read": true, "offer_ack_commit_receipt": true, "password_redacted": true,
            "serial_worker_cleanup": true, "late_result_cancel_cleanup": true, "owned_cleanup": true]
    }
}

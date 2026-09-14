import Foundation
import AstraAppshotCore
import AstraWindowsNative

func emit(_ object: Any) throws {
    let data = try JSONSerialization.data(withJSONObject: object, options: [.sortedKeys, .withoutEscapingSlashes])
    FileHandle.standardOutput.write(data + Data([10]))
}

@MainActor func runWindowsAppshotDaemon(path: String, scope: String) throws {
    let worker = WindowsCaptureWorker(path: path)
    let broker = try WindowsBroker(path: path, scope: scope, capture: { try worker.start($0) })
    defer { worker.stopAccepting(); broker.stop() }
    while !broker.stopped {
        try broker.poll()
        Thread.sleep(forTimeInterval: 0.01)
    }
    worker.stopAccepting()
    // Drain the owner queue on shutdown. Capture children have bounded deadlines.
    while !worker.isIdle { Thread.sleep(forTimeInterval: 0.01) }
}

do {
    let args = Array(CommandLine.arguments.dropFirst())
    switch args.first {
    case "--appshot-daemon" where args.count == 1 || args.count == 3:
        let path = try args.count == 3 ? args[1] : WindowsBrokerRuntime.defaultPath
        let scope = args.count == 3 ? args[2] : "production"
        Task { @MainActor in
            do {
                try runWindowsAppshotDaemon(path: path, scope: scope)
                exit(0)
            } catch {
                FileHandle.standardError.write(Data("appshot_service_unavailable\n".utf8))
                exit(1)
            }
        }
        dispatchMain()
    case "--appshot-client-identity" where args.count == 1:
        let process = try WindowsAppshot.parentIdentity()
        try emit(["version": 2, "platform": "windows", "pid": process.pid,
            "process_start": process.processStart, "user_sid": process.userSID,
            "monotonic_ns": String(as_monotonic_ns())])
    case "--service-info" where args.count == 1:
        // Full Computer Use is separate; this advertises only Appshot v2.
        try emit(["platform": "windows", "protocol_version": 2, "capture_backend": "windows_graphics_capture",
            "text_backend": "ui_automation", "production_ready": true, "media_storage_abi": as_media_storage_abi()])
    case "--appshot-broker-identity" where args.count == 3:
        let descriptor = try WindowsBrokerRuntime.read(path: args[1], scope: args[2])
        FileHandle.standardOutput.write(try descriptor.encode() + Data([10]))
    case "--appshot-relay" where args.count == 8:
        // Explicit verified runtime, with the discovered process held through connection.
        guard let pid = Int32(args[5]) else { throw AppshotBrokerError.unauthorized }
        let expected = try AppshotWindowsProcess.decodeStrict(JSONSerialization.data(withJSONObject:
            ["pid": pid, "process_start": args[6], "user_sid": args[7]]))
        try WindowsRelay.run(path: args[1], scope: args[2], brokerID: args[3], nonce: args[4], expected: expected)
    case "--self-test-consumer-broker" where args.count == 3:
        Task { @MainActor in
            do { try emit(WindowsConsumerTests.run(path: args[1], scope: args[2])); exit(0) }
            catch { try? emit(["ok": false]); exit(1) }
        }
        dispatchMain()
    case "--appshot-read-artifact" where args.count == 4, "--appshot-read-backend-artifact" where args.count == 4:
        // Read-only bridge for TUI/backend consumers. The caller process
        // is kernel-verified, and it must match the manifest's offered recipient.
        let path = args[1].replacingOccurrences(of: "\\", with: "/")
        guard let split = path.lastIndex(of: "/") else { throw WindowsArtifactError.unsafe }
        let directory = try WindowsPrivateDirectory(path: String(path[..<split]))
        func recipientIdentity() throws -> AppshotWindowsProcess {
            if args[0] == "--appshot-read-backend-artifact" {
                var proof = ASProcess()
                guard as_backend_recipient(&proof) == 0 else { throw AppshotBrokerError.unauthorized }
                return try windowsProcess(proof)
            }
            return try WindowsAppshot.parentIdentity()
        }
        let recipient = try recipientIdentity()
        let result = try WindowsVerifiedArtifact.read(directory: directory,
            name: String(path[path.index(after: split)...]), brokerID: args[2], sessionID: args[3], recipient: recipient)
        guard let uia = String(data: result.uia, encoding: .utf8) else { throw WindowsArtifactError.unsafe }
        guard try recipientIdentity() == recipient else { throw WindowsArtifactError.unsafe }
        try emit(["version": 2, "platform": "windows",
            "manifest": JSONSerialization.jsonObject(with: result.manifestData),
            "png_base64": result.png.base64EncodedString(), "uia_json": uia])
    case "--capture-worker" where args.count == 4, "--uia-worker" where args.count == 4:
        guard args.count == 4, let window = UInt64(args[1]), let pid = UInt32(args[2]) else {
            throw AppshotProtocolError.invalidValue
        }
        try WindowsAppshot.workerIdentity(window: window, pid: pid, created: args[3])
        let data: Data
        if args[0] == "--capture-worker" {
            data = try WindowsAppshot.bytes(as_capture_png(window, 4000), maximum: 10 * 1024 * 1024)
            _ = try WindowsAppshot.dimensions(data)
        } else {
            data = try WindowsAppshot.uia(window: window, deadline: AppshotCaptureDeadline(duration: 0.7))
        }
        try WindowsAppshot.workerIdentity(window: window, pid: pid, created: args[3])
        FileHandle.standardOutput.write(data)
    case "--hang-worker" where args.count == 1:
        Thread.sleep(forTimeInterval: 10)
    case "--self-test-ipc" where args.count == 1:
        FileHandle.standardOutput.write(try WindowsAppshot.bytes(as_pipe_self_test(), maximum: 4096) + Data([10]))
    case "--self-test-hotkey" where args.count == 1:
        FileHandle.standardOutput.write(try WindowsAppshot.bytes(as_hotkey_self_test(), maximum: 4096) + Data([10]))
    case "--self-test-broker-relay" where args.count == 3:
        try WindowsRelayFixture.run(path: args[1], scope: args[2])
        FileHandle.standardOutput.write(Data("relay-ok\n".utf8))
    case "--self-test-broker" where args.count == 2, "--self-test-broker-capture" where args.count == 2:
        Task { @MainActor in
            do {
                try emit(args[0] == "--self-test-broker-capture"
                    ? WindowsBrokerCaptureTests.run(path: args[1]) : WindowsServiceTests.run(path: args[1]))
                exit(0)
            }
            catch {
                let stage: String
                switch error {
                case let failure as WindowsServiceTests.Failed: stage = failure.stage
                case AppshotCaptureError.timedOut: stage = "capture_timeout"
                case AppshotCaptureError.cancelled: stage = "capture_cancelled"
                case AppshotCaptureError.sourceWindowChanged: stage = "source_window_changed"
                case AppshotCaptureError.indeterminateTarget: stage = "source_window_unavailable"
                case WindowsArtifactError.system(let code): stage = "storage_" + String(code)
                case WindowsTransportError.native(let code): stage = "transport_" + String(code)
                default: stage = "broker_composition"
                }
                try? emit(["ok": false, "stage": stage])
                exit(1)
            }
        }
        dispatchMain()
    case "--self-test-pipe-client" where args.count == 4:
        guard let pid = UInt32(args[2]), let created = UInt64(args[3]) else { throw AppshotProtocolError.invalidValue }
        var expected = ASProcess()
        guard as_process_identity(pid, &expected) != 0, expected.created == created else { throw AppshotBrokerError.unauthorized }
        var error: Int32 = 0
        guard let client = args[1].withCString({ as_pipe_client_open($0, &expected, 1000, &error) }) else {
            throw WindowsArtifactError.system(error)
        }
        defer { as_pipe_client_close(client) }
        for part in ["pi", "ng\n"] {
            let bytes = Data(part.utf8)
            guard bytes.withUnsafeBytes({ as_pipe_client_write(client, $0.bindMemory(to: UInt8.self).baseAddress,
                bytes.count, 1000, nil, nil) }) == 0 else { throw AppshotBrokerError.systemFailure }
        }
        var echo = Data()
        let until = ProcessInfo.processInfo.systemUptime + 1
        while echo.count < 5 && ProcessInfo.processInfo.systemUptime < until {
            let result = as_pipe_client_read(client, 100, nil, nil)
            defer { as_bytes_free(result) }
            guard result.error == 0, result.size <= 5 - echo.count else { throw AppshotBrokerError.systemFailure }
            if let bytes = result.data { echo.append(Data(bytes: bytes, count: Int(result.size))) }
        }
        guard echo == Data("ping\n".utf8) else { throw AppshotBrokerError.systemFailure }
        FileHandle.standardOutput.write(Data("pipe-child-ok\n".utf8))
    case "--oversize-worker" where args.count == 1:
        FileHandle.standardOutput.write(Data(repeating: 120, count: 8192))
    case "--self-test" where args.count == 1:
        let process = try WindowsAppshot.identity(as_current_pid())
        _ = try WindowsAppshot.parentIdentity()
        let start = ProcessInfo.processInfo.systemUptime
        do {
            _ = try WindowsAppshot.runWorker("--hang-worker",
                deadline: AppshotCaptureDeadline(duration: 0.2), maximum: 1024)
            throw AppshotProtocolError.invalidValue
        } catch AppshotCaptureError.timedOut {}
        guard ProcessInfo.processInfo.systemUptime - start < 1.5 else { throw AppshotCaptureError.timedOut }
        let cancelled = AppshotCaptureDeadline()
        DispatchQueue.global().asyncAfter(deadline: .now() + 0.1) { cancelled.cancel() }
        let cancelStart = ProcessInfo.processInfo.systemUptime
        do {
            _ = try WindowsAppshot.runWorker("--hang-worker", deadline: cancelled, maximum: 1024)
            throw AppshotProtocolError.invalidValue
        } catch AppshotCaptureError.cancelled {}
        guard ProcessInfo.processInfo.systemUptime - cancelStart < 1.5 else { throw AppshotCaptureError.timedOut }
        do {
            _ = try WindowsAppshot.runWorker("--oversize-worker", deadline: AppshotCaptureDeadline(), maximum: 1024)
            throw AppshotProtocolError.invalidValue
        } catch AppshotCaptureError.resourceLimit {}
        try emit(["ok": true, "process_identity": process.pid > 0, "owned_worker_timeout": true,
            "owned_worker_cancel": true, "worker_output_bounded": true])
    case "--self-test-capture" where args.count == 1, "--self-test-artifacts" where args.count == 2:
        let window = as_fixture_open()
        defer { as_fixture_close(window) }
        guard window > 0 else { throw AppshotCaptureError.captureFailed }
        // Manual fixture preparation is outside the five-second capture budget.
        // Only an explicit Start test click on our foreground window arms capture.
        let ready: UInt32 = 63
        let foregroundDeadline = ProcessInfo.processInfo.systemUptime + 60
        while as_fixture_status(window) != ready && ProcessInfo.processInfo.systemUptime < foregroundDeadline {
            if as_fixture_status(window) == 0 { throw AppshotCaptureError.cancelled }
            Thread.sleep(forTimeInterval: 0.05)
        }
        guard as_fixture_status(window) == ready else {
            FileHandle.standardError.write(Data("fixture_status=\(as_fixture_status(window))\n".utf8))
            throw AppshotCaptureError.indeterminateTarget
        }
        let target = try WindowsAppshot.target()
        guard target.handle == window, target.process.pid == as_current_pid() else {
            throw AppshotCaptureError.indeterminateTarget
        }
        let deadline = AppshotCaptureDeadline()
        let result = try WindowsAppshot.capture(target, deadline: deadline)
        let dimensions = try WindowsAppshot.dimensions(result.png)
        let text = String(decoding: result.uia, as: UTF8.self)
        guard text.contains("Astra fixture: readable UI text 12345"),
            !text.contains("fixture-password-must-not-appear") else { throw AppshotCaptureError.captureFailed }
        if args[0] == "--self-test-artifacts" {
            let directory = try WindowsPrivateDirectory(path: args[1], create: true)
            let identity = try WindowsAppshot.identity(as_current_pid())
            let recipient = AppshotRecipient(requestID: UUID().uuidString, connectionID: "fixture",
                sessionID: "fixture-session", identity: identity, instanceID: "fixture-broker")
            let files = try WindowsAppshotArtifacts.publish(directory: directory, source: target,
                capture: result, recipient: recipient, deadline: deadline)
            let verified = try WindowsVerifiedArtifact.read(directory: directory, name: files.manifestName,
                brokerID: recipient.instanceID, sessionID: recipient.sessionID, recipient: identity)
            guard verified.png == result.png, verified.uia == result.uia else { throw WindowsArtifactError.unsafe }
            let bridged = try WindowsAppshot.runWorker(
                "--appshot-read-artifact \"\(files.manifestPath)\" fixture-broker fixture-session",
                deadline: AppshotCaptureDeadline(), maximum: 16 * 1024 * 1024)
            guard let decoded = try JSONSerialization.jsonObject(with: bridged) as? [String: Any],
                let image = decoded["png_base64"] as? String, Data(base64Encoded: image) == result.png,
                let text = decoded["uia_json"] as? String, Data(text.utf8) == result.uia else {
                throw WindowsArtifactError.unsafe
            }
            do {
                _ = try WindowsVerifiedArtifact.read(directory: directory, name: files.manifestName,
                    brokerID: recipient.instanceID, sessionID: "wrong-session", recipient: identity)
                throw AppshotProtocolError.invalidValue
            } catch WindowsArtifactError.unsafe {}
            // Same bytes under a different file ID are not the offered artifact.
            let original = files.files.first { $0.name == verified.manifest.png.name }!
            guard directory.remove(original) else { throw WindowsArtifactError.unsafe }
            let replacement = try directory.publish(name: original.name, data: result.png)
            do {
                _ = try WindowsVerifiedArtifact.read(directory: directory, name: files.manifestName,
                    brokerID: recipient.instanceID, sessionID: recipient.sessionID, recipient: identity)
                throw AppshotProtocolError.invalidValue
            } catch WindowsArtifactError.unsafe {}
            guard !files.destroy() else { throw WindowsArtifactError.unsafe }
            _ = try directory.read(name: replacement.name, maximum: 10 * 1024 * 1024)
            guard directory.remove(replacement), files.destroy() else { throw WindowsArtifactError.unsafe }
            deadline.cancel()
            do {
                _ = try WindowsAppshotArtifacts.publish(directory: directory, source: target,
                    capture: result, recipient: recipient, deadline: deadline)
                throw AppshotProtocolError.invalidValue
            } catch AppshotCaptureError.cancelled {}
            var time: Double = 0
            let expiring = AppshotCaptureDeadline(clock: { defer { time += 1 }; return time })
            do {
                _ = try WindowsAppshotArtifacts.publish(directory: directory, source: target,
                    capture: result, recipient: recipient, deadline: expiring)
                throw AppshotProtocolError.invalidValue
            } catch AppshotCaptureError.timedOut {}
            try emit(["ok": true, "width": dimensions.0, "height": dimensions.1,
                "private_artifact_roundtrip": true, "wrong_session_rejected": true,
                "replacement_preserved": true, "cancelled_publish_rejected": true,
                "partial_timeout_cleanup": true, "owned_cleanup": true, "child_bridge_roundtrip": true])
        } else {
            // Counts only; the basic capture test does not persist the image/text.
            try emit(["ok": true, "width": dimensions.0, "height": dimensions.1,
                "png_bytes": result.png.count, "uia_bytes": result.uia.count, "password_redacted": true])
        }
    case "--self-test-files" where args.count == 2:
        let path = Array(args[1].utf16) + [0]
        let result = path.withUnsafeBufferPointer { as_private_files_self_test($0.baseAddress) }
        FileHandle.standardOutput.write(try WindowsAppshot.bytes(result, maximum: 4096) + Data([10]))
    default:
        throw AppshotProtocolError.unknownMessage
    }
} catch {
    // No titles, text, paths, SIDs or screenshot content in diagnostic errors.
    let code: String
    switch error {
    case AppshotCaptureError.timedOut: code = "capture_timeout"
    case AppshotCaptureError.cancelled: code = "capture_cancelled"
    case AppshotCaptureError.sourceWindowChanged: code = "source_window_changed"
    case AppshotCaptureError.indeterminateTarget: code = "source_window_unavailable"
    default: code = "appshot_failed"
    }
    FileHandle.standardError.write(Data((code + "\n").utf8))
    exit(1)
}

import Foundation
import AstraAppshotCore
import AstraWindowsNative

/// Synthetic image only. Exercises the real relay, broker, ACL storage and Node
/// artifact reader without reading any window or sending data to a model.
@MainActor enum WindowsConsumerTests {
    final class Fixture: WindowsCaptureOperation {
        private var artifact: AppshotCapturedArtifact?
        init(directory: WindowsPrivateDirectory, recipient: AppshotRecipient<AppshotWindowsProcess>, cleaned: @escaping () -> Void) throws {
            let token = UUID().uuidString.replacingOccurrences(of: "-", with: "").lowercased()
            let png = Data(base64Encoded: "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4nGNgYGBgAAAABQABpfZFQAAAAABJRU5ErkJggg==")!
            let uia = try JSONSerialization.data(withJSONObject: ["schema_version": 2, "platform": "windows",
                "coverage": "unavailable", "node_count": 0, "depth": 0, "truncated": true,
                "truncation_reasons": ["uia_unavailable"], "nodes": []], options: [.sortedKeys])
            var receipts: [WindowsFileReceipt] = []
            do {
                let image = try directory.publish(name: "appshot-\(token).png", data: png); receipts.append(image)
                let text = try directory.publish(name: "appshot-\(token).uia.json", data: uia); receipts.append(text)
                let own = try WindowsAppshot.identity(as_current_pid())
                let raw: [String: Any] = ["schema_version": 2, "platform": "windows", "token": token, "captured_at": "2026-09-12T00:00:00Z",
                    "source": ["process": ["pid": own.pid, "process_start": own.processStart, "user_sid": own.userSID],
                        "app_id": "fixture", "app_label": "Windows fixture", "window_title": "Synthetic test image",
                        "window_handle": "1", "bounds": ["x": 0, "y": 0, "width": 1, "height": 1]],
                    "png": ["name": image.name, "size": png.count, "width": 1, "height": 1, "sha256": image.sha256, "identity": image.identity],
                    "uia": ["name": text.name, "size": uia.count, "sha256": text.sha256, "identity": text.identity,
                        "coverage": "unavailable", "node_count": 0, "depth": 0, "truncated": true, "truncation_reasons": ["uia_unavailable"]],
                    "broker": ["instance_id": recipient.instanceID, "session_id": recipient.sessionID,
                        "recipient": ["pid": recipient.identity.pid, "process_start": recipient.identity.processStart, "user_sid": recipient.identity.userSID]]]
                let manifest = try AppshotWindowsManifest.decodeStrict(JSONSerialization.data(withJSONObject: raw))
                let name = "appshot-\(token).manifest.json"
                let data = try JSONEncoder().encode(manifest)
                receipts.append(try directory.publish(name: name, data: data))
                let owned = receipts
                artifact = AppshotCapturedArtifact(manifestPath: directory.path + "/" + name, byteCount: png.count + uia.count + data.count) {
                    if owned.reversed().map({ directory.remove($0) }).allSatisfy({ $0 }) { cleaned() }
                }
            } catch {
                for receipt in receipts.reversed() { directory.remove(receipt) }
                throw error
            }
        }
        func poll() -> Result<AppshotCapturedArtifact, Error>? {
            guard let artifact else { return nil }; self.artifact = nil; return .success(artifact)
        }
        func cancel() { artifact?.destroy(); artifact = nil }
    }
    static func run(path: String, scope: String) throws -> [String: Any] {
        let directory = try WindowsPrivateDirectory(path: path, create: true)
        // Test caller creates a fresh parent; do not adopt preexisting settings.
        for name in ["broker.json", "settings.json"] {
            do { _ = try directory.read(name: name, maximum: 4096); throw WindowsArtifactError.unsafe }
            catch WindowsArtifactError.system(14) {}
        }
        var captured = false, incorporated = false, cleaned = false
        let broker = try WindowsBroker(path: path, scope: scope, outcome: { if $0 == "incorporated" { incorporated = true } }, capture: { recipient in
            guard !captured else { throw WindowsArtifactError.unsafe }; captured = true
            return try Fixture(directory: directory, recipient: recipient, cleaned: { cleaned = true })
        })
        defer { broker.stop() }
        try emit(["ready": true])
        let until = ProcessInfo.processInfo.systemUptime + 12
        while !broker.stopped && ProcessInfo.processInfo.systemUptime < until {
            try broker.poll()
            if !captured { broker.trigger() }
            if incorporated && cleaned {
                // Flush release acknowledgement, then retire only this fixture.
                for _ in 0..<10 { try broker.poll(); Thread.sleep(forTimeInterval: 0.002) }
                broker.stop(); break
            }
            Thread.sleep(forTimeInterval: 0.002)
        }
        broker.stop()
        if let settings = try? directory.read(name: "settings.json", maximum: 1024).1 {
            guard directory.remove(settings) else { throw WindowsArtifactError.unsafe }
        }
        guard captured, incorporated, cleaned else { throw WindowsArtifactError.unsafe }
        try directory.checkQuota(controlOnly: true)
        return ["ok": true, "capture_is_synthetic": true, "real_node_consumer": true,
            "native_relay": true, "incorporated": incorporated, "owned_cleanup": cleaned]
    }
}

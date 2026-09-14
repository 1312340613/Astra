import Foundation
import AstraAppshotCore
import AstraWindowsNative

enum WindowsArtifactError: Error { case unsafe, system(Int32) }

struct WindowsFileReceipt {
    let name: String
    let proof: ASFileProof
    var sha256: String { WindowsAppshot.string(proof.sha256) }
    var fileID: String {
        withUnsafeBytes(of: proof.file_id) { bytes in bytes.map { String(format: "%02x", $0) }.joined() }
    }
    var identity: [String: Any] {
        ["volume_serial": String(proof.volume), "file_id": fileID,
         "owner_sid": WindowsAppshot.string(proof.owner_sid), "link_count": Int(proof.links)]
    }
    func matches(_ expected: AppshotWindowsFileIdentity, size: Int, hash: String) -> Bool {
        expected.volume_serial == String(proof.volume) && expected.file_id == fileID
            && expected.owner_sid == WindowsAppshot.string(proof.owner_sid)
            && expected.link_count == Int(proof.links) && proof.size == size && sha256 == hash
    }
    func sameRead(as other: WindowsFileReceipt) -> Bool {
        fileID == other.fileID && proof.volume == other.proof.volume && sha256 == other.sha256
            && proof.size == other.proof.size && proof.links == other.proof.links
            && proof.changed == other.proof.changed && proof.modified == other.proof.modified
            && WindowsAppshot.string(proof.owner_sid) == WindowsAppshot.string(other.proof.owner_sid)
    }
}

/// Serial, handle-pinned storage. Production callers must run blocking disk work
/// off the broker actor and under its owned worker deadline, not in the TUI loop.
final class WindowsPrivateDirectory {
    let path: String
    private let handle: UnsafeMutableRawPointer
    init(path: String, create: Bool = false) throws {
        guard !path.utf8.contains(0), path.utf8.count <= 4096 else { throw WindowsArtifactError.unsafe }
        self.path = path.replacingOccurrences(of: "\\", with: "/")
        let native = Array(self.path.utf16) + [0]
        var error: Int32 = 0
        guard let result = native.withUnsafeBufferPointer({ as_private_directory_open($0.baseAddress, create ? 1 : 0, &error) }) else {
            throw WindowsArtifactError.system(error)
        }
        handle = result
    }
    deinit { as_private_directory_close(handle) }
    func checkQuota(controlOnly: Bool = false, reserve: Int = 0, reserveEntries: Int = 0) throws {
        guard reserve >= 0, reserve < 256 * 1024 * 1024, (0..<256).contains(reserveEntries) else {
            throw WindowsArtifactError.unsafe
        }
        let code = as_private_check_quota(handle, UInt32(256 - reserveEntries),
            UInt64(256 * 1024 * 1024 - reserve), controlOnly ? 1 : 0)
        guard code == 0 else { throw WindowsArtifactError.system(code) }
    }
    func publish(name: String, data: Data) throws -> WindowsFileReceipt {
        guard !name.utf8.contains(0) else { throw WindowsArtifactError.unsafe }
        var proof = ASFileProof()
        let temporary = "pending-" + UUID().uuidString.lowercased() + ".tmp"
        let code = temporary.withCString { temporary in name.withCString { name in
            data.withUnsafeBytes { bytes in
                as_private_publish(handle, temporary, name, bytes.bindMemory(to: UInt8.self).baseAddress, data.count, &proof)
            }
        } }
        guard code == 0 else { throw WindowsArtifactError.system(code) }
        return WindowsFileReceipt(name: name, proof: proof)
    }
    func read(name: String, maximum: Int) throws -> (Data, WindowsFileReceipt) {
        guard maximum > 0, maximum <= 10 * 1024 * 1024, !name.utf8.contains(0) else { throw WindowsArtifactError.unsafe }
        var proof = ASFileProof()
        let bytes = name.withCString { as_private_read(handle, $0, UInt32(maximum), &proof) }
        defer { as_bytes_free(bytes) }
        guard bytes.error == 0, let data = bytes.data, bytes.size > 0, bytes.size <= maximum else {
            throw WindowsArtifactError.system(bytes.error)
        }
        return (Data(bytes: data, count: Int(bytes.size)), WindowsFileReceipt(name: name, proof: proof))
    }
    @discardableResult func remove(_ receipt: WindowsFileReceipt) -> Bool {
        guard !receipt.name.utf8.contains(0) else { return false }
        var proof = receipt.proof
        return receipt.name.withCString { as_private_remove(handle, $0, &proof) } == 0
    }
}

/// Producer-owned cleanup receipts are never taken from a consumer's JSON.
final class WindowsAppshotArtifacts {
    let directory: WindowsPrivateDirectory
    private(set) var files: [WindowsFileReceipt] = []
    private(set) var manifestName = ""
    var manifestPath: String { directory.path + "/" + manifestName }
    private init(directory: WindowsPrivateDirectory) { self.directory = directory }
    deinit { destroy() }
    @discardableResult func destroy() -> Bool {
        files = files.reversed().filter { !directory.remove($0) }.reversed()
        return files.isEmpty
    }
    static func publish(directory: WindowsPrivateDirectory, source: ASWindow,
        capture: (png: Data, uia: Data), recipient: AppshotRecipient<AppshotWindowsProcess>,
        deadline: AppshotCaptureDeadline) throws -> WindowsAppshotArtifacts {
        guard recipient.identity.pid > 0 else { throw WindowsArtifactError.unsafe }
        let artifacts = WindowsAppshotArtifacts(directory: directory)
        var source = source
        func validate() throws {
            _ = try deadline.remaining()
            guard as_window_matches(&source, 1) != 0 else { throw AppshotCaptureError.sourceWindowChanged }
            guard try WindowsAppshot.identity(UInt32(recipient.identity.pid)) == recipient.identity else {
                throw AppshotBrokerError.recipientDisconnected
            }
        }
        func bounded(_ text: String, _ maximum: Int) -> String {
            var result = text
            while result.utf8.count > maximum { result.removeLast() }
            return result
        }
        func file(_ name: String, _ bytes: Data) throws -> WindowsFileReceipt {
            try validate()
            let receipt = try directory.publish(name: name, data: bytes)
            artifacts.files.append(receipt)
            try validate()
            return receipt
        }
        try validate()
        let token = UUID().uuidString.replacingOccurrences(of: "-", with: "").lowercased()
        let dimensions = try WindowsAppshot.dimensions(capture.png)
        guard capture.uia.count <= 256 * 1024,
            let text = try JSONSerialization.jsonObject(with: capture.uia) as? [String: Any] else {
            throw WindowsArtifactError.unsafe
        }
        let png = try file("appshot-\(token).png", capture.png)
        let uia = try file("appshot-\(token).uia.json", capture.uia)
        let date = ISO8601DateFormatter()
        date.formatOptions = [.withInternetDateTime]
        date.timeZone = TimeZone(secondsFromGMT: 0)
        let raw: [String: Any] = [
            "schema_version": 2, "platform": "windows", "token": token,
            "captured_at": date.string(from: Date()),
            "source": [
                "process": ["pid": source.process.pid, "process_start": String(source.process.created),
                    "user_sid": WindowsAppshot.string(source.process.sid)],
                "app_id": bounded(WindowsAppshot.string(source.app), 256),
                "app_label": bounded(WindowsAppshot.string(source.app), 256),
                "window_title": bounded(WindowsAppshot.string(source.title), 1024),
                "window_handle": String(source.handle),
                "bounds": ["x": source.x, "y": source.y, "width": source.width, "height": source.height],
            ],
            "png": ["name": png.name, "size": capture.png.count, "width": dimensions.0, "height": dimensions.1,
                "sha256": png.sha256, "identity": png.identity],
            "uia": ["name": uia.name, "size": capture.uia.count, "sha256": uia.sha256, "identity": uia.identity,
                "coverage": text["coverage"] ?? "", "node_count": text["node_count"] ?? -1,
                "depth": text["depth"] ?? -1, "truncated": text["truncated"] ?? "",
                "truncation_reasons": text["truncation_reasons"] ?? []],
            "broker": ["instance_id": recipient.instanceID, "session_id": recipient.sessionID,
                "recipient": ["pid": recipient.identity.pid, "process_start": recipient.identity.processStart,
                    "user_sid": recipient.identity.userSID]],
        ]
        let data = try JSONSerialization.data(withJSONObject: raw, options: [.sortedKeys])
        _ = try AppshotWindowsManifest.decodeStrict(data)
        artifacts.manifestName = "appshot-\(token).manifest.json"
        _ = try file(artifacts.manifestName, data)
        return artifacts
    }
}

struct WindowsVerifiedArtifact {
    let manifest: AppshotWindowsManifest
    let manifestData: Data
    let png: Data
    let uia: Data
    /// Explicit binding must come from the authenticated broker/consumer epoch.
    /// Decoding a manifest or guessing its random basename does not grant authority.
    static func read(directory: WindowsPrivateDirectory, name: String, brokerID: String,
        sessionID: String, recipient: AppshotWindowsProcess) throws -> WindowsVerifiedArtifact {
        guard recipient.pid > 0 else { throw WindowsArtifactError.unsafe }
        let (manifestData, before) = try directory.read(name: name, maximum: 65536)
        let manifest = try AppshotWindowsManifest.decodeStrict(manifestData)
        guard name == "appshot-\(manifest.token).manifest.json",
            manifest.broker.instance_id == brokerID, manifest.broker.session_id == sessionID,
            manifest.broker.recipient == recipient,
            try WindowsAppshot.identity(UInt32(recipient.pid)) == recipient else {
            throw WindowsArtifactError.unsafe
        }
        let (png, image) = try directory.read(name: manifest.png.name, maximum: 10 * 1024 * 1024)
        let (uia, text) = try directory.read(name: manifest.uia.name, maximum: 256 * 1024)
        let dimensions = try WindowsAppshot.dimensions(png)
        guard image.matches(manifest.png.identity, size: manifest.png.size, hash: manifest.png.sha256),
            text.matches(manifest.uia.identity, size: manifest.uia.size, hash: manifest.uia.sha256),
            dimensions.0 == manifest.png.width, dimensions.1 == manifest.png.height else {
            throw WindowsArtifactError.unsafe
        }
        let (_, after) = try directory.read(name: name, maximum: 65536)
        guard before.sameRead(as: after), try WindowsAppshot.identity(UInt32(recipient.pid)) == recipient else {
            throw WindowsArtifactError.unsafe
        }
        return WindowsVerifiedArtifact(manifest: manifest, manifestData: manifestData, png: png, uia: uia)
    }
}

import Foundation
import AstraAppshotCore
import AstraWindowsNative

enum WindowsAppshot {
    static func bytes(_ value: ASBytes, maximum: Int) throws -> Data {
        defer { as_bytes_free(value) }
        guard value.error == 0, value.size > 0, value.size <= maximum, let pointer = value.data else {
            if value.error == 2 { throw AppshotCaptureError.resourceLimit }
            if value.error == 3 { throw AppshotCaptureError.timedOut }
            if value.error == 5 { throw AppshotCaptureError.cancelled }
            throw AppshotCaptureError.captureFailed
        }
        return Data(bytes: pointer, count: Int(value.size))
    }
    static func string<T>(_ value: T) -> String {
        withUnsafeBytes(of: value) { bytes in
            String(decoding: bytes.prefix { $0 != 0 }, as: UTF8.self)
        }
    }
    static func identity(_ pid: UInt32) throws -> AppshotWindowsProcess {
        var value = ASProcess()
        guard as_process_identity(pid, &value) != 0, value.pid <= Int32.max else {
            throw AppshotBrokerError.unauthorized
        }
        return try AppshotWindowsProcess.decodeStrict(JSONEncoder().encode(
            AppshotWindowsProcess(pid: Int32(value.pid), processStart: String(value.created),
                userSID: string(value.sid))))
    }
    static func parentIdentity() throws -> AppshotWindowsProcess {
        try ancestorIdentity(depth: 1)
    }
    static func ancestorIdentity(depth: UInt32) throws -> AppshotWindowsProcess {
        var proof = ASProcess()
        guard as_process_ancestor(depth, &proof) == 0 else { throw AppshotBrokerError.unauthorized }
        return try windowsProcess(proof)
    }
    static func runWorker(_ arguments: String, deadline: AppshotCaptureDeadline, maximum: Int) throws -> Data {
        let milliseconds = UInt32(max(1, min(5000, try deadline.remaining() * 1000)))
        let executable = URL(fileURLWithPath: CommandLine.arguments[0]).standardizedFileURL.path
            .replacingOccurrences(of: "/", with: "\\")
        let file = Array(executable.utf16) + [0]
        let command = Array(arguments.utf16) + [0]
        let context = Unmanaged.passRetained(deadline)
        defer { context.release() }
        let result = file.withUnsafeBufferPointer { file in
            command.withUnsafeBufferPointer { command in
                as_run_worker(file.baseAddress, command.baseAddress, milliseconds, UInt32(maximum), { pointer in
                    guard let pointer else { return 1 }
                    let deadline = Unmanaged<AppshotCaptureDeadline>.fromOpaque(pointer).takeUnretainedValue()
                    return (try? deadline.remaining()) == nil ? 1 : 0
                }, context.toOpaque())
            }
        }
        _ = try deadline.remaining()
        return try bytes(result, maximum: maximum)
    }
    static func target() throws -> ASWindow {
        var value = ASWindow()
        guard as_foreground_window(&value) != 0 else { throw AppshotCaptureError.indeterminateTarget }
        return value
    }
    static func workerIdentity(window: UInt64, pid: UInt32, created: String) throws {
        let process = try identity(pid)
        var actual = ASProcess()
        guard process.processStart == created, process.userSID == (try identity(as_current_pid())).userSID,
            window > 0, as_window_process(window, &actual) != 0, actual.pid == pid,
            String(actual.created) == created, string(actual.sid) == process.userSID
            else { throw AppshotCaptureError.sourceWindowChanged }
    }
    static func dimensions(_ png: Data) throws -> (Int, Int) {
        guard png.count >= 33, png.count <= 10 * 1024 * 1024,
            Array(png.prefix(8)) == [137, 80, 78, 71, 13, 10, 26, 10],
            String(data: png[12..<16], encoding: .ascii) == "IHDR" else { throw AppshotCaptureError.captureFailed }
        func integer(_ start: Int) -> Int { png[start..<start + 4].reduce(0) { ($0 << 8) | Int($1) } }
        let width = integer(16), height = integer(20)
        guard (1...16384).contains(width), (1...16384).contains(height),
            width * height <= 32_000_000 else { throw AppshotCaptureError.resourceLimit }
        return (width, height)
    }
    static func uia(window: UInt64, deadline: AppshotCaptureDeadline) throws -> Data {
        guard let context = as_uia_open() else { throw AppshotCaptureError.captureFailed }
        defer { as_uia_close(context) }
        guard let root = as_uia_root(context, window) else { throw AppshotCaptureError.captureFailed }
        var stack: [(UnsafeMutableRawPointer, Int, Int)] = [(root, 0, -1)]
        defer { for (element, _, _) in stack { as_uia_release(element) } }
        var nodes: [[String: Any]] = []
        var used = 512, depth = 0, reasons: Set<String> = []
        while let (element, level, parent) = stack.popLast() {
            defer { as_uia_release(element) }
            guard (try? deadline.remaining()) != nil else { reasons.insert("uia_timeout"); break }
            guard nodes.count < 2000 else { reasons.insert("node_limit"); break }
            guard used < 256 * 1024 - 1024 else { reasons.insert("byte_limit"); break }
            let bytes = try self.bytes(as_uia_properties(element, UInt32(min(2048, (256 * 1024 - used - 512) / 12))),
                maximum: 256 * 1024)
            guard var node = try JSONSerialization.jsonObject(with: bytes) as? [String: Any],
                let password = node["password"] as? Bool else { throw AppshotProtocolError.invalidJSON }
            node["parent"] = parent
            node["depth"] = level
            if node["text_truncated"] as? Bool == true { reasons.insert("text_limit") }
            used += try JSONSerialization.data(withJSONObject: node).count + 1
            let index = nodes.count
            nodes.append(node)
            depth = max(depth, level)
            if element != root, let sibling = as_uia_next_sibling(context, element) {
                stack.append((sibling, level, parent))
            }
            if !password {
                if level >= 64 { reasons.insert("depth_limit") }
                else if let child = as_uia_first_child(context, element) { stack.append((child, level + 1, index)) }
            }
        }
        let data = try JSONSerialization.data(withJSONObject: [
            "schema_version": 2, "platform": "windows", "coverage": "reported_uia_subtree",
            "node_count": nodes.count, "depth": depth, "truncated": !reasons.isEmpty,
            "truncation_reasons": reasons.sorted(), "nodes": nodes,
        ], options: [.sortedKeys])
        guard data.count <= 256 * 1024 else { throw AppshotCaptureError.resourceLimit }
        return data
    }
    static func unavailableUIA(_ reason: String) throws -> Data {
        try JSONSerialization.data(withJSONObject: [
            "schema_version": 2, "platform": "windows", "coverage": "unavailable", "node_count": 0,
            "depth": 0, "truncated": true, "truncation_reasons": [reason], "nodes": [],
        ], options: [.sortedKeys])
    }
    static func capture(_ target: ASWindow, foreground: Bool = true,
        deadline: AppshotCaptureDeadline = AppshotCaptureDeadline()) throws -> (png: Data, uia: Data) {
        var source = target
        func validate() throws {
            _ = try deadline.remaining()
            guard as_window_matches(&source, foreground ? 1 : 0) != 0 else { throw AppshotCaptureError.sourceWindowChanged }
        }
        try validate()
        let binding = "\(source.handle) \(source.process.pid) \(source.process.created)"
        let png = try runWorker("--capture-worker \(binding)", deadline: deadline, maximum: 10 * 1024 * 1024)
        _ = try dimensions(png)
        try validate()
        let text: Data
        let remaining = try deadline.remaining()
        if remaining > 1.1 {
            do {
                text = try runWorker("--uia-worker \(binding)",
                    deadline: deadline.limited(to: min(0.75, remaining - 1)), maximum: 256 * 1024)
            } catch {
                _ = try deadline.remaining()
                text = try unavailableUIA(error as? AppshotCaptureError == .timedOut ? "uia_timeout" : "uia_unavailable")
            }
        } else { text = try unavailableUIA("uia_budget_exhausted") }
        try validate()
        return (png, text)
    }
}

import Foundation
import AstraAppshotCore
import AstraWindowsNative

/// Native-only peer verification; the TUI never opens an unauthenticated pipe.
/// No FileHandle readability callbacks or blocking stdio worker threads.
enum WindowsRelay {
    static func run(path: String, scope: String, brokerID: String, nonce: String, expected: AppshotWindowsProcess) throws {
        var error: Int32 = 0
        guard let stdio = as_stdio_open(&error) else { throw WindowsTransportError.native(error) }
        defer { as_stdio_close(stdio) }
        let descriptor = try WindowsBrokerRuntime.read(path: path, scope: scope)
        guard descriptor.instance_id == brokerID, descriptor.broker_nonce == nonce, descriptor.process == expected else {
            throw AppshotBrokerError.unauthorized
        }
        let parent = try WindowsAppshot.parentIdentity()
        let client = try WindowsPipeClient(scope: scope, expected: descriptor.process)
        defer { client.close() }
        guard try WindowsBrokerRuntime.read(path: path, scope: scope) == descriptor else { throw AppshotBrokerError.unauthorized }
        var input = WindowsFrameDecoder(), session: String?
        var hello = false
        let started = as_monotonic_ns()
        var inputPartial: UInt64?, outputPartial: UInt64?
        var rateSince = started, frames = 0
        while true {
            let now = as_monotonic_ns()
            if !hello && now - started >= 3_000_000_000 { throw AppshotCaptureError.timedOut }
            if now - rateSince >= 1_000_000_000 { rateSince = now; frames = 0 }
            let raw = as_stdio_read(stdio)
            let bytes: Data
            do {
                defer { as_bytes_free(raw) }
                guard raw.error == 0, raw.size <= 4096 else { throw WindowsTransportError.closed }
                bytes = raw.data.map { Data(bytes: $0, count: Int(raw.size)) } ?? Data()
            }
            let messages = try input.feed(bytes)
            frames += messages.count
            guard frames <= 256 else { throw AppshotProtocolError.frameSize }
            for message in messages {
                let f = try JSONSerialization.jsonObject(with: message.data) as! [String: Any]
                if session == nil {
                    guard message.type == "hello", f["pid"] as? Int32 == parent.pid,
                        f["process_start"] as? String == parent.processStart, f["user_sid"] as? String == parent.userSID,
                        f["client_nonce"] as? String == descriptor.broker_nonce else { throw AppshotBrokerError.unauthorized }
                    session = f["session_id"] as? String
                } else {
                    guard hello, ["client_state", "attach_ack", "release", "command"].contains(message.type),
                        f["session_id"] as? String == session, f["broker_id"] as? String == brokerID else {
                        throw AppshotBrokerError.unauthorized
                    }
                }
                try client.enqueue(message)
            }
            try client.flush()
            for message in try client.poll() {
                let f = try JSONSerialization.jsonObject(with: message.data) as! [String: Any]
                guard let session, f["session_id"] as? String == session else { throw AppshotBrokerError.unauthorized }
                if !hello {
                    guard message.type == "hello_ack", f["instance_id"] as? String == brokerID,
                        f["broker_nonce"] as? String == nonce else { throw AppshotBrokerError.unauthorized }
                    hello = true
                } else {
                    guard ["attach_offer", "attach_commit", "attach_revoke", "release_ack", "command_result", "client_state_ack", "status"].contains(message.type),
                        f["broker_id"] as? String == brokerID else { throw AppshotBrokerError.unauthorized }
                }
                let frame = try message.encodeFrame()
                let code = frame.withUnsafeBytes { as_stdio_send(stdio, $0.bindMemory(to: UInt8.self).baseAddress, frame.count) }
                guard code == 0 else { throw WindowsTransportError.native(code) }
            }
            let code = as_stdio_flush(stdio)
            guard code == 0 else { throw WindowsTransportError.native(code) }
            if input.hasPartialFrame { if inputPartial == nil { inputPartial = now } } else { inputPartial = nil }
            if client.hasPartialFrame { if outputPartial == nil { outputPartial = now } } else { outputPartial = nil }
            if [inputPartial, outputPartial].compactMap({ $0 }).contains(where: { now - $0 >= 3_000_000_000 }) {
                throw AppshotCaptureError.timedOut
            }
            Thread.sleep(forTimeInterval: 0.002)
        }
    }
}

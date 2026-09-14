import Darwin
import Foundation

private let protocolVersion = 4
private let maxRequestLineBytes = 4 * 1024 * 1024
private let maxRequestIDBytes = 256
private let maxOperationBytes = 64
private let appRef = "fixture_app"
private let windowRef = "fixture_window"
private let windowIdentityRef = "fixture-window-identity-v1"
private let png = Data(base64Encoded: "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4nGNgYGD4DwABBAEAX+XDSwAAAABJRU5ErkJggg==")!

private func response(requestID: String, ok: Bool, result: [String: Any]? = nil, snapshot: [String: Any]? = nil, code: String? = nil) -> Data {
    var value: [String: Any] = ["protocol_version": protocolVersion, "request_id": requestID, "ok": ok]
    if let result { value["result"] = result }
    if let snapshot { value["snapshot"] = snapshot }
    if let code { value["error"] = ["code": code, "message": "fixture request failed"] }
    return try! JSONSerialization.data(withJSONObject: value)
}

private func plainName(_ value: String) -> Bool {
    let scalars = value.unicodeScalars
    return !scalars.isEmpty
        && scalars.count <= 128
        && value.utf8.count <= 128
        && value != "."
        && value != ".."
        && scalars.allSatisfy { (0x21 ... 0x7E).contains($0.value) }
        && !value.contains("/")
        && !value.contains("\\")
}

private func textDetailName(_ detailName: String, matches imageName: String) -> Bool {
    let prefix = "snapshot-"
    let suffix = ".png"
    guard plainName(detailName),
          imageName.hasPrefix(prefix),
          imageName.hasSuffix(suffix)
    else { return false }
    let start = imageName.index(imageName.startIndex, offsetBy: prefix.count)
    let end = imageName.index(imageName.endIndex, offsetBy: -suffix.count)
    let token = imageName[start..<end]
    return token.count == 32
        && token.allSatisfy { "0123456789abcdef".contains($0) }
        && detailName == String(imageName.dropLast(4)) + ".ax.json"
}

private func getAppStateTextDetailName(_ detailName: String, matches imageName: String) -> Bool {
    plainName(detailName)
        && imageName.hasSuffix(".png")
        && detailName == String(imageName.dropLast(4)) + ".ax.json"
}

private func positiveExactInteger(_ value: Any?) -> Int? {
    guard let number = value as? NSNumber,
          CFGetTypeID(number) != CFBooleanGetTypeID(),
          !["f", "d"].contains(String(cString: number.objCType)),
          number.intValue > 0,
          number.intValue == Int(number.doubleValue)
    else { return nil }
    return number.intValue
}

private func boundedHeader(_ value: String, maximumUTF8Bytes: Int) -> Bool {
    !value.isEmpty && value.utf8.count <= maximumUTF8Bytes
}

private func writeArtifact(named name: String) throws {
    guard plainName(name),
          let raw = ProcessInfo.processInfo.environment["ASTRA_COMPUTER_ARTIFACT_DIR_FD"],
          let directoryFD = Int32(raw), directoryFD >= 0
    else { throw FixtureError.invalidArtifact }
    let flags = O_WRONLY | O_CREAT | O_EXCL | O_NOFOLLOW | O_CLOEXEC
    let descriptor = name.withCString { Darwin.openat(directoryFD, $0, flags, mode_t(0o600)) }
    guard descriptor >= 0 else { throw FixtureError.invalidArtifact }
    defer { Darwin.close(descriptor) }
    guard fchmod(descriptor, mode_t(0o600)) == 0 else { throw FixtureError.invalidArtifact }
    try png.withUnsafeBytes { bytes in
        guard let base = bytes.baseAddress else { return }
        var offset = 0
        while offset < bytes.count {
            let count = Darwin.write(descriptor, base.advanced(by: offset), bytes.count - offset)
            if count < 0 && errno == EINTR { continue }
            guard count > 0 else { throw FixtureError.invalidArtifact }
            offset += count
        }
    }
    guard fsync(descriptor) == 0 else { throw FixtureError.invalidArtifact }
}

private enum FixtureError: Error { case invalidArtifact }

private struct StrictFixtureJSON {
    private let bytes: [UInt8]
    private var index = 0

    static func validate(_ data: Data) -> Bool {
        var parser = Self(bytes: Array(data))
        do {
            try parser.value(depth: 0)
            parser.whitespace()
            return parser.index == parser.bytes.count
        } catch {
            return false
        }
    }

    private mutating func value(depth: Int) throws {
        guard depth <= 32 else { throw ParseError.invalid }
        whitespace()
        guard let byte = current else { throw ParseError.invalid }
        switch byte {
        case 0x7B: try object(depth: depth)
        case 0x5B: try array(depth: depth)
        case 0x22: _ = try string()
        case 0x74: try literal("true")
        case 0x66: try literal("false")
        case 0x6E: try literal("null")
        case 0x2D, 0x30 ... 0x39: try number()
        default: throw ParseError.invalid
        }
    }

    private mutating func object(depth: Int) throws {
        index += 1
        whitespace()
        if take(0x7D) { return }
        var keys = Set<String>()
        while true {
            whitespace()
            guard keys.insert(try string()).inserted else { throw ParseError.invalid }
            whitespace()
            guard take(0x3A) else { throw ParseError.invalid }
            try value(depth: depth + 1)
            whitespace()
            if take(0x7D) { return }
            guard take(0x2C) else { throw ParseError.invalid }
        }
    }

    private mutating func array(depth: Int) throws {
        index += 1
        whitespace()
        if take(0x5D) { return }
        while true {
            try value(depth: depth + 1)
            whitespace()
            if take(0x5D) { return }
            guard take(0x2C) else { throw ParseError.invalid }
        }
    }

    private mutating func string() throws -> String {
        guard current == 0x22 else { throw ParseError.invalid }
        let start = index
        index += 1
        while let byte = current {
            if byte == 0x22 {
                index += 1
                let data = Data(bytes[start..<index])
                guard let decoded = try? JSONDecoder().decode(String.self, from: data) else {
                    throw ParseError.invalid
                }
                return decoded
            }
            if byte == 0x5C {
                index += 1
                guard current != nil else { throw ParseError.invalid }
            } else if byte < 0x20 {
                throw ParseError.invalid
            }
            index += 1
        }
        throw ParseError.invalid
    }

    private mutating func number() throws {
        if take(0x2D), current == nil { throw ParseError.invalid }
        if take(0x30) {
            if current.map({ (0x30 ... 0x39).contains($0) }) == true { throw ParseError.invalid }
        } else {
            guard current.map({ (0x31 ... 0x39).contains($0) }) == true else {
                throw ParseError.invalid
            }
            repeat { index += 1 } while current.map { (0x30 ... 0x39).contains($0) } == true
        }
        if take(0x2E) {
            guard current.map({ (0x30 ... 0x39).contains($0) }) == true else {
                throw ParseError.invalid
            }
            repeat { index += 1 } while current.map { (0x30 ... 0x39).contains($0) } == true
        }
        if current == 0x65 || current == 0x45 {
            index += 1
            if current == 0x2B || current == 0x2D { index += 1 }
            guard current.map({ (0x30 ... 0x39).contains($0) }) == true else {
                throw ParseError.invalid
            }
            repeat { index += 1 } while current.map { (0x30 ... 0x39).contains($0) } == true
        }
    }

    private mutating func literal(_ text: String) throws {
        let value = Array(text.utf8)
        guard index + value.count <= bytes.count,
              Array(bytes[index..<(index + value.count)]) == value
        else { throw ParseError.invalid }
        index += value.count
    }

    private mutating func take(_ byte: UInt8) -> Bool {
        guard current == byte else { return false }
        index += 1
        return true
    }

    private mutating func whitespace() {
        while current.map({ [0x20, 0x09, 0x0A, 0x0D].contains($0) }) == true { index += 1 }
    }

    private var current: UInt8? { index < bytes.count ? bytes[index] : nil }
    private enum ParseError: Error { case invalid }
}

private func canonicalActions(_ actions: [[String: Any]]) -> Data? {
    try? JSONSerialization.data(withJSONObject: actions, options: [.sortedKeys])
}

private func actionClasses(_ actions: [[String: Any]]) -> [String] {
    var classes: [String] = []
    for action in actions {
        let actionClass: String?
        switch action["type"] as? String {
        case "click": actionClass = "click"
        case "double_click": actionClass = "double_click"
        case "type": actionClass = "text"
        case "keypress": actionClass = "press"
        case "scroll": actionClass = "scroll"
        case "drag": actionClass = "drag"
        default: actionClass = nil
        }
        if let actionClass, !classes.contains(actionClass) { classes.append(actionClass) }
    }
    return classes
}

var catalogIssued = false
var selected = false
var latestSnapshotID: String?
var nextPlan = 0
var plans: [String: (snapshotID: String, actions: Data)] = [:]
while let line = readLine() {
    guard line.utf8.count <= maxRequestLineBytes,
          let data = line.data(using: .utf8),
          StrictFixtureJSON.validate(data),
          let frame = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
          Set(frame.keys) == Set(["protocol_version", "request_id", "operation", "payload"]),
          let version = frame["protocol_version"] as? NSNumber,
          CFGetTypeID(version) != CFBooleanGetTypeID(),
          !["f", "d"].contains(String(cString: version.objCType)),
          version.intValue == protocolVersion,
          let requestID = frame["request_id"] as? String,
          boundedHeader(requestID, maximumUTF8Bytes: maxRequestIDBytes),
          let operation = frame["operation"] as? String,
          boundedHeader(operation, maximumUTF8Bytes: maxOperationBytes),
          let payload = frame["payload"] as? [String: Any]
    else {
        FileHandle.standardOutput.write(response(requestID: "invalid", ok: false, code: "protocol_mismatch") + Data([0x0A]))
        continue
    }

    let output: Data
    switch operation {
    case "ping":
        output = response(requestID: requestID, ok: true, result: ["pong": true])
    case "status":
        output = response(requestID: requestID, ok: true, result: ["supported": true, "permissions": ["accessibility": true, "screen_recording": true]])
    case "apps":
        catalogIssued = true
        selected = false
        latestSnapshotID = nil
        plans.removeAll()
        output = response(requestID: requestID, ok: true, result: [
            "catalog_generation": 7,
            "apps": [[
                "app_ref": appRef,
                "name": "Astra computer fixture",
                "bundle_id": "com.astra.computer-fixture",
                "app_version": "1.0",
                "windows": [[
                    "window_ref": windowRef,
                    "title": "Fixture window",
                    "bounds": ["x": 0, "y": 0, "width": 1, "height": 1],
                    "bindable": true,
                    "binding_status": "ready",
                    "window_identity_ref": windowIdentityRef,
                ]],
            ]],
        ])
    case "select":
        guard Set(payload.keys) == Set(["app_ref", "window_ref"]),
              catalogIssued,
              payload["app_ref"] as? String == appRef,
              payload["window_ref"] as? String == windowRef
        else {
            output = response(requestID: requestID, ok: false, code: "target_gone")
            break
        }
        selected = true
        latestSnapshotID = "fixture_snapshot"
        plans.removeAll()
        output = response(requestID: requestID, ok: true, result: [
            "app_ref": appRef,
            "window_ref": windowRef,
            "interaction_mode": "background",
        ])
    case "get_app_state":
        let baseKeys = Set(["app_ref", "window_ref", "catalog_generation", "scope", "artifact_name"])
        let onKeys = baseKeys.union(["text_detail", "text_detail_artifact_name"])
        let detailName = payload["text_detail_artifact_name"] as? String
        let detailOn = Set(payload.keys) == onKeys && payload["text_detail"] as? String == "on"
        guard catalogIssued,
              Set(payload.keys) == baseKeys || detailOn,
              payload["app_ref"] as? String == "app_a",
              payload["window_ref"] as? String == "win_a",
              positiveExactInteger(payload["catalog_generation"]) == 7,
              let scope = payload["scope"] as? String,
              scope == "target_window" || scope == "display",
              let artifactName = payload["artifact_name"] as? String,
              plainName(artifactName),
              artifactName.hasSuffix(".png"),
              (!detailOn || (detailName.map { getAppStateTextDetailName($0, matches: artifactName) } == true))
        else {
            output = response(requestID: requestID, ok: false, code: "protocol_mismatch")
            break
        }
        let snapshotID = "snapshot_fixture_\(UUID().uuidString.lowercased())"
        var snapshotPayload: [String: Any] = [
            "image_artifact": artifactName,
            "logical_size": ["width": 1, "height": 1],
            "pixel_size": ["width": 1, "height": 1],
            "backing_scale": 1,
            "capture_bounds": ["x": 0, "y": 0, "width": 1, "height": 1],
            "ax_tree": [
                "role": "AXWindow",
                "element_ref": "\(snapshotID):window",
                "children": [[
                    "role": "AXButton",
                    "element_ref": "\(snapshotID):default",
                ]],
            ],
            "has_default_button": true,
            "default_button_element_ref": "\(snapshotID):default",
        ]
        if scope == "display" {
            snapshotPayload["display_id"] = 7
            snapshotPayload["target_window_bounds"] = [
                "x": 0, "y": 0, "width": 1, "height": 1,
            ]
        }
        if detailOn, let detailName {
            snapshotPayload["text_detail_artifact"] = detailName
            snapshotPayload["text_detail_metadata"] = [
                "schema_version": 1,
                "snapshot_id": snapshotID,
                "coverage": "reported_ax_subtree",
                "node_count": 2,
                "max_depth_observed": 1,
                "byte_count": 1,
                "sha256": String(repeating: "0", count: 64),
                "truncated": false,
                "truncation_reasons": [],
            ]
        }
        output = response(requestID: requestID, ok: true, result: [
            "app_ref": "app_a",
            "window_ref": "win_a",
            "catalog_generation": 7,
            "interaction_mode": "background",
        ], snapshot: [
            "snapshot_id": snapshotID,
            "payload": snapshotPayload,
        ])
    case "snapshot":
        let baseKeys = Set(["app_ref", "window_ref", "scope", "artifact_name"])
        let onKeys = baseKeys.union(["text_detail", "text_detail_artifact_name"])
        let detailName = payload["text_detail_artifact_name"] as? String
        let detailOn = Set(payload.keys) == onKeys
            && payload["text_detail"] as? String == "on"
        guard selected,
              Set(payload.keys) == baseKeys || detailOn,
              payload["app_ref"] as? String == appRef,
              payload["window_ref"] as? String == windowRef,
              let scope = payload["scope"] as? String,
              scope == "target_window" || scope == "display",
              let artifactName = payload["artifact_name"] as? String,
              (!detailOn || (detailName.map { textDetailName($0, matches: artifactName) } == true)),
              (try? writeArtifact(named: artifactName)) != nil
        else {
            output = response(requestID: requestID, ok: false, code: "helper_failed")
            break
        }
        var snapshotPayload: [String: Any] = [
            "image_artifact": artifactName,
            "logical_size": ["width": 1, "height": 1],
            "pixel_size": ["width": 1, "height": 1],
            "backing_scale": 1,
            "capture_bounds": ["x": 0, "y": 0, "width": 1, "height": 1],
            "ax_tree": ["role": "AXWindow"],
            "has_default_button": false,
        ]
        if scope == "display" {
            snapshotPayload["display_id"] = 7
            snapshotPayload["target_window_bounds"] = [
                "x": 0, "y": 0, "width": 1, "height": 1,
            ]
        }
        let snapshotID = "fixture_snapshot_\(UUID().uuidString.lowercased())"
        if detailOn, let detailName {
            snapshotPayload["text_detail_artifact"] = detailName
            snapshotPayload["text_detail_metadata"] = [
                "schema_version": 1,
                "snapshot_id": snapshotID,
                "coverage": "reported_ax_subtree",
                "node_count": 1,
                "max_depth_observed": 0,
                "byte_count": 1,
                "sha256": String(repeating: "0", count: 64),
                "truncated": false,
                "truncation_reasons": [],
            ]
        }
        latestSnapshotID = snapshotID
        plans.removeAll()
        output = response(requestID: requestID, ok: true, snapshot: [
            "snapshot_id": snapshotID,
            "payload": snapshotPayload,
        ])
    case "plan_actions":
        guard Set(payload.keys) == Set(["interaction_mode", "snapshot_id", "actions"]),
              selected,
              payload["interaction_mode"] as? String == "background",
              let snapshotID = payload["snapshot_id"] as? String,
              snapshotID == latestSnapshotID,
              let actions = payload["actions"] as? [[String: Any]],
              let encodedActions = canonicalActions(actions)
        else {
            output = response(requestID: requestID, ok: false, code: "stale_snapshot")
            break
        }
        nextPlan += 1
        let planRef = "fixture_plan_\(nextPlan)"
        plans[planRef] = (snapshotID, encodedActions)
        output = response(requestID: requestID, ok: true, result: [
            "plan_ref": planRef,
            "interaction_mode": "background",
            "requires_takeover": false,
            "reason": "background_ax_only",
            "action_classes": actionClasses(actions),
            "pid_action_classes": [],
            "last_acknowledged_action": -1,
        ])
    case "act":
        guard Set(payload.keys) == Set(["interaction_mode", "snapshot_id", "plan_ref", "actions"]),
              payload["interaction_mode"] as? String == "background",
              let snapshotID = payload["snapshot_id"] as? String,
              let planRef = payload["plan_ref"] as? String,
              let stored = plans.removeValue(forKey: planRef),
              stored.snapshotID == snapshotID,
              snapshotID == latestSnapshotID,
              let actions = payload["actions"] as? [[String: Any]],
              canonicalActions(actions) == stored.actions,
              let first = actions.first,
              let fixtureCase = first["element_ref"] as? String
        else {
            output = response(
                requestID: requestID,
                ok: false,
                result: ["outcomes": [], "last_acknowledged_action": -1],
                code: "stale_snapshot"
            )
            break
        }
        latestSnapshotID = nil
        let result: [String: Any]
        let ok: Bool
        let code: String?
        switch fixtureCase {
        case "malformed_outcomes_string":
            result = ["outcomes": "bad", "last_acknowledged_action": 0]
            ok = true
            code = nil
        case "malformed_ack_out_of_range":
            result = ["outcomes": [], "last_acknowledged_action": 999]
            ok = true
            code = nil
        case "malformed_extra_field":
            result = ["outcomes": [], "last_acknowledged_action": -1, "extra": true]
            ok = true
            code = nil
        case "malformed_noncontiguous":
            result = ["outcomes": [["index": 1, "ok": false, "error_code": "helper_failed"]], "last_acknowledged_action": -1]
            ok = false
            code = "helper_failed"
        case "malformed_success_ack":
            result = ["outcomes": [["index": 0, "ok": true]], "last_acknowledged_action": -1]
            ok = true
            code = nil
        case "malformed_unknown_ack":
            result = ["outcomes": [["index": 0, "ok": false, "error_code": "unknown_outcome"]], "last_acknowledged_action": 0]
            ok = false
            code = "unknown_outcome"
        case "malicious_unknown_missing_outcome":
            result = ["outcomes": [], "last_acknowledged_action": -1]
            ok = false
            code = "unknown_outcome"
        default:
            result = ["outcomes": [["index": 0, "ok": true]], "last_acknowledged_action": 0]
            ok = true
            code = nil
        }
        output = response(requestID: requestID, ok: ok, result: result, code: code)
    case "close":
        output = response(requestID: requestID, ok: true, result: ["closed": true])
    default:
        output = response(requestID: requestID, ok: false, code: "protocol_mismatch")
    }
    FileHandle.standardOutput.write(output + Data([0x0A]))
    if operation == "close" { break }
}

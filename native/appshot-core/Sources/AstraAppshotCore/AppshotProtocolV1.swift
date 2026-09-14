// Wire v1 is intentionally unchanged. Platform adapters must not reinterpret POSIX evidence.
import Foundation

public enum AppshotProtocolError: Error, Equatable {
  case duplicateKey, invalidJSON, invalidFields, invalidValue, frameSize, unknownMessage,
    wrongDirection, incompleteFrame
}
public enum AppshotLimits {
  public static let protocolVersion = 1
  public static let maximumFrameBytes = 65536
  public static let maximumLabelUTF8Bytes = 256
  public static let maximumTitleUTF8Bytes = 1024
  public static let maximumAppshotsPerDraft = 4
}
public enum AppshotCoverage: String, Codable, Equatable {
  case reportedAXSubtree = "reported_ax_subtree"
  case unavailable = "unavailable"
}
public struct AppshotBounds: Codable, Equatable {
  public let x: Double
  public let y: Double
  public let width: Double
  public let height: Double
  enum CodingKeys: String, CodingKey {
    case x
    case y
    case width
    case height
  }
  public init(x: Double, y: Double, width: Double, height: Double) {
    self.x = x
    self.y = y
    self.width = width
    self.height = height
  }
}
public struct AppshotSource: Codable, Equatable {
  public let pid: Int
  public let processStart: String
  public let bundleId: String
  public let appLabel: String
  public let windowTitle: String
  public let windowId: Int
  public let bounds: AppshotBounds
  enum CodingKeys: String, CodingKey {
    case pid
    case processStart = "process_start"
    case bundleId = "bundle_id"
    case appLabel = "app_label"
    case windowTitle = "window_title"
    case windowId = "window_id"
    case bounds
  }
  public init(
    pid: Int, processStart: String, bundleId: String, appLabel: String, windowTitle: String,
    windowId: Int, bounds: AppshotBounds
  ) {
    self.pid = pid
    self.processStart = processStart
    self.bundleId = bundleId
    self.appLabel = appLabel
    self.windowTitle = windowTitle
    self.windowId = windowId
    self.bounds = bounds
  }
}
public struct AppshotPNG: Codable, Equatable {
  public let name: String
  public let size: Int
  public let width: Int
  public let height: Int
  public let sha256: String
  public let device: String
  public let inode: String
  public let owner: Int
  public let mode: Int
  public let linkCount: Int
  enum CodingKeys: String, CodingKey {
    case name
    case size
    case width
    case height
    case sha256
    case device
    case inode
    case owner
    case mode
    case linkCount = "link_count"
  }
  public init(
    name: String, size: Int, width: Int, height: Int, sha256: String, device: String, inode: String,
    owner: Int, mode: Int, linkCount: Int
  ) {
    self.name = name
    self.size = size
    self.width = width
    self.height = height
    self.sha256 = sha256
    self.device = device
    self.inode = inode
    self.owner = owner
    self.mode = mode
    self.linkCount = linkCount
  }
}
public struct AppshotAX: Codable, Equatable {
  public let name: String
  public let size: Int
  public let sha256: String
  public let device: String
  public let inode: String
  public let owner: Int
  public let mode: Int
  public let linkCount: Int
  public let coverage: AppshotCoverage
  public let nodeCount: Int
  public let depth: Int
  public let truncated: Bool
  public let truncationReasons: [String]
  enum CodingKeys: String, CodingKey {
    case name
    case size
    case sha256
    case device
    case inode
    case owner
    case mode
    case linkCount = "link_count"
    case coverage
    case nodeCount = "node_count"
    case depth
    case truncated
    case truncationReasons = "truncation_reasons"
  }
  public init(
    name: String, size: Int, sha256: String, device: String, inode: String, owner: Int, mode: Int,
    linkCount: Int, coverage: AppshotCoverage, nodeCount: Int, depth: Int, truncated: Bool,
    truncationReasons: [String]
  ) {
    self.name = name
    self.size = size
    self.sha256 = sha256
    self.device = device
    self.inode = inode
    self.owner = owner
    self.mode = mode
    self.linkCount = linkCount
    self.coverage = coverage
    self.nodeCount = nodeCount
    self.depth = depth
    self.truncated = truncated
    self.truncationReasons = truncationReasons
  }
}
public struct AppshotBrokerBinding: Codable, Equatable {
  public let instanceId: String
  public let sessionId: String
  public let processStart: String
  enum CodingKeys: String, CodingKey {
    case instanceId = "instance_id"
    case sessionId = "session_id"
    case processStart = "process_start"
  }
  public init(instanceId: String, sessionId: String, processStart: String) {
    self.instanceId = instanceId
    self.sessionId = sessionId
    self.processStart = processStart
  }
}
public struct AppshotManifest: Codable, Equatable {
  public let schemaVersion: Int
  public let token: String
  public let capturedAt: String
  public let source: AppshotSource
  public let png: AppshotPNG
  public let ax: AppshotAX
  public let broker: AppshotBrokerBinding
  enum CodingKeys: String, CodingKey {
    case schemaVersion = "schema_version"
    case token
    case capturedAt = "captured_at"
    case source
    case png
    case ax
    case broker
  }
  public init(
    schemaVersion: Int, token: String, capturedAt: String, source: AppshotSource, png: AppshotPNG,
    ax: AppshotAX, broker: AppshotBrokerBinding
  ) {
    self.schemaVersion = schemaVersion
    self.token = token
    self.capturedAt = capturedAt
    self.source = source
    self.png = png
    self.ax = ax
    self.broker = broker
  }
}
public struct AppshotHello: Codable, Equatable {
  public let type: String
  public let version: Int
  public let sessionId: String
  public let pid: Int
  public let processStart: String
  public let clientNonce: String
  enum CodingKeys: String, CodingKey {
    case type
    case version
    case sessionId = "session_id"
    case pid
    case processStart = "process_start"
    case clientNonce = "client_nonce"
  }
  public init(
    type: String, version: Int, sessionId: String, pid: Int, processStart: String,
    clientNonce: String
  ) {
    self.type = type
    self.version = version
    self.sessionId = sessionId
    self.pid = pid
    self.processStart = processStart
    self.clientNonce = clientNonce
  }
}
public struct AppshotHelloAck: Codable, Equatable {
  public let type: String
  public let version: Int
  public let instanceId: String
  public let brokerNonce: String
  public let sessionId: String
  enum CodingKeys: String, CodingKey {
    case type
    case version
    case instanceId = "instance_id"
    case brokerNonce = "broker_nonce"
    case sessionId = "session_id"
  }
  public init(
    type: String, version: Int, instanceId: String, brokerNonce: String, sessionId: String
  ) {
    self.type = type
    self.version = version
    self.instanceId = instanceId
    self.brokerNonce = brokerNonce
    self.sessionId = sessionId
  }
}
public struct AppshotClientState: Codable, Equatable {
  public let type: String
  public let version: Int
  public let requestId: String
  public let brokerId: String
  public let sessionId: String
  public let activityNs: String
  public let appshotCount: Int
  public let canAccept: Bool
  enum CodingKeys: String, CodingKey {
    case type
    case version
    case requestId = "request_id"
    case brokerId = "broker_id"
    case sessionId = "session_id"
    case activityNs = "activity_ns"
    case appshotCount = "appshot_count"
    case canAccept = "can_accept"
  }
  public init(
    type: String, version: Int, requestId: String, brokerId: String, sessionId: String,
    activityNs: String, appshotCount: Int, canAccept: Bool
  ) {
    self.type = type
    self.version = version
    self.requestId = requestId
    self.brokerId = brokerId
    self.sessionId = sessionId
    self.activityNs = activityNs
    self.appshotCount = appshotCount
    self.canAccept = canAccept
  }
}
public struct AppshotAttachOffer: Codable, Equatable {
  public let type: String
  public let version: Int
  public let requestId: String
  public let brokerId: String
  public let sessionId: String
  public let manifestPath: String
  enum CodingKeys: String, CodingKey {
    case type
    case version
    case requestId = "request_id"
    case brokerId = "broker_id"
    case sessionId = "session_id"
    case manifestPath = "manifest_path"
  }
  public init(
    type: String, version: Int, requestId: String, brokerId: String, sessionId: String,
    manifestPath: String
  ) {
    self.type = type
    self.version = version
    self.requestId = requestId
    self.brokerId = brokerId
    self.sessionId = sessionId
    self.manifestPath = manifestPath
  }
}
public struct AppshotAttachAck: Codable, Equatable {
  public let type: String
  public let version: Int
  public let requestId: String
  public let brokerId: String
  public let sessionId: String
  public let accepted: Bool
  public let reason: String
  enum CodingKeys: String, CodingKey {
    case type
    case version
    case requestId = "request_id"
    case brokerId = "broker_id"
    case sessionId = "session_id"
    case accepted
    case reason
  }
  public init(
    type: String, version: Int, requestId: String, brokerId: String, sessionId: String,
    accepted: Bool, reason: String
  ) {
    self.type = type
    self.version = version
    self.requestId = requestId
    self.brokerId = brokerId
    self.sessionId = sessionId
    self.accepted = accepted
    self.reason = reason
  }
}
public struct AppshotAttachCommit: Codable, Equatable {
  public let type: String
  public let version: Int
  public let requestId: String
  public let brokerId: String
  public let sessionId: String
  public let manifestPath: String
  enum CodingKeys: String, CodingKey {
    case type
    case version
    case requestId = "request_id"
    case brokerId = "broker_id"
    case sessionId = "session_id"
    case manifestPath = "manifest_path"
  }
  public init(
    type: String, version: Int, requestId: String, brokerId: String, sessionId: String,
    manifestPath: String
  ) {
    self.type = type
    self.version = version
    self.requestId = requestId
    self.brokerId = brokerId
    self.sessionId = sessionId
    self.manifestPath = manifestPath
  }
}
public struct AppshotAttachRevoke: Codable, Equatable {
  public let type: String
  public let version: Int
  public let requestId: String
  public let brokerId: String
  public let sessionId: String
  public let reason: String
  enum CodingKeys: String, CodingKey {
    case type
    case version
    case requestId = "request_id"
    case brokerId = "broker_id"
    case sessionId = "session_id"
    case reason
  }
  public init(
    type: String, version: Int, requestId: String, brokerId: String, sessionId: String,
    reason: String
  ) {
    self.type = type
    self.version = version
    self.requestId = requestId
    self.brokerId = brokerId
    self.sessionId = sessionId
    self.reason = reason
  }
}
public struct AppshotRelease: Codable, Equatable {
  public let type: String
  public let version: Int
  public let requestId: String
  public let brokerId: String
  public let sessionId: String
  enum CodingKeys: String, CodingKey {
    case type
    case version
    case requestId = "request_id"
    case brokerId = "broker_id"
    case sessionId = "session_id"
  }
  public init(type: String, version: Int, requestId: String, brokerId: String, sessionId: String) {
    self.type = type
    self.version = version
    self.requestId = requestId
    self.brokerId = brokerId
    self.sessionId = sessionId
  }
}
public struct AppshotReleaseAck: Codable, Equatable {
  public let type: String
  public let version: Int
  public let requestId: String
  public let brokerId: String
  public let sessionId: String
  public let released: Bool
  enum CodingKeys: String, CodingKey {
    case type
    case version
    case requestId = "request_id"
    case brokerId = "broker_id"
    case sessionId = "session_id"
    case released
  }
  public init(
    type: String, version: Int, requestId: String, brokerId: String, sessionId: String,
    released: Bool
  ) {
    self.type = type
    self.version = version
    self.requestId = requestId
    self.brokerId = brokerId
    self.sessionId = sessionId
    self.released = released
  }
}
public struct AppshotCommand: Codable, Equatable {
  public let type: String
  public let version: Int
  public let requestId: String
  public let brokerId: String
  public let sessionId: String
  public let name: String
  public let argument: String
  enum CodingKeys: String, CodingKey {
    case type
    case version
    case requestId = "request_id"
    case brokerId = "broker_id"
    case sessionId = "session_id"
    case name
    case argument
  }
  public init(
    type: String, version: Int, requestId: String, brokerId: String, sessionId: String,
    name: String, argument: String
  ) {
    self.type = type
    self.version = version
    self.requestId = requestId
    self.brokerId = brokerId
    self.sessionId = sessionId
    self.name = name
    self.argument = argument
  }
}
public struct AppshotCommandResult: Codable, Equatable {
  public let type: String
  public let version: Int
  public let requestId: String
  public let brokerId: String
  public let sessionId: String
  public let ok: Bool
  public let code: String
  public let message: String
  enum CodingKeys: String, CodingKey {
    case type
    case version
    case requestId = "request_id"
    case brokerId = "broker_id"
    case sessionId = "session_id"
    case ok
    case code
    case message
  }
  public init(
    type: String, version: Int, requestId: String, brokerId: String, sessionId: String, ok: Bool,
    code: String, message: String
  ) {
    self.type = type
    self.version = version
    self.requestId = requestId
    self.brokerId = brokerId
    self.sessionId = sessionId
    self.ok = ok
    self.code = code
    self.message = message
  }
}
public struct AppshotClientStateAck: Codable, Equatable {
  public let type: String
  public let version: Int
  public let requestId: String
  public let brokerId: String
  public let sessionId: String
  public let appshotCount: Int
  public let canAccept: Bool
  enum CodingKeys: String, CodingKey {
    case type
    case version
    case requestId = "request_id"
    case brokerId = "broker_id"
    case sessionId = "session_id"
    case appshotCount = "appshot_count"
    case canAccept = "can_accept"
  }
  public init(
    type: String, version: Int, requestId: String, brokerId: String, sessionId: String,
    appshotCount: Int, canAccept: Bool
  ) {
    self.type = type
    self.version = version
    self.requestId = requestId
    self.brokerId = brokerId
    self.sessionId = sessionId
    self.appshotCount = appshotCount
    self.canAccept = canAccept
  }
}
public struct AppshotStatus: Codable, Equatable {
  public let type: String
  public let version: Int
  public let requestId: String
  public let brokerId: String
  public let sessionId: String
  public let enabled: Bool
  public let chord: String
  public let registration: String
  public let connectedTuis: Int
  public let permission: String
  public let appshotCount: Int
  public let canAccept: Bool
  enum CodingKeys: String, CodingKey {
    case type
    case version
    case requestId = "request_id"
    case brokerId = "broker_id"
    case sessionId = "session_id"
    case enabled
    case chord
    case registration
    case connectedTuis = "connected_tuis"
    case permission
    case appshotCount = "appshot_count"
    case canAccept = "can_accept"
  }
  public init(
    type: String, version: Int, requestId: String, brokerId: String, sessionId: String,
    enabled: Bool, chord: String, registration: String, connectedTuis: Int, permission: String,
    appshotCount: Int, canAccept: Bool
  ) {
    self.type = type
    self.version = version
    self.requestId = requestId
    self.brokerId = brokerId
    self.sessionId = sessionId
    self.enabled = enabled
    self.chord = chord
    self.registration = registration
    self.connectedTuis = connectedTuis
    self.permission = permission
    self.appshotCount = appshotCount
    self.canAccept = canAccept
  }
}
enum ASRule {
  case int(Int, Int)
  case str(Int)
  case values([String])
  case uint, hash, token, id, path, date, coord, extent, bool, reasons, sid, windowsPath
  case model(String)
}
let appshotSchemas: [String: [String: ASRule]] = [
  "AppshotBounds": ["x": .coord, "y": .coord, "width": .extent, "height": .extent],
  "AppshotSource": [
    "pid": .int(1, 2_147_483_647), "process_start": .uint, "bundle_id": .str(256),
    "app_label": .str(256), "window_title": .str(1024), "window_id": .int(0, 4_294_967_295),
    "bounds": .model("AppshotBounds"),
  ],
  "AppshotPNG": [
    "name": .str(128), "size": .int(1, 10_485_760), "width": .int(1, 16384),
    "height": .int(1, 16384), "sha256": .hash, "device": .uint, "inode": .uint,
    "owner": .int(0, 4_294_967_295), "mode": .int(384, 384), "link_count": .int(1, 1),
  ],
  "AppshotAX": [
    "name": .str(128), "size": .int(1, 262144), "sha256": .hash, "device": .uint, "inode": .uint,
    "owner": .int(0, 4_294_967_295), "mode": .int(384, 384), "link_count": .int(1, 1),
    "coverage": .values(["reported_ax_subtree", "unavailable"]), "node_count": .int(0, 2000), "depth": .int(0, 64),
    "truncated": .bool, "truncation_reasons": .reasons,
  ],
  "AppshotBrokerBinding": ["instance_id": .id, "session_id": .id, "process_start": .uint],
  "AppshotManifest": [
    "schema_version": .int(1, 1), "token": .token, "captured_at": .date,
    "source": .model("AppshotSource"), "png": .model("AppshotPNG"), "ax": .model("AppshotAX"),
    "broker": .model("AppshotBrokerBinding"),
  ],
  "AppshotHello": [
    "type": .values(["hello"]), "version": .int(1, 1), "session_id": .id,
    "pid": .int(1, 2_147_483_647), "process_start": .uint, "client_nonce": .id,
  ],
  "AppshotHelloAck": [
    "type": .values(["hello_ack"]), "version": .int(1, 1), "instance_id": .id, "broker_nonce": .id,
    "session_id": .id,
  ],
  "AppshotClientState": [
    "type": .values(["client_state"]), "version": .int(1, 1), "request_id": .id, "broker_id": .id,
    "session_id": .id, "activity_ns": .uint, "appshot_count": .int(0, 4), "can_accept": .bool,
  ],
  "AppshotAttachOffer": [
    "type": .values(["attach_offer"]), "version": .int(1, 1), "request_id": .id, "broker_id": .id,
    "session_id": .id, "manifest_path": .path,
  ],
  "AppshotAttachAck": [
    "type": .values(["attach_ack"]), "version": .int(1, 1), "request_id": .id, "broker_id": .id,
    "session_id": .id, "accepted": .bool, "reason": .str(256),
  ],
  "AppshotAttachCommit": [
    "type": .values(["attach_commit"]), "version": .int(1, 1), "request_id": .id, "broker_id": .id,
    "session_id": .id, "manifest_path": .path,
  ],
  "AppshotAttachRevoke": [
    "type": .values(["attach_revoke"]), "version": .int(1, 1), "request_id": .id, "broker_id": .id,
    "session_id": .id, "reason": .str(256),
  ],
  "AppshotRelease": [
    "type": .values(["release"]), "version": .int(1, 1), "request_id": .id, "broker_id": .id,
    "session_id": .id,
  ],
  "AppshotReleaseAck": [
    "type": .values(["release_ack"]), "version": .int(1, 1), "request_id": .id, "broker_id": .id,
    "session_id": .id, "released": .bool,
  ],
  "AppshotCommand": [
    "type": .values(["command"]), "version": .int(1, 1), "request_id": .id, "broker_id": .id,
    "session_id": .id, "name": .values(["status", "enable", "disable", "shortcut"]),
    "argument": .str(256),
  ],
  "AppshotCommandResult": [
    "type": .values(["command_result"]), "version": .int(1, 1), "request_id": .id, "broker_id": .id,
    "session_id": .id, "ok": .bool, "code": .id, "message": .str(1024),
  ],
  "AppshotClientStateAck": [
    "type": .values(["client_state_ack"]), "version": .int(1, 1), "request_id": .id,
    "broker_id": .id, "session_id": .id, "appshot_count": .int(0, 4), "can_accept": .bool,
  ],
  "AppshotStatus": [
    "type": .values(["status"]), "version": .int(1, 1), "request_id": .id, "broker_id": .id,
    "session_id": .id, "enabled": .bool, "chord": .str(128),
    "registration": .values(["registered", "conflict", "unavailable"]),
    "connected_tuis": .int(0, 16), "permission": .values(["ready", "unavailable", "unknown"]),
    "appshot_count": .int(0, 4), "can_accept": .bool,
  ],
]
func asMatch(_ s: String, _ pattern: String) -> Bool {
  s.range(of: pattern, options: .regularExpression) == s.startIndex..<s.endIndex
}
// NSNumber bridges both booleans and numbers. JSON's own encoding preserves their
// distinction on Darwin and swift-foundation without depending on CoreFoundation.
private func asIsBoolean(_ number: NSNumber) -> Bool {
  guard let data = try? JSONSerialization.data(withJSONObject: number, options: .fragmentsAllowed)
  else { return false }
  return data == Data("true".utf8) || data == Data("false".utf8)
}
func asValidate(_ v: Any, _ rule: ASRule) throws {
  var ok = false
  switch rule {
  case .model(let name):
    try asModel(v, name)
    return
  case .int(let lo, let hi):
    if let n = v as? NSNumber, !asIsBoolean(n) {
      let d = n.doubleValue
      ok = d.isFinite && d.rounded() == d && d >= Double(lo) && d <= Double(hi)
    }
  case .coord:
    if let n = v as? NSNumber, !asIsBoolean(n) {
      ok = n.doubleValue.isFinite && abs(n.doubleValue) <= 1_000_000
    }
  case .extent:
    if let n = v as? NSNumber, !asIsBoolean(n) {
      let d = n.doubleValue
      ok = d.isFinite && d > 0 && d <= 1_000_000
    }
  case .bool: if let n = v as? NSNumber { ok = asIsBoolean(n) }
  case .reasons:
    if let a = v as? [String], a.count <= 16 {
      ok = a.allSatisfy { $0.utf8.count <= 128 && !$0.contains("\0") }
    }
  default:
    if let s = v as? String {
      switch rule {
      case .str(let n): ok = s.utf8.count <= n && !s.contains("\0")
      case .values(let a): ok = a.contains(s)
      case .uint: ok = asMatch(s, "^(0|[1-9][0-9]{0,19})$") && UInt64(s) != nil
      case .hash: ok = asMatch(s, "^[0-9a-f]{64}$")
      case .token: ok = asMatch(s, "^[0-9a-f]{32}$")
      case .id: ok = asMatch(s, "^[A-Za-z0-9_-]{1,128}$")
      case .sid: ok = appshotValidWindowsSID(s)
      case .windowsPath: ok = appshotValidWindowsPath(s)
      case .path:
        ok =
          s.hasPrefix("/") && s.utf8.count <= 4096 && !s.contains("\0")
          && !s.split(separator: "/", omittingEmptySubsequences: false).contains(where: {
            $0 == "." || $0 == ".."
          })
      case .date:
        let formatter = ISO8601DateFormatter()
        if asMatch(s, "^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$"),
          let date = formatter.date(from: s)
        {
          ok = formatter.string(from: date) == s
        }
      default: break
      }
    }
  }
  if !ok { throw AppshotProtocolError.invalidValue }
}
func asModel(_ v: Any, _ name: String) throws {
  guard let fields = appshotSchemas[name] ?? appshotWindowsSchemas[name], let o = v as? [String: Any],
    Set(o.keys) == Set(fields.keys)
  else { throw AppshotProtocolError.invalidFields }
  for (k, r) in fields { try asValidate(o[k]!, r) }
}
/// Small bounded recursive scanner detects duplicate keys before Foundation decoding.
private struct ASJSON {
  let bytes: [UInt8]
  var i = 0
  mutating func ws() { while i < bytes.count && [9, 10, 13, 32].contains(bytes[i]) { i += 1 } }
  mutating func string() throws -> String {
    let start = i
    i += 1
    while i < bytes.count {
      let c = bytes[i]
      i += 1
      if c == 34 {
        guard
          let s = try JSONSerialization.jsonObject(
            with: Data(bytes[start..<i]), options: .fragmentsAllowed) as? String
        else { throw AppshotProtocolError.invalidJSON }
        return s
      }
      if c == 92 { i += 1 }
    }
    throw AppshotProtocolError.invalidJSON
  }
  mutating func value(_ depth: Int) throws {
    guard depth <= 32 else { throw AppshotProtocolError.invalidJSON }
    ws()
    guard i < bytes.count else { throw AppshotProtocolError.invalidJSON }
    let c = bytes[i]
    if c == 34 {
      _ = try string()
      return
    }
    if c == 123 {
      i += 1
      ws()
      if i < bytes.count && bytes[i] == 125 {
        i += 1
        return
      }
      var keys = Set<String>()
      while true {
        ws()
        guard i < bytes.count && bytes[i] == 34 else { throw AppshotProtocolError.invalidJSON }
        let k = try string()
        guard keys.insert(k).inserted else { throw AppshotProtocolError.duplicateKey }
        ws()
        guard i < bytes.count && bytes[i] == 58 else { throw AppshotProtocolError.invalidJSON }
        i += 1
        try value(depth + 1)
        ws()
        guard i < bytes.count else { throw AppshotProtocolError.invalidJSON }
        let end = bytes[i]
        i += 1
        if end == 125 { return }
        guard end == 44 else { throw AppshotProtocolError.invalidJSON }
      }
    }
    if c == 91 {
      i += 1
      ws()
      if i < bytes.count && bytes[i] == 93 {
        i += 1
        return
      }
      while true {
        try value(depth + 1)
        ws()
        guard i < bytes.count else { throw AppshotProtocolError.invalidJSON }
        let end = bytes[i]
        i += 1
        if end == 93 { return }
        guard end == 44 else { throw AppshotProtocolError.invalidJSON }
      }
    }
    let start = i
    while i < bytes.count && ![9, 10, 13, 32, 44, 93, 125].contains(bytes[i]) { i += 1 }
    guard i > start else { throw AppshotProtocolError.invalidJSON }
    let v = try JSONSerialization.jsonObject(
      with: Data(bytes[start..<i]), options: .fragmentsAllowed)
    if let n = v as? NSNumber, !n.doubleValue.isFinite { throw AppshotProtocolError.invalidJSON }
  }
}
func asDecode(_ data: Data) throws -> Any {
  guard !data.isEmpty && data.count <= AppshotLimits.maximumFrameBytes else {
    throw AppshotProtocolError.frameSize
  }
  guard String(data: data, encoding: .utf8) != nil else { throw AppshotProtocolError.invalidJSON }
  var p = ASJSON(bytes: Array(data))
  try p.value(0)
  p.ws()
  guard p.i == p.bytes.count else { throw AppshotProtocolError.invalidJSON }
  return try JSONSerialization.jsonObject(with: data, options: .fragmentsAllowed)
}
extension AppshotManifest {
  public static func decodeStrict(_ data: Data) throws -> Self {
    let o = try asDecode(data)
    try asModel(o, "AppshotManifest")
    let m = try JSONDecoder().decode(Self.self, from: data)
    guard m.png.name == "appshot-\(m.token).png", m.ax.name == "appshot-\(m.token).ax.json",
      m.png.width * m.png.height <= 32_000_000
    else { throw AppshotProtocolError.invalidValue }
    if m.ax.coverage == .unavailable {
      guard m.ax.nodeCount == 0, m.ax.depth == 0, m.ax.truncated,
        !m.ax.truncationReasons.isEmpty else { throw AppshotProtocolError.invalidValue }
    }
    return m
  }
  public func encodeStrict() throws -> Data {
    let data = try JSONEncoder().encode(self)
    _ = try Self.decodeStrict(data)
    return data
  }
}
public enum AppshotMessage: Equatable {
  case hello(AppshotHello)
  case helloAck(AppshotHelloAck)
  case clientState(AppshotClientState)
  case attachOffer(AppshotAttachOffer)
  case attachAck(AppshotAttachAck)
  case attachCommit(AppshotAttachCommit)
  case attachRevoke(AppshotAttachRevoke)
  case release(AppshotRelease)
  case releaseAck(AppshotReleaseAck)
  case command(AppshotCommand)
  case commandResult(AppshotCommandResult)
  case clientStateAck(AppshotClientStateAck)
  case status(AppshotStatus)
  public static func decodeStrict(_ data: Data) throws -> Self {
    let o = try asDecode(data)
    guard let d = o as? [String: Any], let type = d["type"] as? String else {
      throw AppshotProtocolError.unknownMessage
    }
    switch type {
    case "hello":
      try asModel(o, "AppshotHello")
      return .hello(try JSONDecoder().decode(AppshotHello.self, from: data))
    case "hello_ack":
      try asModel(o, "AppshotHelloAck")
      return .helloAck(try JSONDecoder().decode(AppshotHelloAck.self, from: data))
    case "client_state":
      try asModel(o, "AppshotClientState")
      return .clientState(try JSONDecoder().decode(AppshotClientState.self, from: data))
    case "attach_offer":
      try asModel(o, "AppshotAttachOffer")
      return .attachOffer(try JSONDecoder().decode(AppshotAttachOffer.self, from: data))
    case "attach_ack":
      try asModel(o, "AppshotAttachAck")
      return .attachAck(try JSONDecoder().decode(AppshotAttachAck.self, from: data))
    case "attach_commit":
      try asModel(o, "AppshotAttachCommit")
      return .attachCommit(try JSONDecoder().decode(AppshotAttachCommit.self, from: data))
    case "attach_revoke":
      try asModel(o, "AppshotAttachRevoke")
      return .attachRevoke(try JSONDecoder().decode(AppshotAttachRevoke.self, from: data))
    case "release":
      try asModel(o, "AppshotRelease")
      return .release(try JSONDecoder().decode(AppshotRelease.self, from: data))
    case "release_ack":
      try asModel(o, "AppshotReleaseAck")
      return .releaseAck(try JSONDecoder().decode(AppshotReleaseAck.self, from: data))
    case "command":
      try asModel(o, "AppshotCommand")
      return .command(try JSONDecoder().decode(AppshotCommand.self, from: data))
    case "command_result":
      try asModel(o, "AppshotCommandResult")
      return .commandResult(try JSONDecoder().decode(AppshotCommandResult.self, from: data))
    case "client_state_ack":
      try asModel(o, "AppshotClientStateAck")
      return .clientStateAck(try JSONDecoder().decode(AppshotClientStateAck.self, from: data))
    case "status":
      try asModel(o, "AppshotStatus")
      return .status(try JSONDecoder().decode(AppshotStatus.self, from: data))
    default: throw AppshotProtocolError.unknownMessage
    }
  }
  public func encodeFrame() throws -> Data {
    let data: Data
    switch self {
    case .hello(let v): data = try JSONEncoder().encode(v)
    case .helloAck(let v): data = try JSONEncoder().encode(v)
    case .clientState(let v): data = try JSONEncoder().encode(v)
    case .attachOffer(let v): data = try JSONEncoder().encode(v)
    case .attachAck(let v): data = try JSONEncoder().encode(v)
    case .attachCommit(let v): data = try JSONEncoder().encode(v)
    case .attachRevoke(let v): data = try JSONEncoder().encode(v)
    case .release(let v): data = try JSONEncoder().encode(v)
    case .releaseAck(let v): data = try JSONEncoder().encode(v)
    case .command(let v): data = try JSONEncoder().encode(v)
    case .commandResult(let v): data = try JSONEncoder().encode(v)
    case .clientStateAck(let v): data = try JSONEncoder().encode(v)
    case .status(let v): data = try JSONEncoder().encode(v)
    }
    _ = try Self.decodeStrict(data)
    guard data.count + 1 <= 65536 else { throw AppshotProtocolError.frameSize }
    var frame = data
    frame.append(10)
    return frame
  }
}
public enum AppshotClientMessage: Equatable {
  case hello(AppshotHello)
  case clientState(AppshotClientState)
  case attachAck(AppshotAttachAck)
  case release(AppshotRelease)
  case command(AppshotCommand)
  public static func decodeStrict(_ data: Data) throws -> Self {
    let o = try asDecode(data)
    guard let d = o as? [String: Any], let type = d["type"] as? String else {
      throw AppshotProtocolError.unknownMessage
    }
    switch type {
    case "hello":
      try asModel(o, "AppshotHello")
      return .hello(try JSONDecoder().decode(AppshotHello.self, from: data))
    case "client_state":
      try asModel(o, "AppshotClientState")
      return .clientState(try JSONDecoder().decode(AppshotClientState.self, from: data))
    case "attach_ack":
      try asModel(o, "AppshotAttachAck")
      return .attachAck(try JSONDecoder().decode(AppshotAttachAck.self, from: data))
    case "release":
      try asModel(o, "AppshotRelease")
      return .release(try JSONDecoder().decode(AppshotRelease.self, from: data))
    case "command":
      try asModel(o, "AppshotCommand")
      return .command(try JSONDecoder().decode(AppshotCommand.self, from: data))
    default: throw AppshotProtocolError.unknownMessage
    }
  }
  public func encodeFrame() throws -> Data {
    let data: Data
    switch self {
    case .hello(let v): data = try JSONEncoder().encode(v)
    case .clientState(let v): data = try JSONEncoder().encode(v)
    case .attachAck(let v): data = try JSONEncoder().encode(v)
    case .release(let v): data = try JSONEncoder().encode(v)
    case .command(let v): data = try JSONEncoder().encode(v)
    }
    _ = try Self.decodeStrict(data)
    guard data.count + 1 <= 65536 else { throw AppshotProtocolError.frameSize }
    var frame = data
    frame.append(10)
    return frame
  }
}
public enum AppshotBrokerMessage: Equatable {
  case helloAck(AppshotHelloAck)
  case attachOffer(AppshotAttachOffer)
  case attachCommit(AppshotAttachCommit)
  case attachRevoke(AppshotAttachRevoke)
  case releaseAck(AppshotReleaseAck)
  case commandResult(AppshotCommandResult)
  case clientStateAck(AppshotClientStateAck)
  case status(AppshotStatus)
  public static func decodeStrict(_ data: Data) throws -> Self {
    let o = try asDecode(data)
    guard let d = o as? [String: Any], let type = d["type"] as? String else {
      throw AppshotProtocolError.unknownMessage
    }
    switch type {
    case "hello_ack":
      try asModel(o, "AppshotHelloAck")
      return .helloAck(try JSONDecoder().decode(AppshotHelloAck.self, from: data))
    case "attach_offer":
      try asModel(o, "AppshotAttachOffer")
      return .attachOffer(try JSONDecoder().decode(AppshotAttachOffer.self, from: data))
    case "attach_commit":
      try asModel(o, "AppshotAttachCommit")
      return .attachCommit(try JSONDecoder().decode(AppshotAttachCommit.self, from: data))
    case "attach_revoke":
      try asModel(o, "AppshotAttachRevoke")
      return .attachRevoke(try JSONDecoder().decode(AppshotAttachRevoke.self, from: data))
    case "release_ack":
      try asModel(o, "AppshotReleaseAck")
      return .releaseAck(try JSONDecoder().decode(AppshotReleaseAck.self, from: data))
    case "command_result":
      try asModel(o, "AppshotCommandResult")
      return .commandResult(try JSONDecoder().decode(AppshotCommandResult.self, from: data))
    case "client_state_ack":
      try asModel(o, "AppshotClientStateAck")
      return .clientStateAck(try JSONDecoder().decode(AppshotClientStateAck.self, from: data))
    case "status":
      try asModel(o, "AppshotStatus")
      return .status(try JSONDecoder().decode(AppshotStatus.self, from: data))
    default: throw AppshotProtocolError.unknownMessage
    }
  }
  public func encodeFrame() throws -> Data {
    let data: Data
    switch self {
    case .helloAck(let v): data = try JSONEncoder().encode(v)
    case .attachOffer(let v): data = try JSONEncoder().encode(v)
    case .attachCommit(let v): data = try JSONEncoder().encode(v)
    case .attachRevoke(let v): data = try JSONEncoder().encode(v)
    case .releaseAck(let v): data = try JSONEncoder().encode(v)
    case .commandResult(let v): data = try JSONEncoder().encode(v)
    case .clientStateAck(let v): data = try JSONEncoder().encode(v)
    case .status(let v): data = try JSONEncoder().encode(v)
    }
    _ = try Self.decodeStrict(data)
    guard data.count + 1 <= 65536 else { throw AppshotProtocolError.frameSize }
    var frame = data
    frame.append(10)
    return frame
  }
}
public struct AppshotFrameDecoder {
  private var buffer = Data()
  public init() {}
  public mutating func feed(_ chunk: Data) throws -> [AppshotMessage] {
    var result = [AppshotMessage]()
    for byte in chunk {
      guard buffer.count + 1 <= 65536 else {
        buffer.removeAll()
        throw AppshotProtocolError.frameSize
      }
      if byte == 10 {
        let data = buffer
        buffer.removeAll()
        result.append(try AppshotMessage.decodeStrict(data))
      } else {
        buffer.append(byte)
      }
    }
    return result
  }
  public mutating func finish() throws {
    if !buffer.isEmpty {
      buffer.removeAll()
      throw AppshotProtocolError.incompleteFrame
    }
  }
}

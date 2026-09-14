import Foundation

// Parsed data is not file/process authority: platform adapters must prove it from held handles.
let appshotWindowsSchemas: [String: [String: ASRule]] = [
  "WindowsProcess": [
    "pid": .int(1, 2_147_483_647),
    "process_start": .uint,
    "user_sid": .sid,
  ],
  "WindowsSource": [
    "process": .model("WindowsProcess"),
    "app_id": .str(256),
    "app_label": .str(256),
    "window_title": .str(1024),
    "window_handle": .uint,
    "bounds": .model("AppshotBounds"),
  ],
  "WindowsFile": [
    "volume_serial": .uint,
    "file_id": .token,
    "owner_sid": .sid,
    "link_count": .int(1, 1),
  ],
  "WindowsPNG": [
    "name": .str(128),
    "size": .int(1, 10_485_760),
    "width": .int(1, 16384),
    "height": .int(1, 16384),
    "sha256": .hash,
    "identity": .model("WindowsFile"),
  ],
  "WindowsUIA": [
    "name": .str(128),
    "size": .int(1, 262144),
    "sha256": .hash,
    "identity": .model("WindowsFile"),
    "coverage": .values(["reported_uia_subtree", "unavailable"]),
    "node_count": .int(0, 2000),
    "depth": .int(0, 64),
    "truncated": .bool,
    "truncation_reasons": .reasons,
  ],
  "WindowsBroker": [
    "instance_id": .id,
    "session_id": .id,
    "recipient": .model("WindowsProcess"),
  ],
  "WindowsManifest": [
    "schema_version": .int(2, 2),
    "platform": .values(["windows"]),
    "token": .token,
    "captured_at": .date,
    "source": .model("WindowsSource"),
    "png": .model("WindowsPNG"),
    "uia": .model("WindowsUIA"),
    "broker": .model("WindowsBroker"),
  ],
]

func appshotValidWindowsSID(_ s: String) -> Bool {
  let parts = s.split(separator: "-", omittingEmptySubsequences: false)
  guard (4...18).contains(parts.count), parts[0] == "S", parts[1] == "1" else { return false }
  for (index, part) in parts.dropFirst(2).enumerated() {
    guard asMatch(String(part), "^(0|[1-9][0-9]{0,14})$"), let n = UInt64(part),
      n <= (index == 0 ? 281_474_976_710_655 : 4_294_967_295) else { return false }
  }
  return true
}

func appshotValidWindowsPath(_ s: String) -> Bool {
  guard s.utf8.count <= 4096, asMatch(String(s.prefix(3)), "^[A-Z]:/$") else { return false }
  let parts = s.dropFirst(3).split(separator: "/", omittingEmptySubsequences: false)
  return !parts.isEmpty && parts.allSatisfy { part in
    !part.isEmpty && part != "." && part != ".." && !part.hasSuffix(".") && !part.hasSuffix(" ")
      && !part.unicodeScalars.contains { $0.value < 32 || "<>:\"\\|?*".unicodeScalars.contains($0) }
      && !asMatch(String(part).uppercased(), "^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(\\..*)?$")
  }
}

public struct AppshotWindowsProcess: Codable, Equatable, Sendable, AppshotPeerIdentity {
  public let pid: Int32
  public let processStart: String
  public let userSID: String
  enum CodingKeys: String, CodingKey {
    case pid
    case processStart = "process_start"
    case userSID = "user_sid"
  }
  public init(pid: Int32, processStart: String, userSID: String) {
    self.pid = pid
    self.processStart = processStart
    self.userSID = userSID
  }
  public static func decodeStrict(_ data: Data) throws -> Self {
    try asModel(asDecode(data), "WindowsProcess")
    let identity = try JSONDecoder().decode(Self.self, from: data)
    guard identity.processStart != "0" else { throw AppshotProtocolError.invalidValue }
    return identity
  }
}

public struct AppshotWindowsFileIdentity: Codable, Equatable, Sendable {
  public let volume_serial: String
  public let file_id: String
  public let owner_sid: String
  public let link_count: Int
}
public struct AppshotWindowsSource: Codable, Equatable {
  public let process: AppshotWindowsProcess
  public let app_id: String
  public let app_label: String
  public let window_title: String
  public let window_handle: String
  public let bounds: AppshotBounds
}
public struct AppshotWindowsPNG: Codable, Equatable {
  public let name: String
  public let size: Int
  public let width: Int
  public let height: Int
  public let sha256: String
  public let identity: AppshotWindowsFileIdentity
}
public struct AppshotWindowsUIA: Codable, Equatable {
  public let name: String
  public let size: Int
  public let sha256: String
  public let identity: AppshotWindowsFileIdentity
  public let coverage: String
  public let node_count: Int
  public let depth: Int
  public let truncated: Bool
  public let truncation_reasons: [String]
}
public struct AppshotWindowsBrokerBinding: Codable, Equatable {
  public let instance_id: String
  public let session_id: String
  public let recipient: AppshotWindowsProcess
}
public struct AppshotWindowsManifest: Codable, Equatable {
  public let schema_version: Int
  public let platform: String
  public let token: String
  public let captured_at: String
  public let source: AppshotWindowsSource
  public let png: AppshotWindowsPNG
  public let uia: AppshotWindowsUIA
  public let broker: AppshotWindowsBrokerBinding

  public static func decodeStrict(_ data: Data) throws -> Self {
    try asModel(asDecode(data), "WindowsManifest")
    let m = try JSONDecoder().decode(Self.self, from: data)
    guard m.source.window_handle != "0",
      m.png.name == "appshot-\(m.token).png", m.uia.name == "appshot-\(m.token).uia.json",
      m.png.width * m.png.height <= 32_000_000,
      m.png.identity.owner_sid == m.broker.recipient.userSID,
      m.uia.identity.owner_sid == m.broker.recipient.userSID,
      m.source.process.userSID == m.broker.recipient.userSID,
      m.source.process.processStart != "0", m.broker.recipient.processStart != "0"
    else { throw AppshotProtocolError.invalidValue }
    if m.uia.coverage == "unavailable" {
      guard m.uia.node_count == 0, m.uia.depth == 0, m.uia.truncated,
        !m.uia.truncation_reasons.isEmpty else { throw AppshotProtocolError.invalidValue }
    }
    return m
  }
  public func encodeStrict() throws -> Data {
    let data = try JSONEncoder().encode(self)
    _ = try Self.decodeStrict(data)
    return data
  }
}

/// Explicit v2 envelope, deliberately not accepted by any of the v1 decoders.
public struct AppshotWindowsMessage: Equatable {
  public let data: Data
  public let type: String
  public static func decodeStrict(_ data: Data) throws -> Self {
    guard let value = try asDecode(data) as? [String: Any],
      let type = value["type"] as? String
    else { throw AppshotProtocolError.unknownMessage }
    let names = [
      "hello": "AppshotHello", "hello_ack": "AppshotHelloAck",
      "client_state": "AppshotClientState", "attach_offer": "AppshotAttachOffer",
      "attach_ack": "AppshotAttachAck", "attach_commit": "AppshotAttachCommit",
      "attach_revoke": "AppshotAttachRevoke", "release": "AppshotRelease",
      "release_ack": "AppshotReleaseAck", "command": "AppshotCommand",
      "command_result": "AppshotCommandResult", "client_state_ack": "AppshotClientStateAck",
      "status": "AppshotStatus",
    ]
    guard let name = names[type], var fields = appshotSchemas[name] else {
      throw AppshotProtocolError.unknownMessage
    }
    fields["version"] = .int(2, 2)
    fields["platform"] = .values(["windows"])
    if fields["manifest_path"] != nil { fields["manifest_path"] = .windowsPath }
    if type == "hello" { fields["user_sid"] = .sid }
    guard Set(fields.keys) == Set(value.keys) else { throw AppshotProtocolError.invalidFields }
    for (key, rule) in fields { try asValidate(value[key]!, rule) }
    if type == "hello", value["process_start"] as? String == "0" {
      throw AppshotProtocolError.invalidValue
    }
    return Self(data: try JSONSerialization.data(withJSONObject: value, options: .sortedKeys), type: type)
  }
  public func encodeFrame() throws -> Data {
    guard data.count + 1 <= AppshotLimits.maximumFrameBytes else { throw AppshotProtocolError.frameSize }
    var frame = data
    frame.append(10)
    return frame
  }
}

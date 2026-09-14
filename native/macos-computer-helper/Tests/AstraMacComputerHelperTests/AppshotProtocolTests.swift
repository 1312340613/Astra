import Foundation
import Testing

@testable import AstraMacComputerHelperCore

@Suite struct AppshotProtocolTests {
  func fixture(_ name: String) throws -> Data {
    try Data(
      contentsOf: URL(fileURLWithPath: #filePath).deletingLastPathComponent()
        .deletingLastPathComponent().appendingPathComponent("Fixtures/" + name))
  }
  @Test func manifestParity() throws {
    let m = try AppshotManifest.decodeStrict(fixture("appshot_manifest_v1.json"))
    #expect(m.source.processStart == "9001")
    #expect(m.ax.coverage == .reportedAXSubtree)
  }
  @Test func messagesParity() throws {
    let rows =
      try JSONSerialization.jsonObject(with: fixture("appshot_messages_v1.json"))
      as! [[String: Any]]
    for row in rows {
      let data = try JSONSerialization.data(withJSONObject: row)
      _ = try AppshotMessage.decodeStrict(data)
      var bad = row
      bad["extra"] = true
      #expect(throws: (any Error).self) {
        try AppshotMessage.decodeStrict(JSONSerialization.data(withJSONObject: bad))
      }
    }
  }
  @Test func strictRejections() throws {
    for raw in [
      "", "[]", "{\"schema_version\":2}", "{\"schema_version\":1,\"schema_version\":1}",
      String(repeating: " ", count: 65537),
    ] { #expect(throws: (any Error).self) { try AppshotManifest.decodeStrict(Data(raw.utf8)) } }
    let mutations: [(String, String, Any)] = [
      ("png", "name", "../x"), ("png", "sha256", String(repeating: "A", count: 64)),
      ("png", "width", 0), ("png", "mode", 420), ("png", "device", 1), ("png", "inode", "01"),
      ("source", "process_start", "18446744073709551616"), ("ax", "node_count", 2001),
      ("ax", "depth", 65), ("ax", "coverage", "all"),
      ("source", "app_label", String(repeating: "x", count: 257)),
      ("source", "window_title", String(repeating: "x", count: 1025)), ("png", "extra", 1),
      ("png", "link_count", 2),
    ]
    for (a, b, v) in mutations {
      var m =
        try JSONSerialization.jsonObject(with: fixture("appshot_manifest_v1.json"))
        as! [String: Any]
      var nested = m[a] as! [String: Any]
      nested[b] = v
      m[a] = nested
      #expect(throws: (any Error).self) {
        try AppshotManifest.decodeStrict(JSONSerialization.data(withJSONObject: m))
      }
    }
  }
  @Test func framingAndDirections() throws {
    let rows =
      try JSONSerialization.jsonObject(with: fixture("appshot_messages_v1.json"))
      as! [[String: Any]]
    for row in rows {
      let data = try JSONSerialization.data(withJSONObject: row)
      let m = try AppshotMessage.decodeStrict(data)
      let frame = try m.encodeFrame()
      var decoder = AppshotFrameDecoder()
      #expect(try decoder.feed(Data(frame.prefix(7))).isEmpty)
      #expect(try decoder.feed(Data(frame.dropFirst(7))) == [m])
      try decoder.finish()
      if ["hello", "client_state", "attach_ack", "release", "command"].contains(
        row["type"] as! String)
      {
        _ = try AppshotClientMessage.decodeStrict(data)
      } else {
        _ = try AppshotBrokerMessage.decodeStrict(data)
      }
    }
    for raw in [
      "\n", String(repeating: " ", count: 65537), #"{"type":"hello","ty\u0070e":"hello"}"# + "\n",
      String(repeating: "[", count: 33) + String(repeating: "]", count: 33) + "\n",
    ] {
      var decoder = AppshotFrameDecoder()
      #expect(throws: (any Error).self) { try decoder.feed(Data(raw.utf8)) }
    }
    var decoder = AppshotFrameDecoder()
    _ = try decoder.feed(Data("{".utf8))
    #expect(throws: (any Error).self) { try decoder.finish() }
  }
  @Test func authoritativeCases() throws {
    let cases =
      try JSONSerialization.jsonObject(with: fixture("appshot_protocol_cases_v1.json"))
      as! [[String: Any]]
    for c in cases {
      let raw = Data((c["raw"] as! String).utf8)
      let parse = {
        if c["kind"] as! String == "manifest" {
          _ = try AppshotManifest.decodeStrict(raw)
        } else {
          _ = try AppshotMessage.decodeStrict(raw)
        }
      }
      if c["valid"] as! Bool {
        try parse()
      } else {
        #expect(throws: (any Error).self) { try parse() }
      }
    }
  }
}

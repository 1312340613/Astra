import Foundation
import XCTest
@testable import AstraAppshotCore

final class AppshotCoreTests: XCTestCase {
  func fixture(_ name: String) throws -> Data {
    let native = URL(fileURLWithPath: #filePath).deletingLastPathComponent()
      .deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
    return try Data(contentsOf: native.appendingPathComponent(
      "macos-computer-helper/Tests/Fixtures/" + name))
  }

  func testAuthoritativeV1CasesRemainUnchanged() throws {
    let cases = try XCTUnwrap(JSONSerialization.jsonObject(
      with: fixture("appshot_protocol_cases_v1.json")) as? [[String: Any]])
    XCTAssertFalse(cases.isEmpty)
    for row in cases {
      let raw = Data(try XCTUnwrap(row["raw"] as? String).utf8)
      let parse = {
        if row["kind"] as? String == "manifest" {
          _ = try AppshotManifest.decodeStrict(raw)
        } else {
          _ = try AppshotMessage.decodeStrict(raw)
        }
      }
      if row["valid"] as? Bool == true { try parse() } else { XCTAssertThrowsError(try parse()) }
    }
  }

  func testWindowsV2ContractFixtures() throws {
    let path = URL(fileURLWithPath: #filePath).deletingLastPathComponent()
      .deletingLastPathComponent().appendingPathComponent("Fixtures/appshot_protocol_cases_v2.json")
    let cases = try XCTUnwrap(JSONSerialization.jsonObject(with: Data(contentsOf: path)) as? [[String: Any]])
    for row in cases {
      let raw = Data(try XCTUnwrap(row["raw"] as? String).utf8)
      let parse = {
        if row["kind"] as? String == "manifest" {
          let m = try AppshotWindowsManifest.decodeStrict(raw)
          XCTAssertEqual(try AppshotWindowsManifest.decodeStrict(m.encodeStrict()), m)
          XCTAssertThrowsError(try AppshotManifest.decodeStrict(raw))
        } else {
          let m = try AppshotWindowsMessage.decodeStrict(raw)
          XCTAssertEqual(try AppshotWindowsMessage.decodeStrict(Data(m.encodeFrame().dropLast())), m)
          XCTAssertThrowsError(try AppshotMessage.decodeStrict(raw))
        }
      }
      if row["valid"] as? Bool == true { try parse() }
      else { XCTAssertThrowsError(try parse(), String(describing: row["name"])) }
    }
  }

  func testV1FramingAllDirectionsAndPartialRead() throws {
    let rows = try XCTUnwrap(JSONSerialization.jsonObject(
      with: fixture("appshot_messages_v1.json")) as? [[String: Any]])
    for row in rows {
      let message = try AppshotMessage.decodeStrict(JSONSerialization.data(withJSONObject: row))
      let frame = try message.encodeFrame()
      var decoder = AppshotFrameDecoder()
      XCTAssertEqual(try decoder.feed(Data(frame.prefix(3))), [])
      XCTAssertEqual(try decoder.feed(Data(frame.dropFirst(3))), [message])
      try decoder.finish()
    }
    var decoder = AppshotFrameDecoder()
    _ = try decoder.feed(Data("{".utf8))
    XCTAssertThrowsError(try decoder.finish())
  }

  func testDeadlineCapsAtFiveSecondsAndSharesCancellation() throws {
    var now: TimeInterval = 100
    let parent = AppshotCaptureDeadline(duration: 500, clock: { now })
    XCTAssertEqual(try parent.remaining(), 5)
    let child = try parent.limited(to: 1)
    now += 0.5
    XCTAssertEqual(try child.remaining(), 0.5)
    parent.cancel()
    XCTAssertThrowsError(try child.remaining()) { XCTAssertEqual($0 as? AppshotCaptureError, .cancelled) }
  }

  func testChildBudgetNeverExtendsParentOrResetsOnRetry() throws {
    var now: TimeInterval = 0
    let parent = AppshotCaptureDeadline(clock: { now })
    now = 4.9
    let child = try parent.limited(to: 2)
    now = 5
    XCTAssertThrowsError(try parent.remaining())
    XCTAssertThrowsError(try child.remaining())
    XCTAssertThrowsError(try parent.limited(to: 5))
    XCTAssertThrowsError(try AppshotCaptureDeadline(duration: .nan).remaining())
  }
}

private struct TestPeer: AppshotPeerIdentity {
  let pid: Int32
  let processStart: String
  let account: String
}

private final class TestClock {
  var now: UInt64 = 10_000_000_000
  var identities: [Int32: TestPeer] = [
    10: .init(pid: 10, processStart: "100", account: "current-user"),
    20: .init(pid: 20, processStart: "200", account: "current-user"),
  ]
}

final class AppshotRegistryTests: XCTestCase {
  @MainActor private func fixture() throws -> (AppshotRegistry<TestPeer>, TestClock) {
    let clock = TestClock()
    let registry = AppshotRegistry<TestPeer>(instanceID: "broker", nonce: "nonce",
      identityLookup: { clock.identities[$0] }, monotonicNS: { clock.now },
      peerAllowed: { $0.account == "current-user" })
    for pid: Int32 in [10, 20] {
      try registry.authenticate(connectionID: String(pid), peer: clock.identities[pid]!,
        sessionID: "s\(pid)", pid: Int(pid), processStart: String(pid * 10), clientNonce: "nonce")
    }
    return (registry, clock)
  }

  @MainActor private func state(_ registry: AppshotRegistry<TestPeer>, _ pid: Int32,
    activity: UInt64, count: Int = 0, request: String = "state", accept: Bool = true) throws {
    try registry.update(connectionID: String(pid), requestID: request, brokerID: "broker",
      sessionID: "s\(pid)", activity: activity, count: count, canAccept: accept)
  }

  func testUniqueNewestAndNoFallbackWhenFull() async throws {
    try await MainActor.run {
      let (registry, _) = try fixture()
      XCTAssertThrowsError(try registry.reserveCapture())
      try state(registry, 10, activity: 1)
      try state(registry, 20, activity: 1)
      XCTAssertThrowsError(try registry.reserveCapture()) {
        XCTAssertEqual($0 as? AppshotBrokerError, .receivingSessionAmbiguous)
      }
      try state(registry, 20, activity: 2, count: 4)
      XCTAssertThrowsError(try registry.reserveCapture()) {
        XCTAssertEqual($0 as? AppshotBrokerError, .attachmentLimitReached)
      }
      try state(registry, 20, activity: 3)
      let binding = try registry.reserveCapture()
      XCTAssertEqual(binding.connectionID, "20")
      try state(registry, 10, activity: 4)
      XCTAssertEqual(try registry.revalidate(binding), binding)
      XCTAssertThrowsError(try registry.reserveCapture())
    }
  }

  func testAckConfirmationAndExactOnceCleanup() async throws {
    try await MainActor.run {
      let (registry, _) = try fixture()
      try state(registry, 10, activity: 1)
      let binding = try registry.reserveCapture()
      var cleaned = 0
      try registry.stage(binding, artifact: .init(manifestPath: "opaque-owned-manifest",
        byteCount: 100, cleanup: { cleaned += 1 }))
      XCTAssertNotNil(try registry.acknowledge(connectionID: "10", requestID: binding.requestID, accepted: true))
      XCTAssertThrowsError(try registry.acknowledge(connectionID: "10", requestID: binding.requestID, accepted: true))
      XCTAssertThrowsError(try registry.reserveCapture())
      try state(registry, 10, activity: 2, count: 1, request: binding.requestID)
      XCTAssertTrue(registry.isCommitted(connectionID: "10", requestID: binding.requestID))
      XCTAssertFalse(registry.release(connectionID: "20", requestID: binding.requestID))
      XCTAssertEqual(cleaned, 0)
      XCTAssertTrue(registry.release(connectionID: "10", requestID: binding.requestID))
      registry.disconnect(connectionID: "10")
      XCTAssertEqual(cleaned, 1)
      XCTAssertEqual(registry.liveBytes, 0)
    }
  }

  func testPIDReuseDisconnectTimeoutAndLateArtifactFailClosed() async throws {
    try await MainActor.run {
      let (registry, clock) = try fixture()
      try state(registry, 10, activity: 1)
      let binding = try registry.reserveCapture()
      clock.identities[10] = .init(pid: 10, processStart: "999", account: "current-user")
      XCTAssertThrowsError(try registry.revalidate(binding))
      XCTAssertEqual(registry.deadConnections(), ["10"])
      registry.disconnect(connectionID: "10")
      var cleaned = 0
      XCTAssertThrowsError(try registry.stage(binding, artifact: .init(
        manifestPath: "late", byteCount: 1, cleanup: { cleaned += 1 })))
      XCTAssertEqual(cleaned, 1)
      try state(registry, 20, activity: 2)
      let second = try registry.reserveCapture()
      clock.now += 5_000_000_000
      XCTAssertEqual(registry.expire(), [second])
      XCTAssertEqual(registry.liveBytes, 0)
    }
  }

  func testKernelIdentityUserAndNonceAreRequired() async throws {
    try await MainActor.run {
      let (registry, clock) = try fixture()
      clock.identities[30] = .init(pid: 30, processStart: "300", account: "other-user")
      XCTAssertThrowsError(try registry.authenticate(connectionID: "30", peer: clock.identities[30]!,
        sessionID: "s30", pid: 30, processStart: "300", clientNonce: "nonce"))
      clock.identities[30] = .init(pid: 30, processStart: "300", account: "current-user")
      XCTAssertThrowsError(try registry.authenticate(connectionID: "30", peer: clock.identities[30]!,
        sessionID: "s30", pid: 30, processStart: "301", clientNonce: "nonce"))
      XCTAssertThrowsError(try registry.authenticate(connectionID: "30", peer: clock.identities[30]!,
        sessionID: "s30", pid: 30, processStart: "300", clientNonce: "stale"))
    }
  }
}

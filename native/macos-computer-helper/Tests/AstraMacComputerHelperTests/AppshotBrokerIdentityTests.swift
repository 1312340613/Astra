import Darwin
import Foundation
import Testing

@testable import AstraMacComputerHelperCore

@Suite struct AppshotBrokerIdentityTests {
  private func descriptor(start: String = "42", inode: String = "9", extra: Bool = false)
    -> AppshotRuntimeDescriptor
  {
    .init(
      schemaVersion: 1, instanceID: "01234567-89ab-cdef-0123-456789abcdef",
      brokerNonce: "11234567-89ab-cdef-0123-456789abcdef", pid: 123, uid: 501,
      processStart: start, socketName: "broker.sock",
      entries: [.init(name: "broker.sock", device: "1", inode: inode, kind: "socket")]
        + (extra
          ? [
            .init(
              name: "appshot-0123456789abcdef0123456789abcdef.png",
              device: "1", inode: "10", kind: "file")
          ] : []))
  }

  @Test func exactModeRejectsChosenPIDAndPath() {
    #expect(AppshotHelperMode(arguments: ["--appshot-broker-identity"]) == .brokerIdentity)
    for extra in ["123", "/tmp/other", "--capture"] {
      #expect(AppshotHelperMode(arguments: ["--appshot-broker-identity", extra]) == .unsupported)
    }
  }

  @Test func liveIdentityAllowsUnrelatedJournalUpdate() throws {
    var reads = 0
    var processReads = 0
    let reader = AppshotBrokerIdentityReader(
      uid: 501,
      readDescriptor: {
        reads += 1
        return descriptor(extra: reads > 1)
      },
      identity: { pid in
        processReads += 1
        #expect(pid == 123)
        return .init(pid: 123, uid: 501, processStart: "42")
      })
    let data = try reader.readJSON()
    let object = try #require(JSONSerialization.jsonObject(with: data) as? [String: Any])
    #expect(Set(object.keys) == ["instance_id", "broker_nonce", "pid", "uid", "process_start"])
    #expect(object["pid"] as? Int == 123)
    #expect(object["process_start"] as? String == "42")
    #expect(reads == 2 && processReads == 2)
    #expect(data.count < 512)
  }

  @Test(arguments: ["dead", "reused", "owner", "wrong_pid", "socket", "epoch", "dies_late"])
  func rejectsChangedOrUnownedBroker(change: String) {
    var reads = 0
    var processReads = 0
    let reader = AppshotBrokerIdentityReader(
      uid: 501,
      readDescriptor: {
        reads += 1
        if reads > 1 && change == "socket" { return descriptor(inode: "20") }
        if reads > 1 && change == "epoch" { return descriptor(start: "43") }
        return descriptor()
      },
      identity: { _ in
        processReads += 1
        if change == "dead" || (change == "dies_late" && processReads > 1) { return nil }
        return .init(
          pid: change == "wrong_pid" ? 124 : 123,
          uid: change == "owner" ? 502 : 501,
          processStart: change == "reused" ? "43" : "42")
      })
    #expect(throws: AppshotErrorCode.brokerUnavailable) { try reader.readJSON() }
  }

  @Test func missingDescriptorFailsWithoutQueryingProcess() {
    let reader = AppshotBrokerIdentityReader(
      uid: 501, readDescriptor: { throw AppshotBrokerError.unsafeRuntime },
      identity: { _ in
        Issue.record("missing descriptor must not query a PID")
        return nil
      })
    #expect(throws: AppshotErrorCode.brokerUnavailable) { try reader.readJSON() }
  }

  @Test func privateSocketAndKernelIdentityAreReadWithoutMutation() throws {
    let path = "/private/tmp/as-ident-\(UUID().uuidString.prefix(12))"
    defer { try? FileManager.default.removeItem(atPath: path) }
    let runtime = try AppshotRuntimeDirectory(path: path)
    defer { runtime.close() }
    #expect(try runtime.elect())
    _ = try runtime.publishListener()
    let before = try Data(contentsOf: URL(fileURLWithPath: path + "/broker.json"))
    let reader = AppshotBrokerIdentityReader(readDescriptor: {
      try AppshotRuntimeDirectory.readDescriptor(at: path)
    })
    let data = try reader.readJSON()
    let object = try #require(JSONSerialization.jsonObject(with: data) as? [String: Any])
    #expect(object["pid"] as? Int == Int(getpid()))
    #expect(object["uid"] as? Int == Int(getuid()))
    #expect(try Data(contentsOf: URL(fileURLWithPath: path + "/broker.json")) == before)
    #expect(
      try FileManager.default.contentsOfDirectory(atPath: path).sorted()
        == ["broker.json", "broker.lock", "broker.sock"])
    #expect(unlink(path + "/broker.sock") == 0)
    #expect(throws: AppshotErrorCode.brokerUnavailable) { try reader.readJSON() }
  }
}

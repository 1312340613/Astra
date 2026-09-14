import Darwin
import Foundation
import AstraAppshotCore

/// Fixed-target, read-only discovery. The descriptor supplies a candidate PID, never proof
/// that it still belongs to the broker; kernel start identity and socket epoch must agree.
public func appshotBrokerIdentityJSON() throws -> Data {
  try AppshotBrokerIdentityReader().readJSON()
}

struct AppshotBrokerIdentityReader {
  var uid: UInt32 = getuid()
  var readDescriptor: () throws -> AppshotRuntimeDescriptor = {
    try AppshotRuntimeDirectory.readDescriptor(at: AppshotRuntimeDirectory.defaultPath)
  }
  var identity: (Int32) -> AppshotProcessIdentity? = {
    AppshotSystemProcesses().identity(pid: $0)
  }

  private struct Response: Encodable, Equatable {
    let instance_id: String
    let broker_nonce: String
    let pid: Int32
    let uid: UInt32
    let process_start: String

    init(_ descriptor: AppshotRuntimeDescriptor) {
      instance_id = descriptor.instanceID
      broker_nonce = descriptor.brokerNonce
      pid = descriptor.pid
      uid = descriptor.uid
      process_start = descriptor.processStart
    }
  }

  func readJSON() throws -> Data {
    do {
      let first = try readDescriptor()
      let expected = Response(first)
      guard expected.uid == uid,
        let socket = first.entries.first(where: { $0.name == "broker.sock" && $0.kind == "socket" }
        ),
        identity(first.pid)
          == AppshotProcessIdentity(
            pid: first.pid, uid: uid, processStart: first.processStart)
      else { throw AppshotErrorCode.brokerUnavailable }

      // Other clients can change the artifact journal between reads. Only the broker
      // identity and socket authority define this connection epoch.
      let second = try readDescriptor()
      guard Response(second) == expected,
        second.entries.first(where: { $0.name == "broker.sock" && $0.kind == "socket" }) == socket,
        identity(second.pid)
          == AppshotProcessIdentity(
            pid: second.pid, uid: uid, processStart: second.processStart)
      else { throw AppshotErrorCode.brokerUnavailable }
      let encoder = JSONEncoder()
      encoder.outputFormatting = [.sortedKeys]
      let data = try encoder.encode(expected)
      guard data.count < 512 else { throw AppshotErrorCode.brokerUnavailable }
      return data
    } catch {
      throw AppshotErrorCode.brokerUnavailable
    }
  }
}

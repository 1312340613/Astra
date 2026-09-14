import ApplicationServices
import CoreGraphics
import CryptoKit
import Darwin
import Foundation
import Testing

@testable import AstraMacComputerHelperCore

@Suite struct AppshotArtifactTests {
  let token = String(repeating: "a", count: 32)
  func withPublisher(_ body: (ArtifactBundlePublisher, Int32, String) throws -> Void) throws {
    let path = "/private/tmp/as-pub-\(UUID().uuidString)"
    #expect(mkdir(path, 0o700) == 0)
    defer { try? FileManager.default.removeItem(atPath: path) }
    let fd = open(path, O_RDONLY | O_DIRECTORY | O_CLOEXEC)
    defer { Darwin.close(fd) }
    let publisher = ArtifactBundlePublisher(directoryFD: fd)
    defer { publisher.close() }
    try body(publisher, fd, path)
  }
  func members() -> [ArtifactBundleMember] {
    [
      .init(finalName: "appshot-\(token).png", data: Data([1, 2, 3])),
      .init(finalName: "appshot-\(token).ax.json", data: Data("{}".utf8)),
    ]
  }
  @Test func stagedAuthoritiesManifestLastAndDurable() throws {
    try withPublisher { publisher, fd, path in
      let result = try publisher.publish(
        members(), check: {},
        manifest: { authorities in
          #expect(authorities.count == 2)
          #expect(!FileManager.default.fileExists(atPath: path + "/" + authorities[0].name))
          #expect(authorities[0].size == 3)
          #expect(authorities[0].mode == 0o600)
          #expect(authorities[0].linkCount == 1)
          #expect(authorities[0].owner == Int(getuid()))
          return .init(finalName: "appshot-\(token).manifest.json", data: Data("{}".utf8))
        })
      #expect(result.map(\.name) == members().map(\.finalName) + ["appshot-\(token).manifest.json"])
      for authority in result {
        var info = stat()
        #expect(fstatat(fd, authority.name, &info, AT_SYMLINK_NOFOLLOW) == 0)
        #expect(authority.inode == String(info.st_ino))
        #expect(authority.device == String(UInt64(UInt32(bitPattern: info.st_dev))))
      }
    }
  }
  @Test func namespaceAndCollisionRefused() throws {
    try withPublisher { publisher, _, _ in
      #expect(throws: ArtifactBundleError.invalidNames) {
        try publisher.publish(
          [.init(finalName: "../escape", data: Data([1]))], check: {},
          manifest: { _ in members()[0] })
      }
      _ = try publisher.publish(
        members(), check: {},
        manifest: { _ in .init(finalName: "appshot-\(token).manifest.json", data: Data([4])) })
      #expect(throws: (any Error).self) {
        try publisher.publish(
          members(), check: {},
          manifest: { _ in .init(finalName: "appshot-\(token).manifest.json", data: Data([5])) })
      }
    }
  }
  @Test func cancellationAfterStagingNeverCommitsManifest() throws {
    try withPublisher { publisher, _, path in
      let deadline = AppshotCaptureDeadline()
      #expect(throws: AppshotCaptureError.cancelled) {
        try publisher.publish(
          members(), check: { _ = try deadline.remaining() },
          manifest: { _ in
            deadline.cancel()
            return .init(finalName: "appshot-\(token).manifest.json", data: Data([4]))
          })
      }
      let names = try FileManager.default.contentsOfDirectory(atPath: path)
      #expect(!names.contains(where: { $0.hasPrefix("appshot-") }))
      #expect(names.allSatisfy { $0.hasSuffix(".quarantine") })
      #expect(throws: ArtifactBundleError.poisoned) {
        try publisher.publish(members(), check: {}, manifest: { _ in members()[0] })
      }
    }
  }
  @Test(arguments: [
    BundleTestArtifactSyscalls.Fault.open(1), .open(2), .open(3),
    .write(1), .write(2), .write(3), .fileSync(1), .fileSync(2), .fileSync(3),
    .close(1), .close(2), .close(3), .closeStillOpen(1), .rename(1), .rename(2), .rename(3),
    .descriptorStatus(1), .descriptorStatus(3), .fileMode(1), .specialFileMode(1),
    .directoryRead, .directorySync, .unsafeDirectoryAfterFileSync, .replaceRollbackTarget,
    .replaceQuarantineAfterStatus,
  ])
  func syscallFailuresNeverLeaveOfferableTriplet(fault: BundleTestArtifactSyscalls.Fault) throws {
    try withPublisher { _, fd, path in
      let calls = BundleTestArtifactSyscalls(directoryFD: fd, fault: fault)
      let publisher = ArtifactBundlePublisher(directoryFD: fd, syscalls: calls)
      defer {
        publisher.close()
        _ = fchmod(fd, 0o700)
      }
      #expect(throws: (any Error).self) {
        try publisher.publish(
          members(), check: {},
          manifest: { _ in
            .init(finalName: "appshot-\(token).manifest.json", data: Data([4]))
          })
      }
      #expect(!FileManager.default.fileExists(atPath: path + "/appshot-\(token).manifest.json"))
      #expect(calls.unlinkCalls == 0)
    }
  }
  @Test func sixtyFourTripletsAndCombinedQuota() throws {
    try withPublisher { publisher, _, _ in
      for number in 0..<64 {
        let key = String(format: "%032x", number)
        _ = try publisher.publish(
          [
            .init(finalName: "appshot-\(key).png", data: Data([1])),
            .init(finalName: "appshot-\(key).ax.json", data: Data([2])),
          ], check: {},
          manifest: { _ in
            .init(finalName: "appshot-\(key).manifest.json", data: Data([3]))
          })
      }
      #expect(throws: ArtifactBundleError.quotaExceeded) {
        try publisher.publish(members(), check: {}, manifest: { _ in members()[0] })
      }
    }
    try withPublisher { publisher, fd, _ in
      for number in 0..<26 {
        let key = String(format: "%032x", number)
        for (suffix, size) in [
          (".png", 10 * 1024 * 1024), (".ax.json", 256 * 1024), (".manifest.json", 1),
        ] {
          let file = openat(fd, "appshot-\(key)\(suffix)", O_WRONLY | O_CREAT | O_EXCL, 0o600)
          #expect(file >= 0)
          #expect(ftruncate(file, off_t(size)) == 0)
          Darwin.close(file)
        }
      }
      #expect(throws: ArtifactBundleError.quotaExceeded) {
        try publisher.publish(members(), check: {}, manifest: { _ in members()[0] })
      }
    }
  }
  @Test func runtimeLeaseFrozenEnrollmentAndCleanup() throws {
    let path = "/private/tmp/as-custody-\(UUID().uuidString.prefix(10))"
    defer { try? FileManager.default.removeItem(atPath: path) }
    let runtime = try AppshotRuntimeDirectory(path: path)
    #expect(try runtime.elect())
    _ = try runtime.publishListener()
    defer { runtime.close() }
    let authorities = try runtime.withArtifactPublication { fd in
      let publisher = ArtifactBundlePublisher(directoryFD: fd)
      defer { publisher.close() }
      return try publisher.publish(
        members(), check: { try runtime.verifyAuthority() },
        manifest: { _ in
          .init(finalName: "appshot-\(token).manifest.json", data: Data([4]))
        }, enroll: { try runtime.trackArtifacts(authorities: $0) })
    }
    #expect(runtime.descriptor?.entries.count == 4)
    let name = authorities[0].name
    #expect(rename(path + "/" + name, path + "/stolen") == 0)
    try Data("replacement".utf8).write(to: URL(fileURLWithPath: path + "/" + name))
    try runtime.releaseArtifacts(names: authorities.map(\.name))
    #expect(
      try Data(contentsOf: URL(fileURLWithPath: path + "/" + name)) == Data("replacement".utf8))
    try runtime.releaseArtifacts(names: authorities.map(\.name))
    #expect(runtime.descriptor?.entries.count == 1)
  }
  @Test func journalSyncFailureHasCoherentStateAndNeverOffers() throws {
    let path = "/private/tmp/as-sync-\(UUID().uuidString.prefix(10))"
    defer { try? FileManager.default.removeItem(atPath: path) }
    let runtime = try AppshotRuntimeDirectory(path: path)
    #expect(try runtime.elect())
    _ = try runtime.publishListener()
    defer { runtime.close() }
    try runtime.withArtifactPublication { fd in
      let publisher = ArtifactBundlePublisher(directoryFD: fd)
      defer { publisher.close() }
      #expect(throws: AppshotBrokerError.systemFailure) {
        try publisher.publish(
          members(), check: {},
          manifest: { _ in
            .init(finalName: "appshot-\(token).manifest.json", data: Data([4]))
          }, enroll: { try runtime.trackArtifacts(authorities: $0, sync: { _ in -1 }) })
      }
      #expect(runtime.descriptor?.entries.count == 4)
      try runtime.releaseArtifacts(
        names: members().map(\.finalName) + ["appshot-\(token).manifest.json"])
      #expect(runtime.descriptor?.entries.count == 1)
      #expect(!FileManager.default.fileExists(atPath: path + "/appshot-\(token).manifest.json"))
    }
  }

  func captured(_ runtime: AppshotRuntimeDirectory, binding: AppshotRecipientBinding? = nil) throws
    -> AppshotCaptureResult
  {
    let context = CGContext(
      data: nil, width: 1, height: 1, bitsPerComponent: 8, bytesPerRow: 4,
      space: CGColorSpaceCreateDeviceRGB(), bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue)!
    context.data!.assumingMemoryBound(to: UInt8.self)[3] = 255
    let source = AppshotSource(
      pid: 42, processStart: "9", bundleId: "app", appLabel: "App", windowTitle: "Window",
      windowId: 7, bounds: .init(x: 0, y: 0, width: 1, height: 1))
    let recipient =
      binding
      ?? AppshotRecipientBinding(
        requestID: "r", connectionID: "c", sessionID: "s",
        identity: .init(pid: 1, uid: getuid(), processStart: "1"),
        instanceID: runtime.descriptor!.instanceID)
    return .init(
      authority: .init(
        recipient: recipient,
        target: .init(source: source, axRoot: AXUIElementCreateApplication(42), window: nil),
        nonce: UUID()),
      png: try AppshotCaptureImage.png(context.makeImage()!), width: 1, height: 1, deadline: .init()
    )
  }
  @Test @MainActor func realManifestHashesDimensionsJournalAndIdempotentRelease() throws {
    let path = "/private/tmp/as-man-\(UUID().uuidString.prefix(10))"
    defer { try? FileManager.default.removeItem(atPath: path) }
    let runtime = try AppshotRuntimeDirectory(path: path)
    #expect(try runtime.elect())
    _ = try runtime.publishListener()
    defer { runtime.close() }
    let result = try captured(runtime)
    let tree = AppshotAXFixture()
    var child = tree
    for _ in 0..<64 {
      let next = AppshotAXFixture()
      child.descendants = [next]
      child = next
    }
    let projection = try AppshotProjectionBuilder().build(
      provider: tree, deadline: result.deadline)
    #expect(projection.metadata.depth == 64)
    var rechecks = 0
    let bundle = try AppshotArtifactPublisher(runtime: runtime).publish(
      result, projection: projection, revalidate: { _ in rechecks += 1 })
    #expect(rechecks == 1)
    let artifact = try bundle.makeCapturedArtifact()
    let manifest = try AppshotManifest.decodeStrict(
      Data(contentsOf: URL(fileURLWithPath: artifact.manifestPath)))
    #expect(manifest.png.width == 1 && manifest.png.height == 1)
    #expect(
      manifest.png.sha256
        == SHA256.hash(data: result.png).map { String(format: "%02x", $0) }.joined())
    #expect(
      manifest.ax.sha256
        == SHA256.hash(data: projection.json).map { String(format: "%02x", $0) }.joined())
    #expect(manifest.broker == result.authority.recipient.manifestBinding)
    #expect(runtime.descriptor?.entries.count == 4)
    #expect(
      artifact.byteCount == result.png.count + projection.json.count
        + (try Data(contentsOf: URL(fileURLWithPath: artifact.manifestPath))).count)
    artifact.destroy()
    artifact.destroy()
    #expect(runtime.descriptor?.entries.count == 1)
    #expect(!FileManager.default.fileExists(atPath: artifact.manifestPath))
    #expect(throws: (any Error).self) { try bundle.makeCapturedArtifact() }
  }
  @Test @MainActor func cancelledOrStoppedHandoffCannotEscape() throws {
    for stop in [false, true] {
      let path = "/private/tmp/as-stop-\(UUID().uuidString.prefix(10))"
      defer { try? FileManager.default.removeItem(atPath: path) }
      let runtime = try AppshotRuntimeDirectory(path: path)
      #expect(try runtime.elect())
      _ = try runtime.publishListener()
      defer { runtime.close() }
      let result = try captured(runtime)
      let projection = try AppshotProjectionBuilder().build(
        provider: AppshotAXFixture(), deadline: result.deadline)
      let bundle = try AppshotArtifactPublisher(runtime: runtime).publish(
        result, projection: projection, revalidate: { _ in })
      if stop { runtime.close() } else { result.deadline.cancel() }
      #expect(throws: (any Error).self) { try bundle.makeCapturedArtifact() }
      bundle.destroy()
      #expect(
        !(try FileManager.default.contentsOfDirectory(atPath: path)).contains(where: {
          $0.hasPrefix("appshot-")
        }))
    }
  }

  @Test @MainActor func collidingCaptureNeverReleasesPriorCustody() throws {
    let path = "/private/tmp/as-coll-\(UUID().uuidString.prefix(10))"
    defer { try? FileManager.default.removeItem(atPath: path) }
    let runtime = try AppshotRuntimeDirectory(path: path)
    #expect(try runtime.elect())
    _ = try runtime.publishListener()
    defer { runtime.close() }
    let result = try captured(runtime)
    let projection = try AppshotProjectionBuilder().build(
      provider: AppshotAXFixture(), deadline: result.deadline)
    let publisher = AppshotArtifactPublisher(runtime: runtime)
    let first = try publisher.publish(result, projection: projection, revalidate: { _ in })
    let artifact = try first.makeCapturedArtifact()
    #expect(throws: (any Error).self) {
      try publisher.publish(result, projection: projection, revalidate: { _ in })
    }
    #expect(runtime.descriptor?.entries.count == 4)
    #expect(FileManager.default.fileExists(atPath: artifact.manifestPath))
    artifact.destroy()
  }

}

private final class AppshotPartialWriteCalls: ArtifactSyscalls {
  let deadline: AppshotCaptureDeadline
  var writes = 0
  init(_ deadline: AppshotCaptureDeadline) { self.deadline = deadline }
  func openFile(at fd: Int32, name: String, flags: Int32, mode: mode_t) -> Int32 {
    Darwin.openat(fd, name, flags, mode)
  }
  func writeFile(_ fd: Int32, buffer: UnsafeRawPointer, count: Int) -> Int {
    writes += 1
    deadline.cancel()
    return Darwin.write(fd, buffer, min(count, 1))
  }
  func sync(_ fd: Int32) -> Int32 { Darwin.fsync(fd) }
  func renameExclusive(at fd: Int32, from: String, to: String) -> Int32 {
    Darwin.renameatx_np(fd, from, fd, to, UInt32(RENAME_EXCL))
  }
  func unlink(at fd: Int32, name: String) -> Int32 { Darwin.unlinkat(fd, name, 0) }
}
extension AppshotArtifactTests {
  @Test func partialWriteCancellationStopsBeforeAnotherSyscall() throws {
    try withPublisher { _, fd, _ in
      let deadline = AppshotCaptureDeadline()
      let calls = AppshotPartialWriteCalls(AppshotCaptureDeadline())
      let publisher = ArtifactBundlePublisher(directoryFD: fd, syscalls: calls)
      defer { publisher.close() }
      #expect(throws: AppshotCaptureError.cancelled) {
        try publisher.publish(
          members(),
          check: {
            _ = try calls.deadline.remaining()
            _ = try deadline.remaining()
          }, manifest: { _ in members()[0] })
      }
      #expect(calls.writes == 1)
    }
  }
  @Test func stopWaitsForPublicationEnrollmentLease() throws {
    let path = "/private/tmp/as-lease-\(UUID().uuidString.prefix(10))"
    defer { try? FileManager.default.removeItem(atPath: path) }
    let runtime = try AppshotRuntimeDirectory(path: path)
    #expect(try runtime.elect())
    _ = try runtime.publishListener()
    let started = DispatchSemaphore(value: 0)
    let stopped = DispatchSemaphore(value: 0)
    try runtime.withArtifactPublication { fd in
      let publisher = ArtifactBundlePublisher(directoryFD: fd)
      defer { publisher.close() }
      _ = try publisher.publish(
        members(), check: {},
        manifest: { _ in
          .init(finalName: "appshot-\(token).manifest.json", data: Data([4]))
        },
        enroll: { authorities in
          DispatchQueue.global().async {
            started.signal()
            runtime.close()
            stopped.signal()
          }
          #expect(started.wait(timeout: .now() + 1) == .success)
          #expect(stopped.wait(timeout: .now() + 0.02) == .timedOut)
          try runtime.trackArtifacts(authorities: authorities)
          #expect(runtime.descriptor?.entries.count == 4)
        })
    }
    #expect(stopped.wait(timeout: .now() + 1) == .success)
    #expect(
      !(try FileManager.default.contentsOfDirectory(atPath: path)).contains(where: {
        $0.hasPrefix("appshot-")
      }))
  }
  @Test func enrollmentRefusesSubstitutedFinalInode() throws {
    let path = "/private/tmp/as-swap-\(UUID().uuidString.prefix(10))"
    defer { try? FileManager.default.removeItem(atPath: path) }
    let runtime = try AppshotRuntimeDirectory(path: path)
    #expect(try runtime.elect())
    _ = try runtime.publishListener()
    defer { runtime.close() }
    try runtime.withArtifactPublication { fd in
      let publisher = ArtifactBundlePublisher(directoryFD: fd)
      defer { publisher.close() }
      #expect(throws: AppshotBrokerError.unsafeRuntime) {
        try publisher.publish(
          members(), check: {},
          manifest: { _ in
            .init(finalName: "appshot-\(token).manifest.json", data: Data([4]))
          },
          enroll: { authorities in
            #expect(renameat(fd, authorities[0].name, fd, "stolen") == 0)
            let replacement = openat(fd, authorities[0].name, O_WRONLY | O_CREAT | O_EXCL, 0o600)
            #expect(replacement >= 0)
            #expect(ftruncate(replacement, 3) == 0)
            Darwin.close(replacement)
            try runtime.trackArtifacts(authorities: authorities)
          })
      }
      #expect(runtime.descriptor?.entries.count == 1)
    }
  }
  @Test @MainActor func concreteTripletBrokerCustody() throws {
    for action in ["timeout", "reject", "disconnect", "release"] {
      let path = "/private/tmp/as-flow-\(UUID().uuidString.prefix(10))"
      defer { try? FileManager.default.removeItem(atPath: path) }
      let runtime = try AppshotRuntimeDirectory(path: path)
      #expect(try runtime.elect())
      _ = try runtime.publishListener()
      defer { runtime.close() }
      let processes = AppshotTestProcesses()
      processes.identities[10] = .init(pid: 10, uid: getuid(), processStart: "100")
      let brokerID = runtime.descriptor!.instanceID
      let registry = AppshotClientRegistry(
        instanceID: brokerID, nonce: "nonce", processes: processes)
      try registry.authenticate(
        connectionID: "c", peer: processes.identities[10]!,
        hello: .init(
          type: "hello", version: 1, sessionId: "s", pid: 10, processStart: "100",
          clientNonce: "nonce"))
      try registry.update(
        connectionID: "c",
        state: .init(
          type: "client_state", version: 1, requestId: "state", brokerId: brokerID, sessionId: "s",
          activityNs: "10", appshotCount: 0, canAccept: true))
      let binding = try registry.reserveCapture()
      let result = try captured(runtime, binding: binding)
      let projection = try AppshotProjectionBuilder().build(
        provider: AppshotAXFixture(), deadline: result.deadline)
      let bundle = try AppshotArtifactPublisher(runtime: runtime).publish(
        result, projection: projection, revalidate: { _ in })
      let artifact = try bundle.makeCapturedArtifact()
      try registry.stage(binding, artifact: artifact)
      switch action {
      case "timeout":
        processes.now += 3_000_000_001
        #expect(registry.expire().count == 1)
        #expect(throws: AppshotBrokerError.unknownAttachment) {
          try registry.acknowledge(connectionID: "c", requestID: binding.requestID, accepted: true)
        }
      case "reject":
        _ = try registry.acknowledge(
          connectionID: "c", requestID: binding.requestID, accepted: false)
      case "disconnect": registry.disconnect(connectionID: "c")
      default:
        _ = try registry.acknowledge(
          connectionID: "c", requestID: binding.requestID, accepted: true)
        #expect(FileManager.default.fileExists(atPath: artifact.manifestPath))
        #expect(!registry.release(connectionID: "other", requestID: binding.requestID))
        #expect(registry.release(connectionID: "c", requestID: binding.requestID))
        #expect(registry.release(connectionID: "c", requestID: binding.requestID))
      }
      #expect(runtime.descriptor?.entries.count == 1)
      #expect(!FileManager.default.fileExists(atPath: artifact.manifestPath))
    }
  }
}

extension AppshotArtifactTests {
  @Test @MainActor func producerJournalSyncFailureCleansOnlyNewCustody() throws {
    let path = "/private/tmp/as-jfail-\(UUID().uuidString.prefix(10))"
    defer { try? FileManager.default.removeItem(atPath: path) }
    let runtime = try AppshotRuntimeDirectory(path: path)
    #expect(try runtime.elect())
    _ = try runtime.publishListener()
    defer { runtime.close() }
    let first = try captured(runtime)
    let projection = try AppshotProjectionBuilder().build(
      provider: AppshotAXFixture(), deadline: first.deadline)
    let kept = try AppshotArtifactPublisher(runtime: runtime).publish(
      first, projection: projection, revalidate: { _ in })
    let artifact = try kept.makeCapturedArtifact()
    let second = try captured(runtime)
    #expect(throws: AppshotBrokerError.systemFailure) {
      try AppshotArtifactPublisher(runtime: runtime, journalSync: { _ in -1 }).publish(
        second, projection: projection, revalidate: { _ in })
    }
    #expect(runtime.descriptor?.entries.count == 4)
    #expect(FileManager.default.fileExists(atPath: artifact.manifestPath))
    let names = try FileManager.default.contentsOfDirectory(atPath: path)
    #expect(names.filter { $0.hasPrefix("appshot-") }.count == 3)
    #expect(names.filter { $0.hasSuffix(".quarantine") }.count == 3)
    artifact.destroy()
  }
}

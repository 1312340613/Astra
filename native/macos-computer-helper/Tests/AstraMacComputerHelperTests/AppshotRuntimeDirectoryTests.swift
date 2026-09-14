import Darwin
import Foundation
import Testing

@testable import AstraMacComputerHelperCore

@Suite struct AppshotRuntimeDirectoryTests {
  func root() -> String { "/private/tmp/as-test-\(UUID().uuidString.prefix(12))" }

  @Test func identityUsesKernelStartAndMonotonicClock() throws {
    let provider = AppshotSystemProcesses()
    let identity = try #require(provider.identity(pid: getpid()))
    #expect(identity.pid == getpid())
    #expect(identity.uid == getuid())
    #expect(UInt64(identity.processStart)! > 0)
    #expect(provider.monotonicNS() > 0)
    #expect(provider.identity(pid: -1) == nil)
  }

  @Test func privateDirectoryElectionAndAtomicDescriptor() throws {
    let path = root()
    defer { try? FileManager.default.removeItem(atPath: path) }
    let runtime = try AppshotRuntimeDirectory(path: path)
    #expect(try runtime.elect())
    let other = try AppshotRuntimeDirectory(path: path)
    #expect(try !other.elect())
    let fd = try runtime.publishListener()
    #expect(fd >= 0)
    let descriptor = try AppshotRuntimeDirectory.readDescriptor(at: path)
    #expect(descriptor.pid == getpid())
    #expect(descriptor.socketName == "broker.sock")
    var info = stat()
    #expect(lstat(path, &info) == 0)
    #expect(info.st_mode & 0o777 == 0o700)
    #expect(lstat(path + "/broker.sock", &info) == 0)
    #expect(info.st_mode & S_IFMT == S_IFSOCK)
    #expect(info.st_mode & 0o777 == 0o600)
    runtime.close()
    #expect(!FileManager.default.fileExists(atPath: path + "/broker.json"))
    #expect(try other.elect())
    other.close()
  }

  @Test func refusesLinksAndReplacedDirectory() throws {
    let path = root()
    let target = root()
    defer {
      try? FileManager.default.removeItem(atPath: path)
      try? FileManager.default.removeItem(atPath: target)
    }
    try FileManager.default.createDirectory(
      atPath: target, withIntermediateDirectories: false, attributes: [.posixPermissions: 0o700])
    #expect(symlink(target, path) == 0)
    #expect(throws: AppshotBrokerError.unsafeRuntime) { try AppshotRuntimeDirectory(path: path) }
    #expect(unlink(path) == 0)
    let runtime = try AppshotRuntimeDirectory(path: path)
    #expect(try runtime.elect())
    #expect(rmdir(path) != 0)  // election lock remains owned
    let moved = path + "-old"
    defer { try? FileManager.default.removeItem(atPath: moved) }
    #expect(rename(path, moved) == 0)
    #expect(symlink(target, path) == 0)
    #expect(throws: AppshotBrokerError.unsafeRuntime) { try runtime.publishListener() }
    runtime.close()
    #expect(FileManager.default.fileExists(atPath: path))
  }

  @Test func concurrentElectionHasExactlyOneOwner() async throws {
    let path = root()
    defer { try? FileManager.default.removeItem(atPath: path) }
    let contenders = try (0..<8).map { _ in try AppshotRuntimeDirectory(path: path) }
    let results = await withTaskGroup(of: Bool.self) { group in
      for contender in contenders { group.addTask { (try? contender.elect()) == true } }
      var values = [Bool]()
      for await value in group { values.append(value) }
      return values
    }
    #expect(results.filter { $0 }.count == 1)
    contenders.forEach { $0.close() }
  }

  @Test func replacedSocketIsNeverDeleted() throws {
    let path = root()
    defer { try? FileManager.default.removeItem(atPath: path) }
    let runtime = try AppshotRuntimeDirectory(path: path)
    #expect(try runtime.elect())
    _ = try runtime.publishListener()
    #expect(unlink(path + "/broker.sock") == 0)
    try Data("replacement".utf8).write(to: URL(fileURLWithPath: path + "/broker.sock"))
    runtime.close()
    #expect(try String(contentsOfFile: path + "/broker.sock") == "replacement")
  }
}

extension AppshotRuntimeDirectoryTests {
  @Test func journalCleansOnlyExactPublishedArtifacts() throws {
    let path = root()
    defer { try? FileManager.default.removeItem(atPath: path) }
    let runtime = try AppshotRuntimeDirectory(path: path)
    #expect(try runtime.elect())
    _ = try runtime.publishListener()
    let name = "appshot-" + String(repeating: "a", count: 32) + ".png"
    let url = URL(fileURLWithPath: path + "/" + name)
    try Data("png".utf8).write(to: url)
    #expect(chmod(url.path, 0o600) == 0)
    try runtime.trackArtifacts(names: [name])
    #expect(try AppshotRuntimeDirectory.readDescriptor(at: path).entries.count == 2)
    #expect(throws: AppshotBrokerError.unsafeRuntime) {
      try runtime.trackArtifacts(names: ["../elsewhere"])
    }
    // Atomic replacement must survive a release carrying the old authority.
    try Data("replacement".utf8).write(to: url, options: .atomic)
    #expect(chmod(url.path, 0o600) == 0)
    try runtime.releaseArtifacts(names: [name])
    #expect(try String(contentsOf: url) == "replacement")
    runtime.close()
    #expect(try String(contentsOf: url) == "replacement")
  }

  @Test func descriptorReaderRejectsReplacedSocketAndDoesNotCreateDirectory() throws {
    let path = root()
    defer { try? FileManager.default.removeItem(atPath: path) }
    #expect(throws: AppshotBrokerError.unsafeRuntime) {
      try AppshotRuntimeDirectory.readDescriptor(at: path)
    }
    #expect(!FileManager.default.fileExists(atPath: path))
    let runtime = try AppshotRuntimeDirectory(path: path)
    #expect(try runtime.elect())
    _ = try runtime.publishListener()
    #expect(unlink(path + "/broker.sock") == 0)
    #expect(symlink("/dev/null", path + "/broker.sock") == 0)
    #expect(throws: AppshotBrokerError.unsafeRuntime) {
      try AppshotRuntimeDirectory.readDescriptor(at: path)
    }
    runtime.close()
  }

  @Test func replacedElectionLockRefusesPublication() throws {
    let path = root()
    defer { try? FileManager.default.removeItem(atPath: path) }
    let runtime = try AppshotRuntimeDirectory(path: path)
    #expect(try runtime.elect())
    #expect(unlink(path + "/broker.lock") == 0)
    let fd = open(path + "/broker.lock", O_WRONLY | O_CREAT | O_EXCL, 0o600)
    #expect(fd >= 0)
    Darwin.close(fd)
    #expect(throws: AppshotBrokerError.unsafeRuntime) { try runtime.publishListener() }
    runtime.close()
  }

  @Test func staleSocketNeedsDeadProcessProofAndExactInode() throws {
    let path = root()
    defer { try? FileManager.default.removeItem(atPath: path) }
    try FileManager.default.createDirectory(
      atPath: path, withIntermediateDirectories: false, attributes: [.posixPermissions: 0o700])
    let child = Process()
    let output = Pipe()
    child.executableURL = URL(fileURLWithPath: "/usr/bin/python3")
    child.arguments = [
      "-c",
      "import socket,os,sys,time; s=socket.socket(socket.AF_UNIX); s.bind(sys.argv[1]+'/broker.sock'); os.chmod(sys.argv[1]+'/broker.sock',0o600); print('ready',flush=True); time.sleep(30)",
      path,
    ]
    child.standardOutput = output
    try child.run()
    defer {
      if child.isRunning {
        child.terminate()
        child.waitUntilExit()
      }
    }
    #expect(!output.fileHandleForReading.availableData.isEmpty)
    let identity = try #require(AppshotSystemProcesses().identity(pid: child.processIdentifier))
    var info = stat()
    #expect(lstat(path + "/broker.sock", &info) == 0)
    let descriptor = AppshotRuntimeDescriptor(
      schemaVersion: 1, instanceID: UUID().uuidString, brokerNonce: UUID().uuidString,
      pid: identity.pid, uid: identity.uid, processStart: identity.processStart,
      socketName: "broker.sock",
      entries: [
        .init(
          name: "broker.sock", device: String(UInt32(bitPattern: info.st_dev)),
          inode: String(info.st_ino), kind: "socket")
      ])
    let encoder = JSONEncoder()
    encoder.outputFormatting = [.sortedKeys, .withoutEscapingSlashes]
    try encoder.encode(descriptor).write(to: URL(fileURLWithPath: path + "/broker.json"))
    #expect(chmod(path + "/broker.json", 0o600) == 0)
    let runtime = try AppshotRuntimeDirectory(path: path)
    #expect(throws: AppshotBrokerError.unsafeRuntime) { try runtime.elect() }
    #expect(FileManager.default.fileExists(atPath: path + "/broker.sock"))
    child.terminate()
    child.waitUntilExit()
    #expect(try runtime.elect())
    #expect(!FileManager.default.fileExists(atPath: path + "/broker.sock"))
    #expect(!FileManager.default.fileExists(atPath: path + "/broker.json"))
    _ = try runtime.publishListener()
    runtime.close()
  }

  @Test func malformedDescriptorIsQuarantinedInPlace() throws {
    let path = root()
    defer { try? FileManager.default.removeItem(atPath: path) }
    let runtime = try AppshotRuntimeDirectory(path: path)
    let bytes = Data("{\"pid\":1,\"pid\":2}".utf8)
    try bytes.write(to: URL(fileURLWithPath: path + "/broker.json"))
    #expect(chmod(path + "/broker.json", 0o600) == 0)
    #expect(throws: AppshotBrokerError.unsafeRuntime) { try runtime.elect() }
    #expect(try Data(contentsOf: URL(fileURLWithPath: path + "/broker.json")) == bytes)
  }
}

extension AppshotRuntimeDirectoryTests {
  @Test(arguments: [
    "socket-mode", "descriptor-mode", "descriptor-link", "descriptor-fifo", "descriptor-hardlink",
  ])
  func rejectsUnsafeOpenedRuntimeEntries(_ scenario: String) throws {
    let path = root()
    defer { try? FileManager.default.removeItem(atPath: path) }
    let runtime = try AppshotRuntimeDirectory(path: path)
    #expect(try runtime.elect())
    _ = try runtime.publishListener()
    switch scenario {
    case "socket-mode": #expect(chmod(path + "/broker.sock", 0o666) == 0)
    case "descriptor-mode": #expect(chmod(path + "/broker.json", 0o644) == 0)
    case "descriptor-hardlink": #expect(link(path + "/broker.json", path + "/extra") == 0)
    case "descriptor-link":
      #expect(unlink(path + "/broker.json") == 0)
      #expect(symlink("/dev/null", path + "/broker.json") == 0)
    default:
      #expect(unlink(path + "/broker.json") == 0)
      #expect(mkfifo(path + "/broker.json", 0o600) == 0)
    }
    #expect(throws: AppshotBrokerError.unsafeRuntime) {
      try AppshotRuntimeDirectory.readDescriptor(at: path)
    }
    runtime.close()
  }
}

extension AppshotRuntimeDirectoryTests {
  @Test func publicationDoesNotOverwriteDescriptorInsertedAfterElection() throws {
    let path = root()
    defer { try? FileManager.default.removeItem(atPath: path) }
    let runtime = try AppshotRuntimeDirectory(path: path)
    #expect(try runtime.elect())
    let replacement = Data("replacement".utf8)
    let descriptor = URL(fileURLWithPath: path + "/broker.json")
    try replacement.write(to: descriptor)
    #expect(throws: AppshotBrokerError.unsafeRuntime) { try runtime.publishListener() }
    #expect(try Data(contentsOf: descriptor) == replacement)
    runtime.close()
  }
}

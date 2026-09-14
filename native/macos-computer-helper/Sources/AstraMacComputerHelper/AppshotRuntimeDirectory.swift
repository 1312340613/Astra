import Darwin
import Foundation
import AstraAppshotCore


public struct AppshotProcessIdentity: Equatable, Codable, Sendable {
  public let pid: Int32
  public let uid: UInt32
  public let processStart: String
  public init(pid: Int32, uid: UInt32, processStart: String) {
    self.pid = pid
    self.uid = uid
    self.processStart = processStart
  }
  enum CodingKeys: String, CodingKey {
    case pid, uid
    case processStart = "process_start"
  }
}

public protocol AppshotProcessProviding {
  func identity(pid: Int32) -> AppshotProcessIdentity?
  func monotonicNS() -> UInt64
}

/// No capture, Accessibility, input monitoring or hotkey side effects.
public struct AppshotSystemProcesses: AppshotProcessProviding {
  public init() {}
  public func identity(pid: Int32) -> AppshotProcessIdentity? {
    guard pid > 0 else { return nil }
    var info = proc_bsdinfo()
    guard
      proc_pidinfo(pid, PROC_PIDTBSDINFO, 0, &info, Int32(MemoryLayout.size(ofValue: info)))
        == MemoryLayout.size(ofValue: info)
    else { return nil }
    let (seconds, overflow) = info.pbi_start_tvsec.multipliedReportingOverflow(by: 1_000_000)
    let (start, overflow2) = seconds.addingReportingOverflow(info.pbi_start_tvusec)
    guard !overflow, !overflow2 else { return nil }
    return .init(pid: pid, uid: info.pbi_uid, processStart: String(start))
  }
  public func monotonicNS() -> UInt64 { clock_gettime_nsec_np(CLOCK_UPTIME_RAW) }

  public func peerIdentity(socket: Int32) throws -> AppshotProcessIdentity {
    var uid: uid_t = 0
    var gid: gid_t = 0
    var pid: Int32 = 0
    var length = socklen_t(MemoryLayout.size(ofValue: pid))
    guard getpeereid(socket, &uid, &gid) == 0,
      getsockopt(socket, SOL_LOCAL, LOCAL_PEERPID, &pid, &length) == 0,
      length == MemoryLayout.size(ofValue: pid), uid == getuid(),
      let identity = identity(pid: pid), identity.uid == uid
    else { throw AppshotBrokerError.unauthorized }
    return identity
  }
}

public struct AppshotRuntimeEntry: Codable, Equatable, Sendable {
  public let name: String
  public let device: String
  public let inode: String
  public let kind: String
}

public struct AppshotRuntimeDescriptor: Codable, Equatable, Sendable {
  public let schemaVersion: Int
  public let instanceID: String
  public let brokerNonce: String
  public let pid: Int32
  public let uid: UInt32
  public let processStart: String
  public let socketName: String
  public let entries: [AppshotRuntimeEntry]
  enum CodingKeys: String, CodingKey {
    case schemaVersion = "schema_version"
    case instanceID = "instance_id"
    case brokerNonce = "broker_nonce"
    case processStart = "process_start"
    case socketName = "socket_name"
    case pid, uid, entries
  }
}

/// All pathname authority is relative to a held, revalidated, no-follow directory.
/// The lock file is deliberately never unlinked: removing it permits split elections.
public final class AppshotRuntimeDirectory: @unchecked Sendable {
  public static var defaultPath: String { "/private/tmp/astra-appshot-\(getuid())" }
  public let path: String
  private let mutex = NSRecursiveLock()
  private var directoryFD: Int32
  private var lockFD: Int32 = -1
  private var lockEntry: AppshotRuntimeEntry?
  private var listenerFD: Int32 = -1
  private var owned = false
  private var entries: [AppshotRuntimeEntry] = []
  private var descriptorEntry: AppshotRuntimeEntry?
  private let directoryInfo: stat
  private let processes: AppshotProcessProviding
  public private(set) var descriptor: AppshotRuntimeDescriptor?

  public convenience init(
    path: String = AppshotRuntimeDirectory.defaultPath,
    processes: AppshotProcessProviding = AppshotSystemProcesses()
  ) throws {
    try self.init(path: path, processes: processes, create: true)
  }
  private init(path: String, processes: AppshotProcessProviding, create: Bool) throws {
    self.path = path
    self.processes = processes
    directoryFD = try Self.openDirectory(path: path, create: create)
    var info = stat()
    guard fstat(directoryFD, &info) == 0 else {
      Darwin.close(directoryFD)
      throw AppshotBrokerError.unsafeRuntime
    }
    directoryInfo = info
  }
  deinit {
    close()
    Darwin.close(directoryFD)
  }

  public func elect() throws -> Bool {
    mutex.lock()
    defer { mutex.unlock() }
    try validateDirectory()
    if owned { return true }
    if lockFD < 0 {
      lockFD = openat(directoryFD, "broker.lock", O_RDWR | O_CREAT | O_NOFOLLOW | O_CLOEXEC, 0o600)
    }
    guard lockFD >= 0 else { throw AppshotBrokerError.unsafeRuntime }
    let lockEntry = try entry(name: "broker.lock", fd: lockFD, kind: "file")
    self.lockEntry = lockEntry
    guard matches(lockEntry) else { throw AppshotBrokerError.unsafeRuntime }
    guard flock(lockFD, LOCK_EX | LOCK_NB) == 0 else {
      if errno == EWOULDBLOCK { return false }
      throw AppshotBrokerError.systemFailure
    }
    do {
      guard matches(lockEntry) else { throw AppshotBrokerError.unsafeRuntime }
      try recoverStaleEntries()
      owned = true
      return true
    } catch {
      flock(lockFD, LOCK_UN)
      throw error
    }
  }

  /// Returns a nonblocking listening Unix socket, owned until close().
  public func publishListener() throws -> Int32 {
    mutex.lock()
    defer { mutex.unlock() }
    try validateDirectory()
    guard owned else { throw AppshotBrokerError.notOwner }
    guard listenerFD < 0 else { return listenerFD }
    guard let identity = processes.identity(pid: getpid()), identity.uid == getuid() else {
      throw AppshotBrokerError.unauthorized
    }
    let socketPath = path + "/broker.sock"
    var address = sockaddr_un()
    guard socketPath.utf8.count < MemoryLayout.size(ofValue: address.sun_path) else {
      throw AppshotBrokerError.unsafeRuntime
    }
    address.sun_family = sa_family_t(AF_UNIX)
    address.sun_len = UInt8(MemoryLayout<sockaddr_un>.size)
    withUnsafeMutableBytes(of: &address.sun_path) { bytes in
      bytes.copyBytes(from: Array(socketPath.utf8) + [0])
    }
    let fd = socket(AF_UNIX, SOCK_STREAM, 0)
    guard fd >= 0 else { throw AppshotBrokerError.systemFailure }
    do {
      try Self.configureSocket(fd)
      let result = withUnsafePointer(to: &address) {
        $0.withMemoryRebound(to: sockaddr.self, capacity: 1) {
          bind(fd, $0, socklen_t(MemoryLayout<sockaddr_un>.size))
        }
      }
      guard result == 0 else { throw AppshotBrokerError.unsafeRuntime }
      // The owner-only directory gates access before the socket mode is tightened.
      guard fchmodat(directoryFD, "broker.sock", 0o600, AT_SYMLINK_NOFOLLOW) == 0 else {
        throw AppshotBrokerError.unsafeRuntime
      }
      let socketEntry = try namedEntry("broker.sock", kind: "socket")
      entries = [socketEntry]
      guard listen(fd, 16) == 0 else { throw AppshotBrokerError.systemFailure }
      try validateDirectory()
      let descriptor = AppshotRuntimeDescriptor(
        schemaVersion: 1, instanceID: UUID().uuidString, brokerNonce: UUID().uuidString,
        pid: identity.pid, uid: identity.uid, processStart: identity.processStart,
        socketName: "broker.sock", entries: entries)
      try publish(descriptor)
      self.descriptor = descriptor
      listenerFD = fd
      return fd
    } catch {
      Darwin.close(fd)
      for entry in entries { removeExact(entry) }
      entries.removeAll()
      throw error
    }
  }

  /// Borrow the held descriptor only while runtime ownership cannot be closed or
  /// transferred. Keep capture, AX and source revalidation outside this short lease.
  func withArtifactPublication<T>(_ body: (Int32) throws -> T) throws -> T {
    mutex.lock()
    defer { mutex.unlock() }
    try verifyAuthority()
    return try body(directoryFD)
  }

  /// Frozen descriptor evidence is compared in the same critical section that builds
  /// the journal. A successful return includes explicit directory durability. Failure
  /// after journal rename leaves coherent committed/uncertain state for exact cleanup.
  func trackArtifacts(
    authorities: [ArtifactPublishedAuthority],
    sync: (Int32) -> Int32 = Darwin.fsync
  ) throws {
    mutex.lock()
    defer { mutex.unlock() }
    try verifyAuthority()
    guard authorities.count == 3, Set(authorities.map(\.name)).count == 3 else {
      throw AppshotBrokerError.unsafeRuntime
    }
    try trackArtifacts(names: authorities.map(\.name), expected: authorities)
    guard sync(directoryFD) == 0 else { throw AppshotBrokerError.systemFailure }
    try verifyAuthority()
    for authority in authorities {
      var info = stat()
      guard fstatat(directoryFD, authority.name, &info, AT_SYMLINK_NOFOLLOW) == 0,
        authority.matches(info)
      else { throw AppshotBrokerError.unsafeRuntime }
    }
  }

  /// Journal exact publisher-created flat bundle files before offering them. Unknown names
  /// and replaced inodes are never reaped. Task 5 should call this after staged publication.
  public func trackArtifacts(names: [String]) throws {
    try trackArtifacts(names: names, expected: nil)
  }

  private func trackArtifacts(names: [String], expected: [ArtifactPublishedAuthority]?) throws {
    mutex.lock()
    defer { mutex.unlock() }
    try validateDirectory()
    guard owned, let previous = descriptor else { throw AppshotBrokerError.notOwner }
    let additions = try names.map { name -> AppshotRuntimeEntry in
      guard Self.artifactName(name) else { throw AppshotBrokerError.unsafeRuntime }
      if let expected {
        guard let authority = expected.first(where: { $0.name == name }) else {
          throw AppshotBrokerError.unsafeRuntime
        }
        var info = stat()
        guard fstatat(directoryFD, name, &info, AT_SYMLINK_NOFOLLOW) == 0,
          authority.matches(info)
        else { throw AppshotBrokerError.unsafeRuntime }
        return try validatedEntry(name: name, info: info, kind: "file")
      }
      return try namedEntry(name, kind: "file")
    }
    var updated = entries
    for addition in additions {
      guard !updated.contains(where: { $0.name == addition.name }) else {
        throw AppshotBrokerError.unsafeRuntime
      }
      updated.append(addition)
    }
    guard updated.count <= 193 else { throw AppshotBrokerError.quotaExceeded }
    let next = AppshotRuntimeDescriptor(
      schemaVersion: 1, instanceID: previous.instanceID, brokerNonce: previous.brokerNonce,
      pid: previous.pid, uid: previous.uid, processStart: previous.processStart,
      socketName: previous.socketName, entries: updated)
    try publish(next)
    entries = updated
    descriptor = next
  }

  /// Cleanup cannot release a later bundle that happens to reuse the same names.
  func releaseArtifacts(authorities: [ArtifactPublishedAuthority]) throws {
    mutex.lock()
    defer { mutex.unlock() }
    let names = authorities.filter { authority in
      entries.contains {
        $0.name == authority.name && $0.device == authority.device
          && $0.inode == authority.inode && $0.kind == "file"
      }
    }.map(\.name)
    guard !names.isEmpty else { return }
    try releaseArtifacts(names: names)
  }

  /// Idempotent exact-inode deletion; no client-supplied path enters this API.
  public func releaseArtifacts(names: [String]) throws {
    mutex.lock()
    defer { mutex.unlock() }
    try validateDirectory()
    guard owned, let previous = descriptor else { throw AppshotBrokerError.notOwner }
    let selected = entries.filter { names.contains($0.name) && $0.kind == "file" }
    for item in selected { removeExact(item) }
    let updated = entries.filter { !selected.contains($0) }
    let next = AppshotRuntimeDescriptor(
      schemaVersion: 1, instanceID: previous.instanceID, brokerNonce: previous.brokerNonce,
      pid: previous.pid, uid: previous.uid, processStart: previous.processStart,
      socketName: previous.socketName, entries: updated)
    try publish(next)
    entries = updated
    descriptor = next
  }

  /// Broker readiness sources take fd lifetime ownership; cancel handlers close it only
  /// after Dispatch has stopped observing it. Runtime still owns the named entries.
  func detachListener() -> Int32 {
    mutex.lock()
    defer { mutex.unlock() }
    let fd = listenerFD
    listenerFD = -1
    return fd
  }

  public func verifyAuthority() throws {
    mutex.lock()
    defer { mutex.unlock() }
    try validateDirectory()
    guard owned, let descriptorEntry, matches(descriptorEntry),
      let socketEntry = entries.first(where: { $0.kind == "socket" }), matches(socketEntry)
    else { throw AppshotBrokerError.unsafeRuntime }
  }

  public func close() {
    mutex.lock()
    defer { mutex.unlock() }
    if listenerFD >= 0 {
      Darwin.close(listenerFD)
      listenerFD = -1
    }
    if owned {
      if (try? validateDirectory()) != nil {
        for entry in entries { removeExact(entry) }
        if let descriptorEntry { removeExact(descriptorEntry) }
      }
      entries.removeAll()
      descriptor = nil
      descriptorEntry = nil
      owned = false
      flock(lockFD, LOCK_UN)
    }
    if lockFD >= 0 {
      Darwin.close(lockFD)
      lockFD = -1
      lockEntry = nil
    }
  }

  public static func readDescriptor(at path: String) throws -> AppshotRuntimeDescriptor {
    let runtime = try AppshotRuntimeDirectory(
      path: path, processes: AppshotSystemProcesses(), create: false)
    let descriptor = try runtime.readDescriptor().0
    guard let socket = descriptor.entries.first(where: { $0.kind == "socket" }),
      runtime.matches(socket)
    else { throw AppshotBrokerError.unsafeRuntime }
    try runtime.validateDirectory()
    return descriptor
  }

  static func configureSocket(_ fd: Int32) throws {
    var one: Int32 = 1
    guard fcntl(fd, F_SETFD, FD_CLOEXEC) == 0,
      fcntl(fd, F_SETFL, O_NONBLOCK) == 0,
      setsockopt(fd, SOL_SOCKET, SO_NOSIGPIPE, &one, socklen_t(MemoryLayout.size(ofValue: one)))
        == 0
    else { throw AppshotBrokerError.systemFailure }
  }

  private func validateDirectory() throws {
    if let lockEntry, !matches(lockEntry) { throw AppshotBrokerError.unsafeRuntime }
    let fresh = try Self.openDirectory(path: path, create: false)
    defer { Darwin.close(fresh) }
    var info = stat()
    guard fstat(fresh, &info) == 0, info.st_dev == directoryInfo.st_dev,
      info.st_ino == directoryInfo.st_ino
    else { throw AppshotBrokerError.unsafeRuntime }
  }
  private static func openDirectory(path: String, create: Bool) throws -> Int32 {
    guard path.hasPrefix("/"), !path.contains("\0"), !path.hasSuffix("/"),
      !path.split(separator: "/").contains(where: { $0 == "." || $0 == ".." })
    else { throw AppshotBrokerError.unsafeRuntime }
    let parts = path.split(separator: "/").map(String.init)
    guard !parts.isEmpty else { throw AppshotBrokerError.unsafeRuntime }
    var fd = open("/", O_RDONLY | O_DIRECTORY | O_CLOEXEC)
    for (index, part) in parts.enumerated() {
      let final = index == parts.count - 1
      if final, create, mkdirat(fd, part, 0o700) != 0, errno != EEXIST {
        Darwin.close(fd)
        throw AppshotBrokerError.unsafeRuntime
      }
      let next = openat(fd, part, O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC)
      Darwin.close(fd)
      fd = next
      guard fd >= 0 else { throw AppshotBrokerError.unsafeRuntime }
      if final {
        var info = stat()
        guard fstat(fd, &info) == 0, info.st_uid == getuid(), info.st_mode & 0o7777 == 0o700 else {
          Darwin.close(fd)
          throw AppshotBrokerError.unsafeRuntime
        }
      }
    }
    return fd
  }
  private func entry(name: String, fd: Int32, kind: String) throws -> AppshotRuntimeEntry {
    var info = stat()
    guard fstat(fd, &info) == 0 else { throw AppshotBrokerError.unsafeRuntime }
    return try validatedEntry(name: name, info: info, kind: kind)
  }
  private func namedEntry(_ name: String, kind: String) throws -> AppshotRuntimeEntry {
    var info = stat()
    guard fstatat(directoryFD, name, &info, AT_SYMLINK_NOFOLLOW) == 0 else {
      throw AppshotBrokerError.unsafeRuntime
    }
    return try validatedEntry(name: name, info: info, kind: kind)
  }
  private func validatedEntry(name: String, info: stat, kind: String) throws -> AppshotRuntimeEntry
  {
    guard info.st_uid == getuid(), info.st_nlink == 1, info.st_mode & 0o7777 == 0o600,
      info.st_mode & S_IFMT == (kind == "socket" ? S_IFSOCK : S_IFREG)
    else { throw AppshotBrokerError.unsafeRuntime }
    return .init(
      name: name, device: String(UInt32(bitPattern: info.st_dev)), inode: String(info.st_ino),
      kind: kind)
  }
  private func matches(_ entry: AppshotRuntimeEntry) -> Bool {
    (try? namedEntry(entry.name, kind: entry.kind)) == entry
  }
  private func removeExact(_ entry: AppshotRuntimeEntry) {
    if matches(entry) { _ = unlinkat(directoryFD, entry.name, 0) }
  }
  private static func artifactName(_ name: String) -> Bool {
    name.range(
      of: "^appshot-[0-9a-f]{32}\\.(png|ax\\.json|manifest\\.json)$", options: .regularExpression)
      != nil
  }
  private static func encoded(_ descriptor: AppshotRuntimeDescriptor) throws -> Data {
    let encoder = JSONEncoder()
    encoder.outputFormatting = [.sortedKeys, .withoutEscapingSlashes]
    return try encoder.encode(descriptor)
  }
  private func publish(_ descriptor: AppshotRuntimeDescriptor) throws {
    let data = try Self.encoded(descriptor)
    guard data.count <= 65536 else { throw AppshotBrokerError.quotaExceeded }
    if let descriptorEntry, !matches(descriptorEntry) { throw AppshotBrokerError.unsafeRuntime }
    let name = ".descriptor-\(UUID().uuidString)"
    let fd = openat(directoryFD, name, O_WRONLY | O_CREAT | O_EXCL | O_NOFOLLOW | O_CLOEXEC, 0o600)
    guard fd >= 0 else { throw AppshotBrokerError.systemFailure }
    defer {
      Darwin.close(fd)
      _ = unlinkat(directoryFD, name, 0)
    }
    try data.withUnsafeBytes { bytes in
      var offset = 0
      while offset < bytes.count {
        let count = write(fd, bytes.baseAddress!.advanced(by: offset), bytes.count - offset)
        if count < 0, errno == EINTR { continue }
        guard count > 0 else { throw AppshotBrokerError.systemFailure }
        offset += count
      }
    }
    guard fsync(fd) == 0 else { throw AppshotBrokerError.systemFailure }
    let published = try entry(name: "broker.json", fd: fd, kind: "file")
    try validateDirectory()
    let result: Int32
    if let descriptorEntry {
      guard matches(descriptorEntry) else { throw AppshotBrokerError.unsafeRuntime }
      result = renameat(directoryFD, name, directoryFD, "broker.json")
    } else {
      // First publication must not overwrite an entry inserted since election.
      result = renameatx_np(directoryFD, name, directoryFD, "broker.json", UInt32(RENAME_EXCL))
    }
    guard result == 0 else {
      throw errno == EEXIST ? AppshotBrokerError.unsafeRuntime : AppshotBrokerError.systemFailure
    }
    descriptorEntry = published
    _ = fsync(directoryFD)
  }
  private func readDescriptor() throws -> (AppshotRuntimeDescriptor, AppshotRuntimeEntry) {
    let fd = openat(directoryFD, "broker.json", O_RDONLY | O_NOFOLLOW | O_CLOEXEC | O_NONBLOCK)
    guard fd >= 0 else { throw AppshotBrokerError.unsafeRuntime }
    defer { Darwin.close(fd) }
    let authority = try entry(name: "broker.json", fd: fd, kind: "file")
    guard matches(authority) else { throw AppshotBrokerError.unsafeRuntime }
    var data = Data()
    var buffer = [UInt8](repeating: 0, count: 4096)
    while true {
      let count = read(fd, &buffer, buffer.count)
      if count < 0, errno == EINTR { continue }
      guard count >= 0, data.count + count <= 65536 else { throw AppshotBrokerError.unsafeRuntime }
      if count == 0 { break }
      data.append(contentsOf: buffer.prefix(count))
    }
    let descriptor: AppshotRuntimeDescriptor
    do { descriptor = try JSONDecoder().decode(AppshotRuntimeDescriptor.self, from: data) } catch {
      throw AppshotBrokerError.unsafeRuntime
    }
    // Only this canonical format is published. Byte equality also rejects duplicate/unknown keys.
    guard try Self.encoded(descriptor) == data, descriptor.schemaVersion == 1,
      descriptor.uid == getuid(), descriptor.pid > 0,
      UInt64(descriptor.processStart).map(String.init) == descriptor.processStart,
      UUID(uuidString: descriptor.instanceID) != nil,
      UUID(uuidString: descriptor.brokerNonce) != nil,
      descriptor.socketName == "broker.sock", descriptor.entries.count <= 193,
      Set(descriptor.entries.map(\.name)).count == descriptor.entries.count,
      descriptor.entries.filter({ $0.name == "broker.sock" && $0.kind == "socket" }).count == 1,
      descriptor.entries.allSatisfy({
        ($0.name == "broker.sock" && $0.kind == "socket")
          || (Self.artifactName($0.name) && $0.kind == "file")
      }),
      descriptor.entries.allSatisfy({
        UInt64($0.device).map(String.init) == $0.device
          && UInt64($0.inode).map(String.init) == $0.inode
      })
    else { throw AppshotBrokerError.unsafeRuntime }
    return (descriptor, authority)
  }
  private func recoverStaleEntries() throws {
    var info = stat()
    if fstatat(directoryFD, "broker.json", &info, AT_SYMLINK_NOFOLLOW) != 0 {
      guard errno == ENOENT else { throw AppshotBrokerError.unsafeRuntime }
      // Unjournaled socket: quarantine in place; no authority to unlink it.
      if fstatat(directoryFD, "broker.sock", &info, AT_SYMLINK_NOFOLLOW) == 0 || errno != ENOENT {
        throw AppshotBrokerError.unsafeRuntime
      }
      return
    }
    let (old, authority) = try readDescriptor()
    if let live = processes.identity(pid: old.pid) {
      guard live.uid == old.uid, live.processStart != old.processStart else {
        throw AppshotBrokerError.unsafeRuntime
      }
    } else {
      guard kill(old.pid, 0) == -1, errno == ESRCH else { throw AppshotBrokerError.unsafeRuntime }
    }
    // Verify every extant entry before any deletion. Missing is an already released file.
    for entry in old.entries {
      if fstatat(directoryFD, entry.name, &info, AT_SYMLINK_NOFOLLOW) != 0, errno == ENOENT {
        continue
      }
      guard matches(entry) else { throw AppshotBrokerError.unsafeRuntime }
    }
    for entry in old.entries { removeExact(entry) }
    removeExact(authority)
  }
}

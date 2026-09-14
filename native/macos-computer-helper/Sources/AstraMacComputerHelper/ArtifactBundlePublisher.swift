import Darwin
import Foundation

protocol ArtifactSyscalls {
    func openFile(at directoryFD: Int32, name: String, flags: Int32, mode: mode_t) -> Int32
    func directoryEntryNames(
        _ directoryFD: Int32,
        maximumEntries: Int,
        maximumNameBytes: Int
    ) throws -> Set<String>
    func status(_ fileDescriptor: Int32, into value: UnsafeMutablePointer<stat>) -> Int32
    func setMode(_ fileDescriptor: Int32, mode: mode_t) -> Int32
    func writeFile(_ fileDescriptor: Int32, buffer: UnsafeRawPointer, count: Int) -> Int
    func sync(_ fileDescriptor: Int32) -> Int32
    func closeFile(_ fileDescriptor: Int32) -> Int32
    func renameExclusive(at directoryFD: Int32, from temporary: String, to final: String) -> Int32
    func status(at directoryFD: Int32, name: String, into value: UnsafeMutablePointer<stat>) -> Int32
    func unlink(at directoryFD: Int32, name: String) -> Int32
}

extension ArtifactSyscalls {
    func directoryEntryNames(
        _ directoryFD: Int32,
        maximumEntries: Int,
        maximumNameBytes: Int
    ) throws -> Set<String> {
        try defaultArtifactDirectoryEntryNames(
            directoryFD,
            maximumEntries: maximumEntries,
            maximumNameBytes: maximumNameBytes
        )
    }

    func status(_ fileDescriptor: Int32, into value: UnsafeMutablePointer<stat>) -> Int32 {
        Darwin.fstat(fileDescriptor, value)
    }

    func setMode(_ fileDescriptor: Int32, mode: mode_t) -> Int32 {
        Darwin.fchmod(fileDescriptor, mode)
    }

    func closeFile(_ fileDescriptor: Int32) -> Int32 { Darwin.close(fileDescriptor) }

    func status(at directoryFD: Int32, name: String, into value: UnsafeMutablePointer<stat>) -> Int32 {
        name.withCString { Darwin.fstatat(directoryFD, $0, value, AT_SYMLINK_NOFOLLOW) }
    }
}

struct DarwinArtifactSyscalls: ArtifactSyscalls {
    func openFile(at directoryFD: Int32, name: String, flags: Int32, mode: mode_t) -> Int32 {
        name.withCString { Darwin.openat(directoryFD, $0, flags, mode) }
    }

    func writeFile(_ fileDescriptor: Int32, buffer: UnsafeRawPointer, count: Int) -> Int {
        Darwin.write(fileDescriptor, buffer, count)
    }

    func status(_ fileDescriptor: Int32, into value: UnsafeMutablePointer<stat>) -> Int32 {
        Darwin.fstat(fileDescriptor, value)
    }

    func setMode(_ fileDescriptor: Int32, mode: mode_t) -> Int32 {
        Darwin.fchmod(fileDescriptor, mode)
    }

    func sync(_ fileDescriptor: Int32) -> Int32 { Darwin.fsync(fileDescriptor) }

    func closeFile(_ fileDescriptor: Int32) -> Int32 { Darwin.close(fileDescriptor) }

    func renameExclusive(at directoryFD: Int32, from temporary: String, to final: String) -> Int32 {
        temporary.withCString { source in
            final.withCString { destination in
                Darwin.renameatx_np(directoryFD, source, directoryFD, destination, UInt32(RENAME_EXCL))
            }
        }
    }

    func status(at directoryFD: Int32, name: String, into value: UnsafeMutablePointer<stat>) -> Int32 {
        name.withCString { Darwin.fstatat(directoryFD, $0, value, AT_SYMLINK_NOFOLLOW) }
    }

    func unlink(at directoryFD: Int32, name: String) -> Int32 {
        name.withCString { Darwin.unlinkat(directoryFD, $0, 0) }
    }
}

func defaultArtifactDirectoryEntryNames(
    _ directoryFD: Int32,
    maximumEntries: Int = 128,
    maximumNameBytes: Int = 32 * 1024
) throws -> Set<String> {
    guard maximumEntries > 0, maximumNameBytes > 0 else {
        throw ArtifactBundleError.unsafeDirectory
    }
    let independent = Darwin.openat(directoryFD, ".", O_RDONLY | O_DIRECTORY | O_CLOEXEC)
    guard independent >= 0 else { throw ArtifactBundleError.unsafeDirectory }
    guard let directory = fdopendir(independent) else {
        _ = Darwin.close(independent)
        throw ArtifactBundleError.unsafeDirectory
    }
    defer { closedir(directory) }
    rewinddir(directory)
    var names: Set<String> = []
    var nameBytes = 0
    while true {
        errno = 0
        guard let entry = readdir(directory) else {
            guard errno == 0 else { throw ArtifactBundleError.unsafeDirectory }
            break
        }
        let name = withUnsafePointer(to: &entry.pointee.d_name) { pointer in
            pointer.withMemoryRebound(to: CChar.self, capacity: Int(MAXNAMLEN) + 1) {
                String(cString: $0)
            }
        }
        if name != ".", name != ".." {
            let bytes = name.utf8.count
            guard names.count < maximumEntries,
                  bytes <= maximumNameBytes - nameBytes
            else { throw ArtifactBundleError.unsafeDirectory }
            names.insert(name)
            nameBytes += bytes
        }
    }
    return names
}

enum ArtifactBundleError: Error, Equatable {
    case invalidNames
    case invalidData
    case quotaExceeded
    case unsafeDirectory
    case poisoned
    case closed
}

private struct ArtifactFileIdentity: Equatable {
    let device: dev_t
    let inode: ino_t
}

fileprivate struct ArtifactFileAuthority: Equatable {
    let device: dev_t
    let inode: ino_t
    let owner: uid_t
    let mode: mode_t
    let linkCount: nlink_t
}

private enum AmbiguousArtifactDescriptorAuthority {
    case exact(ArtifactFileAuthority)
    case unknown
}

private enum RetainedQuarantineClassification {
    case verified(ArtifactFileAuthority)
    case identityMismatch
    case statusUnavailable
}

private struct PreparedArtifactFile {
    let temporaryName: String
    let finalName: String
    let identity: ArtifactFileAuthority
    var descriptor: Int32
    var published = false
}

struct ArtifactBundleMember {
    let finalName: String
    let data: Data
}

/// Immutable evidence from a verified prepared descriptor, then checked at its final name.
struct ArtifactPublishedAuthority: Equatable {
    let name: String
    let size: Int
    let device: String
    let inode: String
    let owner: Int
    let mode: Int
    let linkCount: Int
    fileprivate let identity: ArtifactFileAuthority
    fileprivate init(name: String, size: Int, identity: ArtifactFileAuthority) {
        self.name = name; self.size = size; self.identity = identity
        device = String(UInt64(UInt32(bitPattern: identity.device)))
        inode = String(identity.inode); owner = Int(identity.owner)
        mode = Int(identity.mode & 0o7777); linkCount = Int(identity.linkCount)
    }
    func matches(_ info: stat) -> Bool {
        info.st_dev == identity.device && info.st_ino == identity.inode && info.st_uid == identity.owner
            && info.st_mode == identity.mode && info.st_nlink == identity.linkCount && info.st_size == size
    }
}

final class ArtifactBundlePublisher {
    private let directoryFD: Int32
    private let syscalls: any ArtifactSyscalls
    private let maximumDetailArtifacts: Int
    private let maximumDetailBytes: Int
    private let maximumDirectoryEntries: Int
    private let maximumDirectoryNameBytes: Int
    private let lock = NSLock()
    private var retainedQuarantines: [String: RetainedQuarantineClassification] = [:]
    private var ambiguousFileDescriptors: [Int32: AmbiguousArtifactDescriptorAuthority] = [:]
    private var poisoned = false
    private var closed = false

    init(
        directoryFD: Int32,
        syscalls: any ArtifactSyscalls = DarwinArtifactSyscalls(),
        maximumDetailArtifacts: Int = 8,
        maximumDetailBytes: Int = 64 * 1024 * 1024,
        maximumDirectoryEntries: Int = 128,
        maximumDirectoryNameBytes: Int = 32 * 1024
    ) {
        self.directoryFD = directoryFD
        self.syscalls = syscalls
        self.maximumDetailArtifacts = maximumDetailArtifacts
        self.maximumDetailBytes = maximumDetailBytes
        self.maximumDirectoryEntries = maximumDirectoryEntries
        self.maximumDirectoryNameBytes = maximumDirectoryNameBytes
    }

    func publish(
        image: Data,
        imageName: String,
        detail: Data,
        detailName: String
    ) throws -> ArtifactPublication {
        lock.lock()
        defer { lock.unlock() }
        guard !closed else { throw ArtifactBundleError.closed }
        guard !poisoned else { throw ArtifactBundleError.poisoned }
        guard validSmartSnapshotBundleNames(image: imageName, detail: detailName) else {
            throw ArtifactBundleError.invalidNames
        }
        guard !image.isEmpty,
              !detail.isEmpty,
              detail.count <= maximumAXTextDetailJSONBytes,
              maximumDetailArtifacts > 0,
              maximumDetailBytes > 0
        else { throw ArtifactBundleError.invalidData }
        let directoryIdentity = try validatedDirectoryIdentity()
        let quota = try scanQuota(directoryIdentity: directoryIdentity)
        guard quota.count < maximumDetailArtifacts,
              quota.bytes <= maximumDetailBytes,
              detail.count <= maximumDetailBytes - quota.bytes
        else { throw ArtifactBundleError.quotaExceeded }

        var files: [PreparedArtifactFile] = []
        var mutationNeedsSync = false
        do {
            files.append(try prepareFile(finalName: imageName, directoryIdentity: directoryIdentity))
            files.append(try prepareFile(finalName: detailName, directoryIdentity: directoryIdentity))
            for index in files.indices {
                let data = index == 0 ? image : detail
                try write(data, to: files[index].descriptor)
                guard syscalls.sync(files[index].descriptor) == 0 else {
                    throw ArtifactPublicationError(
                        stage: .fileSync,
                        errnoCode: errno,
                        finalVisible: false,
                        bytesComplete: true
                    )
                }
            }
            for index in files.indices {
                let descriptor = files[index].descriptor
                guard closeOrRetainAmbiguous(descriptor, identity: files[index].identity) else {
                    files[index].descriptor = -1
                    poisoned = true
                    throw ArtifactPublicationError(
                        stage: .close,
                        errnoCode: errno,
                        finalVisible: false,
                        bytesComplete: true,
                        publisherPoisoned: true
                    )
                }
                files[index].descriptor = -1
            }
            guard try validatedDirectoryIdentity() == directoryIdentity else {
                poisoned = true
                throw ArtifactBundleError.unsafeDirectory
            }
            for index in files.indices {
                guard syscalls.renameExclusive(
                    at: directoryFD,
                    from: files[index].temporaryName,
                    to: files[index].finalName
                ) == 0 else {
                    if index == 1 { poisoned = true }
                    throw ArtifactPublicationError(
                        stage: .publish,
                        errnoCode: errno,
                        finalVisible: index > 0,
                        bytesComplete: true
                    )
                }
                files[index].published = true
                mutationNeedsSync = true
                guard artifactIdentity(
                    named: files[index].finalName,
                    directoryIdentity: directoryIdentity
                ) == files[index].identity else {
                    poisoned = true
                    throw ArtifactPublicationError(
                        stage: .publish,
                        errnoCode: EIO,
                        finalVisible: true,
                        bytesComplete: true
                    )
                }
            }
            guard syscalls.sync(directoryFD) == 0 else {
                poisoned = true
                throw ArtifactPublicationError(
                    stage: .directorySync,
                    errnoCode: errno,
                    finalVisible: true,
                    bytesComplete: true
                )
            }
            return .durable
        } catch {
            let rollbackOutcome = rollback(
                &files,
                directoryIdentity: directoryIdentity,
                mutationNeedsSync: mutationNeedsSync
            )
            if !rollbackOutcome.fullyRecoverable { poisoned = true }
            if let publication = error as? ArtifactPublicationError {
                throw ArtifactPublicationError(
                    stage: publication.stage,
                    errnoCode: publication.errnoCode,
                    finalVisible: publication.finalVisible && !rollbackOutcome.originalNamesCleared,
                    bytesComplete: publication.bytesComplete,
                    publisherPoisoned: publication.publisherPoisoned || poisoned || !rollbackOutcome.fullyRecoverable
                )
            }
            throw error
        }
    }

    /// Appshot-only staged triplets. All three descriptors are fsynced/closed before
    /// any final rename. The manifest factory receives descriptor authorities, and
    /// journal enrollment runs only after verified manifest-last directory durability.
    func publish(
        _ members: [ArtifactBundleMember],
        check: () throws -> Void,
        manifest: ([ArtifactPublishedAuthority]) throws -> ArtifactBundleMember,
        enroll: ([ArtifactPublishedAuthority]) throws -> Void = { _ in }
    ) throws -> [ArtifactPublishedAuthority] {
        lock.lock(); defer { lock.unlock() }
        guard !closed else { throw ArtifactBundleError.closed }
        guard !poisoned else { throw ArtifactBundleError.poisoned }
        guard members.count == 2, let token = appshotArtifactToken(members[0].finalName),
            members.map(\.finalName) == ["appshot-\(token).png", "appshot-\(token).ax.json"]
        else { throw ArtifactBundleError.invalidNames }
        guard !members[0].data.isEmpty, members[0].data.count <= 10 * 1024 * 1024,
            !members[1].data.isEmpty, members[1].data.count <= 256 * 1024
        else { throw ArtifactBundleError.invalidData }
        try check()
        let directoryIdentity = try validatedDirectoryIdentity()
        let quota = try scanAppshotQuota(directoryIdentity: directoryIdentity)
        let dataBytes = members.reduce(0) { $0 + $1.data.count }
        guard quota.count < 64, dataBytes + 65536 <= 256 * 1024 * 1024 - quota.bytes
        else { throw ArtifactBundleError.quotaExceeded }
        var files: [PreparedArtifactFile] = []
        var authorities: [ArtifactPublishedAuthority] = []
        var mutated = false
        do {
            func boundary() throws {
                try check()
                guard try validatedDirectoryIdentity() == directoryIdentity else {
                    throw ArtifactBundleError.unsafeDirectory
                }
            }
            for member in members {
                try boundary()
                files.append(try prepareFile(finalName: member.finalName, directoryIdentity: directoryIdentity))
                let file = files[files.count - 1]
                try write(member.data, to: file.descriptor, check: boundary)
                try boundary()
                guard syscalls.sync(file.descriptor) == 0 else {
                    throw ArtifactPublicationError(stage: .fileSync, errnoCode: errno, finalVisible: false, bytesComplete: true)
                }
                try boundary()
                var info = stat()
                guard syscalls.status(file.descriptor, into: &info) == 0,
                    artifactAuthority(info) == file.identity, info.st_size == member.data.count
                else { throw ArtifactBundleError.unsafeDirectory }
                authorities.append(.init(name: member.finalName, size: member.data.count, identity: file.identity))
            }
            try boundary()
            let last = try manifest(authorities)
            guard last.finalName == "appshot-\(token).manifest.json" else { throw ArtifactBundleError.invalidNames }
            guard !last.data.isEmpty, last.data.count <= 65536 else { throw ArtifactBundleError.invalidData }
            try boundary()
            files.append(try prepareFile(finalName: last.finalName, directoryIdentity: directoryIdentity))
            let lastFile = files[2]
            try write(last.data, to: lastFile.descriptor, check: boundary)
            try boundary()
            guard syscalls.sync(lastFile.descriptor) == 0 else {
                throw ArtifactPublicationError(stage: .fileSync, errnoCode: errno, finalVisible: false, bytesComplete: true)
            }
            try boundary()
            var lastInfo = stat()
            guard syscalls.status(lastFile.descriptor, into: &lastInfo) == 0,
                artifactAuthority(lastInfo) == lastFile.identity, lastInfo.st_size == last.data.count
            else { throw ArtifactBundleError.unsafeDirectory }
            authorities.append(.init(name: last.finalName, size: last.data.count, identity: lastFile.identity))
            for index in files.indices {
                try boundary()
                let descriptor = files[index].descriptor
                files[index].descriptor = -1
                guard closeOrRetainAmbiguous(descriptor, identity: files[index].identity) else {
                    poisoned = true
                    throw ArtifactPublicationError(stage: .close, errnoCode: errno, finalVisible: false, bytesComplete: true, publisherPoisoned: true)
                }
            }
            for index in files.indices {
                try boundary()
                // Refuse a substituted staging name before granting it final visibility.
                guard artifactIdentity(named: files[index].temporaryName, directoryIdentity: directoryIdentity) == files[index].identity else {
                    throw ArtifactBundleError.unsafeDirectory
                }
                guard syscalls.renameExclusive(at: directoryFD, from: files[index].temporaryName, to: files[index].finalName) == 0 else {
                    throw ArtifactPublicationError(stage: .publish, errnoCode: errno, finalVisible: index > 0, bytesComplete: true)
                }
                files[index].published = true; mutated = true
                var info = stat()
                guard syscalls.status(at: directoryFD, name: files[index].finalName, into: &info) == 0,
                    authorities[index].matches(info) else { throw ArtifactBundleError.unsafeDirectory }
                try boundary()
            }
            guard syscalls.sync(directoryFD) == 0 else {
                throw ArtifactPublicationError(stage: .directorySync, errnoCode: errno, finalVisible: true, bytesComplete: true)
            }
            try boundary()
            // Recheck the full triplet, not only the most recently renamed manifest.
            for authority in authorities {
                var info = stat()
                guard syscalls.status(at: directoryFD, name: authority.name, into: &info) == 0,
                    authority.matches(info) else { throw ArtifactBundleError.unsafeDirectory }
            }
            try enroll(authorities)
            try boundary()
            return authorities
        } catch {
            let outcome = rollback(&files, directoryIdentity: directoryIdentity, mutationNeedsSync: mutated)
            if !outcome.fullyRecoverable { poisoned = true }
            if let publication = error as? ArtifactPublicationError {
                throw ArtifactPublicationError(stage: publication.stage, errnoCode: publication.errnoCode,
                    finalVisible: publication.finalVisible && !outcome.originalNamesCleared,
                    bytesComplete: publication.bytesComplete, publisherPoisoned: poisoned || publication.publisherPoisoned)
            }
            throw error
        }
    }

    private func scanAppshotQuota(directoryIdentity: ArtifactFileIdentity) throws -> (count: Int, bytes: Int) {
        guard retainedQuarantines.isEmpty else { throw ArtifactBundleError.poisoned }
        let names = try syscalls.directoryEntryNames(directoryFD, maximumEntries: 256, maximumNameBytes: 32768)
        guard names.allSatisfy({ ["broker.lock", "broker.sock", "broker.json"].contains($0) || appshotArtifactToken($0) != nil }) else {
            poisoned = true; throw ArtifactBundleError.poisoned
        }
        let artifacts = names.filter { appshotArtifactToken($0) != nil }
        let tokens = Set(artifacts.compactMap(appshotArtifactToken))
        var bytes = 0
        for token in tokens {
            for suffix in [".png", ".ax.json", ".manifest.json"] {
                let name = "appshot-\(token)\(suffix)"
                var info = stat()
                let limit = suffix == ".png" ? 10 * 1024 * 1024 : (suffix == ".ax.json" ? 256 * 1024 : 65536)
                guard names.contains(name), syscalls.status(at: directoryFD, name: name, into: &info) == 0,
                    safeArtifactStatus(info, directoryIdentity: directoryIdentity), info.st_size > 0,
                    info.st_size <= limit, info.st_size <= Int.max - bytes else {
                    poisoned = true; throw ArtifactBundleError.poisoned
                }
                bytes += Int(info.st_size)
            }
        }
        return (tokens.count, bytes)
    }

    func close() {
        lock.lock()
        defer { lock.unlock() }
        guard !closed else { return }
        closed = true
        recoverAmbiguousFileDescriptors()
        if !retainedQuarantines.isEmpty { poisoned = true }
    }

    private func validatedDirectoryIdentity() throws -> ArtifactFileIdentity {
        var value = stat()
        guard fstat(directoryFD, &value) == 0,
              (value.st_mode & S_IFMT) == S_IFDIR,
              (value.st_mode & mode_t(0o7777)) == mode_t(0o700),
              value.st_uid == getuid()
        else {
            poisoned = true
            throw ArtifactBundleError.unsafeDirectory
        }
        return ArtifactFileIdentity(device: value.st_dev, inode: value.st_ino)
    }

    private func prepareFile(
        finalName: String,
        directoryIdentity: ArtifactFileIdentity
    ) throws -> PreparedArtifactFile {
        let temporaryName = ".\(finalName).\(UUID().uuidString.lowercased()).tmp"
        let flags = O_WRONLY | O_CREAT | O_EXCL | O_NOFOLLOW | O_CLOEXEC
        let descriptor = syscalls.openFile(
            at: directoryFD,
            name: temporaryName,
            flags: flags,
            mode: mode_t(0o600)
        )
        guard descriptor >= 0 else {
            throw ArtifactPublicationError(
                stage: .open,
                errnoCode: errno,
                finalVisible: false,
                bytesComplete: false
            )
        }
        var value = stat()
        guard syscalls.status(descriptor, into: &value) == 0 else {
            let savedErrno = errno
            var recovered = stat()
            let recoveredStatus = syscalls.status(descriptor, into: &recovered) == 0
            let identity = recoveredStatus ? artifactAuthority(recovered) : nil
            let closeClean = closeOrRetainAmbiguous(descriptor, identity: identity)
            let outcome = quarantineForRetention(
                name: temporaryName,
                expectedIdentity: identity,
                directoryIdentity: directoryIdentity
            )
            let syncClean = !outcome.mutated || syscalls.sync(directoryFD) == 0
            if !closeClean || !outcome.fullyRecoverable || !syncClean { poisoned = true }
            throw ArtifactPublicationError(
                stage: .open,
                errnoCode: savedErrno,
                finalVisible: false,
                bytesComplete: false,
                publisherPoisoned: poisoned
            )
        }
        var identity = artifactAuthority(value)

        var verified = stat()
        guard (value.st_mode & S_IFMT) == S_IFREG,
              value.st_uid == getuid(),
              value.st_nlink == 1,
              value.st_dev == directoryIdentity.device,
              syscalls.setMode(descriptor, mode: mode_t(0o600)) == 0,
              syscalls.status(descriptor, into: &verified) == 0,
              (verified.st_mode & S_IFMT) == S_IFREG,
              (verified.st_mode & mode_t(0o7777)) == mode_t(0o600),
              verified.st_uid == getuid(),
              verified.st_nlink == 1,
              ArtifactFileIdentity(device: verified.st_dev, inode: verified.st_ino)
                == ArtifactFileIdentity(device: identity.device, inode: identity.inode)
        else {
            if syscalls.status(descriptor, into: &verified) == 0,
               ArtifactFileIdentity(device: verified.st_dev, inode: verified.st_ino)
                == ArtifactFileIdentity(device: identity.device, inode: identity.inode)
            {
                identity = artifactAuthority(verified)
            }
            let savedErrno = errno == 0 ? EIO : errno
            let closeClean = closeOrRetainAmbiguous(descriptor, identity: identity)
            let outcome = quarantineForRetention(
                name: temporaryName,
                expectedIdentity: identity,
                directoryIdentity: directoryIdentity
            )
            let syncClean = !outcome.mutated || syscalls.sync(directoryFD) == 0
            if !closeClean || !outcome.fullyRecoverable || !syncClean { poisoned = true }
            throw ArtifactPublicationError(
                stage: .open,
                errnoCode: savedErrno,
                finalVisible: false,
                bytesComplete: false,
                publisherPoisoned: poisoned
            )
        }
        identity = artifactAuthority(verified)
        return PreparedArtifactFile(
            temporaryName: temporaryName,
            finalName: finalName,
            identity: identity,
            descriptor: descriptor
        )
    }

    private func write(_ data: Data, to descriptor: Int32, check: () throws -> Void = {}) throws {
        try data.withUnsafeBytes { rawBuffer in
            guard let base = rawBuffer.baseAddress else { throw ArtifactBundleError.invalidData }
            var written = 0
            while written < rawBuffer.count {
                try check()
                let count = syscalls.writeFile(
                    descriptor,
                    buffer: base.advanced(by: written),
                    count: rawBuffer.count - written
                )
                try check()
                if count < 0, errno == EINTR { continue }
                guard count > 0 else {
                    throw ArtifactPublicationError(
                        stage: .write,
                        errnoCode: count < 0 ? errno : EIO,
                        finalVisible: false,
                        bytesComplete: false
                    )
                }
                written += count
            }
        }
    }

    private struct RollbackOutcome {
        let originalNamesCleared: Bool
        let retainedQuarantine: Bool
        let syncClean: Bool

        var fullyRecoverable: Bool {
            originalNamesCleared && !retainedQuarantine && syncClean
        }
    }

    private func rollback(
        _ files: inout [PreparedArtifactFile],
        directoryIdentity: ArtifactFileIdentity,
        mutationNeedsSync: Bool
    ) -> RollbackOutcome {
        var originalNamesCleared = true
        var retainedQuarantine = false
        var mutated = mutationNeedsSync
        for index in files.indices {
            if files[index].descriptor >= 0 {
                let descriptor = files[index].descriptor
                files[index].descriptor = -1
                if !closeOrRetainAmbiguous(descriptor, identity: files[index].identity) {
                    poisoned = true
                }
            }
            let name = files[index].published ? files[index].finalName : files[index].temporaryName
            let outcome = quarantineForRetention(
                name: name,
                expectedIdentity: files[index].identity,
                directoryIdentity: directoryIdentity
            )
            originalNamesCleared = originalNamesCleared && outcome.originalNameCleared
            retainedQuarantine = retainedQuarantine || outcome.retained
            mutated = mutated || outcome.mutated
        }
        let syncClean = !mutated || syscalls.sync(directoryFD) == 0
        return RollbackOutcome(
            originalNamesCleared: originalNamesCleared,
            retainedQuarantine: retainedQuarantine,
            syncClean: syncClean
        )
    }

    private struct QuarantineRetentionOutcome {
        let originalNameCleared: Bool
        let retained: Bool
        let mutated: Bool

        var fullyRecoverable: Bool { originalNameCleared && !retained }
    }

    private func quarantineForRetention(
        name: String,
        expectedIdentity: ArtifactFileAuthority?,
        directoryIdentity: ArtifactFileIdentity
    ) -> QuarantineRetentionOutcome {
        let quarantineName = ".rollback-\(UUID().uuidString.lowercased()).quarantine"
        errno = 0
        guard syscalls.renameExclusive(
            at: directoryFD,
            from: name,
            to: quarantineName
        ) == 0 else {
            return QuarantineRetentionOutcome(
                originalNameCleared: errno == ENOENT,
                retained: false,
                mutated: false
            )
        }
        var value = stat()
        if syscalls.status(at: directoryFD, name: quarantineName, into: &value) == 0,
           rollbackArtifactStatus(value, directoryIdentity: directoryIdentity)
        {
            let observed = artifactAuthority(value)
            if let expectedIdentity, observed == expectedIdentity {
                retainedQuarantines[quarantineName] = .verified(observed)
            } else if expectedIdentity != nil {
                retainedQuarantines[quarantineName] = .identityMismatch
            } else {
                retainedQuarantines[quarantineName] = .statusUnavailable
            }
        } else {
            retainedQuarantines[quarantineName] = .statusUnavailable
        }
        return QuarantineRetentionOutcome(
            originalNameCleared: true,
            retained: true,
            mutated: true
        )
    }

    private func artifactIdentity(
        named name: String,
        directoryIdentity: ArtifactFileIdentity
    ) -> ArtifactFileAuthority? {
        var value = stat()
        guard syscalls.status(at: directoryFD, name: name, into: &value) == 0,
              safeArtifactStatus(value, directoryIdentity: directoryIdentity)
        else { return nil }
        return artifactAuthority(value)
    }

    private func artifactAuthority(_ value: stat) -> ArtifactFileAuthority {
        ArtifactFileAuthority(
            device: value.st_dev,
            inode: value.st_ino,
            owner: value.st_uid,
            mode: value.st_mode,
            linkCount: value.st_nlink
        )
    }

    private func closeOrRetainAmbiguous(
        _ descriptor: Int32,
        identity: ArtifactFileAuthority?
    ) -> Bool {
        guard syscalls.closeFile(descriptor) == 0 else {
            ambiguousFileDescriptors[descriptor] = identity.map {
                AmbiguousArtifactDescriptorAuthority.exact($0)
            } ?? .unknown
            return false
        }
        ambiguousFileDescriptors.removeValue(forKey: descriptor)
        return true
    }

    private func recoverAmbiguousFileDescriptors() {
        for (descriptor, authority) in Array(ambiguousFileDescriptors) {
            var value = stat()
            errno = 0
            let result = syscalls.status(descriptor, into: &value)
            if result == -1, errno == EBADF {
                ambiguousFileDescriptors.removeValue(forKey: descriptor)
                continue
            }
            guard result == 0 else {
                poisoned = true
                continue
            }
            guard case let .exact(identity) = authority else {
                poisoned = true
                continue
            }
            let observed = artifactAuthority(value)
            guard observed == identity else {
                poisoned = true
                ambiguousFileDescriptors.removeValue(forKey: descriptor)
                continue
            }
            if syscalls.closeFile(descriptor) == 0 {
                ambiguousFileDescriptors.removeValue(forKey: descriptor)
            } else {
                poisoned = true
            }
        }
    }

    private func safeArtifactStatus(
        _ value: stat,
        directoryIdentity: ArtifactFileIdentity
    ) -> Bool {
        rollbackArtifactStatus(value, directoryIdentity: directoryIdentity)
            && (value.st_mode & mode_t(0o7777)) == mode_t(0o600)
    }

    private func rollbackArtifactStatus(
        _ value: stat,
        directoryIdentity: ArtifactFileIdentity
    ) -> Bool {
        (value.st_mode & S_IFMT) == S_IFREG
            && value.st_uid == getuid()
            && value.st_nlink == 1
            && value.st_dev == directoryIdentity.device
    }

    private func scanQuota(
        directoryIdentity: ArtifactFileIdentity
    ) throws -> (count: Int, bytes: Int) {
        guard retainedQuarantines.isEmpty else {
            poisoned = true
            throw ArtifactBundleError.poisoned
        }
        let names: Set<String>
        do {
            names = try syscalls.directoryEntryNames(
                directoryFD,
                maximumEntries: maximumDirectoryEntries,
                maximumNameBytes: maximumDirectoryNameBytes
            )
        } catch {
            poisoned = true
            throw ArtifactBundleError.unsafeDirectory
        }
        guard !names.contains(where: { $0.hasSuffix(".tmp") || $0.hasSuffix(".quarantine") }) else {
            poisoned = true
            throw ArtifactBundleError.poisoned
        }
        guard !names.contains(where: { $0.hasSuffix(".ax.json") && !validSmartSnapshotDetailName($0) }) else {
            poisoned = true
            throw ArtifactBundleError.poisoned
        }
        let details = names.filter { validSmartSnapshotDetailName($0) }
        var bytes = 0
        for detailName in details {
            let imageName = String(detailName.dropLast(".ax.json".count)) + ".png"
            guard names.contains(imageName) else {
                poisoned = true
                throw ArtifactBundleError.poisoned
            }
            var detailStatus = stat()
            var imageStatus = stat()
            guard syscalls.status(at: directoryFD, name: detailName, into: &detailStatus) == 0,
                  syscalls.status(at: directoryFD, name: imageName, into: &imageStatus) == 0,
                  safeArtifactStatus(detailStatus, directoryIdentity: directoryIdentity),
                  safeArtifactStatus(imageStatus, directoryIdentity: directoryIdentity),
                  detailStatus.st_size > 0,
                  detailStatus.st_size <= maximumAXTextDetailJSONBytes,
                  imageStatus.st_size > 0,
                  detailStatus.st_size <= off_t(Int.max - bytes)
            else {
                poisoned = true
                throw ArtifactBundleError.poisoned
            }
            bytes += Int(detailStatus.st_size)
        }
        return (details.count, bytes)
    }
}

private func validSmartSnapshotBundleNames(image: String, detail: String) -> Bool {
    guard ArtifactDirectory.isPlainName(image), ArtifactDirectory.isPlainName(detail) else {
        return false
    }
    let prefix = "snapshot-"
    let suffix = ".png"
    guard image.hasPrefix(prefix), image.hasSuffix(suffix) else { return false }
    let token = image.dropFirst(prefix.count).dropLast(suffix.count)
    return token.utf8.count == 32
        && token.allSatisfy { "0123456789abcdef".contains($0) }
        && detail == String(image.dropLast(suffix.count)) + ".ax.json"
}

private func validSmartSnapshotDetailName(_ value: String) -> Bool {
    guard value.hasSuffix(".ax.json") else { return false }
    let image = String(value.dropLast(".ax.json".count)) + ".png"
    return validSmartSnapshotBundleNames(image: image, detail: value)
}


func appshotArtifactToken(_ name: String) -> String? {
    guard name.hasPrefix("appshot-") else { return nil }
    let suffixes = [".png", ".ax.json", ".manifest.json"]
    guard let suffix = suffixes.first(where: name.hasSuffix) else { return nil }
    let token = String(name.dropFirst(8).dropLast(suffix.count))
    return token.utf8.count == 32 && token.utf8.allSatisfy { (48...57).contains($0) || (97...102).contains($0) } ? token : nil
}

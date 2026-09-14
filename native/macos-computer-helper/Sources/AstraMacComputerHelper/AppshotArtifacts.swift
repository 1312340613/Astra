import CryptoKit
import Darwin
import Foundation
import AstraAppshotCore
import ImageIO
import UniformTypeIdentifiers

/// Worker-side publication only. Source revalidation runs immediately before the
/// short runtime publication/journal lease, and uses the capture's original deadline.
struct AppshotArtifactPublisher {
  let runtime: AppshotRuntimeDirectory
  var journalSync: (Int32) -> Int32 = Darwin.fsync
  func publish(
    _ result: AppshotCaptureResult,
    projection: AppshotProjection,
    revalidate: (AppshotCaptureResult) throws -> Void
  ) throws -> AppshotPublishedBundle {
    _ = try result.deadline.remaining()
    try AppshotCaptureImage.validatePNGSize(result.png.count)
    try AppshotCaptureImage.validateDimensions(width: result.width, height: result.height)
    guard let image = CGImageSourceCreateWithData(result.png as CFData, nil),
      CGImageSourceGetCount(image) == 1,
      CGImageSourceGetStatus(image) == .statusComplete,
      CGImageSourceGetType(image) as String? == UTType.png.identifier,
      let properties = CGImageSourceCopyPropertiesAtIndex(image, 0, nil) as? [CFString: Any],
      (properties[kCGImagePropertyPixelWidth] as? NSNumber)?.intValue == result.width,
      (properties[kCGImagePropertyPixelHeight] as? NSNumber)?.intValue == result.height,
      !projection.json.isEmpty, projection.json.count <= AppshotProjectionBuilder.maximumBytes,
      (0...2000).contains(projection.metadata.nodeCount),
      (0...AppshotProjectionBuilder.maximumDepth).contains(projection.metadata.depth)
    else { throw ArtifactBundleError.invalidData }
    let token = result.authority.nonce.uuidString.replacingOccurrences(of: "-", with: "")
      .lowercased()
    let names = [
      "appshot-\(token).png", "appshot-\(token).ax.json", "appshot-\(token).manifest.json",
    ]
    let pngHash = Self.hash(result.png)
    let axHash = Self.hash(projection.json)
    let capturedAt = ISO8601DateFormatter().string(from: Date())
    _ = try result.deadline.remaining()
    try revalidate(result)
    return try runtime.withArtifactPublication { fd in
      guard runtime.descriptor?.instanceID == result.authority.recipient.instanceID else {
        throw AppshotBrokerError.unauthorized
      }
      let publisher = ArtifactBundlePublisher(directoryFD: fd)
      defer { publisher.close() }
      var enrolling: [ArtifactPublishedAuthority] = []
      do {
        let authorities = try publisher.publish(
          [
            .init(finalName: names[0], data: result.png),
            .init(finalName: names[1], data: projection.json),
          ],
          check: {
            _ = try result.deadline.remaining()
            try runtime.verifyAuthority()
          },
          manifest: { authorities in
            let png = authorities[0]
            let ax = authorities[1]
            let metadata = projection.metadata
            let manifest = AppshotManifest(
              schemaVersion: 1, token: token, capturedAt: capturedAt,
              source: result.source,
              png: .init(
                name: png.name, size: png.size, width: result.width, height: result.height,
                sha256: pngHash, device: png.device, inode: png.inode, owner: png.owner,
                mode: png.mode, linkCount: png.linkCount),
              ax: .init(
                name: ax.name, size: ax.size, sha256: axHash, device: ax.device, inode: ax.inode,
                owner: ax.owner, mode: ax.mode, linkCount: ax.linkCount,
                coverage: metadata.coverage,
                nodeCount: metadata.nodeCount, depth: metadata.depth, truncated: metadata.truncated,
                truncationReasons: metadata.truncationReasons),
              broker: result.authority.recipient.manifestBinding)
            return .init(finalName: names[2], data: try manifest.encodeStrict())
          },
          enroll: {
            enrolling = $0
            try runtime.trackArtifacts(authorities: $0, sync: journalSync)
          })
        return AppshotPublishedBundle(
          runtime: runtime, authorities: authorities, deadline: result.deadline)
      } catch {
        // Enrollment may have committed its journal before a sync/cancellation failure.
        // Publisher has quarantined failures; clear only this triplet's recorded custody.
        try? runtime.releaseArtifacts(authorities: enrolling)
        throw error
      }
    }
  }
  private static func hash(_ data: Data) -> String {
    SHA256.hash(data: data).map { String(format: "%02x", $0) }.joined()
  }
}

/// Owns journaled files while the worker result crosses to MainActor. Dropped, late,
/// cancelled and stopped deliveries release custody. Exactly one broker transfer is allowed.
final class AppshotPublishedBundle: @unchecked Sendable {
  private let runtime: AppshotRuntimeDirectory
  private let authorities: [ArtifactPublishedAuthority]
  private let deadline: AppshotCaptureDeadline
  private let mutex = NSLock()
  private var transferred = false
  private var destroyed = false
  fileprivate init(
    runtime: AppshotRuntimeDirectory, authorities: [ArtifactPublishedAuthority],
    deadline: AppshotCaptureDeadline
  ) {
    self.runtime = runtime
    self.authorities = authorities
    self.deadline = deadline
  }
  @MainActor func makeCapturedArtifact() throws -> AppshotCapturedArtifact {
    mutex.lock()
    defer { mutex.unlock() }
    guard !transferred, !destroyed else { throw ArtifactBundleError.closed }
    do {
      _ = try deadline.remaining()
      try runtime.withArtifactPublication { fd in
        for authority in authorities {
          var info = stat()
          guard fstatat(fd, authority.name, &info, AT_SYMLINK_NOFOLLOW) == 0,
            authority.matches(info)
          else { throw AppshotBrokerError.unsafeRuntime }
        }
      }
    } catch {
      destroyed = true
      try? runtime.releaseArtifacts(authorities: authorities)
      throw error
    }
    transferred = true
    return AppshotCapturedArtifact(
      manifestPath: runtime.path + "/" + authorities[2].name,
      byteCount: authorities.reduce(0) { $0 + $1.size }, cleanup: { self.destroy() })
  }
  func destroy() {
    mutex.lock()
    guard !destroyed else {
      mutex.unlock()
      return
    }
    destroyed = true
    mutex.unlock()
    try? runtime.releaseArtifacts(authorities: authorities)
  }
  deinit { destroy() }
}

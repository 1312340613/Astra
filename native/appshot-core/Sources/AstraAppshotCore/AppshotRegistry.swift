import Foundation

public enum AppshotBrokerError: Error, Equatable {
  case unsafeRuntime, systemFailure, notOwner, unauthorized, tooManyClients
  case noReceivingSession, receivingSessionAmbiguous, attachmentLimitReached
  case invalidActivity, recipientDisconnected, captureBusy, quotaExceeded
  case unknownAttachment, captureExpired, stopped
}

public protocol AppshotPeerIdentity: Equatable, Sendable {
  var pid: Int32 { get }
  var processStart: String { get }
}

public struct AppshotRecipient<Identity: AppshotPeerIdentity>: Equatable, Sendable {
  public let requestID: String
  public let connectionID: String
  public let sessionID: String
  public let identity: Identity
  public let instanceID: String
  public init(requestID: String, connectionID: String, sessionID: String, identity: Identity, instanceID: String) {
    self.requestID = requestID
    self.connectionID = connectionID
    self.sessionID = sessionID
    self.identity = identity
    self.instanceID = instanceID
  }
}

/// The publisher hands off exact-owned cleanup, never a deletion path from a client.
@MainActor public final class AppshotCapturedArtifact {
  public let manifestPath: String
  public let byteCount: Int
  private var cleanup: (() -> Void)?
  public init(manifestPath: String, byteCount: Int, cleanup: @escaping () -> Void) {
    self.manifestPath = manifestPath
    self.byteCount = byteCount
    self.cleanup = cleanup
  }
  public func destroy() {
    let action = cleanup
    cleanup = nil
    action?()
  }
  deinit { cleanup?() }
}

/// Main-actor ownership serializes routing, reservations and controller operations.
@MainActor public final class AppshotRegistry<Identity: AppshotPeerIdentity> {
  public static var maximumLiveBytes: Int { 256 * 1024 * 1024 }
  public static var captureReservationBytes: Int { 10 * 1024 * 1024 + 256 * 1024 + 65536 }
  private struct Client {
    let sessionID: String
    let identity: Identity
    var activity: UInt64 = 0
    var count = 0
    var canAccept = false
    var unconfirmedCommit: (requestID: String, deadline: UInt64)?
  }
  private struct Attachment {
    let binding: AppshotRecipient<Identity>
    var deadline: UInt64
    var artifact: AppshotCapturedArtifact?
    var committed = false
  }
  public let instanceID: String
  public let nonce: String
  private let identityLookup: (Int32) -> Identity?
  private let monotonicNS: () -> UInt64
  private let peerAllowed: (Identity) -> Bool
  private var clients: [String: Client] = [:]
  private var attachments: [String: Attachment] = [:]
  private var activeCapture: String?
  public var connectedCount: Int { clients.count }
  public var liveBytes: Int {
    attachments.values.reduce(0) { $0 + ($1.artifact?.byteCount ?? Self.captureReservationBytes) }
  }
  public var connectionIDs: [String] { Array(clients.keys) }

  public init(
    instanceID: String, nonce: String,
    identityLookup: @escaping (Int32) -> Identity?,
    monotonicNS: @escaping () -> UInt64, peerAllowed: @escaping (Identity) -> Bool
  ) {
    self.instanceID = instanceID
    self.nonce = nonce
    self.identityLookup = identityLookup
    self.monotonicNS = monotonicNS
    self.peerAllowed = peerAllowed
  }
  /// Inputs are normalized by a strict, version-specific wire adapter.
  public func authenticate(connectionID: String, peer: Identity, sessionID: String,
    pid: Int, processStart: String, clientNonce: String) throws
  {
    // client_nonce echoes the private descriptor's broker_nonce (proof of epoch access).
    guard clientNonce == nonce, peerAllowed(peer), pid == Int(peer.pid),
      processStart == peer.processStart, identityLookup(peer.pid) == peer,
      clients[connectionID] == nil,
      !clients.values.contains(where: { $0.sessionID == sessionID || $0.identity == peer })
    else { throw AppshotBrokerError.unauthorized }
    guard clients.count < 16 else { throw AppshotBrokerError.tooManyClients }
    clients[connectionID] = Client(sessionID: sessionID, identity: peer)
  }
  public func validate(connectionID: String, brokerID: String, sessionID: String) throws {
    guard brokerID == instanceID, let client = clients[connectionID], client.sessionID == sessionID,
      identityLookup(client.identity.pid) == client.identity
    else { throw AppshotBrokerError.unauthorized }
  }
  @discardableResult
  public func update(connectionID: String, requestID: String, brokerID: String,
    sessionID: String, activity: UInt64, count: Int, canAccept: Bool) throws -> Bool {
    var incorporated = false
    try validate(connectionID: connectionID, brokerID: brokerID, sessionID: sessionID)
    guard var client = clients[connectionID], (0...AppshotLimits.maximumAppshotsPerDraft).contains(count),
      activity >= client.activity,
      activity <= monotonicNS().addingReportingOverflow(1_000_000_000).partialValue
    else { throw AppshotBrokerError.invalidActivity }
    if let pending = client.unconfirmedCommit {
      guard monotonicNS() < pending.deadline else {
        throw AppshotBrokerError.captureExpired
      }
      // Only a current-state echo of attach_commit proves incorporation. Ordinary
      // state frames may have been written before the client observed that commit.
      if requestID == pending.requestID {
        client.unconfirmedCommit = nil
        incorporated = true
      }
    }
    client.activity = activity
    client.count = count
    client.canAccept = canAccept
    clients[connectionID] = client
    return incorporated
  }
  public func state(connectionID: String) -> (sessionID: String, count: Int, canAccept: Bool)? {
    guard let client = clients[connectionID] else { return nil }
    return (
      client.sessionID, occupied(connectionID), client.canAccept && client.unconfirmedCommit == nil
    )
  }
  private func occupied(_ connectionID: String) -> Int {
    let owned = attachments.values.filter { $0.binding.connectionID == connectionID }
    let unconfirmed = clients[connectionID]?.unconfirmedCommit == nil ? 0 : 1
    return max((clients[connectionID]?.count ?? 0) + unconfirmed, owned.filter(\.committed).count)
      + owned.filter { !$0.committed }.count
  }
  public func reserveCapture() throws -> AppshotRecipient<Identity> {
    guard activeCapture == nil else { throw AppshotBrokerError.captureBusy }
    let alive = clients.filter {
      $0.value.activity > 0 && identityLookup($0.value.identity.pid) == $0.value.identity
    }
    guard let newest = alive.values.map(\.activity).max() else {
      throw AppshotBrokerError.noReceivingSession
    }
    let selected = alive.filter { $0.value.activity == newest }
    guard selected.count == 1, let (connection, client) = selected.first else {
      throw AppshotBrokerError.receivingSessionAmbiguous
    }
    guard client.canAccept, client.unconfirmedCommit == nil, occupied(connection) < 4 else {
      throw AppshotBrokerError.attachmentLimitReached
    }
    guard liveBytes <= Self.maximumLiveBytes - Self.captureReservationBytes else {
      throw AppshotBrokerError.quotaExceeded
    }
    let binding = AppshotRecipient<Identity>(
      requestID: UUID().uuidString, connectionID: connection, sessionID: client.sessionID,
      identity: client.identity, instanceID: instanceID)
    attachments[binding.requestID] = Attachment(
      binding: binding, deadline: monotonicNS() &+ 5_000_000_000)
    activeCapture = binding.requestID
    return binding
  }
  public func revalidate(_ binding: AppshotRecipient<Identity>) throws -> AppshotRecipient<Identity> {
    guard let client = clients[binding.connectionID], client.identity == binding.identity,
      client.sessionID == binding.sessionID, binding.instanceID == instanceID,
      identityLookup(binding.identity.pid) == binding.identity
    else { throw AppshotBrokerError.recipientDisconnected }
    guard client.canAccept, occupied(binding.connectionID) <= 4 else {
      throw AppshotBrokerError.attachmentLimitReached
    }
    return binding
  }
  public func stage(_ binding: AppshotRecipient<Identity>, artifact: AppshotCapturedArtifact) throws {
    do {
      guard var attachment = attachments[binding.requestID], attachment.binding == binding,
        attachment.artifact == nil, !attachment.committed
      else { throw AppshotBrokerError.unknownAttachment }
      guard monotonicNS() < attachment.deadline else {
        throw AppshotBrokerError.captureExpired
      }
      _ = try revalidate(binding)
      guard artifact.byteCount > 0, artifact.byteCount <= Self.captureReservationBytes else {
        throw AppshotBrokerError.quotaExceeded
      }
      attachment.artifact = artifact
      attachment.deadline = monotonicNS() &+ 3_000_000_000
      attachments[binding.requestID] = attachment
    } catch {
      artifact.destroy()
      cancelCapture(binding)
      throw error
    }
  }
  public func isCommitted(connectionID: String, requestID: String) -> Bool {
    guard let attachment = attachments[requestID] else { return false }
    return attachment.binding.connectionID == connectionID && attachment.committed
  }
  public func acknowledge(connectionID: String, requestID: String, accepted: Bool) throws
    -> AppshotCapturedArtifact?
  {
    guard var attachment = attachments[requestID], attachment.binding.connectionID == connectionID,
      !attachment.committed, let artifact = attachment.artifact
    else { throw AppshotBrokerError.unknownAttachment }
    do {
      guard monotonicNS() < attachment.deadline else {
        throw AppshotBrokerError.captureExpired
      }
      _ = try revalidate(attachment.binding)
    } catch {
      cancelCapture(attachment.binding)
      throw error
    }
    guard accepted else {
      cancelCapture(attachment.binding)
      return nil
    }
    clients[connectionID]?.unconfirmedCommit = (requestID, monotonicNS() &+ 3_000_000_000)
    attachment.committed = true
    attachments[requestID] = attachment
    if activeCapture == requestID { activeCapture = nil }
    return artifact
  }
  public func cancelCapture(_ binding: AppshotRecipient<Identity>) {
    guard let attachment = attachments[binding.requestID], attachment.binding == binding else {
      return
    }
    let removed = attachments.removeValue(forKey: binding.requestID)
    if clients[binding.connectionID]?.unconfirmedCommit?.requestID == binding.requestID {
      clients[binding.connectionID]?.unconfirmedCommit = nil
    }
    if activeCapture == binding.requestID { activeCapture = nil }
    // Complete state transitions before publisher cleanup can reenter the registry.
    removed?.artifact?.destroy()
  }
  public func release(connectionID: String, requestID: String) -> Bool {
    guard let attachment = attachments[requestID] else {
      // No deletion and no retained unbounded tombstones: unknown IDs are harmless no-ops.
      return clients[connectionID] != nil
    }
    guard attachment.binding.connectionID == connectionID else { return false }
    cancelCapture(attachment.binding)
    return true
  }
  public func disconnect(connectionID: String) {
    clients.removeValue(forKey: connectionID)
    for item in Array(attachments.values) where item.binding.connectionID == connectionID {
      cancelCapture(item.binding)
    }
  }
  public func expire() -> [AppshotRecipient<Identity>] {
    let expired = attachments.values.filter {
      !$0.committed && monotonicNS() >= $0.deadline
    }.map(\.binding)
    for binding in expired { cancelCapture(binding) }
    return expired
  }
  /// A missing commit confirmation terminates the connection, rather than silently
  /// reopening capacity while the client's committed population remains unknown.
  public func confirmationExpiredConnections() -> [String] {
    clients.filter {
      guard let pending = $0.value.unconfirmedCommit else { return false }
      return monotonicNS() >= pending.deadline
    }.map(\.key)
  }
  public func deadConnections() -> [String] {
    clients.filter { identityLookup($0.value.identity.pid) != $0.value.identity }.map(
      \.key)
  }
}

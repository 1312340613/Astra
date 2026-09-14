import Darwin
import Foundation
import AstraAppshotCore

public typealias AppshotRecipientBinding = AppshotRecipient<AppshotProcessIdentity>
public typealias AppshotClientRegistry = AppshotRegistry<AppshotProcessIdentity>

extension AppshotProcessIdentity: AppshotPeerIdentity {}

extension AppshotRecipient where Identity == AppshotProcessIdentity {
  public var manifestBinding: AppshotBrokerBinding {
    .init(instanceId: instanceID, sessionId: sessionID, processStart: identity.processStart)
  }
}

extension AppshotRegistry where Identity == AppshotProcessIdentity {
  public convenience init(
    instanceID: String, nonce: String, processes: AppshotProcessProviding = AppshotSystemProcesses()
  ) {
    self.init(instanceID: instanceID, nonce: nonce, identityLookup: processes.identity,
      monotonicNS: processes.monotonicNS, peerAllowed: { $0.uid == getuid() })
  }
  public func authenticate(connectionID: String, peer: Identity, hello: AppshotHello) throws {
    _ = try AppshotMessage.hello(hello).encodeFrame()
    try authenticate(connectionID: connectionID, peer: peer, sessionID: hello.sessionId,
      pid: hello.pid, processStart: hello.processStart, clientNonce: hello.clientNonce)
  }
  @discardableResult
  public func update(connectionID: String, state: AppshotClientState) throws -> Bool {
    _ = try AppshotMessage.clientState(state).encodeFrame()
    guard let activity = UInt64(state.activityNs) else { throw AppshotBrokerError.invalidActivity }
    return try update(connectionID: connectionID, requestID: state.requestId,
      brokerID: state.brokerId, sessionID: state.sessionId, activity: activity,
      count: state.appshotCount, canAccept: state.canAccept)
  }
}

public enum AppshotTransactionOutcome: Equatable, Sendable {
  case incorporated
  case failed(AppshotErrorCode)
}

/// Production transport and lifecycle, with capture injected by Task 4/6. All callbacks,
/// including asynchronous capture completion, must enter the main actor. No entry point
/// creates this object implicitly, so importing this module cannot register a shortcut.
@MainActor public final class AppshotBroker {
  public typealias Capture =
    @MainActor (
      AppshotRecipientBinding, @escaping @MainActor (Result<AppshotCapturedArtifact, Error>) -> Void
    ) -> (() -> Void)
  final class Connection {
    let fd: Int32
    let peer: AppshotProcessIdentity
    let opened: UInt64
    private let closeFD: (Int32) -> Void
    var decoder = AppshotFrameDecoder()
    var output = Data()
    var readSource: DispatchSourceRead?
    var writeSource: DispatchSourceWrite?
    var authenticated = false
    var sourceCount = 0
    var closing = false
    func sourceCancelled() {
      sourceCount -= 1
      if closing && sourceCount == 0 { closeFD(fd) }
    }
    func close() {
      guard !closing else { return }
      closing = true
      readSource?.cancel()
      readSource = nil
      writeSource?.cancel()
      writeSource = nil
      if sourceCount == 0 { closeFD(fd) }
    }
    init(
      fd: Int32, peer: AppshotProcessIdentity, opened: UInt64,
      closeFD: @escaping (Int32) -> Void = { Darwin.close($0) }
    ) {
      self.fd = fd
      self.peer = peer
      self.opened = opened
      self.closeFD = closeFD
    }
  }
  private let runtime: AppshotRuntimeDirectory
  private let controller: AppshotShortcutController
  private let processes: AppshotProcessProviding
  private let capture: Capture
  private let onStop: () -> Void
  private let onOutcome: (String?, AppshotTransactionOutcome) -> Void
  private let permission: () -> String
  private var registry: AppshotClientRegistry?
  private var connections: [String: Connection] = [:]
  private var listenerSource: DispatchSourceRead?
  private var timer: DispatchSourceTimer?
  private var graceDeadline: UInt64?
  private var completions: [String: () -> Void] = [:]
  private var captureBindings: [String: AppshotRecipientBinding] = [:]
  private var cancellations: [String: () -> Void] = [:]
  private var registration = "unavailable"
  private var stopped = false
  public var authenticatedClientCount: Int { registry?.connectedCount ?? 0 }
  public var socketClientCount: Int { connections.count }

  public init(
    runtime: AppshotRuntimeDirectory, controller: AppshotShortcutController,
    processes: AppshotProcessProviding = AppshotSystemProcesses(), capture: @escaping Capture,
    onStop: @escaping () -> Void = {},
    onOutcome: @escaping (String?, AppshotTransactionOutcome) -> Void = { _, _ in },
    permission: @escaping () -> String = {
      AppshotBroker.permissionStatus()
    }
  ) {
    self.runtime = runtime
    self.controller = controller
    self.processes = processes
    self.capture = capture
    self.onStop = onStop
    self.onOutcome = onOutcome
    self.permission = permission
  }
  nonisolated public static func permissionStatus() -> String {
    let status = SystemPermissionStatus()
    return status.screenRecordingAllowed ? "ready" : "unavailable"
  }
  deinit {
    controller.stop()
    timer?.cancel()
    listenerSource?.cancel()
    for connection in connections.values { connection.close() }
    for cancellation in cancellations.values { cancellation() }
    for completion in completions.values { completion() }
    runtime.close()
  }
  public func start() throws {
    guard !stopped else { throw AppshotBrokerError.stopped }
    guard listenerSource == nil else { return }
    do {
      guard try runtime.elect() else { throw AppshotBrokerError.notOwner }
      let fd = try runtime.publishListener()
      guard let descriptor = runtime.descriptor else { throw AppshotBrokerError.unsafeRuntime }
      registry = AppshotClientRegistry(
        instanceID: descriptor.instanceID, nonce: descriptor.brokerNonce, processes: processes)
      let source = DispatchSource.makeReadSource(fileDescriptor: fd, queue: .main)
      source.setEventHandler { [weak self] in self?.acceptReady(fd) }
      _ = runtime.detachListener()
      source.setCancelHandler { Darwin.close(fd) }
      listenerSource = source
      source.resume()
      let timer = DispatchSource.makeTimerSource(queue: .main)
      timer.schedule(deadline: .now() + .milliseconds(50), repeating: .milliseconds(50))
      timer.setEventHandler { [weak self] in self?.poll() }
      self.timer = timer
      timer.resume()
      graceDeadline = processes.monotonicNS() &+ 2_000_000_000
    } catch {
      stop()
      throw error
    }
  }
  public func stop() {
    guard !stopped else { return }
    stopped = true
    controller.stop()
    registration = "unavailable"
    timer?.cancel()
    timer = nil
    listenerSource?.cancel()
    listenerSource = nil
    for id in Array(connections.keys) { disconnect(id) }
    for completion in completions.values { completion() }
    completions.removeAll()
    runtime.close()
    onStop()
  }
  private func acceptReady(_ listener: Int32) {
    guard !stopped else { return }
    // Bound work per readiness event as well as the number of retained sockets.
    for _ in 0..<16 {
      let fd = accept(listener, nil, nil)
      if fd < 0 {
        if errno == EINTR { continue }
        return
      }
      do {
        guard connections.count < 16 else { throw AppshotBrokerError.tooManyClients }
        try AppshotRuntimeDirectory.configureSocket(fd)
        let peer = try AppshotSystemProcesses().peerIdentity(socket: fd)
        let id = UUID().uuidString
        let connection = Connection(fd: fd, peer: peer, opened: processes.monotonicNS())
        let source = DispatchSource.makeReadSource(fileDescriptor: fd, queue: .main)
        source.setEventHandler { [weak self] in self?.readReady(id) }
        connection.sourceCount += 1
        source.setCancelHandler { connection.sourceCancelled() }
        connection.readSource = source
        connections[id] = connection
        source.resume()
      } catch { Darwin.close(fd) }
    }
  }
  private func readReady(_ id: String) {
    guard let connection = connections[id], !stopped else { return }
    var buffer = [UInt8](repeating: 0, count: 8192)
    for _ in 0..<8 {
      let count = read(connection.fd, &buffer, buffer.count)
      if count < 0 {
        if errno == EINTR { continue }
        if errno == EAGAIN || errno == EWOULDBLOCK { return }
        disconnect(id)
        return
      }
      if count == 0 {
        disconnect(id)
        return
      }
      do {
        let messages = try connection.decoder.feed(Data(buffer.prefix(count)))
        for message in messages {
          guard connections[id] != nil else { return }
          try receive(message, from: id)
        }
      } catch {
        disconnect(id)
        return
      }
    }
  }
  private func receive(_ message: AppshotMessage, from id: String) throws {
    guard let connection = connections[id], let registry else { throw AppshotBrokerError.stopped }
    if !connection.authenticated {
      guard case .hello(let hello) = message else { throw AppshotBrokerError.unauthorized }
      try registry.authenticate(connectionID: id, peer: connection.peer, hello: hello)
      connection.authenticated = true
      graceDeadline = nil
      if registry.connectedCount == 1 { startShortcut() }
      try send(
        .helloAck(
          .init(
            type: "hello_ack", version: 1, instanceId: registry.instanceID,
            brokerNonce: registry.nonce, sessionId: hello.sessionId)), to: id)
      return
    }
    switch message {
    case .clientState(let state):
      let incorporated = try registry.update(connectionID: id, state: state)
      guard let current = registry.state(connectionID: id) else {
        throw AppshotBrokerError.recipientDisconnected
      }
      // Terminal completion/observer may synchronously stop and remove the client.
      if incorporated { finish(state.requestId, connectionID: id, outcome: .incorporated) }
      try send(
        .clientStateAck(
          .init(
            type: "client_state_ack", version: 1, requestId: state.requestId,
            brokerId: registry.instanceID, sessionId: state.sessionId,
            appshotCount: min(4, current.count), canAccept: current.canAccept && current.count < 4)),
        to: id)
    case .attachAck(let ack):
      try registry.validate(connectionID: id, brokerID: ack.brokerId, sessionID: ack.sessionId)
      // Retransmitted ACK cannot revoke committed custody or reset its receipt deadline.
      if registry.isCommitted(connectionID: id, requestID: ack.requestId) { return }
      do {
        let artifact = try registry.acknowledge(
          connectionID: id, requestID: ack.requestId, accepted: ack.accepted)
        if let artifact {
          try send(
            .attachCommit(
              .init(
                type: "attach_commit", version: 1, requestId: ack.requestId,
                brokerId: registry.instanceID, sessionId: ack.sessionId,
                manifestPath: artifact.manifestPath)), to: id)
        } else {
          try revoke(id: id, session: ack.sessionId, request: ack.requestId, reason: "rejected")
          finish(ack.requestId, connectionID: id, outcome: .failed(.attachmentRejected))
        }
      } catch {
        try revoke(id: id, session: ack.sessionId, request: ack.requestId, reason: "expired")
        finish(ack.requestId, connectionID: id, outcome: .failed(AppshotErrorCode.map(error)))
      }
    case .release(let release):
      try registry.validate(
        connectionID: id, brokerID: release.brokerId, sessionID: release.sessionId)
      let released = registry.release(connectionID: id, requestID: release.requestId)
      if released {
        finish(release.requestId, connectionID: id, outcome: .failed(.attachmentRejected))
      }
      try send(
        .releaseAck(
          .init(
            type: "release_ack", version: 1, requestId: release.requestId,
            brokerId: registry.instanceID, sessionId: release.sessionId, released: released)),
        to: id)
    case .command(let command):
      try registry.validate(
        connectionID: id, brokerID: command.brokerId, sessionID: command.sessionId)
      try commandResponse(command, connectionID: id)
    default: throw AppshotBrokerError.unauthorized
    }
  }
  private func startShortcut() {
    do {
      try controller.start { [weak self] completion in
        // Carbon event handlers run on the main run loop; do not block while capturing.
        Task { @MainActor [weak self] in
          guard let self else {
            completion()
            return
          }
          self.trigger(completion: completion)
        }
      }
      registration = controller.settings.enabled ? "registered" : "unavailable"
    } catch AppshotError.shortcutConflict {
      registration = "conflict"
      onOutcome(nil, .failed(.shortcutConflict))
    } catch {
      registration = "unavailable"
      onOutcome(nil, .failed(AppshotErrorCode.map(error)))
    }
  }
  private func trigger(completion: @escaping () -> Void) {
    guard !stopped, let registry else {
      completion()
      return
    }
    // Keep a single transaction through the positive commit-incorporation receipt.
    guard completions.isEmpty else {
      completion()
      return
    }
    let binding: AppshotRecipientBinding
    do { binding = try registry.reserveCapture() } catch {
      onOutcome(nil, .failed(AppshotErrorCode.map(error)))
      completion()
      return
    }
    completions[binding.requestID] = completion
    captureBindings[binding.requestID] = binding
    var delivered = false
    var deliveredArtifact: AppshotCapturedArtifact?
    let cancel = capture(binding) { [weak self] result in
      if delivered {
        if case .success(let artifact) = result, artifact !== deliveredArtifact {
          artifact.destroy()
        }
        return
      }
      delivered = true
      if case .success(let artifact) = result { deliveredArtifact = artifact }
      guard let self else {
        if case .success(let artifact) = result { artifact.destroy() }
        return
      }
      self.cancellations.removeValue(forKey: binding.requestID)
      guard !self.stopped, self.completions[binding.requestID] != nil else {
        if case .success(let artifact) = result { artifact.destroy() }
        return
      }
      do {
        let artifact = try result.get()
        try registry.stage(binding, artifact: artifact)
        try self.send(
          .attachOffer(
            .init(
              type: "attach_offer", version: 1, requestId: binding.requestID,
              brokerId: registry.instanceID, sessionId: binding.sessionID,
              manifestPath: artifact.manifestPath)), to: binding.connectionID)
      } catch {
        registry.cancelCapture(binding)
        self.finish(binding.requestID, outcome: .failed(AppshotErrorCode.map(error)))
      }
    }
    if !delivered {
      if completions[binding.requestID] != nil {
        cancellations[binding.requestID] = cancel
      } else {
        cancel()
      }
    }
  }
  private func commandResponse(_ command: AppshotCommand, connectionID: String) throws {
    guard let registry, let state = registry.state(connectionID: connectionID) else {
      throw AppshotBrokerError.unauthorized
    }
    if command.name == "status" {
      try send(
        .status(
          .init(
            type: "status", version: 1, requestId: command.requestId, brokerId: registry.instanceID,
            sessionId: command.sessionId, enabled: controller.settings.enabled,
            chord: controller.settings.shortcut.description, registration: registration,
            connectedTuis: registry.connectedCount,
            permission: permission() == "ready" ? "ready" : "unavailable",
            appshotCount: min(4, state.count), canAccept: state.canAccept && state.count < 4)),
        to: connectionID)
      return
    }
    var failure: AppshotErrorCode?
    do {
      switch command.name {
      case "enable": try controller.setEnabled(true)
      case "disable": try controller.setEnabled(false)
      case "shortcut": try controller.replace(with: AppshotChord.parse(command.argument))
      default: throw AppshotBrokerError.unauthorized
      }
      if registry.connectedCount > 0 { startShortcut() }
    } catch {
      failure = AppshotErrorCode.map(error)
    }
    try send(
      .commandResult(
        .init(
          type: "command_result", version: 1, requestId: command.requestId,
          brokerId: registry.instanceID, sessionId: command.sessionId, ok: failure == nil,
          code: failure?.rawValue ?? "ok",
          message: failure.map { AppshotHUDMessage.failure($0).text } ?? "Appshot settings updated."
        )),
      to: connectionID)
  }
  private func send(_ message: AppshotMessage, to id: String) throws {
    guard let connection = connections[id] else { throw AppshotBrokerError.recipientDisconnected }
    let frame = try message.encodeFrame()
    guard connection.output.count + frame.count <= 4 * 65536 else {
      disconnect(id)
      throw AppshotBrokerError.quotaExceeded
    }
    connection.output.append(frame)
    flush(id)
    guard connections[id] != nil else { throw AppshotBrokerError.recipientDisconnected }
  }
  private func flush(_ id: String) {
    guard let connection = connections[id] else { return }
    while !connection.output.isEmpty {
      let count = connection.output.withUnsafeBytes {
        write(connection.fd, $0.baseAddress, $0.count)
      }
      if count < 0 {
        if errno == EINTR { continue }
        if errno == EAGAIN || errno == EWOULDBLOCK {
          if connection.writeSource == nil {
            let source = DispatchSource.makeWriteSource(fileDescriptor: connection.fd, queue: .main)
            source.setEventHandler { [weak self] in self?.flush(id) }
            connection.sourceCount += 1
            source.setCancelHandler { connection.sourceCancelled() }
            connection.writeSource = source
            source.resume()
          }
          return
        }
        disconnect(id)
        return
      }
      if count == 0 {
        disconnect(id)
        return
      }
      connection.output.removeFirst(count)
    }
    connection.writeSource?.cancel()
    connection.writeSource = nil
  }
  private func revoke(id: String, session: String, request: String, reason: String) throws {
    guard let registry else { return }
    try send(
      .attachRevoke(
        .init(
          type: "attach_revoke", version: 1, requestId: request, brokerId: registry.instanceID,
          sessionId: session, reason: reason)), to: id)
  }
  private func finish(
    _ request: String, connectionID: String? = nil,
    outcome: AppshotTransactionOutcome = .failed(.captureCancelled)
  ) {
    if let connectionID, captureBindings[request]?.connectionID != connectionID { return }
    captureBindings.removeValue(forKey: request)
    let completion = completions.removeValue(forKey: request)
    cancellations.removeValue(forKey: request)?()
    if let completion {
      onOutcome(request, outcome)
      completion()
    }
  }
  private func disconnect(_ id: String, reason: AppshotErrorCode = .recipientDisconnected) {
    guard let connection = connections.removeValue(forKey: id) else { return }
    connection.close()
    registry?.disconnect(connectionID: id)
    for binding in Array(captureBindings.values) where binding.connectionID == id {
      finish(binding.requestID, outcome: .failed(reason))
    }
    // A capture belongs to one client; disconnect is a terminal capture outcome.
    if registry?.connectedCount == 0 {
      controller.stop()
      registration = "unavailable"
      for request in Array(completions.keys) { finish(request) }
      if graceDeadline == nil { graceDeadline = processes.monotonicNS() &+ 2_000_000_000 }
    }
  }
  /// Deterministic clock seam; production invokes this every 50 ms.
  public func poll() {
    guard !stopped, let registry else { return }
    do { try runtime.verifyAuthority() } catch {
      stop()
      return
    }
    let now = processes.monotonicNS()
    for id in registry.deadConnections() { disconnect(id) }
    for id in registry.confirmationExpiredConnections() { disconnect(id, reason: .captureTimeout) }
    for (id, connection) in Array(connections)
    where !connection.authenticated && now >= connection.opened &+ 2_000_000_000 { disconnect(id) }
    for binding in registry.expire() {
      try? revoke(
        id: binding.connectionID, session: binding.sessionID, request: binding.requestID,
        reason: "timeout")
      finish(binding.requestID, outcome: .failed(.captureTimeout))
    }
    if let deadline = graceDeadline, now >= deadline, registry.connectedCount == 0 { stop() }
  }
}

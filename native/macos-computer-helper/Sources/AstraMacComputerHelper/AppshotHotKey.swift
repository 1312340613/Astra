import Carbon
import Foundation
import AstraAppshotCore

public final class AppshotHotKeyToken {
  public let chord: AppshotChord
  private let cancellation: () -> Void
  private let lock = NSLock()
  private var cancelled = false

  public init(chord: AppshotChord, cancellation: @escaping () -> Void) {
    self.chord = chord
    self.cancellation = cancellation
  }

  deinit { cancel() }

  public func cancel() {
    lock.lock()
    guard !cancelled else {
      lock.unlock()
      return
    }
    cancelled = true
    lock.unlock()
    cancellation()
  }
}

/// Schedule capture work and invoke completion exactly when that transaction terminates.
/// The Carbon callback must return promptly; completion can arrive asynchronously.
public typealias AppshotCaptureHandler = (@escaping () -> Void) -> Void

public protocol AppshotHotKeyRegistering: AnyObject {
  func register(_ chord: AppshotChord, handler: @escaping AppshotCaptureHandler) throws
    -> AppshotHotKeyToken
  func unregister(_ token: AppshotHotKeyToken)
}

public final class AppshotHotKeyTriggerGate {
  private let lock = NSLock()
  private var held: Set<UInt32> = []
  private var captureInFlight = false

  public init() {}

  public func beginPress(isRepeat: Bool, identifier: UInt32 = 0) -> Bool {
    lock.lock()
    defer { lock.unlock() }
    let wasHeld = !held.insert(identifier).inserted
    guard !isRepeat, !wasHeld, !captureInFlight else { return false }
    captureInFlight = true
    return true
  }

  public func release(identifier: UInt32 = 0) {
    lock.lock()
    held.remove(identifier)
    lock.unlock()
  }

  public func captureFinished() {
    lock.lock()
    captureInFlight = false
    lock.unlock()
  }
}

/// Single-use completion prevents a delayed duplicate from finishing a newer capture.
private final class AppshotCaptureCompletion {
  private let lock = NSLock()
  private var gate: AppshotHotKeyTriggerGate?

  init(gate: AppshotHotKeyTriggerGate) { self.gate = gate }

  func finish() {
    lock.lock()
    defer { lock.unlock() }
    gate?.captureFinished()
    gate = nil
  }
}

protocol AppshotCarbonAPI: AnyObject {
  func install(
    handler: EventHandlerUPP, context: UnsafeMutableRawPointer,
    reference: inout EventHandlerRef?
  ) -> OSStatus
  func register(
    chord: AppshotChord, identifier: UInt32,
    reference: inout EventHotKeyRef?
  ) -> OSStatus
  func unregister(_ reference: EventHotKeyRef)
  func removeHandler(_ reference: EventHandlerRef)
}

private final class SystemAppshotCarbonAPI: AppshotCarbonAPI {
  func install(
    handler: EventHandlerUPP, context: UnsafeMutableRawPointer,
    reference: inout EventHandlerRef?
  ) -> OSStatus {
    var specifications = [
      EventTypeSpec(
        eventClass: OSType(kEventClassKeyboard), eventKind: UInt32(kEventHotKeyPressed)),
      EventTypeSpec(
        eventClass: OSType(kEventClassKeyboard), eventKind: UInt32(kEventHotKeyReleased)),
    ]
    return InstallEventHandler(
      GetApplicationEventTarget(), handler, specifications.count,
      &specifications, context, &reference)
  }

  func register(
    chord: AppshotChord, identifier: UInt32,
    reference: inout EventHotKeyRef?
  ) -> OSStatus {
    RegisterEventHotKey(
      chord.virtualKeyCode, chord.carbonModifiers,
      EventHotKeyID(signature: CarbonAppshotHotKeyRegistrar.signature, id: identifier),
      GetApplicationEventTarget(), OptionBits(kEventHotKeyExclusive), &reference)
  }

  func unregister(_ reference: EventHotKeyRef) { UnregisterEventHotKey(reference) }
  func removeHandler(_ reference: EventHandlerRef) { RemoveEventHandler(reference) }
}

public final class CarbonAppshotHotKeyRegistrar: AppshotHotKeyRegistering {
  fileprivate static let signature: OSType = 0x4150_5348  // APSH
  private static let shared = CarbonHotKeyEventRouter(api: SystemAppshotCarbonAPI())
  private let router: CarbonHotKeyEventRouter

  public init() { router = Self.shared }
  init(api: AppshotCarbonAPI) { router = CarbonHotKeyEventRouter(api: api) }

  public func register(_ chord: AppshotChord, handler: @escaping AppshotCaptureHandler) throws
    -> AppshotHotKeyToken
  {
    try router.register(chord, handler: handler)
  }

  public func unregister(_ token: AppshotHotKeyToken) { token.cancel() }

  // Deterministic dispatch seam exercises the same path as Carbon, without registration.
  func dispatch(identifier: UInt32, pressed: Bool) {
    router.dispatch(identifier: identifier, pressed: pressed)
  }
}

private final class CarbonHotKeyEventRouter {
  private let api: AppshotCarbonAPI
  private let lock = NSLock()
  private var nextID: UInt32 = 1
  private var entries: [UInt32: AppshotCaptureHandler] = [:]
  private var eventHandler: EventHandlerRef?
  // Process-wide for production: replacing or stopping a token does not end a capture.
  private let gate = AppshotHotKeyTriggerGate()

  init(api: AppshotCarbonAPI) { self.api = api }

  deinit {
    if let eventHandler { api.removeHandler(eventHandler) }
  }

  func register(_ chord: AppshotChord, handler: @escaping AppshotCaptureHandler) throws
    -> AppshotHotKeyToken
  {
    lock.lock()
    defer { lock.unlock() }
    if eventHandler == nil {
      var installed: EventHandlerRef?
      let result = api.install(
        handler: { _, event, context in
          guard let event, let context else { return OSStatus(eventNotHandledErr) }
          return Unmanaged<CarbonHotKeyEventRouter>.fromOpaque(context).takeUnretainedValue()
            .handle(event)
        }, context: Unmanaged.passUnretained(self).toOpaque(), reference: &installed)
      guard result == noErr, let installed else {
        if let installed { api.removeHandler(installed) }
        throw AppshotError.hotKeyRegistrationFailed
      }
      eventHandler = installed
    }
    guard nextID != 0 else { throw AppshotError.hotKeyRegistrationFailed }
    let identifier = nextID
    nextID &+= 1
    var reference: EventHotKeyRef?
    let result = api.register(chord: chord, identifier: identifier, reference: &reference)
    if result == eventHotKeyExistsErr { throw AppshotError.shortcutConflict }
    guard result == noErr, let reference else { throw AppshotError.hotKeyRegistrationFailed }
    entries[identifier] = handler
    return AppshotHotKeyToken(chord: chord) { [self] in
      lock.lock()
      entries.removeValue(forKey: identifier)
      gate.release(identifier: identifier)
      lock.unlock()
      api.unregister(reference)
    }
  }

  func dispatch(identifier: UInt32, pressed: Bool) {
    lock.lock()
    guard let handler = entries[identifier] else {
      lock.unlock()
      return
    }
    if !pressed {
      gate.release(identifier: identifier)
      lock.unlock()
      return
    }
    let admitted = gate.beginPress(isRepeat: false, identifier: identifier)
    lock.unlock()
    if admitted {
      let completion = AppshotCaptureCompletion(gate: gate)
      handler { completion.finish() }
    }
  }

  private func handle(_ event: EventRef) -> OSStatus {
    var identifier = EventHotKeyID()
    guard
      GetEventParameter(
        event, EventParamName(kEventParamDirectObject),
        EventParamType(typeEventHotKeyID), nil, MemoryLayout<EventHotKeyID>.size, nil,
        &identifier) == noErr,
      identifier.signature == CarbonAppshotHotKeyRegistrar.signature
    else { return OSStatus(eventNotHandledErr) }
    dispatch(identifier: identifier.id, pressed: GetEventKind(event) == UInt32(kEventHotKeyPressed))
    return noErr
  }
}

public final class AppshotShortcutController {
  private let registrar: AppshotHotKeyRegistering
  private let store: AppshotSettingsStoring
  private var token: AppshotHotKeyToken?
  private var handler: AppshotCaptureHandler?
  public private(set) var settings: AppshotSettings
  public let loadWarning: AppshotSettingsWarning?

  public init(registrar: AppshotHotKeyRegistering, store: AppshotSettingsStoring) throws {
    self.registrar = registrar
    self.store = store
    let result = try store.load()
    settings = result.settings
    loadWarning = result.warning
  }

  public func start(handler: @escaping AppshotCaptureHandler) throws {
    guard token == nil else { return }
    self.handler = handler
    guard settings.enabled else { return }
    token = try registrar.register(settings.shortcut, handler: handler)
  }

  deinit { stop() }

  public func stop() {
    if let token { registrar.unregister(token) }
    token = nil
    handler = nil
  }

  public func replace(with chord: AppshotChord) throws {
    guard chord != settings.shortcut else { return }
    let oldSettings = settings
    let oldToken = token
    var newToken: AppshotHotKeyToken?
    if oldSettings.enabled, oldToken != nil, let handler {
      newToken = try registrar.register(chord, handler: handler)
    }
    var updated = oldSettings
    updated.shortcut = chord
    do {
      try store.save(updated)
    } catch {
      if let newToken { registrar.unregister(newToken) }
      throw error
    }
    settings = updated
    token = newToken
    if let oldToken { registrar.unregister(oldToken) }
  }

  public func setEnabled(_ enabled: Bool) throws {
    guard enabled != settings.enabled else { return }
    var updated = settings
    updated.enabled = enabled
    if enabled {
      var newToken: AppshotHotKeyToken?
      if let handler {
        newToken = try registrar.register(settings.shortcut, handler: handler)
      }
      do {
        try store.save(updated)
      } catch {
        if let newToken { registrar.unregister(newToken) }
        throw error
      }
      settings = updated
      token = newToken
    } else {
      try store.save(updated)
      settings = updated
      if let token { registrar.unregister(token) }
      token = nil
    }
  }
}

extension AppshotChord {
  fileprivate var carbonModifiers: UInt32 {
    modifiers.reduce(into: UInt32(0)) { result, modifier in
      switch modifier {
      case .control: result |= UInt32(controlKey)
      case .option: result |= UInt32(optionKey)
      case .shift: result |= UInt32(shiftKey)
      case .command: result |= UInt32(cmdKey)
      }
    }
  }

  fileprivate var virtualKeyCode: UInt32 {
    let codes: [Character: UInt32] = [
      "A": 0, "S": 1, "D": 2, "F": 3, "H": 4, "G": 5, "Z": 6, "X": 7,
      "C": 8, "V": 9, "B": 11, "Q": 12, "W": 13, "E": 14, "R": 15,
      "Y": 16, "T": 17, "1": 18, "2": 19, "3": 20, "4": 21, "6": 22,
      "5": 23, "9": 25, "7": 26, "8": 28, "0": 29, "O": 31, "U": 32,
      "I": 34, "P": 35, "L": 37, "J": 38, "K": 40, "N": 45, "M": 46,
    ]
    return codes[key]!
  }
}

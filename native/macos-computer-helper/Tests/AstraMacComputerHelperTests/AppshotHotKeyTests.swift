import Carbon
import Foundation
import Testing

@testable import AstraMacComputerHelperCore

@Suite struct AppshotHotKeyTests {
  @Test func failedRegistrationKeepsOldRegistrationAndFile() throws {
    let registrar = FakeHotKeyRegistrar()
    let store = FakeSettingsStore(current: .default)
    let controller = try AppshotShortcutController(registrar: registrar, store: store)
    try controller.start { _ in }
    registrar.failure = .shortcutConflict
    #expect(throws: AppshotError.shortcutConflict) {
      try controller.replace(with: AppshotChord.parse("Control+Option+K"))
    }
    #expect(registrar.activeChords == [.default])
    #expect(store.saved.isEmpty)
  }

  @Test func persistenceFailureRollsBackNewRegistration() throws {
    let registrar = FakeHotKeyRegistrar()
    let store = FakeSettingsStore(current: .default)
    let controller = try AppshotShortcutController(registrar: registrar, store: store)
    try controller.start { _ in }
    store.saveFailure = .settingsIO
    #expect(throws: AppshotError.settingsIO) {
      try controller.replace(with: AppshotChord.parse("Control+Option+K"))
    }
    #expect(registrar.activeChords == [.default])
    #expect(registrar.unregisteredChords == [try AppshotChord.parse("Control+Option+K")])
    #expect(controller.settings == .default)
  }

  @Test func successfulReplacementPersistsBeforeOldUnregisterAndSameChordIsNoOp() throws {
    var events: [String] = []
    let registrar = FakeHotKeyRegistrar(events: { events.append($0) })
    let store = FakeSettingsStore(current: .default, events: { events.append($0) })
    let controller = try AppshotShortcutController(registrar: registrar, store: store)
    try controller.start { _ in }
    events.removeAll()
    let chord = try AppshotChord.parse("Control+Option+K")
    try controller.replace(with: chord)
    #expect(
      events == [
        "register Control+Option+K", "save Control+Option+K", "unregister Control+Shift+Z",
      ])
    try controller.replace(with: chord)
    #expect(events.count == 3)
  }

  @Test func enablingRegistrationFailureAndDisablingPersistenceFailurePreserveState() throws {
    let disabled = AppshotSettings(enabled: false, shortcut: .default)
    let registrar = FakeHotKeyRegistrar()
    let disabledStore = FakeSettingsStore(current: disabled)
    let disabledController = try AppshotShortcutController(
      registrar: registrar, store: disabledStore)
    try disabledController.start { _ in }
    registrar.failure = .shortcutConflict
    #expect(throws: AppshotError.shortcutConflict) { try disabledController.setEnabled(true) }
    #expect(disabledController.settings == disabled)
    #expect(disabledStore.saved.isEmpty)

    registrar.failure = nil
    let enabledStore = FakeSettingsStore(current: .default)
    let enabledController = try AppshotShortcutController(registrar: registrar, store: enabledStore)
    try enabledController.start { _ in }
    enabledStore.saveFailure = .settingsIO
    #expect(throws: AppshotError.settingsIO) { try enabledController.setEnabled(false) }
    #expect(enabledController.settings == .default)
    #expect(registrar.activeChords == [.default])
  }

  @Test func handlerInstallationFailureDoesNotAcquireChordAndCanRetry() throws {
    let api = FakeAppshotCarbonAPI()
    let registrar = CarbonAppshotHotKeyRegistrar(api: api)
    api.installStatus = OSStatus(eventNotHandledErr)
    #expect(throws: AppshotError.hotKeyRegistrationFailed) {
      _ = try registrar.register(.default) { _ in }
    }
    #expect(api.registeredIDs.isEmpty)
    api.installStatus = noErr
    let token = try registrar.register(.default) { _ in }
    #expect(api.installCount == 2)
    #expect(api.registeredIDs.count == 1)
    token.cancel()
  }

  @Test func asyncCaptureGateSurvivesReplacementAndRemembersBusyPress() throws {
    let api = FakeAppshotCarbonAPI()
    let registrar = CarbonAppshotHotKeyRegistrar(api: api)
    let controller = try AppshotShortcutController(
      registrar: registrar, store: FakeSettingsStore(current: .default))
    var completions: [() -> Void] = []
    try controller.start { completions.append($0) }
    let first = try #require(api.registeredIDs.last)
    registrar.dispatch(identifier: first, pressed: true)
    #expect(completions.count == 1)
    registrar.dispatch(identifier: first, pressed: false)
    try controller.replace(with: AppshotChord.parse("Control+Option+K"))
    let second = try #require(api.registeredIDs.last)
    registrar.dispatch(identifier: second, pressed: true)
    #expect(completions.count == 1)
    completions[0]()
    registrar.dispatch(identifier: second, pressed: true)
    #expect(completions.count == 1)
    registrar.dispatch(identifier: second, pressed: false)
    registrar.dispatch(identifier: second, pressed: true)
    #expect(completions.count == 2)
    // A duplicate old completion must not release the newer capture.
    completions[0]()
    controller.stop()
    try controller.start { completions.append($0) }
    let third = try #require(api.registeredIDs.last)
    registrar.dispatch(identifier: third, pressed: true)
    #expect(completions.count == 2)
    completions[1]()
    registrar.dispatch(identifier: third, pressed: true)
    #expect(completions.count == 2)
    registrar.dispatch(identifier: third, pressed: false)
    registrar.dispatch(identifier: third, pressed: true)
    #expect(completions.count == 3)
    completions[2]()
    controller.stop()
  }

  @Test func droppingControllerCancelsRegistration() throws {
    let api = FakeAppshotCarbonAPI()
    let registrar = CarbonAppshotHotKeyRegistrar(api: api)
    var controller: AppshotShortcutController? = try AppshotShortcutController(
      registrar: registrar, store: FakeSettingsStore(current: .default))
    try controller?.start { _ in }
    controller = nil
    #expect(api.unregisteredCount == 1)
  }

  @Test func busyPressRemainsHeldAfterPriorCaptureFinishes() {
    let gate = AppshotHotKeyTriggerGate()
    #expect(gate.beginPress(isRepeat: false))
    gate.release()
    #expect(!gate.beginPress(isRepeat: false))
    gate.captureFinished()
    #expect(!gate.beginPress(isRepeat: false))
    gate.release()
    #expect(gate.beginPress(isRepeat: false))
  }

  @Test func triggerGateSuppressesRepeatUntilReleaseAndConcurrentCapture() {
    let gate = AppshotHotKeyTriggerGate()
    #expect(gate.beginPress(isRepeat: false))
    gate.captureFinished()
    #expect(!gate.beginPress(isRepeat: true))
    #expect(!gate.beginPress(isRepeat: false))
    gate.release()
    #expect(gate.beginPress(isRepeat: false))
    #expect(!gate.beginPress(isRepeat: false))
    gate.release()
    gate.captureFinished()
    #expect(gate.beginPress(isRepeat: false))
  }

}

private final class FakeSettingsStore: AppshotSettingsStoring {
  var current: AppshotSettings
  var saved: [AppshotSettings] = []
  var saveFailure: AppshotError?
  let events: (String) -> Void
  init(current: AppshotSettings, events: @escaping (String) -> Void = { _ in }) {
    self.current = current
    self.events = events
  }
  func load() throws -> AppshotSettingsLoadResult { .init(settings: current, warning: nil) }
  func save(_ settings: AppshotSettings) throws {
    events("save \(settings.shortcut)")
    if let saveFailure { throw saveFailure }
    saved.append(settings)
    current = settings
  }
}

private final class FakeHotKeyRegistrar: AppshotHotKeyRegistering {
  var failure: AppshotError?
  var activeChords: [AppshotChord] = []
  var unregisteredChords: [AppshotChord] = []
  let events: (String) -> Void
  init(events: @escaping (String) -> Void = { _ in }) { self.events = events }
  func register(_ chord: AppshotChord, handler: @escaping AppshotCaptureHandler) throws
    -> AppshotHotKeyToken
  {
    events("register \(chord)")
    if let failure { throw failure }
    activeChords.append(chord)
    return AppshotHotKeyToken(
      chord: chord,
      cancellation: { [weak self] in
        self?.activeChords.removeAll { $0 == chord }
        self?.unregisteredChords.append(chord)
      })
  }
  func unregister(_ token: AppshotHotKeyToken) {
    events("unregister \(token.chord)")
    token.cancel()
  }
}

private final class FakeAppshotCarbonAPI: AppshotCarbonAPI {
  var installStatus: OSStatus = noErr
  var installCount = 0
  var registeredIDs: [UInt32] = []
  var unregisteredCount = 0
  func install(
    handler: EventHandlerUPP, context: UnsafeMutableRawPointer,
    reference: inout EventHandlerRef?
  ) -> OSStatus {
    installCount += 1
    if installStatus == noErr { reference = EventHandlerRef(bitPattern: 1) }
    return installStatus
  }
  func register(
    chord: AppshotChord, identifier: UInt32,
    reference: inout EventHotKeyRef?
  ) -> OSStatus {
    registeredIDs.append(identifier)
    reference = EventHotKeyRef(bitPattern: Int(identifier))
    return noErr
  }
  func unregister(_ reference: EventHotKeyRef) { unregisteredCount += 1 }
  func removeHandler(_ reference: EventHandlerRef) {}
}

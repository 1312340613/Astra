@testable import AstraMacComputerHelperCore
@preconcurrency import ApplicationServices
import Foundation
import Testing

private final class ReadinessFixture: AXReadinessProviding {
    var value: Bool? = false
    var settable = true
    var valueAfterWrite: Bool? = true
    var writeResult: AXError = .success
    var writes = 0
    var reads = 0
    var supportReads = 0
    func enabled(_ application: AXUIElement) -> Bool? { reads += 1; return value }
    func isSettable(_ application: AXUIElement) -> Bool { supportReads += 1; return settable }
    func enable(_ application: AXUIElement) -> AXError {
        writes += 1
        value = valueAfterWrite
        return writeResult
    }
}

private func readinessBudget(_ duration: TimeInterval = 12) -> AXObservationBudget {
    AXObservationBudget(duration: duration, setTimeout: { _, _ in .success })
}

@Test(arguments: [AXError.success, .notImplemented, .cannotComplete])
func observationReadinessUsesReadbackEvenAfterError(result: AXError) throws {
    let provider = ReadinessFixture()
    provider.writeResult = result
    var settles: [TimeInterval] = []
    let readiness = AXObservationReadiness(provider: provider, settle: { settles.append($0) })
    #expect(try readiness.prepare(application: AXUIElementCreateApplication(42), pid: 42,
        launchedAt: 1, budget: readinessBudget()) == .enabled)
    #expect(provider.writes == 1)
    #expect(provider.reads == 2)
    #expect(settles == [2.25])
}

@Test(arguments: ["enabled", "unsupported", "not_settable"])
func observationReadinessAvoidsUnnecessaryWrites(reason: String) throws {
    let provider = ReadinessFixture()
    provider.value = reason == "enabled" ? true : (reason == "unsupported" ? nil : false)
    provider.settable = false
    let readiness = AXObservationReadiness(provider: provider, settle: { _ in Issue.record("unexpected settle") })
    let result = try readiness.prepare(application: AXUIElementCreateApplication(42), pid: 42,
        launchedAt: 1, budget: readinessBudget())
    #expect(result == (reason == "enabled" ? .alreadyEnabled : .unavailable))
    #expect(provider.writes == 0)
}

@Test(arguments: [nil, false] as [Bool?])
func observationReadinessDoesNotTreatWriteSuccessAsReadiness(readback: Bool?) throws {
    let provider = ReadinessFixture()
    provider.valueAfterWrite = readback
    let readiness = AXObservationReadiness(provider: provider, settle: { _ in Issue.record("unconfirmed settle") })
    let application = AXUIElementCreateApplication(42)
    #expect(try readiness.prepare(application: application, pid: 42, launchedAt: 1,
        budget: readinessBudget()) == .unconfirmed)
    provider.value = false
    #expect(try readiness.prepare(application: application, pid: 42, launchedAt: 1,
        budget: readinessBudget()) == .alreadyRequested)
    #expect(provider.writes == 1)
}

@Test func observationReadinessRenegotiatesOnlyForNewApplicationInstance() throws {
    let provider = ReadinessFixture()
    provider.valueAfterWrite = false
    let readiness = AXObservationReadiness(provider: provider, settle: { _ in })
    let application = AXUIElementCreateApplication(42)
    for launchedAt in [1.0, 1.0, 2.0, 2.0] {
        _ = try readiness.prepare(application: application, pid: 42, launchedAt: launchedAt,
            budget: readinessBudget())
    }
    #expect(provider.writes == 2)
}

@Test(arguments: [0.0, 1.0])
func observationReadinessHonorsDeadline(duration: TimeInterval) {
    let provider = ReadinessFixture()
    let readiness = AXObservationReadiness(provider: provider, settle: { _ in Issue.record("out-of-budget settle") })
    #expect(throws: WindowObservationError.self) {
        _ = try readiness.prepare(application: AXUIElementCreateApplication(42), pid: 42, launchedAt: 1,
            budget: readinessBudget(duration))
    }
    #expect(provider.writes == (duration == 0 ? 0 : 1))
}

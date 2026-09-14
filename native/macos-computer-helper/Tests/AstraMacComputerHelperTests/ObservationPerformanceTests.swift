import ApplicationServices
import Foundation
import Testing
@testable import AstraMacComputerHelperCore

@Test func observationInventoryLoadsEachPIDOnceAndDoesNotSurviveRequests() {
    var loads = 0
    let inventory = ObservationPIDInventory<[Int]>()
    for _ in 0..<20 {
        #expect(inventory.value(for: 42) { loads += 1; return [1, 2] } == [1, 2])
    }
    #expect(loads == 1)
    _ = inventory.value(for: 43) { loads += 1; return [] }
    #expect(loads == 2)
    _ = ObservationPIDInventory<[Int]>().value(for: 42) { loads += 1; return [3] }
    #expect(loads == 3)
}

@Test func observationBudgetStopsCallsAndRestoresMessagingTimeout() {
    var now = 0.0
    var timeouts: [Float] = []
    var calls = 0
    let element = AXUIElementCreateApplication(42)
    let budget = AXObservationBudget(duration: 1, clock: { now }, setTimeout: { _, timeout in
        timeouts.append(timeout); return .success
    })
    let value: Int? = budget.call(element: element) { calls += 1; now = 2; return 7 }
    #expect(value == nil)
    let second: Int? = budget.call(element: element) { calls += 1; return 8 }
    #expect(second == nil)
    #expect(calls == 1)
    #expect(timeouts == [0.25, 0])
    #expect(budget.expired)
}

@Test func observationCacheReusesOnlyGeometryAndNeverSecurityOrTextAttributes() {
    let element = AXUIElementCreateApplication(42)
    let cache = AXObservationAttributeCache()
    var reads: [String: Int] = [:]
    for attribute in [kAXEnabledAttribute, kAXRoleAttribute, kAXSubroleAttribute, kAXValueAttribute] {
        for _ in 0..<2 {
            _ = cache.read(element: element, attribute: attribute) {
                reads[attribute, default: 0] += 1
                return (.success, kCFBooleanTrue)
            }
        }
    }
    #expect(reads[kAXEnabledAttribute] == 1)
    #expect(reads[kAXRoleAttribute] == 2)
    #expect(reads[kAXSubroleAttribute] == 2)
    #expect(reads[kAXValueAttribute] == 2)
}

@Test func helperBuildIdentityRejectsPayloadTextAndMalformedHashes() throws {
    let build = helperBuildIdentity(data: Data("{\"helper_git_revision\":\"secret title\",\"build_id\":\"path/private\",\"extra\":\"private\"}".utf8))
    let encoder = JSONEncoder(); encoder.outputFormatting = [.sortedKeys]
    #expect(try encoder.encode(build) == encoder.encode(JSONValue.object([
        "helper_git_revision": .string("unknown"),
        "compatibility_registry_sha256": .string("unknown"),
        "build_id": .string("unknown"),
        "helper_source_dirty": .bool(true),
    ])))
}

@Test func ordinaryTraversalStopsBeforeContentAfterClockExhaustion() {
    var clock = 0.0
    let provider = SlowObservationProvider { clock += 0.6 }
    let budget = AXObservationBudget(duration: 1, clock: { clock }, setTimeout: { _, _ in .success })
    let node = AXNodeReader.read(provider: provider, windowBounds: .zero, budget: budget)
    #expect(provider.identityReads == 2)
    #expect(provider.contentReads == 0)
    #expect(provider.otherReads == 0)
    #expect(node.sourceElement == nil)
    #expect(budget.expired)
    #expect(AXObservationBudget.current == nil)
}

@Test func observationBudgetCannotOutliveOperationAndTimeoutSetupFailureReadsNothing() throws {
    var clock = 0.0
    let budget = AXObservationBudget(duration: 12, clock: { clock }, setTimeout: { _, _ in .failure })
    clock = 10
    #expect(try budget.phaseTimeout(maximum: 5) == 2)
    var invoked = false
    let value: Int? = budget.call(element: AXUIElementCreateApplication(Int32.max)) { invoked = true; return 1 }
    #expect(value == nil)
    #expect(!invoked)
    #expect(throws: WindowObservationError.self) { try budget.check() }
}

@Test func catalogInventoryPreservesUniqueWindowMatchingAndRejectsAmbiguity() {
    let element = AXUIElementCreateApplication(Int32.max)
    let bounds = CGRect(x: 0, y: 0, width: 800, height: 600)
    let target = WindowTarget(appRef: "a", windowRef: "w", pid: Int32.max, windowID: 1, bounds: bounds, title: "Synthetic")
    let record = CatalogAXWindowRecord(element: element, bounds: bounds, title: BoundedAXStringResult(value: "Synthetic", status: .complete))
    #expect(matchingCatalogAXWindow(target: target, records: [record]) != nil)
    #expect(matchingCatalogAXWindow(target: target, records: [record, record]) == nil)
    let unavailable = CatalogAXWindowRecord(element: element, bounds: bounds, title: BoundedAXStringResult(value: "Synthetic", status: .truncated))
    #expect(matchingCatalogAXWindow(target: target, records: [unavailable]) == nil)
}

private final class SlowObservationProvider: AXNodeAttributeProvider {
    let advance: () -> Void
    var identityReads = 0
    var contentReads = 0
    var otherReads = 0
    init(advance: @escaping () -> Void) { self.advance = advance }
    func stringValue(for attribute: String) -> BoundedAXStringResult {
        if attribute == kAXRoleAttribute || attribute == kAXSubroleAttribute {
            identityReads += 1; advance()
            return BoundedAXStringResult(value: attribute == kAXRoleAttribute ? "AXTextField" : "AXSecureTextField", status: .complete)
        }
        contentReads += 1
        return BoundedAXStringResult(value: "must not be read", status: .complete)
    }
    func boolValue(for attribute: String) -> Bool? { otherReads += 1; return true }
    func bounds() -> CGRect { otherReads += 1; return .zero }
    func actions() -> [BoundedAXStringResult] { otherReads += 1; return [] }
    func children(remaining: Int) -> [any AXNodeAttributeProvider] { otherReads += 1; return [] }
    func sourceElement() -> AXUIElement? { otherReads += 1; return nil }
}

@Test func observationInventoryCachesMissingOptionalMetadata() {
    let inventory = ObservationPIDInventory<String?>()
    var loads = 0
    for _ in 0..<20 {
        let version = inventory.value(for: 42) { loads += 1; return nil }
        #expect(version == nil)
    }
    #expect(loads == 1)
}

@Test func externalObservationBudgetSharesDeadlineAndCancellation() throws {
    var remaining = 0.2
    var timeouts: [Float] = []
    let budget = AXObservationBudget(remainingBudget: { remaining }, setTimeout: { _, timeout in
        timeouts.append(timeout); return .success
    })
    let root = AXUIElementCreateApplication(42)
    #expect(try budget.phaseTimeout(maximum: 1) == 0.2)
    #expect(budget.call(element: root) { true } == true)
    #expect(timeouts.first! <= 0.2)
    remaining = 0.01
    #expect(try budget.phaseTimeout(maximum: 1) == 0.01)
    remaining = 0
    #expect(!budget.available)
    #expect(budget.call(element: root) { Issue.record("expired budget entered AX"); return true } == nil)
    #expect(throws: (any Error).self) { try budget.phaseTimeout(maximum: 1) }
}

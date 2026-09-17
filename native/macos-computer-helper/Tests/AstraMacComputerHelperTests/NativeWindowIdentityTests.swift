import ApplicationServices
import Foundation
import Testing
@testable import AstraMacComputerHelperCore

private let bounds = CGRect(x: 0, y: 30, width: 1352, height: 768)
private let title = BoundedAXStringResult(value: "New tab", status: .complete)
private let first = AXUIElementCreateApplication(42)
private let second = AXUIElementCreateApplication(43)
private func target(_ id: CGWindowID = 100, element: AXUIElement? = nil) -> WindowTarget {
    WindowTarget(appRef: "app", windowRef: "window", pid: 42, windowID: id,
        bounds: bounds, title: "New tab", axIdentity: element.map(CFHash), axElement: element)
}
private func record(_ element: AXUIElement, _ id: CGWindowID?) -> CatalogAXWindowRecord {
    CatalogAXWindowRecord(element: element, bounds: bounds, title: title, windowID: id)
}
private func screen(_ id: CGWindowID, pid: pid_t = 42, frame: CGRect = bounds,
                    name: String? = "New tab", visible: Bool = true) -> PIDScreenCaptureWindowObservation {
    PIDScreenCaptureWindowObservation(pid: pid, windowID: id, bounds: frame,
        title: name, isOnScreen: visible)
}

@Test func sameTitleAndGeometryAreDisambiguatedByNativeWindowIdentity() {
    let records = [record(first, 100), record(second, 101)]
    #expect(matchingCatalogAXWindow(target: target(), records: records).map { CFEqual($0, first) } == true)
    #expect(matchingCatalogAXWindow(target: target(101), records: records).map { CFEqual($0, second) } == true)
    // The old metadata-only path must still reject exactly this ambiguity.
    #expect(matchingCatalogAXWindow(target: target(), records: [record(first, nil), record(second, nil)]) == nil)
}

@Test func explicitNativeIdentityCannotFallBackToIdenticalSiblingMetadata() {
    #expect(matchingCatalogAXWindow(target: target(), records: [record(second, 101)]) == nil)
    #expect(matchingCatalogAXWindow(target: target(), records: [record(first, 100), record(second, 100)]) == nil)
    #expect(matchingCatalogAXWindow(target: target(0), records: [record(first, 0)]) == nil)
    #expect(matchingCatalogAXWindow(target: target(element: second), records: [record(first, 100)]) == nil)
    var wrongFrame = record(first, 100)
    wrongFrame = CatalogAXWindowRecord(element: wrongFrame.element,
        bounds: bounds.offsetBy(dx: 100, dy: 0), title: title, windowID: 100)
    #expect(matchingCatalogAXWindow(target: target(), records: [wrongFrame]) == nil)
}

@Test func nativeWindowIdentitySurvivesTitlePublicationLag() {
    let staleTitle = CatalogAXWindowRecord(element: first, bounds: bounds,
        title: BoundedAXStringResult(value: "Previous page", status: .complete), windowID: 100)
    #expect(matchingCatalogAXWindow(target: target(), records: [staleTitle]).map { CFEqual($0, first) } == true)
    let incomplete = CatalogAXWindowRecord(element: first, bounds: bounds,
        title: BoundedAXStringResult(value: nil, status: .failed), windowID: 100)
    #expect(matchingCatalogAXWindow(target: target(), records: [incomplete]).map { CFEqual($0, first) } == true)
    #expect(matchingCatalogAXWindow(target: target(), records: [record(first, nil)]).map { CFEqual($0, first) } == true)
}

@Test func reverseMappingUsesNativeIDAndPreservesOwnershipGeometryVisibility() {
    func mapped(_ windows: [PIDScreenCaptureWindowObservation], id: CGWindowID? = 100) -> CGWindowID? {
        matchingScreenWindow(pid: 42, bounds: bounds, title: title, windowID: id, windows: windows)?.windowID
    }
    #expect(mapped([screen(100), screen(101)]) == 100)
    #expect(mapped([screen(100, name: nil), screen(101)]) == 100)
    #expect(mapped([screen(101)]) == nil)
    #expect(mapped([screen(100), screen(100)]) == nil)
    #expect(mapped([screen(100, pid: 43), screen(101)]) == nil)
    #expect(mapped([screen(100, visible: false), screen(101)]) == nil)
    #expect(mapped([screen(100, frame: bounds.offsetBy(dx: 20, dy: 0)), screen(101)]) == nil)
    #expect(mapped([screen(0)], id: 0) == nil)
    #expect(mapped([screen(100), screen(101)], id: nil) == nil)
    #expect(mapped([screen(100)], id: nil) == 100)
}

@Test func focusedIdenticalWindowMustProveItsExactNativeIdentity() {
    let exact = ActionTargetState(pid: 42, windowID: 100, bounds: bounds,
        axIdentity: CFHash(first), focusedAXIdentity: CFHash(first), focusedAXBounds: bounds)
    func state(id: CGWindowID? = 100, identity: CFHashCode = CFHash(first), foreground: pid_t? = 42,
               screens: [PIDScreenCaptureWindowObservation] = [screen(100), screen(101)]) -> PIDActionTargetState {
        ForegroundPIDActionStateFactory(focusedAXWindow: {
            PIDAXFocusedWindowObservation(pid: 42, bounds: bounds, axIdentity: identity,
                title: "New tab", windowID: id)
        }, screenCaptureWindows: { screens }).make(target: exact, snapshotID: "snap", frontmostPID: foreground)
    }
    #expect(state().isKeyWindow)
    #expect(!state(id: 101).isKeyWindow)
    #expect(!state(id: nil).isKeyWindow)
    #expect(!state(identity: CFHash(second)).isKeyWindow)
    #expect(!state(screens: [screen(100), screen(100)]).isKeyWindow)
    #expect(!state(screens: [screen(100, visible: false), screen(101)]).isKeyWindow)
    #expect(!state(foreground: 43).isFrontmost)
}

@Test func directlyMappedDuplicateTitleBehindDoesNotBecomeAnUnknownOverlay() throws {
    let roles = BoundedAXStringResult(value: "AXWindow", status: .complete)
    let subroles = BoundedAXStringResult(value: "AXStandardWindow", status: .complete)
    func makeRecord(_ behind: Bool) -> TargetCatalogRecord {
        TargetCatalogRecord(appRef: "app", windowRef: "window", pid: 42, windowID: 100,
            bounds: bounds, title: "New tab", axWindows: [
                TargetAXWindowRecord(windowID: 100, bounds: bounds, identity: CFHash(first),
                    element: first, role: roles, subrole: subroles, zOrder: 2, layer: 0, alpha: 1, isModal: false),
                TargetAXWindowRecord(windowID: 101, bounds: bounds, identity: CFHash(second),
                    element: second, role: roles, subrole: subroles, zOrder: behind ? 3 : 1,
                    layer: 0, alpha: 1, isModal: false),
            ])
    }
    let behind = makeRecord(true)
    let allowed = ClosureTargetCatalog(recordProvider: { _, _ in behind }, currentProvider: { _ in behind })
    #expect(try BackgroundTargetController(catalog: allowed).select(appRef: "app", windowRef: "window").windowID == 100)
    let covering = makeRecord(false)
    let blocked = ClosureTargetCatalog(recordProvider: { _, _ in covering }, currentProvider: { _ in covering })
    do {
        _ = try BackgroundTargetController(catalog: blocked).select(appRef: "app", windowRef: "window")
        Issue.record("actual covering sibling was accepted")
    } catch WindowObservationError.overlayBlocked { }
}

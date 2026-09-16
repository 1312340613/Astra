import ApplicationServices
import Foundation
import Testing
@testable import AstraMacComputerHelperCore

private let unnamed = BoundedAXStringResult(value: nil, status: .complete)
private func complete(_ value: String) -> BoundedAXStringResult {
    BoundedAXStringResult(value: value, status: .complete)
}

@Test func descriptionOnlySheetMatchesCatalogAndForegroundFocusWithoutLosingIdentity() {
    let element = AXUIElementCreateApplication(42)
    let bounds = CGRect(x: 236, y: 190, width: 880, height: 448)
    let name = accessibilityWindowName(element) { _, attribute in
        switch attribute {
        case kAXTitleAttribute: return unnamed
        case kAXRoleAttribute: return complete("AXSheet")
        case kAXDescriptionAttribute: return complete("Open fixture")
        default: Issue.record("unexpected attribute"); return unnamed
        }
    }
    let target = WindowTarget(appRef: "app", windowRef: "panel", pid: 42,
        windowID: 99, bounds: bounds, title: "Open fixture")
    let record = CatalogAXWindowRecord(element: element, bounds: bounds, title: name)
    let matched = matchingCatalogAXWindow(target: target, records: [record])
    #expect(matched.map { CFEqual($0, element) } == true)
    #expect(matchingCatalogAXWindow(target: target, records: [record, record]) == nil)
    let factory = ForegroundPIDActionStateFactory(
        focusedAXWindow: { PIDAXFocusedWindowObservation(pid: 42, bounds: bounds,
            axIdentity: CFHash(element), title: name.value) },
        screenCaptureWindows: { [PIDScreenCaptureWindowObservation(pid: 42,
            windowID: 99, bounds: bounds, title: "Open fixture", isOnScreen: true)] })
    let exact = ActionTargetState(pid: 42, windowID: 99, bounds: bounds,
        axIdentity: CFHash(element), focusedAXIdentity: CFHash(element), focusedAXBounds: bounds)
    #expect(factory.make(target: exact, snapshotID: "snap", frontmostPID: 42).isKeyWindow)
    let replaced = ActionTargetState(pid: 42, windowID: 100, bounds: bounds,
        axIdentity: CFHash(element), focusedAXIdentity: CFHash(element), focusedAXBounds: bounds)
    #expect(!factory.make(target: replaced, snapshotID: "snap", frontmostPID: 42).isKeyWindow)
}

@Test func windowNameNeverReplacesPresentFailedOrTruncatedTitle() {
    for title in [complete("Authoritative title"),
                  BoundedAXStringResult(value: nil, status: .failed),
                  BoundedAXStringResult(value: "Partial", status: .truncated)] {
        var calls: [String] = []
        let result = accessibilityWindowName(AXUIElementCreateApplication(42)) { _, attribute in
            calls.append(attribute)
            return title
        }
        #expect(result.value == title.value)
        #expect(result.status == title.status)
        #expect(calls == [kAXTitleAttribute])
    }
}

@Test func unnamedSheetFocusRequiresProvenOwnershipAndSoleExactCGIdentity() {
    let bounds = CGRect(x: 446, y: 306, width: 460, height: 215)
    let target = ActionTargetState(pid: 42, windowID: 99, bounds: bounds,
        axIdentity: 123, focusedAXIdentity: 123, focusedAXBounds: bounds)
    let screen = PIDScreenCaptureWindowObservation(pid: 42, windowID: 99,
        bounds: bounds, title: "", isOnScreen: true)
    func accepts(proven: Bool = true, title: String? = "", identity: CFHashCode = 123,
                 screens: [PIDScreenCaptureWindowObservation] = [screen]) -> Bool {
        ForegroundPIDActionStateFactory(focusedAXWindow: {
            PIDAXFocusedWindowObservation(pid: 42, bounds: bounds, axIdentity: identity,
                title: title, isProvenSheet: proven)
        }, screenCaptureWindows: { screens }).make(target: target, snapshotID: "snap", frontmostPID: 42).isKeyWindow
    }
    #expect(accepts())
    #expect(!accepts(proven: false))
    #expect(!accepts(title: nil))
    #expect(!accepts(identity: 456))
    #expect(!accepts(screens: [screen, screen]))
    #expect(!accepts(screens: [PIDScreenCaptureWindowObservation(pid: 42, windowID: 100,
        bounds: bounds, title: "", isOnScreen: true)]))
    #expect(!accepts(screens: [PIDScreenCaptureWindowObservation(pid: 42, windowID: 99,
        bounds: bounds, title: nil, isOnScreen: true)]))
}

@Test func onlyProvenSheetMayReadDescriptionAsWindowName() {
    for role in [complete("AXWindow"), complete("AXGroup"), unnamed,
                 BoundedAXStringResult(value: "AXSheet", status: .failed),
                 BoundedAXStringResult(value: "AXSheet", status: .truncated)] {
        var descriptionRead = false
        let result = accessibilityWindowName(AXUIElementCreateApplication(42)) { _, attribute in
            if attribute == kAXTitleAttribute { return unnamed }
            if attribute == kAXRoleAttribute { return role }
            descriptionRead = true
            return complete("Must not become a window title")
        }
        #expect(result.value == nil)
        #expect(!descriptionRead)
    }
}

@Test func incompleteSheetDescriptionCannotMatchEvenAnUntitledScreenWindow() {
    let element = AXUIElementCreateApplication(42)
    let bounds = CGRect(x: 0, y: 0, width: 300, height: 200)
    for description in [BoundedAXStringResult(value: nil, status: .failed),
                        BoundedAXStringResult(value: "Open", status: .truncated)] {
        let name = accessibilityWindowName(element) { _, attribute in
            if attribute == kAXTitleAttribute { return complete("") }
            if attribute == kAXRoleAttribute { return complete("AXSheet") }
            return description
        }
        let record = CatalogAXWindowRecord(element: element, bounds: bounds, title: name)
        let target = WindowTarget(appRef: "app", windowRef: "window", pid: 42,
            windowID: 1, bounds: bounds, title: "")
        #expect(matchingCatalogAXWindow(target: target, records: [record]) == nil)
    }
}

@Test func presentButUnmappedWindowIsNotReportedAsGone() throws {
    let bounds = CGRect(x: 0, y: 0, width: 300, height: 200)
    let element = AXUIElementCreateApplication(42)
    for candidates in [[], [TargetAXWindowRecord(windowID: nil, bounds: bounds,
        identity: CFHash(element), element: element)]] {
        let record = TargetCatalogRecord(appRef: "app", windowRef: "window", pid: 42,
            windowID: 1, bounds: bounds, title: "Present", axWindows: candidates)
        let catalog = ClosureTargetCatalog(recordProvider: { _, _ in record }, currentProvider: { _ in record })
        do {
            _ = try BackgroundTargetController(catalog: catalog).select(appRef: "app", windowRef: "window")
            Issue.record("unmatched window was accepted")
        } catch WindowObservationError.axWindowUnmatched {
            // Present CG identity, no matching AX identity.
        }
    }
    let absent = ClosureTargetCatalog(recordProvider: { _, _ in nil }, currentProvider: { _ in nil })
    do {
        _ = try BackgroundTargetController(catalog: absent).select(appRef: "app", windowRef: "window")
        Issue.record("missing window was accepted")
    } catch WindowObservationError.targetGone {
        // A missing catalog target remains a separate condition.
    }
}

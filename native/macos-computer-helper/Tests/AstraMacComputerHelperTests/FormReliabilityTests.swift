@testable import AstraMacComputerHelperCore
@preconcurrency import ApplicationServices
import CoreGraphics
import Foundation
import Testing

private final class DeepFormProvider: AXNodeAttributeProvider {
    let depth: Int
    let web: Bool
    init(_ depth: Int = 0, web: Bool = true) { self.depth = depth; self.web = web }
    func stringValue(for attribute: String) -> BoundedAXStringResult {
        let value: String?
        switch attribute {
        case kAXRoleAttribute: value = depth == 33 ? "AXRadioButton" : depth == 8 && web ? "AXWebArea" : "AXGroup"
        case kAXValueAttribute: value = depth == 33 ? "1" : nil
        case kAXDescriptionAttribute: value = depth == 33 ? "Deep answer" : nil
        default: value = nil
        }
        return BoundedAXStringResult(value: value, status: .complete)
    }
    func boolValue(for attribute: String) -> Bool? { attribute == kAXEnabledAttribute ? true : false }
    func bounds() -> CGRect { CGRect(x: 10, y: 20, width: 100, height: 30) }
    func actions() -> [BoundedAXStringResult] { [] }
    func children(remaining: Int) -> [any AXNodeAttributeProvider] { depth < 33 ? [DeepFormProvider(depth + 1, web: web)] : [] }
    func sourceElement() -> AXUIElement? { nil }
}

@Test func subtreeProtocolRejectsDisplayBeforeCallingWindowProvider() {
    let response = Dispatcher().handle(
        #"{"protocol_version":4,"request_id":"subtree-display","operation":"snapshot_subtree","payload":{"app_ref":"app","window_ref":"window","snapshot_id":"current","subtree_ref":"web","scope":"display","artifact_name":"snapshot-token.png"}}"#
    )
    #expect(!response.ok)
    #expect(response.error?.message == "snapshot_subtree requires target_window scope")
}

@Test func webFormReaderAndSerializerReachBeyondTheDefaultDepth() {
    let node = AXNodeReader.read(provider: DeepFormProvider(), windowBounds: .zero)
    let tree = AXSerializer.serialize(node)
    #expect(tree.nodeCount == 34)
    #expect(tree.references().values.filter { $0.role == "AXRadioButton" }.count == 1)
    let plain = AXNodeReader.read(provider: DeepFormProvider(web: false), windowBounds: .zero)
    #expect(AXSerializer.serialize(plain).nodeCount == maximumAXDepth)
    let targeted = AXNodeReader.read(provider: DeepFormProvider(web: false), windowBounds: .zero, maximumDepth: maximumAXSubtreeDepth)
    #expect(AXSerializer.serialize(targeted).nodeCount == 34)
}

@Test func checkedActionSkipsSatisfiedStateAndWaitsForOneDelayedMutation() throws {
    var value = true, writes = 0
    let noop = try performCheckedAction(expected: true, radio: true, read: { value }, perform: {
        writes += 1; return ActionPerformance(inputStarted: true)
    })
    #expect(!noop.inputStarted && noop.effectVerification == .verified && writes == 0)
    value = false
    var now: TimeInterval = 0
    let selected = try performCheckedAction(expected: true, radio: true, read: { value }, perform: {
        writes += 1; return ActionPerformance(inputStarted: true)
    }, clock: { now }, sleep: { now += $0; if now >= 0.03 { value = true } })
    #expect(selected.effectVerification == .verified && writes == 1)
}

@Test func checkedActionStopsAfterOneRefusedMutationAndRejectsUnknownState() {
    var writes = 0
    var now: TimeInterval = 0
    #expect(throws: ActionPerformFailure.self) {
        try performCheckedAction(expected: true, radio: false, read: { false }, perform: {
            writes += 1; return ActionPerformance(inputStarted: true)
        }, clock: { now }, sleep: { now += $0 })
    }
    #expect(writes == 1 && now >= 0.4)
    #expect(throws: ActionPerformFailure.self) {
        try performCheckedAction(expected: true, radio: false, read: { nil }, perform: {
            writes += 1; return ActionPerformance(inputStarted: true)
        })
    }
    #expect(writes == 1)
}

@Test func checkedWireFieldSurvivesIndexResolutionAndSealsDifferentGoals() throws {
    let checked = try NativeAction.parse(.object(["type": .string("click"), "element_index": .number(12), "checked": .bool(true)]))
    let resolved = checked.resolvingElementReference("deep-ref")
    #expect(resolved.checked == true && resolved.elementRef == "deep-ref")
    let unchecked = try NativeAction.parse(.object(["type": .string("click"), "element_ref": .string("deep-ref"), "checked": .bool(false)]))
    #expect(InputDispatcher.digest([resolved]) != InputDispatcher.digest([unchecked]))
    #expect(try NativeAction.parse(.object(["type": .string("click"), "element_ref": .string("r"), "checked": .bool(false)])).checked == false)
    #expect(throws: ActionExecutionError.self) {
        try NativeAction.parse(.object(["type": .string("click"), "x": .number(1), "y": .number(1), "checked": .bool(true)]))
    }
}

@Test func sharingIndicatorRequiresExactNonmodalAXShapeAndTitlebarPosition() {
    let badge = CGRect(x: 10, y: 41, width: 66, height: 20)
    let window = CGRect(x: 0, y: 30, width: 1352, height: 759)
    #expect(isWindowSharingIndicator(role: "AXWindow", subrole: "AXDialog", modal: false,
        bounds: badge, childRolesAndTitles: [("AXButton", "WindowSharingSessionButton")]))
    #expect(sharingIndicatorWithinTitlebar(badge, targetBounds: window))
    #expect(!sharingIndicatorWithinTitlebar(badge.offsetBy(dx: 0, dy: 80), targetBounds: window))
    for modal in [true, nil] as [Bool?] {
        #expect(!isWindowSharingIndicator(role: "AXWindow", subrole: "AXDialog", modal: modal,
            bounds: badge, childRolesAndTitles: [("AXButton", "WindowSharingSessionButton")]))
    }
    #expect(!isWindowSharingIndicator(role: "AXWindow", subrole: "AXDialog", modal: false,
        bounds: badge, childRolesAndTitles: [("AXButton", "Confirm")]))
    #expect(!isWindowSharingIndicator(role: "AXWindow", subrole: "AXDialog", modal: false,
        bounds: window, childRolesAndTitles: [("AXButton", "WindowSharingSessionButton")]))
    #expect(!isWindowSharingIndicator(role: "AXWindow", subrole: "AXDialog", modal: false,
        bounds: badge, childRolesAndTitles: nil))
}

@Test func numericAndBooleanAXValuesPublishCheckedAndKeepLongChoiceLabels() throws {
    for (value, expected) in [("0.0", false), ("1.0", true), ("false", false), ("true", true)] {
        let label = String(repeating: "long answer ", count: 20)
        let node = AXNode(role: "AXRadioButton", label: label, value: value, bounds: .zero)
        let tree = AXSerializer.serialize(node)
        guard case let .object(fields) = tree.asJSON() else { Issue.record("Expected object"); return }
        #expect(fields["checked"] == .bool(expected))
        #expect(fields["label"] == .string(label))
        let titled = AXSerializer.serialize(AXNode(role: "AXRadioButton", title: label, value: value, bounds: .zero))
        guard case let .object(titleFields) = titled.asJSON() else { Issue.record("Expected object"); return }
        #expect(titleFields["title"] == .string(label))
    }
    #expect(boundedAXValueResult(kCFBooleanFalse).value == "false")
}

@preconcurrency import ApplicationServices
import CoreGraphics
import Foundation

// Proves non-occlusion for a complete equal-geometry group without assigning
// identities to its unresolved members. This never authorizes input to them.
struct BackgroundSiblingOrderingProof {
    private let targetPID: pid_t
    private let targetWindowID: CGWindowID
    private let targetBounds: CGRect
    private let selectedElement: AXUIElement
    private let siblings: [AXUIElement]

    fileprivate init(targetPID: pid_t, targetWindowID: CGWindowID, targetBounds: CGRect,
                     selectedElement: AXUIElement, siblings: [AXUIElement]) {
        self.targetPID = targetPID
        self.targetWindowID = targetWindowID
        self.targetBounds = targetBounds
        self.selectedElement = selectedElement
        self.siblings = siblings
    }

    func covers(targetPID: pid_t, targetWindowID: CGWindowID, targetBounds: CGRect,
                selected: TargetAXWindowRecord, sibling: TargetAXWindowRecord) -> Bool {
        guard self.targetPID == targetPID, self.targetWindowID == targetWindowID, self.targetBounds == targetBounds,
              selected.windowID == targetWindowID, let selectedAX = selected.element,
              CFEqual(selectedAX, selectedElement), ordinaryNonmodalWindow(selected), sibling.windowID == nil,
              ordinaryNonmodalWindow(sibling), let siblingAX = sibling.element,
              screenCaptureBoundsMatchAXBounds(screenCapture: targetBounds, accessibility: sibling.bounds)
        else { return false }
        return siblings.contains { CFEqual($0, siblingAX) }
    }
}

private func ordinaryNonmodalWindow(_ window: TargetAXWindowRecord) -> Bool {
    window.identity != 0 && window.element != nil && window.isModal == false &&
        window.role.status == .complete && window.role.value == "AXWindow" &&
        window.subrole.status == .complete && window.subrole.value == "AXStandardWindow"
}

func backgroundSiblingOrderingProof(
    targetPID: pid_t, targetWindowID: CGWindowID, targetBounds: CGRect,
    axWindows: [TargetAXWindowRecord],
    screenWindows: [PIDScreenCaptureWindowObservation],
    visibleWindows: [VisibleWindowRecord]
) -> BackgroundSiblingOrderingProof? {
    let axGroup = axWindows.filter {
        screenCaptureBoundsMatchAXBounds(screenCapture: targetBounds, accessibility: $0.bounds)
    }
    let screenGroup = screenWindows.filter {
        $0.pid == targetPID && $0.isOnScreen &&
            screenCaptureBoundsMatchAXBounds(screenCapture: $0.bounds, accessibility: targetBounds)
    }
    let visualGroup = visibleWindows.filter {
        $0.pid == targetPID &&
            screenCaptureBoundsMatchAXBounds(screenCapture: $0.bounds, accessibility: targetBounds)
    }
    guard targetPID > 0, targetWindowID != 0,
          axGroup.count > 1, axGroup.count <= maximumObservedWindows,
          axGroup.count == screenGroup.count, axGroup.count == visualGroup.count,
          axGroup.allSatisfy(ordinaryNonmodalWindow)
    else { return nil }
    let elements = axGroup.compactMap(\.element)
    for index in elements.indices {
        guard !elements[..<index].contains(where: { CFEqual($0, elements[index]) }) else { return nil }
    }
    let screenIDs = Set(screenGroup.map(\.windowID))
    guard !screenIDs.contains(0), screenIDs.count == screenGroup.count,
          Set(visualGroup.map(\.windowID)) == screenIDs,
          Set(visualGroup.map(\.windowID)).count == visualGroup.count,
          screenGroup.allSatisfy({ screen in
              visualGroup.contains { $0.windowID == screen.windowID && $0.bounds == screen.bounds } &&
                  axGroup.allSatisfy {
                      screenCaptureBoundsMatchAXBounds(screenCapture: screen.bounds, accessibility: $0.bounds)
                  }
          })
    else { return nil }
    let selected = axGroup.filter { $0.windowID == targetWindowID }
    let selectedVisual = visualGroup.filter { $0.windowID == targetWindowID && $0.bounds == targetBounds }
    guard selected.count == 1, let selectedAX = selected[0].element,
          selectedVisual.count == 1 else { return nil }
    let knownIDs = axGroup.compactMap(\.windowID)
    guard Set(knownIDs).count == knownIDs.count, Set(knownIDs).isSubset(of: screenIDs) else { return nil }
    let unknownAX = axGroup.filter { $0.windowID == nil }
    let remainingIDs = screenIDs.subtracting(knownIDs)
    guard !unknownAX.isEmpty, unknownAX.count == remainingIDs.count,
          visualGroup.filter({ remainingIDs.contains($0.windowID) }).allSatisfy({ candidate in
              windowIsProvablyBehind(candidateLayer: candidate.layer, candidateOrder: candidate.zOrder,
                  selectedLayer: selectedVisual[0].layer, selectedOrder: selectedVisual[0].zOrder)
          })
    else { return nil }
    return BackgroundSiblingOrderingProof(targetPID: targetPID, targetWindowID: targetWindowID, targetBounds: targetBounds,
        selectedElement: selectedAX, siblings: unknownAX.compactMap(\.element))
}

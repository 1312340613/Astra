import ApplicationServices
import Foundation

func focusedSheetCandidate<Element>(
    windows: [Element], focused: Element,
    same: (Element, Element) -> Bool, isOwned: (Element) -> Bool,
    parent: (Element) -> Element?, isSheet: (Element) -> Bool,
    contained: (Element, Element) -> Bool
) -> Element? {
    let matches = windows.compactMap { window -> Element? in
        guard let child = focusedAncestorBranch(root: window, focused: focused,
            same: same, isOwned: isOwned, parent: parent),
            isSheet(child), contained(window, child) else { return nil }
        return child
    }
    return matches.count == 1 ? matches[0] : nil
}

func observedAXPID(_ element: AXUIElement) -> pid_t? {
    observationAXCall(element: element, fallback: nil as pid_t?) {
        var pid: pid_t = 0
        return AXUIElementGetPid(element, &pid) == .success && pid > 0 ? pid : nil
    }
}

func observedAXParent(_ element: AXUIElement) -> AXUIElement? {
    let (error, value) = observationAXAttribute(element, kAXParentAttribute)
    return error == .success ? decodeAXElement(value) : nil
}

func focusBelongsToExactAXRoot(app: AXUIElement, root: AXUIElement) -> Bool {
    guard let owner = observedAXPID(app), observedAXPID(root) == owner else { return false }
    let (error, value) = observationAXAttribute(app, kAXFocusedUIElementAttribute)
    guard error == .success, let focused = decodeAXElement(value), observedAXPID(focused) == owner else { return false }
    return CFEqual(focused, root) || focusedAncestorBranch(root: root, focused: focused,
        same: { CFEqual($0, $1) }, isOwned: { observedAXPID($0) == owner },
        parent: observedAXParent) != nil
}

// Reconstruct sheet membership on every inventory; focus loss removes the candidate.
// CG matching and stored AX identity checks remain the caller's responsibility.
func completeObservedAXWindows(_ app: AXUIElement) -> [AXUIElement]? {
    guard let windows = completeAXElementArray(app, attribute: kAXWindowsAttribute,
        maximum: maximumObservedWindows) else { return nil }
    guard windows.count < maximumObservedWindows, let owner = observedAXPID(app) else { return windows }
    let (error, value) = observationAXAttribute(app, kAXFocusedUIElementAttribute)
    guard error == .success, let focused = decodeAXElement(value) else { return windows }
    let sheet = focusedSheetCandidate(windows: windows, focused: focused,
        same: { CFEqual($0, $1) }, isOwned: { observedAXPID($0) == owner },
        parent: observedAXParent,
        isSheet: { AXNodeReader.stringAttribute($0, kAXRoleAttribute).value == kAXSheetRole },
        contained: { window, child in
            guard AXNodeReader.stringAttribute(window, kAXRoleAttribute).value == kAXWindowRole,
                  let outer = AXNodeReader.frameAttribute(window),
                  let inner = AXNodeReader.frameAttribute(child) else { return false }
            return trustedFocusedWindow(expectedIdentity: 1, targetBounds: outer,
                focusedIdentity: 2, focusedBounds: inner)
        })
    guard let sheet, !windows.contains(where: { CFEqual($0, sheet) }) else { return windows }
    return windows + [sheet]
}

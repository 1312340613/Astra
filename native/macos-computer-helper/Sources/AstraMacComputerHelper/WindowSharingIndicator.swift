@preconcurrency import ApplicationServices
import CoreGraphics

// macOS presents its capture indicator as a separate AXDialog owned by the
// captured application. It is nonmodal window chrome, not an application dialog.
// Match its complete AX shape; geometry or a nonmodal bit alone is insufficient.
func isWindowSharingIndicator(
    role: String?, subrole: String?, modal: Bool?, bounds: CGRect,
    childRolesAndTitles: [(String?, String?)]?
) -> Bool {
    guard role == "AXWindow", subrole == "AXDialog", modal == false,
          bounds.width > 0, bounds.width <= 128, bounds.height > 0, bounds.height <= 32,
          let children = childRolesAndTitles, children.count == 1
    else { return false }
    return children[0].0 == "AXButton" && children[0].1 == "WindowSharingSessionButton"
}

func windowSharingIndicator(_ element: AXUIElement, bounds: CGRect) -> Bool {
    guard bounds.width <= 128, bounds.height <= 32 else { return false }
    let children = completeAXElementArray(element, attribute: kAXChildrenAttribute, maximum: 2)
    return isWindowSharingIndicator(
        role: AXNodeReader.stringAttribute(element, kAXRoleAttribute).value,
        subrole: AXNodeReader.stringAttribute(element, kAXSubroleAttribute).value,
        modal: AXNodeReader.boolAttribute(element, kAXModalAttribute), bounds: bounds,
        childRolesAndTitles: children?.map {
            (AXNodeReader.stringAttribute($0, kAXRoleAttribute).value,
             AXNodeReader.stringAttribute($0, kAXTitleAttribute).value)
        }
    )
}

func sharingIndicatorWithinTitlebar(_ bounds: CGRect, targetBounds: CGRect) -> Bool {
    bounds.width > 0 && bounds.height > 0 && targetBounds.contains(bounds) &&
        bounds.maxY <= targetBounds.minY + 40 && bounds.maxX <= targetBounds.minX + 150
}

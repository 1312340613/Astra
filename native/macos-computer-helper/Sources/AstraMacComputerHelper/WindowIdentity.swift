import ApplicationServices
import Foundation

// File panels can expose their visible name as AXDescription instead of AXTitle.
// This is matching metadata, never a replacement for PID, geometry, uniqueness,
// retained AX identity or current-focus validation.
func accessibilityWindowName(
    _ element: AXUIElement,
    read: (AXUIElement, String) -> BoundedAXStringResult = AXNodeReader.stringAttribute
) -> BoundedAXStringResult {
    let title = read(element, kAXTitleAttribute)
    guard title.status == .complete,
          title.value?.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty != false
    else { return title }
    let role = read(element, kAXRoleAttribute)
    guard role.status == .complete, role.value == kAXSheetRole else { return title }
    let description = read(element, kAXDescriptionAttribute)
    guard description.status == .complete else { return description }
    return description.value?.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty == false
        ? description : title
}

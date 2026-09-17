import ApplicationServices

// NSWorkspace.frontmostApplication is updated by the main run loop. The CU
// helper serves synchronous stdin requests, so its cached value can still name
// yesterday's foreground app. Never use that cache as input-delivery authority.
func liveFrontmostPID() -> pid_t? {
    let system = AXUIElementCreateSystemWide()
    guard AXUIElementSetMessagingTimeout(system, 0.2) == .success else { return nil }
    // Respect any enclosing observation deadline, but bypass its attribute
    // cache: focus is revalidated at every activation/input/cleanup boundary.
    return observationAXCall(element: system, fallback: nil as pid_t?) {
        var value: CFTypeRef?
        let error = AXUIElementCopyAttributeValue(
            system, kAXFocusedApplicationAttribute as CFString, &value
        )
        return focusedApplicationPID(error: error, value: value)
    }
}

func focusedApplicationPID(error: AXError, value: CFTypeRef?) -> pid_t? {
    guard error == .success, let application = decodeAXElement(value) else { return nil }
    var pid: pid_t = 0
    guard AXUIElementGetPid(application, &pid) == .success, pid > 0 else { return nil }
    return pid
}

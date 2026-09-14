@preconcurrency import ApplicationServices
import CoreGraphics

protocol PermissionStatusProviding {
    var accessibilityTrusted: Bool { get }
    var screenRecordingAllowed: Bool { get }
}

struct SystemPermissionStatus: PermissionStatusProviding {
    var accessibilityTrusted: Bool {
        AXIsProcessTrustedWithOptions(
            [kAXTrustedCheckOptionPrompt.takeUnretainedValue() as String: false] as CFDictionary
        )
    }

    var screenRecordingAllowed: Bool {
        CGPreflightScreenCaptureAccess()
    }
}

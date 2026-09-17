@testable import AstraMacComputerHelperCore
import ApplicationServices
import Foundation
import Testing

@Test func focusedApplicationQueryFailsClosedOnErrorsAndMalformedValues() {
    #expect(focusedApplicationPID(error: .cannotComplete, value: AXUIElementCreateApplication(123)) == nil)
    #expect(focusedApplicationPID(error: .success, value: nil) == nil)
    #expect(focusedApplicationPID(error: .success, value: "not an AX application" as CFString) == nil)
    #expect(focusedApplicationPID(error: .success, value: AXUIElementCreateSystemWide()) == nil)
}

@Test func focusedApplicationQueryUsesEachNewObservedPID() {
    #expect(focusedApplicationPID(error: .success, value: AXUIElementCreateApplication(123)) == 123)
    #expect(focusedApplicationPID(error: .success, value: AXUIElementCreateApplication(456)) == 456)
}

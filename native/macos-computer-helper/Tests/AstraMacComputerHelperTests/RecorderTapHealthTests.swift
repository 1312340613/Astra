import Foundation
import Testing
@testable import AstraMacComputerHelperCore

@Test func recorderTapHealthRecoversWithoutDisabledCallback() {
    var health = RecorderTapHealth()
    let now = Date(timeIntervalSince1970: 100)
    #expect(health.action(at: now, permitted: true, installed: true, keyboardIncluded: true, enabled: false) == .enable)
    #expect(health.action(at: now.addingTimeInterval(0.2), permitted: true, installed: true, keyboardIncluded: true, enabled: false) == .none)
    #expect(health.action(at: now.addingTimeInterval(2), permitted: true, installed: true, keyboardIncluded: true, enabled: false) == .enable)
}

@Test func recorderTapHealthRecreatesPartialTapOnlyAfterPermission() {
    var health = RecorderTapHealth()
    let now = Date(timeIntervalSince1970: 100)
    #expect(health.action(at: now, permitted: false, installed: true, keyboardIncluded: false, enabled: false) == .none)
    #expect(health.action(at: now.addingTimeInterval(2), permitted: true, installed: true, keyboardIncluded: false, enabled: false) == .install)
}

@Test func recorderTapHealthDoesNotTouchHealthyOrUnauthorizedTap() {
    var health = RecorderTapHealth()
    let now = Date(timeIntervalSince1970: 100)
    #expect(health.action(at: now, permitted: true, installed: true, keyboardIncluded: true, enabled: true) == .none)
    #expect(health.action(at: now.addingTimeInterval(2), permitted: false, installed: false, keyboardIncluded: false, enabled: false) == .none)
    #expect(health.action(at: now.addingTimeInterval(4), permitted: true, installed: false, keyboardIncluded: false, enabled: false) == .install)
}

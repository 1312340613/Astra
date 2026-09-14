import Foundation
import Testing

@testable import AstraMacComputerHelperCore

@Suite @MainActor struct AppshotHUDTests {
  @Test func hardcodedMessagesDismissAndOldTimerCannotHideNewMessage() {
    var rendered: [AppshotHUDMessage?] = []
    var timers: [() -> Void] = []
    let hud = AppshotHUD(
      render: { rendered.append($0) },
      schedule: { delay, action in
        #expect(delay == 1.5)
        timers.append(action)
      })
    hud.show(.success)
    hud.show(.failure(.protectedUI))
    timers[0]()
    #expect(rendered.count == 2)
    timers[1]()
    #expect(rendered.count == 3 && rendered.last! == nil)
    #expect(AppshotHUDMessage.success.text == "✓ Appshot 已附加到 Astra")
    for code in AppshotErrorCode.allCases {
      #expect(AppshotHUDMessage.failure(code).text.utf8.count < 200)
    }
  }
  @Test func hideInvalidatesPendingDismissal() {
    var timer: (() -> Void)?
    var draws = 0
    let hud = AppshotHUD(render: { _ in draws += 1 }, schedule: { _, action in timer = action })
    hud.show(.success)
    hud.hide()
    timer?()
    #expect(draws == 2)
  }
}

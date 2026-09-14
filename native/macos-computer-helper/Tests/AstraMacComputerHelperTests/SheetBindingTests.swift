@testable import AstraMacComputerHelperCore
import Testing

@Test func focusedSheetRequiresExactOwnedParentWindowAndSheetRole() {
    func candidate(windows: [Int] = [1], foreign: Int = -1, sheet: Bool = true, contained: Bool = true) -> Int? {
        focusedSheetCandidate(windows: windows, focused: 3, same: ==,
            isOwned: { $0 != foreign }, parent: { [3: 2, 2: 1][$0] },
            isSheet: { $0 == 2 && sheet }, contained: { _, _ in contained })
    }
    #expect(candidate() == 2)
    #expect(candidate(windows: [9]) == nil)
    #expect(candidate(windows: [1, 1]) == nil)
    #expect(candidate(foreign: 2) == nil)
    #expect(candidate(sheet: false) == nil)
    #expect(candidate(contained: false) == nil)
}

@Test func focusedSheetRejectsDetachedCyclicAndNonImmediateSheets() {
    let detached = focusedSheetCandidate(windows: [1], focused: 3, same: ==,
        isOwned: { _ in true }, parent: { _ in nil },
        isSheet: { _ in true }, contained: { _, _ in true })
    #expect(detached == nil)
    let cyclic = focusedSheetCandidate(windows: [1], focused: 3, same: ==,
        isOwned: { _ in true }, parent: { $0 == 3 ? 2 : 3 },
        isSheet: { _ in true }, contained: { _, _ in true })
    #expect(cyclic == nil)
    let nested = focusedSheetCandidate(windows: [1], focused: 4, same: ==,
        isOwned: { _ in true }, parent: { [4: 3, 3: 2, 2: 1][$0] },
        isSheet: { $0 == 3 }, contained: { _, _ in true })
    #expect(nested == nil)
}

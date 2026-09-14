@testable import AstraMacComputerHelperCore
import Testing

@Test func focusedBranchRecoversOnlyImmediateChildOfExactRoot() {
    let parents = [4: 3, 3: 2, 2: 1]
    #expect(focusedAncestorBranch(root: 1, focused: 4, same: ==, isOwned: { _ in true }, parent: { parents[$0] }) == 2)
    #expect(focusedAncestorBranch(root: 9, focused: 4, same: ==, isOwned: { _ in true }, parent: { parents[$0] }) == nil)
    #expect(focusedAncestorBranch(root: 1, focused: 1, same: ==, isOwned: { _ in true }, parent: { parents[$0] }) == nil)
}

@Test func focusedBranchRejectsForeignProcessCyclesAndExcessiveDepth() {
    #expect(focusedAncestorBranch(root: 1, focused: 4, same: ==, isOwned: { $0 != 3 }, parent: { $0 - 1 }) == nil)
    #expect(focusedAncestorBranch(root: 1, focused: 4, same: ==, isOwned: { _ in true }, parent: { $0 == 4 ? 3 : 4 }) == nil)
    #expect(focusedAncestorBranch(root: 1, focused: 100, same: ==, isOwned: { _ in true }, parent: { $0 - 1 }) == nil)
}

@Test func focusedBranchInsertionPreservesExistingChildrenAndCapacity() {
    #expect(insertingFocusedBranch(children: [2, 3], remaining: 3, same: ==, recover: { 4 }) == [4, 2, 3])
    #expect(insertingFocusedBranch(children: [2, 3], remaining: 3, same: ==, recover: { 2 }) == [2, 3])
    #expect(insertingFocusedBranch(children: [2, 3], remaining: 3, same: ==, recover: { nil }) == [2, 3])
    var called = false
    let full = insertingFocusedBranch(children: [2, 3], remaining: 2, same: ==, recover: { called = true; return 4 })
    #expect(full == [2, 3])
    #expect(!called)
}

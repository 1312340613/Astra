@testable import AstraMacComputerHelperCore
import Testing

private let fragmentHashA = String(repeating: "a", count: 64)
private let fragmentHashB = String(repeating: "b", count: 64)
private let stageHash0 = String(repeating: "0", count: 64)
private let stageHash1 = String(repeating: "1", count: 64)

@Test func foregroundFragmentDeclarationAcceptsExactAXScrollBackends() throws {
    for backend in [DispatchBackend.axIncrement, .axDecrement, .axPress] {
        let requirement = ForegroundFragmentActionRequirement(
            backend: backend,
            actionClass: .scroll,
            intent: nil
        )
        _ = try ForegroundFragmentAuthority(declaration: .init(
            fragmentHash: fragmentHashA,
            stages: [.init(stageHash: stageHash0, expectedActionCount: 1, actions: [requirement])],
            maxActions: 1,
            wallClockLimitMS: 1,
            restorePreviousFocus: true
        ))
    }
}

@Test func foregroundFragmentDeclarationRejectsMalformedAndExcessiveBounds() {
    let requirement = ForegroundFragmentActionRequirement(
        backend: .pidPointer,
        actionClass: .click,
        intent: .pointer(.click)
    )
    let validStage = ForegroundFragmentStageDeclaration(
        stageHash: stageHash0,
        expectedActionCount: 1,
        actions: [requirement]
    )
    let invalid: [ForegroundFragmentDeclaration] = [
        .init(fragmentHash: "ABC", stages: [validStage], maxActions: 1, wallClockLimitMS: 1, restorePreviousFocus: true),
        .init(fragmentHash: fragmentHashA, stages: [], maxActions: 1, wallClockLimitMS: 1, restorePreviousFocus: true),
        .init(fragmentHash: fragmentHashA, stages: Array(repeating: validStage, count: 33), maxActions: 33, wallClockLimitMS: 1, restorePreviousFocus: true),
        .init(fragmentHash: fragmentHashA, stages: [validStage], maxActions: 65, wallClockLimitMS: 1, restorePreviousFocus: true),
        .init(fragmentHash: fragmentHashA, stages: [validStage], maxActions: 1, wallClockLimitMS: 120_001, restorePreviousFocus: true),
        .init(
            fragmentHash: fragmentHashA,
            stages: [.init(stageHash: stageHash0, expectedActionCount: 2, actions: [requirement])],
            maxActions: 2,
            wallClockLimitMS: 1,
            restorePreviousFocus: true
        ),
    ]

    for declaration in invalid {
        #expect(throws: ForegroundFragmentAuthorityError.invalidDeclaration) {
            _ = try ForegroundFragmentAuthority(declaration: declaration)
        }
    }
}

@Test func foregroundFragmentAuthorityRequiresOrderedFreshSingleUseStages() throws {
    let first = ForegroundFragmentActionRequirement(
        backend: .pidPointer,
        actionClass: .click,
        intent: .pointer(.click)
    )
    let second = ForegroundFragmentActionRequirement(
        backend: .foregroundKeyboard,
        actionClass: .text,
        intent: .textEntry
    )
    let authority = try ForegroundFragmentAuthority(declaration: .init(
        fragmentHash: fragmentHashA,
        stages: [
            .init(stageHash: stageHash0, expectedActionCount: 1, actions: [first]),
            .init(stageHash: stageHash1, expectedActionCount: 1, actions: [second]),
        ],
        maxActions: 2,
        wallClockLimitMS: 10_000,
        restorePreviousFocus: true
    ))
    let stage0 = FragmentStageAuthority(
        fragmentHash: fragmentHashA,
        stageIndex: 0,
        stageHash: stageHash0,
        inputSnapshotID: "snapshot-0"
    )

    try authority.bindPlannedStage(stage0, requirements: [first], planRef: "plan-0")
    try authority.beginStage(stage0, planRef: "plan-0")
    authority.finishStage(stage0, planRef: "plan-0", succeeded: true)
    #expect(try authority.commit(.init(
        fragmentHash: fragmentHashA,
        stageIndex: 0,
        stageHash: stageHash0,
        planRef: "plan-0",
        freshSnapshotID: "snapshot-1",
        postconditionVerified: true
    )) == false)

    let stage1 = FragmentStageAuthority(
        fragmentHash: fragmentHashA,
        stageIndex: 1,
        stageHash: stageHash1,
        inputSnapshotID: "snapshot-1"
    )
    try authority.bindPlannedStage(stage1, requirements: [second], planRef: "plan-1")
    try authority.beginStage(stage1, planRef: "plan-1")
    authority.finishStage(stage1, planRef: "plan-1", succeeded: true)
    #expect(try authority.commit(.init(
        fragmentHash: fragmentHashA,
        stageIndex: 1,
        stageHash: stageHash1,
        planRef: "plan-1",
        freshSnapshotID: "snapshot-2",
        postconditionVerified: true
    )))
    #expect(authority.isTerminal)
    #expect(throws: ForegroundFragmentAuthorityError.alreadyConsumed) {
        try authority.bindPlannedStage(stage1, requirements: [second], planRef: "replay")
    }
}

@Test func fragmentMismatchFailedPostconditionAndPartialInputConsumeAuthority() throws {
    let requirement = ForegroundFragmentActionRequirement(
        backend: .pidPointer,
        actionClass: .click,
        intent: .pointer(.click)
    )

    for failure in ["hash", "postcondition", "partial"] {
        let authority = try ForegroundFragmentAuthority(declaration: .init(
            fragmentHash: fragmentHashA,
            stages: [.init(stageHash: stageHash0, expectedActionCount: 1, actions: [requirement])],
            maxActions: 1,
            wallClockLimitMS: 10_000,
            restorePreviousFocus: true
        ))
        let stage = FragmentStageAuthority(
            fragmentHash: failure == "hash" ? fragmentHashB : fragmentHashA,
            stageIndex: 0,
            stageHash: stageHash0,
            inputSnapshotID: "input"
        )
        if failure == "hash" {
            #expect(throws: ForegroundFragmentAuthorityError.authorityMismatch) {
                try authority.bindPlannedStage(stage, requirements: [requirement], planRef: "plan")
            }
        } else {
            try authority.bindPlannedStage(stage, requirements: [requirement], planRef: "plan")
            try authority.beginStage(stage, planRef: "plan")
            authority.finishStage(stage, planRef: "plan", succeeded: failure != "partial")
            if failure == "postcondition" {
                #expect(throws: ForegroundFragmentAuthorityError.postconditionFailed) {
                    _ = try authority.commit(.init(
                        fragmentHash: fragmentHashA,
                        stageIndex: 0,
                        stageHash: stageHash0,
                        planRef: "plan",
                        freshSnapshotID: "fresh",
                        postconditionVerified: false
                    ))
                }
            }
        }
        #expect(authority.isConsumed)
    }
}

@Test func fragmentExpiryConsumesAuthorityBeforeNextTransition() throws {
    var now = 10.0
    let requirement = ForegroundFragmentActionRequirement(
        backend: .wait,
        actionClass: nil,
        intent: nil
    )
    let authority = try ForegroundFragmentAuthority(
        declaration: .init(
            fragmentHash: fragmentHashA,
            stages: [.init(stageHash: stageHash0, expectedActionCount: 1, actions: [requirement])],
            maxActions: 1,
            wallClockLimitMS: 1,
            restorePreviousFocus: false
        ),
        clock: { now }
    )
    now += 0.002

    #expect(throws: ForegroundFragmentAuthorityError.expired) {
        try authority.bindPlannedStage(.init(
            fragmentHash: fragmentHashA,
            stageIndex: 0,
            stageHash: stageHash0,
            inputSnapshotID: "snapshot"
        ), requirements: [requirement], planRef: "plan")
    }
    #expect(authority.isConsumed)
}

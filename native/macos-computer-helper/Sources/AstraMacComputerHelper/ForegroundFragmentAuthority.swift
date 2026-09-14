import Foundation

let maximumForegroundFragmentStages = 32
let maximumForegroundFragmentActions = 64
let maximumForegroundFragmentWallMilliseconds = 120_000

struct ForegroundFragmentActionRequirement: Equatable {
    let backend: DispatchBackend
    let actionClass: DispatchActionClass?
    let intent: SyntheticInputIntent?
}

struct ForegroundFragmentStageDeclaration: Equatable {
    let stageHash: String
    let expectedActionCount: Int
    let actions: [ForegroundFragmentActionRequirement]
}

struct ForegroundFragmentDeclaration: Equatable {
    let fragmentHash: String
    let stages: [ForegroundFragmentStageDeclaration]
    let maxActions: Int
    let wallClockLimitMS: Int
    let restorePreviousFocus: Bool
}

enum ForegroundFragmentPlanRequest: Equatable {
    case initial(authority: FragmentStageAuthority)
    case continuing(
        takeoverRef: String,
        authority: FragmentStageAuthority
    )

    var authority: FragmentStageAuthority {
        switch self {
        case let .initial(authority), let .continuing(_, authority): authority
        }
    }

    var takeoverRef: String? {
        if case let .continuing(reference, _) = self { return reference }
        return nil
    }
}

struct FragmentStageAuthority: Equatable {
    let fragmentHash: String
    let stageIndex: Int
    let stageHash: String
    let inputSnapshotID: String
}

struct FragmentStageCommit: Equatable {
    let fragmentHash: String
    let stageIndex: Int
    let stageHash: String
    let planRef: String
    let freshSnapshotID: String
    let postconditionVerified: Bool
}

enum ForegroundFragmentAuthorityError: Error, Equatable {
    case invalidDeclaration
    case authorityMismatch
    case alreadyConsumed
    case expired
    case postconditionFailed
}

final class ForegroundFragmentAuthority {
    private enum State {
        case ready(index: Int, expectedSnapshotID: String?)
        case planned(stage: FragmentStageAuthority, planRef: String)
        case executing(stage: FragmentStageAuthority, planRef: String)
        case awaitingCommit(stage: FragmentStageAuthority, planRef: String)
        case terminal
        case consumed
    }

    let declaration: ForegroundFragmentDeclaration
    private let clock: () -> TimeInterval
    private let deadline: TimeInterval
    private let lock = NSLock()
    private var state: State = .ready(index: 0, expectedSnapshotID: nil)
    private var usedSnapshotIDs: Set<String> = []

    init(
        declaration: ForegroundFragmentDeclaration,
        clock: @escaping () -> TimeInterval = { ProcessInfo.processInfo.systemUptime }
    ) throws {
        guard Self.valid(declaration) else {
            throw ForegroundFragmentAuthorityError.invalidDeclaration
        }
        self.declaration = declaration
        self.clock = clock
        deadline = clock() + Double(declaration.wallClockLimitMS) / 1_000
    }

    var isConsumed: Bool {
        lock.lock()
        defer { lock.unlock() }
        if case .consumed = state { return true }
        return false
    }

    var isTerminal: Bool {
        lock.lock()
        defer { lock.unlock() }
        if case .terminal = state { return true }
        return false
    }

    var permitsFreshObservation: Bool {
        lock.lock()
        defer { lock.unlock() }
        if case .awaitingCommit = state { return true }
        return false
    }

    func bindPlannedStage(
        _ stage: FragmentStageAuthority,
        requirements: [ForegroundFragmentActionRequirement],
        planRef: String
    ) throws {
        lock.lock()
        defer { lock.unlock() }
        try requireLiveLocked()
        guard case let .ready(index, expectedSnapshotID) = state else {
            try consumeAndThrowLocked(.alreadyConsumed)
        }
        guard validReference(planRef), validReference(stage.inputSnapshotID),
              stage.fragmentHash == declaration.fragmentHash,
              stage.stageIndex == index,
              declaration.stages.indices.contains(index),
              stage.stageHash == declaration.stages[index].stageHash,
              requirements == declaration.stages[index].actions,
              expectedSnapshotID == nil || stage.inputSnapshotID == expectedSnapshotID,
              !usedSnapshotIDs.contains(stage.inputSnapshotID)
        else { try consumeAndThrowLocked(.authorityMismatch) }
        usedSnapshotIDs.insert(stage.inputSnapshotID)
        state = .planned(stage: stage, planRef: planRef)
    }

    func beginStage(_ stage: FragmentStageAuthority, planRef: String) throws {
        lock.lock()
        defer { lock.unlock() }
        try requireLiveLocked()
        guard case let .planned(expected, expectedPlanRef) = state,
              expected == stage,
              expectedPlanRef == planRef
        else { try consumeAndThrowLocked(.authorityMismatch) }
        state = .executing(stage: stage, planRef: planRef)
    }

    func finishStage(_ stage: FragmentStageAuthority, planRef: String, succeeded: Bool) {
        lock.lock()
        defer { lock.unlock() }
        guard case let .executing(expected, expectedPlanRef) = state,
              expected == stage,
              expectedPlanRef == planRef,
              succeeded
        else {
            state = .consumed
            return
        }
        state = .awaitingCommit(stage: stage, planRef: planRef)
    }

    @discardableResult
    func commit(_ commit: FragmentStageCommit) throws -> Bool {
        lock.lock()
        defer { lock.unlock() }
        try requireLiveLocked()
        guard case let .awaitingCommit(stage, planRef) = state,
              commit.fragmentHash == stage.fragmentHash,
              commit.stageIndex == stage.stageIndex,
              commit.stageHash == stage.stageHash,
              commit.planRef == planRef,
              validReference(commit.freshSnapshotID),
              commit.freshSnapshotID != stage.inputSnapshotID,
              !usedSnapshotIDs.contains(commit.freshSnapshotID)
        else { try consumeAndThrowLocked(.authorityMismatch) }
        guard commit.postconditionVerified else {
            try consumeAndThrowLocked(.postconditionFailed)
        }
        usedSnapshotIDs.insert(commit.freshSnapshotID)
        let next = stage.stageIndex + 1
        if next == declaration.stages.count {
            state = .terminal
            return true
        }
        state = .ready(index: next, expectedSnapshotID: commit.freshSnapshotID)
        // The fresh observation is allowed once as the next input snapshot.
        usedSnapshotIDs.remove(commit.freshSnapshotID)
        return false
    }

    func consume() {
        lock.lock()
        state = .consumed
        lock.unlock()
    }

    private func requireLiveLocked() throws {
        switch state {
        case .terminal, .consumed:
            throw ForegroundFragmentAuthorityError.alreadyConsumed
        case .ready, .planned, .executing, .awaitingCommit:
            guard clock() <= deadline else {
                state = .consumed
                throw ForegroundFragmentAuthorityError.expired
            }
        }
    }

    private func consumeAndThrowLocked(_ error: ForegroundFragmentAuthorityError) throws -> Never {
        state = .consumed
        throw error
    }

    private static func valid(_ declaration: ForegroundFragmentDeclaration) -> Bool {
        guard validHash(declaration.fragmentHash),
              (1 ... maximumForegroundFragmentStages).contains(declaration.stages.count),
              (1 ... maximumForegroundFragmentActions).contains(declaration.maxActions),
              (1 ... maximumForegroundFragmentWallMilliseconds).contains(declaration.wallClockLimitMS),
              Set(declaration.stages.map(\.stageHash)).count == declaration.stages.count
        else { return false }
        var actionCount = 0
        for stage in declaration.stages {
            guard validHash(stage.stageHash),
                  (1 ... maximumForegroundFragmentActions).contains(stage.expectedActionCount),
                  !stage.actions.isEmpty,
                  stage.actions.count == stage.expectedActionCount
            else { return false }
            actionCount += stage.expectedActionCount
            guard actionCount <= declaration.maxActions,
                  stage.actions.allSatisfy(validRequirement)
            else { return false }
        }
        return actionCount > 0
    }

    private static func validRequirement(_ requirement: ForegroundFragmentActionRequirement) -> Bool {
        switch (requirement.backend, requirement.actionClass, requirement.intent) {
        case let (.pidPointer, actionClass?, .pointer(intentClass)),
             let (.foregroundPointer, actionClass?, .pointer(intentClass)):
            return actionClass == intentClass && [.click, .doubleClick, .scroll, .drag].contains(actionClass)
        case (.foregroundKeyboard, .text?, .textEntry),
             (.foregroundKeyboard, .text?, .keyChord):
            return true
        case (.axPress, .press?, nil),
             (.axPress, .scroll?, nil),
             (.axIncrement, .scroll?, nil),
             (.axDecrement, .scroll?, nil),
             (.axSelectedText, .text?, nil),
             (.wait, nil, nil):
            return true
        default:
            return false
        }
    }
}

func foregroundFragmentHashIsValid(_ value: String) -> Bool {
    value.utf8.count == 64 && value.utf8.allSatisfy {
        ($0 >= Character("0").asciiValue! && $0 <= Character("9").asciiValue!) ||
            ($0 >= Character("a").asciiValue! && $0 <= Character("f").asciiValue!)
    }
}

private func validHash(_ value: String) -> Bool { foregroundFragmentHashIsValid(value) }

private func validReference(_ value: String) -> Bool {
    !value.isEmpty && value.unicodeScalars.count <= 256
}

extension DispatchPlan {
    func foregroundFragmentRequirements(actions: [NativeAction]) -> [ForegroundFragmentActionRequirement]? {
        guard actions.count == backends.count else { return nil }
        return zip(actions, backends).map { action, backend in
            switch backend {
            case .axPress:
                return ForegroundFragmentActionRequirement(
                    backend: backend,
                    actionClass: action.kind == .scroll ? .scroll : .press,
                    intent: nil
                )
            case .axIncrement, .axDecrement:
                return ForegroundFragmentActionRequirement(backend: backend, actionClass: .scroll, intent: nil)
            case .axSelectedText:
                return ForegroundFragmentActionRequirement(backend: backend, actionClass: .text, intent: nil)
            case .wait:
                return ForegroundFragmentActionRequirement(backend: backend, actionClass: nil, intent: nil)
            case .pidPointer, .foregroundPointer:
                let actionClass: DispatchActionClass = switch action.kind {
                case .click, .rightClick: .click
                case .doubleClick: .doubleClick
                case .scroll: .scroll
                case .drag: .drag
                case .type, .keypress, .wait: .click
                }
                return ForegroundFragmentActionRequirement(
                    backend: backend,
                    actionClass: actionClass,
                    intent: .pointer(actionClass)
                )
            case .foregroundKeyboard:
                let intent: SyntheticInputIntent? = action.kind == .type
                    ? .textEntry
                    : ApprovedKeyChord(action: action).map(SyntheticInputIntent.keyChord)
                return ForegroundFragmentActionRequirement(backend: backend, actionClass: .text, intent: intent)
            case .pidKeyboard:
                let intent: SyntheticInputIntent? = action.kind == .type
                    ? .textEntry
                    : ApprovedKeyChord(action: action).map(SyntheticInputIntent.keyChord)
                return ForegroundFragmentActionRequirement(backend: backend, actionClass: .text, intent: intent)
            }
        }
    }
}

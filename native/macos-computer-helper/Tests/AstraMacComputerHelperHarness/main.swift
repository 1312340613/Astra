@testable import AstraMacComputerHelperCore
import AppKit
import ApplicationServices
import CoreGraphics
import CryptoKit
import Darwin
import Foundation
import ImageIO

private let maxMessageBytes = 4 * 1024 * 1024
private let deepCompatibilityProbeArgument = "--deep-compatibility-probe"
private let axTextDetailConformanceArgument = "--emit-ax-text-detail-conformance"

if CommandLine.arguments.count == 2, CommandLine.arguments[1] == axTextDetailConformanceArgument {
    do {
        FileHandle.standardOutput.write(try makeAXTextDetailConformanceVector())
        Darwin.exit(0)
    } catch {
        FileHandle.standardError.write(Data("AX text detail conformance fixture failed: \(error)\n".utf8))
        Darwin.exit(1)
    }
}

if CommandLine.arguments.count == 3, CommandLine.arguments[1] == deepCompatibilityProbeArgument {
    let kind = CommandLine.arguments[2]
    let nested: String
    switch kind {
    case "array":
        nested = String(repeating: "[", count: 20_000) + "null" + String(repeating: "]", count: 20_000)
    case "object":
        nested = String(repeating: #"{"x":"#, count: 8_000) + "null" + String(repeating: "}", count: 8_000)
    default:
        Darwin.exit(2)
    }
    let payload = Data("{\"schema_version\":1,\"applications\":[],\"extra\":\(nested)}".utf8)
    let registry = PIDInputCompatibilityRegistry(bytes: payload)
    let application = PIDTargetApplication(bundleIdentifier: "com.example.editor", version: "1.2.3")
    Darwin.exit(registry.allows(application: application, action: .scroll) ? 3 : 0)
}

private enum HarnessFailure: Error, CustomStringConvertible {
    case assertion(String)

    var description: String {
        switch self {
        case let .assertion(message):
            return message
        }
    }
}

private final class HarnessSensitiveAccessorCounter {
    var count = 0
}

private final class HarnessAXTextDetailProvider: AXTextDetailAttributeProvider {
    let role: AXTextDetailStringResult
    let subrole: AXTextDetailStringResult
    let value: String
    let childProviders: [HarnessAXTextDetailProvider]
    let sensitiveCounter: HarnessSensitiveAccessorCounter

    init(
        role: AXTextDetailStringResult,
        subrole: AXTextDetailStringResult,
        value: String,
        children: [HarnessAXTextDetailProvider] = [],
        sensitiveCounter: HarnessSensitiveAccessorCounter
    ) {
        self.role = role
        self.subrole = subrole
        self.value = value
        self.childProviders = children
        self.sensitiveCounter = sensitiveCounter
    }

    func stringValue(for attribute: String, maximumBytes _: Int) -> AXTextDetailStringResult {
        switch attribute {
        case kAXRoleAttribute:
            return role
        case kAXSubroleAttribute:
            return subrole
        case kAXValueAttribute,
             kAXSelectedTextAttribute,
             kAXSelectedTextRangeAttribute,
             kAXVisibleCharacterRangeAttribute,
             "AXAttributedStringForRange":
            sensitiveCounter.count += 1
            return AXTextDetailStringResult(value: value, status: .complete)
        default:
            return AXTextDetailStringResult(value: nil, status: .missing)
        }
    }

    func boolValue(for _: String) -> AXTextDetailBoolResult {
        AXTextDetailBoolResult(value: nil, status: .missing)
    }

    func bounds() -> AXTextDetailBoundsResult {
        AXTextDetailBoundsResult(value: nil, status: .missing)
    }

    func children(maximumCount: Int) -> AXTextDetailChildrenResult {
        let values = Array(childProviders.prefix(maximumCount))
        return AXTextDetailChildrenResult(
            values: values,
            status: values.count == childProviders.count ? .complete : .truncated
        )
    }
}

private func harnessTextResult(
    _ value: String?,
    status: AXTextDetailAttributeStatus = .complete
) -> AXTextDetailStringResult {
    AXTextDetailStringResult(value: value, status: status)
}

private func makeAXTextDetailConformanceVector() throws -> Data {
    let counter = HarnessSensitiveAccessorCounter()
    let children = [
        HarnessAXTextDetailProvider(
            role: harnessTextResult("AXTextField"),
            subrole: harnessTextResult("AXStandard", status: .truncated),
            value: "incomplete-subrole-secret",
            sensitiveCounter: counter
        ),
        HarnessAXTextDetailProvider(
            role: harnessTextResult("AXUnknown"),
            subrole: harnessTextResult("AXStandardContent"),
            value: "unknown-role-secret",
            sensitiveCounter: counter
        ),
        HarnessAXTextDetailProvider(
            role: harnessTextResult("AXTextField"),
            subrole: harnessTextResult("AXUnknown"),
            value: "unknown-subrole-secret",
            sensitiveCounter: counter
        ),
    ]
    let root = HarnessAXTextDetailProvider(
        role: harnessTextResult("AXWind", status: .truncated),
        subrole: harnessTextResult("AXStandardWindow"),
        value: "incomplete-role-secret",
        children: children,
        sensitiveCounter: counter
    )
    let envelope = try AXTextDetailSerializer.serialize(
        provider: root,
        snapshotID: "swift-python-identity",
        clock: { 0 }
    )
    try require(counter.count == 0, "sensitive AX accessor ran while identity was indeterminate")
    return try JSONSerialization.data(withJSONObject: [
        "artifact_base64": envelope.data.base64EncodedString(),
        "sensitive_accessor_count": counter.count,
    ], options: [.sortedKeys])
}

private func require(_ condition: @autoclosure () -> Bool, _ message: String) throws {
    guard condition() else {
        throw HarnessFailure.assertion(message)
    }
}

private func packageRoot() -> URL {
    URL(fileURLWithPath: #filePath)
        .deletingLastPathComponent()
        .deletingLastPathComponent()
        .deletingLastPathComponent()
}

private func shellOutput(_ arguments: [String]) throws -> String {
    let process = Process()
    let output = Pipe()
    process.executableURL = URL(fileURLWithPath: "/usr/bin/env")
    process.arguments = arguments
    process.standardOutput = output
    process.standardError = FileHandle.standardError
    try process.run()
    process.waitUntilExit()
    try require(process.terminationStatus == 0, "command failed: \(arguments.joined(separator: " "))")
    return String(decoding: output.fileHandleForReading.readDataToEndOfFile(), as: UTF8.self)
}

private func helperURL() throws -> URL {
    let root = packageRoot()
    let buildArguments = ["swift", "build", "-c", "debug", "--package-path", root.path]
    _ = try shellOutput(buildArguments)
    let binPath = try shellOutput(buildArguments + ["--show-bin-path"]).trimmingCharacters(in: .whitespacesAndNewlines)
    return URL(fileURLWithPath: binPath).appendingPathComponent("AstraMacComputerHelper")
}

private func runHelper(_ requests: [Data]) throws -> [Data] {
    let process = Process()
    let input = Pipe()
    let output = Pipe()
    let errors = Pipe()
    process.executableURL = try helperURL()
    process.standardInput = input
    process.standardOutput = output
    process.standardError = errors
    try process.run()
    for request in requests {
        input.fileHandleForWriting.write(request)
        input.fileHandleForWriting.write(Data([0x0A]))
    }
    try input.fileHandleForWriting.close()
    process.waitUntilExit()
    let stderr = errors.fileHandleForReading.readDataToEndOfFile()
    try require(process.terminationStatus == 0, "helper exited \(process.terminationStatus): \(String(decoding: stderr, as: UTF8.self))")
    try require(stderr.isEmpty, "helper wrote to stderr")

    let stdout = output.fileHandleForReading.readDataToEndOfFile()
    let bytes = [UInt8](stdout)
    let lines = bytes.split(separator: 0x0A, omittingEmptySubsequences: true).map { Data($0) }
    try require(lines.allSatisfy { $0.count <= maxMessageBytes }, "helper emitted an oversized response")
    return lines
}

private func jsonObject(_ data: Data) throws -> [String: Any] {
    let object = try JSONSerialization.jsonObject(with: data)
    guard let dictionary = object as? [String: Any] else {
        throw HarnessFailure.assertion("response was not a JSON object")
    }
    return dictionary
}

private func request(_ source: String) -> Data {
    Data(source.utf8)
}

private func malformedResponse(_ line: Data) throws -> [String: Any] {
    let response = try jsonObject(line)
    try require(response["ok"] as? Bool == false, "invalid request unexpectedly succeeded: \(response)")
    try require((response["error"] as? [String: Any])?["code"] as? String == "protocol_mismatch", "invalid request was not protocol_mismatch")
    return response
}

private func testCooperativeProtocolV4TypesAndStrictFrames() throws {
    try require(InteractionMode(rawValue: "background") == .background, "background interaction mode did not decode")
    try require(InteractionMode(rawValue: "foreground_takeover") == .foregroundTakeover, "foreground interaction mode did not decode")
    try require(DispatchBackend.axPress.rawValue == "ax_press", "dispatch backend encoding changed")
    let publicActionClasses = ["press", "text", "click", "double_click", "scroll", "drag"]
    try require(publicActionClasses.allSatisfy { DispatchActionClass(rawValue: $0) != nil }, "public action capability did not decode")
    try require(DispatchActionClass(rawValue: "keypress") == nil, "unsupported keypress capability decoded")
    try require(DispatchActionClass(rawValue: "wait") == nil, "wait leaked into the public action vocabulary")
    try require(CooperativeErrorCode.foregroundTakeoverRequired.rawValue == "foreground_takeover_required", "foreground takeover code changed")
    try require(CooperativeErrorCode.backgroundActionUnsupported.rawValue == "background_action_unsupported", "background unsupported code changed")
    try require(CooperativeErrorCode.userActivityPaused.rawValue == "user_activity_paused", "activity paused code changed")
    try require(CooperativeErrorCode.sidecarFailed.rawValue == "sidecar_failed", "sidecar failure code changed")

    let validSelect = #"{"protocol_version":4,"request_id":"select","operation":"select","payload":{"app_ref":"app","window_ref":"window"}}"#
    let validForegroundAct = #"{"protocol_version":4,"request_id":"foreground","operation":"act","payload":{"interaction_mode":"foreground_takeover","snapshot_id":"snapshot","plan_ref":"plan","takeover_ref":"takeover","fragment_hash":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","stage_index":0,"stage_hash":"0000000000000000000000000000000000000000000000000000000000000000","actions":[]}}"#
    let missingTakeoverRef = #"{"protocol_version":4,"request_id":"missing-takeover","operation":"act","payload":{"interaction_mode":"foreground_takeover","snapshot_id":"snapshot","plan_ref":"plan","actions":[]}}"#
    let extraField = #"{"protocol_version":4,"request_id":"extra","operation":"plan_actions","payload":{"interaction_mode":"background","snapshot_id":"snapshot","actions":[],"extra":true}}"#
    let legacyV1 = #"{"protocol_version":1,"request_id":"legacy","operation":"ping","payload":{}}"#
    let lines = try runHelper([validSelect, validForegroundAct, missingTakeoverRef, extraField, legacyV1].map(request))
    try require(lines.count == 5, "expected one response for every cooperative frame")
    let selectResponse = try jsonObject(lines[0])
    try require(selectResponse["ok"] as? Bool == false, "select unexpectedly resolved an unlisted target")
    try require(
        (selectResponse["error"] as? [String: Any])?["code"] as? String != "protocol_mismatch",
        "select remained on the Task 1 unsupported-operation placeholder"
    )
    let foregroundResponse = try jsonObject(lines[1])
    try require(foregroundResponse["ok"] as? Bool == false, "unbound foreground act unexpectedly succeeded")
    try require(
        (foregroundResponse["error"] as? [String: Any])?["code"] as? String != "protocol_mismatch",
        "valid foreground cooperative shape was rejected at protocol validation"
    )
    for line in lines.dropFirst(2) {
        let response = try malformedResponse(line)
        try require(response["protocol_version"] as? Int == 4, "v4 helper emitted the wrong protocol version")
    }
}

private func testHappyPath() throws {
    let lines = try runHelper([
        request(#"{"protocol_version":4,"request_id":"ping-1","operation":"ping","payload":{}}"#),
        request(#"{"protocol_version":4,"request_id":"status-1","operation":"status","payload":{}}"#),
        request(#"{"protocol_version":4,"request_id":"close-1","operation":"close","payload":{}}"#),
    ])
    try require(lines.count == 3, "expected 3 helper responses")
    let ping = try jsonObject(lines[0])
    let status = try jsonObject(lines[1])
    let close = try jsonObject(lines[2])
    try require(ping["protocol_version"] as? Int == 4 && (ping["result"] as? [String: Any])?["pong"] as? Bool == true, "ping response invalid")
    let permissions = (status["result"] as? [String: Any])?["permissions"] as? [String: Any]
    try require(permissions?["accessibility"] is Bool && permissions?["screen_recording"] is Bool, "status permission states invalid")
    try require((close["result"] as? [String: Any])?["closed"] as? Bool == true, "close response invalid")
}

private func testStrictFrameValidation() throws {
    let invalid = [
        #"{"protocol_version":1.0,"request_id":"version","operation":"ping","payload":{}}"#,
        #"{"protocol_version":true,"request_id":"bool","operation":"ping","payload":{}}"#,
        #"{"protocol_version":4,"request_id":"missing","operation":"ping"}"#,
        #"{"protocol_version":4,"request_id":"array","operation":"ping","payload":[]}"#,
        #"{"protocol_version":4,"request_id":"unknown","operation":"ping","payload":{},"extra":true}"#,
        #"{"protocol_version":4,"request_id":"first","request_id":"second","operation":"ping","payload":{}}"#,
    ]
    let lines = try runHelper(invalid.map(request))
    try require(lines.count == invalid.count, "expected one rejection for each invalid frame")
    for line in lines {
        _ = try malformedResponse(line)
    }
}

private func testRecognizedRequestErrorsEchoTheirIDs() throws {
    let lines = try runHelper([
        request(#"{"protocol_version":1,"request_id":"version-r1","operation":"ping","payload":{}}"#),
        request(#"{"protocol_version":4,"request_id":"operation-r2","operation":"not-supported","payload":{}}"#),
    ])
    try require(lines.count == 2, "expected two recognized-request failures")
    let wrongVersion = try malformedResponse(lines[0])
    let unsupportedOperation = try malformedResponse(lines[1])
    try require(wrongVersion["request_id"] as? String == "version-r1", "wrong-version response lost request ID")
    try require(unsupportedOperation["request_id"] as? String == "operation-r2", "unsupported-operation response lost request ID")
}

private func testMessageBoundaryAndRequestID() throws {
    let prefix = #"{"protocol_version":4,"request_id":"boundary","operation":"ping","payload":{"padding":""#
    let suffix = #""}}"#
    let exact = prefix + String(repeating: "x", count: maxMessageBytes - prefix.utf8.count - suffix.utf8.count) + suffix
    let oversized = exact + "x"
    let longID = #"{"protocol_version":4,"request_id":""# + String(repeating: "i", count: maxMessageBytes) + #"","operation":"ping","payload":{}}"#
    let lines = try runHelper([request(exact), request(oversized), request(longID)])
    try require(lines.count == 3, "expected boundary responses")
    let exactResponse = try jsonObject(lines[0])
    try require(exactResponse["ok"] as? Bool == true, "exact 4 MiB request should be accepted")
    _ = try malformedResponse(lines[1])
    _ = try malformedResponse(lines[2])
}

private func testObservationProtocolBoundaries() throws {
    let lines = try runHelper([
        request(#"{"protocol_version":4,"request_id":"apps-1","operation":"apps","payload":{}}"#),
        request(#"{"protocol_version":4,"request_id":"scope-1","operation":"snapshot","payload":{"app_ref":"app_missing","window_ref":"win_missing","scope":"global"}}"#),
    ])
    try require(lines.count == 2, "expected two observation responses")
    let apps = try jsonObject(lines[0])
    let scope = try jsonObject(lines[1])
    try require(apps["request_id"] as? String == "apps-1", "apps response lost request ID")
    let appsError = (apps["error"] as? [String: Any])?["code"] as? String
    try require(apps["ok"] as? Bool == true || appsError == "permission_denied", "apps must succeed or report missing Screen Recording permission")
    try require(scope["ok"] as? Bool == false, "invalid scope unexpectedly succeeded")
    try require((scope["error"] as? [String: Any])?["code"] as? String == "protocol_mismatch", "invalid scope was not rejected")
}

private struct HarnessFixtureCatalogSelection {
    let appRef: String
    let windowRef: String
    let catalogGeneration: Int
}

private final class HarnessFixtureCatalogEnvironment {
    private let bounds = CGRect(x: 100, y: 100, width: 640, height: 680)
    private var sequence = 0
    private var liveWindowID: CGWindowID = 7001
    private var windowOpen = true

    func catalog() -> WindowCatalogObservation {
        sequence += 1
        let appRef = "app_fixture_\(sequence)"
        let windowRef = "win_fixture_\(sequence)"
        let target = target(appRef: appRef, windowRef: windowRef)
        return WindowCatalogObservation(
            targets: [windowRef: target],
            apps: [.object([
                "app_ref": .string(appRef),
                "name": .string("AstraComputerFixture"),
                "bundle_id": .string("dev.astra.computer-fixture"),
                "app_version": .string("1.0.0"),
                "windows": .array([.object([
                    "window_ref": .string(windowRef),
                    "title": .string("Astra Computer Fixture"),
                    "bounds": CGRectJSON.encode(bounds),
                ])]),
            ])]
        )
    }

    func currentTarget(for catalogTarget: WindowTarget) -> WindowTarget? {
        guard windowOpen else { return nil }
        return target(appRef: catalogTarget.appRef, windowRef: catalogTarget.windowRef)
    }

    func closeSelectedWindow() { windowOpen = false }

    func replaceWithSameTitle() {
        liveWindowID += 1
        windowOpen = true
    }

    private func target(appRef: String, windowRef: String) -> WindowTarget {
        WindowTarget(
            appRef: appRef,
            windowRef: windowRef,
            pid: getpid(),
            windowID: liveWindowID,
            bounds: bounds,
            title: "Astra Computer Fixture",
            axIdentity: UInt(liveWindowID),
            interactionMode: .background
        )
    }
}

private struct HarnessPreparedFixtureAX {
    let tree: SerializedAXTree
    let detail: AXTextDetailEnvelope?
}

private final class HarnessPlanningOnlyProvider: ActionProviding {
    private let snapshotID: String
    private let state: ActionTargetState
    private let elements: [String: ActionElement]
    private(set) var performed = false

    init(snapshotID: String, state: ActionTargetState, elements: [String: ActionElement]) {
        self.snapshotID = snapshotID
        self.state = state
        self.elements = elements
    }

    func currentTargetState() throws -> ActionTargetState { state }

    func element(reference: String, snapshotID: String) -> ActionElement? {
        guard snapshotID == self.snapshotID else { return nil }
        return elements[reference]
    }

    func perform(_: ResolvedAction) throws -> ActionPerformance {
        performed = true
        throw HarnessFailure.assertion("planning executed an input action")
    }
}

private final class DeterministicFixtureAppStateObserver: WindowObserving {
    private let environment: HarnessFixtureCatalogEnvironment
    private let resolver: SystemWindowObserver
    private let artifactSession: SnapshotArtifactSession
    private let snapshotReferences = SnapshotReferenceRegistry()
    private(set) var selectCalls = 0
    private var snapshotSequence = 0

    init(directoryFD: Int32) {
        let environment = HarnessFixtureCatalogEnvironment()
        self.environment = environment
        self.resolver = SystemWindowObserver(
            permissions: StaticHarnessPermissions(),
            catalogBuilder: { environment.catalog() }
        )
        self.artifactSession = SnapshotArtifactSession(
            publishPNG: { data, name in
                _ = try ArtifactDirectory.publishPNG(
                    data,
                    named: name,
                    directoryFD: directoryFD,
                    syscalls: DarwinArtifactSyscalls()
                )
            },
            bundlePublisher: ArtifactBundlePublisher(directoryFD: directoryFD)
        )
    }

    func apps() throws -> JSONValue { try resolver.apps() }

    func select(appRef _: String, windowRef _: String) throws -> WindowTarget {
        selectCalls += 1
        throw WindowObservationError.targetGone
    }

    func getAppState(
        appRef: String,
        windowRef: String,
        catalogGeneration: Int,
        scope: String,
        artifactName: String,
        textDetail: SnapshotTextDetailRequest
    ) throws -> AppStateObservation {
        snapshotReferences.invalidate(.newSnapshot)
        guard scope == "target_window" else { throw WindowObservationError.invalidScope }
        let candidate = try resolver.resolveCatalogTarget(
            catalogGeneration: catalogGeneration,
            appRef: appRef,
            windowRef: windowRef,
            current: { self.environment.currentTarget(for: $0) }
        )
        guard candidate.interactionMode == .background else {
            throw WindowObservationError.staleTarget
        }

        snapshotSequence += 1
        let snapshotID = "fixture-state-\(snapshotSequence)"
        let request = GetAppStateRequest(
            appRef: appRef,
            windowRef: windowRef,
            catalogGeneration: catalogGeneration,
            scope: scope,
            artifactName: artifactName,
            textDetail: textDetail
        )
        let completed: SnapshotCaptureTransactionResult<AppStateObservation> = try completeSnapshotCaptureTransaction(
            capturePNG: { try harnessPNGData() },
            serializeAX: {
                let source = AXUIElementCreateApplication(getpid())
                let tree = AXSerializer.serialize(
                    AXNode(
                        role: "AXWindow",
                        subrole: "AXStandardWindow",
                        title: "Astra Computer Fixture",
                        bounds: candidate.bounds,
                        children: [
                            AXNode(
                                role: "AXStaticText",
                                subrole: "AXText",
                                label: "App state transaction marker",
                                value: "app-state:primary",
                                bounds: CGRect(x: 20, y: 20, width: 200, height: 20)
                            ),
                            AXNode(
                                role: "AXButton",
                                subrole: "AXStandardButton",
                                label: "Fixture button",
                                enabled: true,
                                actions: [kAXPressAction as String],
                                bounds: CGRect(x: 20, y: 60, width: 120, height: 30),
                                sourceElement: source
                            ),
                        ]
                    ),
                    snapshotID: snapshotID
                )
                return HarnessPreparedFixtureAX(
                    tree: tree,
                    detail: try self.smartDetail(textDetail, snapshotID: snapshotID)
                )
            },
            buildSnapshot: { _, prepared in
                var payload: [String: JSONValue] = [
                    "image_artifact": .string(artifactName),
                    "logical_size": .object(["width": .number(1), "height": .number(1)]),
                    "pixel_size": .object(["width": .number(1), "height": .number(1)]),
                    "backing_scale": .number(1),
                    "capture_bounds": CGRectJSON.encode(candidate.bounds),
                    "ax_tree": prepared.tree.asJSON(),
                ]
                if case let .on(detailName) = textDetail, let detail = prepared.detail {
                    payload["text_detail_artifact"] = .string(detailName)
                    payload["text_detail_metadata"] = smartSnapshotMetadata(detail, snapshotID: snapshotID)
                }
                return .object([
                    "snapshot_id": .string(snapshotID),
                    "payload": .object(payload),
                ])
            },
            prepareCommit: { snapshot in
                let observation = AppStateObservation(
                    target: candidate,
                    catalogGeneration: catalogGeneration,
                    snapshot: snapshot
                )
                guard validGetAppStateResponse(
                    result: observation.helperResultJSON,
                    snapshot: snapshot,
                    request: request
                ) else { throw WindowObservationError.captureFailed }
                return SnapshotFinalCommit(result: observation, commit: {})
            },
            validateFinalIdentity: {
                try self.resolver.resolveCatalogTarget(
                    catalogGeneration: catalogGeneration,
                    appRef: appRef,
                    windowRef: windowRef,
                    current: { self.environment.currentTarget(for: $0) }
                )
            },
            publish: { png, prepared in
                try self.artifactSession.publish(
                    png: png,
                    named: artifactName,
                    textDetail: textDetail,
                    detail: prepared.detail
                )
            },
            register: { finalTarget, prepared in
                let guardValue = ActionGuard(
                    pid: finalTarget.pid,
                    windowID: finalTarget.windowID,
                    bounds: finalTarget.bounds,
                    axIdentity: finalTarget.axIdentity ?? UInt(finalTarget.windowID),
                    snapshotID: snapshotID,
                    interactionMode: .background
                )
                self.snapshotReferences.register(
                    snapshotID: snapshotID,
                    references: prepared.tree.references(),
                    actionGuard: guardValue,
                    target: finalTarget
                )
            }
        )
        return completed.result
    }

    func planOrdinaryPress(snapshotID: String, elementRef: String) throws -> DispatchPlan {
        guard let context = snapshotReferences.contextForPlanning(snapshotID: snapshotID),
              let snapshotElement = context.references[elementRef]
        else { throw ActionExecutionError.staleSnapshot }
        let actionElement = ActionElement(
            element: snapshotElement.element,
            bounds: snapshotElement.bounds,
            role: snapshotElement.role,
            subrole: snapshotElement.subrole,
            enabled: snapshotElement.enabled,
            actions: snapshotElement.actions
        )
        let guardValue = context.guardValue
        let provider = HarnessPlanningOnlyProvider(
            snapshotID: snapshotID,
            state: ActionTargetState(
                pid: guardValue.pid,
                windowID: guardValue.windowID,
                bounds: guardValue.bounds,
                axIdentity: guardValue.axIdentity
            ),
            elements: [elementRef: actionElement]
        )
        let plan = try InputDispatcher(performer: provider).plan(
            actions: [.click(elementRef: elementRef)],
            context: DispatchContext(guardValue: guardValue)
        )
        guard !provider.performed else {
            throw HarnessFailure.assertion("planning performed an input action")
        }
        return plan
    }

    func registeredElement(reference: String, snapshotID: String) -> SnapshotElement? {
        snapshotReferences.element(for: reference, snapshotID: snapshotID)
    }

    private func smartDetail(
        _ request: SnapshotTextDetailRequest,
        snapshotID: String
    ) throws -> AXTextDetailEnvelope? {
        guard case .on = request else { return nil }
        let document = JSONValue.object([
            "schema_version": .number(1),
            "snapshot_id": .string(snapshotID),
            "coverage": .string("reported_ax_subtree"),
            "limits": .object([
                "maximum_depth": .number(20),
                "maximum_nodes": .number(4_000),
                "maximum_structural_string_bytes": .number(4 * 1_024),
                "maximum_value_bytes": .number(256 * 1_024),
                "maximum_aggregate_text_bytes": .number(4 * 1_024 * 1_024),
                "maximum_final_bytes": .number(8 * 1_024 * 1_024),
                "wall_clock_ms": .number(5_000),
            ]),
            "stats": .object([
                "node_count": .number(1),
                "max_depth_observed": .number(0),
                "truncated": .bool(false),
                "truncation_reasons": .array([]),
            ]),
            "root": .object([
                "node_id": .string("node_0"),
                "role": .string("AXStaticText"),
                "label": .string("App state transaction marker"),
                "value": .string("app-state:primary"),
            ]),
        ])
        return AXTextDetailEnvelope(
            data: try JSONEncoder().encode(document),
            nodeCount: 1,
            maxDepthObserved: 0,
            truncated: false,
            truncationReasons: []
        )
    }

    func snapshot(
        appRef _: String,
        windowRef _: String,
        scope _: String,
        artifactName _: String?,
        textDetail _: SnapshotTextDetailRequest
    ) throws -> JSONValue {
        throw WindowObservationError.captureFailed
    }

    func invalidateSnapshots() {}

    func closeSelectedWindow() { environment.closeSelectedWindow() }

    func replaceWithSameTitle() { environment.replaceWithSameTitle() }

    func closeArtifacts() { artifactSession.close() }
}

private func harnessFixtureSelection(_ response: HelperResponse) throws -> HarnessFixtureCatalogSelection {
    guard response.ok,
          case let .object(root)? = response.result,
          case let .number(generation)? = root["catalog_generation"],
          generation.rounded() == generation,
          case let .array(apps)? = root["apps"],
          apps.count == 1,
          case let .object(app) = apps[0],
          case let .string(appRef)? = app["app_ref"],
          case let .array(windows)? = app["windows"],
          windows.count == 1,
          case let .object(window) = windows[0],
          case let .string(windowRef)? = window["window_ref"]
    else { throw HarnessFailure.assertion("deterministic Fixture catalog was malformed") }
    return HarnessFixtureCatalogSelection(
        appRef: appRef,
        windowRef: windowRef,
        catalogGeneration: Int(generation)
    )
}

private func harnessFixtureGetStateRequest(
    _ selection: HarnessFixtureCatalogSelection,
    requestID: String,
    artifactName: String,
    detailName: String? = nil
) -> String {
    let detailFields = detailName.map {
        ",\"text_detail\":\"on\",\"text_detail_artifact_name\":\"\($0)\""
    } ?? ""
    return "{\"protocol_version\":4,\"request_id\":\"\(requestID)\",\"operation\":\"get_app_state\",\"payload\":{\"app_ref\":\"\(selection.appRef)\",\"window_ref\":\"\(selection.windowRef)\",\"catalog_generation\":\(selection.catalogGeneration),\"scope\":\"target_window\",\"artifact_name\":\"\(artifactName)\"\(detailFields)}}"
}

private struct HarnessFixtureSnapshotReferences {
    let snapshotID: String
    let markerRef: String
    let buttonRef: String
}

private func harnessFixtureSnapshotReferences(
    _ snapshot: JSONValue
) throws -> HarnessFixtureSnapshotReferences {
    guard case let .object(snapshotRoot) = snapshot,
          case let .string(snapshotID)? = snapshotRoot["snapshot_id"],
          case let .object(payload)? = snapshotRoot["payload"],
          case let .object(axTree)? = payload["ax_tree"],
          case let .array(children)? = axTree["children"]
    else { throw HarnessFailure.assertion("background Fixture snapshot omitted ordinary AX refs") }
    var references: [String: String] = [:]
    for child in children {
        guard case let .object(node) = child,
              case let .string(label)? = node["label"],
              case let .string(elementRef)? = node["element_ref"]
        else { continue }
        references[label] = elementRef
    }
    guard let markerRef = references["App state transaction marker"],
          let buttonRef = references["Fixture button"]
    else { throw HarnessFailure.assertion("background Fixture snapshot omitted marker or button ref") }
    return HarnessFixtureSnapshotReferences(
        snapshotID: snapshotID,
        markerRef: markerRef,
        buttonRef: buttonRef
    )
}

private func requireFixturePlanningUnavailable(
    observer: DeterministicFixtureAppStateObserver,
    references: HarnessFixtureSnapshotReferences,
    message: String
) throws {
    try require(
        observer.registeredElement(
            reference: references.buttonRef,
            snapshotID: references.snapshotID
        ) == nil,
        "\(message): registry still exposed the old ref"
    )
    do {
        _ = try observer.planOrdinaryPress(
            snapshotID: references.snapshotID,
            elementRef: references.buttonRef
        )
        throw HarnessFailure.assertion("\(message): old ref still planned")
    } catch is ActionExecutionError {
        // Expected: planning cannot resolve an invalidated or failed snapshot.
    }
}

private func testDeterministicBackgroundFixtureGetAppState() throws {
    try withArtifactDirectory { root, directoryFD in
        let observer = DeterministicFixtureAppStateObserver(directoryFD: directoryFD)
        defer { observer.closeArtifacts() }
        let dispatcher = Dispatcher(permissions: StaticHarnessPermissions(), windows: observer)
        guard let sentinelFrontmostPID = NSWorkspace.shared.frontmostApplication?.processIdentifier else {
            throw HarnessFailure.assertion("frontmost sentinel PID was unavailable")
        }
        guard let cursorBefore = CGEvent(source: nil)?.location else {
            throw HarnessFailure.assertion("real cursor position was unavailable")
        }
        let apps = dispatcher.handle(
            #"{"protocol_version":4,"request_id":"fixture-apps","operation":"apps","payload":{}}"#
        )
        let selection = try harnessFixtureSelection(apps)
        let imageName = harnessSmartBundleNames(21).image
        let request = GetAppStateRequest(
            appRef: selection.appRef,
            windowRef: selection.windowRef,
            catalogGeneration: selection.catalogGeneration,
            scope: "target_window",
            artifactName: imageName,
            textDetail: .off
        )
        let state = dispatcher.handle(harnessFixtureGetStateRequest(
            selection,
            requestID: "fixture-state",
            artifactName: imageName
        ))
        try require(state.ok, "background Fixture get_app_state failed")
        guard let result = state.result, let snapshot = state.snapshot else {
            throw HarnessFailure.assertion("background Fixture get_app_state omitted its atomic result")
        }
        try require(
            validGetAppStateResponse(result: result, snapshot: snapshot, request: request),
            "background Fixture response did not match its exact request"
        )
        let references = try harnessFixtureSnapshotReferences(snapshot)
        try require(
            references.markerRef.hasPrefix("ax_")
                && references.buttonRef.hasPrefix("ax_")
                && references.markerRef != references.buttonRef,
            "background Fixture AX refs were malformed"
        )
        try require(
            observer.registeredElement(
                reference: references.buttonRef,
                snapshotID: references.snapshotID
            ) != nil,
            "committed Fixture button ref was not registered"
        )
        let plan = try observer.planOrdinaryPress(
            snapshotID: references.snapshotID,
            elementRef: references.buttonRef
        )
        try require(
            plan.backends == [.axPress]
                && plan.cooperativeError == nil
                && !plan.requiresTakeover,
            "committed Fixture button ref did not produce a background AXPress plan"
        )
        let image = try Data(contentsOf: root.appendingPathComponent(imageName))
        try require(
            CGImageSourceCreateWithData(image as CFData, nil) != nil,
            "background Fixture artifact was not a valid PNG"
        )
        try require(
            NSWorkspace.shared.frontmostApplication?.processIdentifier == sentinelFrontmostPID,
            "background Fixture get_app_state changed the sentinel frontmost PID"
        )
        try require(CGEvent(source: nil)?.location == cursorBefore, "background Fixture get_app_state moved the real cursor")
        try require(observer.selectCalls == 0, "background Fixture get_app_state called select")

        let smartNames = harnessSmartBundleNames(25)
        let smartRequest = GetAppStateRequest(
            appRef: selection.appRef,
            windowRef: selection.windowRef,
            catalogGeneration: selection.catalogGeneration,
            scope: "target_window",
            artifactName: smartNames.image,
            textDetail: .on(artifactName: smartNames.detail)
        )
        let smart = dispatcher.handle(harnessFixtureGetStateRequest(
            selection,
            requestID: "fixture-smart-state",
            artifactName: smartNames.image,
            detailName: smartNames.detail
        ))
        guard smart.ok, let smartResult = smart.result, let smartSnapshot = smart.snapshot else {
            throw HarnessFailure.assertion("background Fixture Smart get_app_state failed")
        }
        let smartReferences = try harnessFixtureSnapshotReferences(smartSnapshot)
        try require(
            observer.registeredElement(
                reference: smartReferences.buttonRef,
                snapshotID: smartReferences.snapshotID
            ) != nil,
            "committed Smart Fixture ref was not registered"
        )
        try requireFixturePlanningUnavailable(
            observer: observer,
            references: references,
            message: "new committed snapshot"
        )
        try require(
            validGetAppStateResponse(result: smartResult, snapshot: smartSnapshot, request: smartRequest),
            "background Fixture Smart response did not match its exact request"
        )
        try require(
            String(smartNames.image.dropLast(".png".count))
                == String(smartNames.detail.dropLast(".ax.json".count)),
            "Smart get_app_state pair did not share one stem"
        )
        let smartImageURL = root.appendingPathComponent(smartNames.image)
        let smartDetailURL = root.appendingPathComponent(smartNames.detail)
        var smartImageBefore = stat()
        var smartDetailBefore = stat()
        try require(
            Darwin.lstat(smartImageURL.path, &smartImageBefore) == 0
                && Darwin.lstat(smartDetailURL.path, &smartDetailBefore) == 0,
            "Smart get_app_state pair was not published"
        )
        for value in [smartImageBefore, smartDetailBefore] {
            try require(
                (value.st_mode & S_IFMT) == S_IFREG
                    && (value.st_mode & mode_t(0o7777)) == mode_t(0o600)
                    && value.st_uid == getuid()
                    && value.st_nlink == 1,
                "Smart get_app_state pair did not retain private authority"
            )
        }
        try require(
            (smartImageBefore.st_dev, smartImageBefore.st_ino)
                != (smartDetailBefore.st_dev, smartDetailBefore.st_ino),
            "Smart get_app_state pair shared one inode"
        )
        let detailData = try Data(contentsOf: smartDetailURL)
        let detailDigest = SHA256.hash(data: detailData).map { String(format: "%02x", $0) }.joined()
        guard case let .object(smartRoot) = smartSnapshot,
              case let .string(smartSnapshotID)? = smartRoot["snapshot_id"],
              case let .object(smartPayload)? = smartRoot["payload"],
              case let .object(metadata)? = smartPayload["text_detail_metadata"],
              case let .string(metadataSnapshotID)? = metadata["snapshot_id"],
              case let .number(metadataByteCount)? = metadata["byte_count"],
              case let .string(metadataDigest)? = metadata["sha256"]
        else { throw HarnessFailure.assertion("Smart get_app_state metadata was incomplete") }
        try require(
            metadataSnapshotID == smartSnapshotID
                && metadataByteCount == Double(detailData.count)
                && metadataDigest == detailDigest,
            "Smart get_app_state detail metadata did not match its snapshot and SHA"
        )
        var smartImageAfter = stat()
        var smartDetailAfter = stat()
        try require(
            Darwin.lstat(smartImageURL.path, &smartImageAfter) == 0
                && Darwin.lstat(smartDetailURL.path, &smartDetailAfter) == 0
                && smartImageAfter.st_dev == smartImageBefore.st_dev
                && smartImageAfter.st_ino == smartImageBefore.st_ino
                && smartDetailAfter.st_dev == smartDetailBefore.st_dev
                && smartDetailAfter.st_ino == smartDetailBefore.st_ino,
            "Smart get_app_state pair changed inode authority after verification"
        )

        observer.closeSelectedWindow()
        let closed = dispatcher.handle(harnessFixtureGetStateRequest(
            selection,
            requestID: "fixture-closed",
            artifactName: harnessSmartBundleNames(22).image
        ))
        try require(!closed.ok && closed.error?.code == "target_gone", "closed old Fixture ref did not fail target_gone")
        try requireFixturePlanningUnavailable(
            observer: observer,
            references: smartReferences,
            message: "closed-window failure"
        )
    }

    try withArtifactDirectory { _, directoryFD in
        let observer = DeterministicFixtureAppStateObserver(directoryFD: directoryFD)
        defer { observer.closeArtifacts() }
        let dispatcher = Dispatcher(permissions: StaticHarnessPermissions(), windows: observer)
        let selection = try harnessFixtureSelection(dispatcher.handle(
            #"{"protocol_version":4,"request_id":"replace-apps","operation":"apps","payload":{}}"#
        ))
        let committed = dispatcher.handle(harnessFixtureGetStateRequest(
            selection,
            requestID: "fixture-before-replacement",
            artifactName: harnessSmartBundleNames(23).image
        ))
        guard committed.ok, let committedSnapshot = committed.snapshot else {
            throw HarnessFailure.assertion("pre-replacement Fixture snapshot failed")
        }
        let committedReferences = try harnessFixtureSnapshotReferences(committedSnapshot)
        _ = try observer.planOrdinaryPress(
            snapshotID: committedReferences.snapshotID,
            elementRef: committedReferences.buttonRef
        )
        observer.replaceWithSameTitle()
        let replaced = dispatcher.handle(harnessFixtureGetStateRequest(
            selection,
            requestID: "fixture-replaced",
            artifactName: harnessSmartBundleNames(26).image
        ))
        try require(!replaced.ok && replaced.error?.code == "stale_target", "same-title replacement reused an old Fixture ref")
        try require(observer.selectCalls == 0, "same-title replacement was selected")
        try requireFixturePlanningUnavailable(
            observer: observer,
            references: committedReferences,
            message: "same-title replacement failure"
        )
    }

    try withArtifactDirectory { _, directoryFD in
        let observer = DeterministicFixtureAppStateObserver(directoryFD: directoryFD)
        defer { observer.closeArtifacts() }
        let dispatcher = Dispatcher(permissions: StaticHarnessPermissions(), windows: observer)
        let selection = try harnessFixtureSelection(dispatcher.handle(
            #"{"protocol_version":4,"request_id":"refresh-apps-1","operation":"apps","payload":{}}"#
        ))
        let committed = dispatcher.handle(harnessFixtureGetStateRequest(
            selection,
            requestID: "fixture-before-refresh",
            artifactName: harnessSmartBundleNames(24).image
        ))
        guard committed.ok, let committedSnapshot = committed.snapshot else {
            throw HarnessFailure.assertion("pre-refresh Fixture snapshot failed")
        }
        let committedReferences = try harnessFixtureSnapshotReferences(committedSnapshot)
        _ = try observer.planOrdinaryPress(
            snapshotID: committedReferences.snapshotID,
            elementRef: committedReferences.buttonRef
        )
        _ = try harnessFixtureSelection(dispatcher.handle(
            #"{"protocol_version":4,"request_id":"refresh-apps-2","operation":"apps","payload":{}}"#
        ))
        let stale = dispatcher.handle(harnessFixtureGetStateRequest(
            selection,
            requestID: "fixture-stale-catalog",
            artifactName: harnessSmartBundleNames(27).image
        ))
        try require(!stale.ok && stale.error?.code == "stale_target", "native catalog refresh did not fail stale_target")
        try require(observer.selectCalls == 0, "catalog refresh selected a replacement")
        try requireFixturePlanningUnavailable(
            observer: observer,
            references: committedReferences,
            message: "catalog-refresh failure"
        )
    }
}

private final class HarnessSecureValueProbe: AXNodeAttributeProvider {
    private let role: BoundedAXStringResult
    private let subrole: BoundedAXStringResult
    private(set) var valueReads = 0

    init(role: String = "AXTextField", subrole: String = "AXSecureTextField") {
        self.role = BoundedAXStringResult(value: role, status: .complete)
        self.subrole = BoundedAXStringResult(value: subrole, status: .complete)
    }

    init(role: BoundedAXStringResult, subrole: BoundedAXStringResult) {
        self.role = role
        self.subrole = subrole
    }

    func stringValue(for attribute: String) -> BoundedAXStringResult {
        switch attribute {
        case kAXRoleAttribute: return role
        case kAXSubroleAttribute: return subrole
        case kAXValueAttribute:
            valueReads += 1
            return BoundedAXStringResult(value: "secret", status: .complete)
        default: return BoundedAXStringResult(value: nil, status: .complete)
        }
    }

    func boolValue(for _: String) -> Bool? { nil }
    func bounds() -> CGRect { .zero }
    func actions() -> [BoundedAXStringResult] { [] }
    func children(remaining _: Int) -> [any AXNodeAttributeProvider] { [] }
    func sourceElement() -> AXUIElement? { nil }
}

private func testSecureSubroleNeverReadsValue() throws {
    for probe in [
        HarnessSecureValueProbe(),
        HarnessSecureValueProbe(role: "AXSecureTextField", subrole: ""),
    ] {
        let node = AXNodeReader.read(provider: probe, windowBounds: .zero)
        try require(node.value == "<redacted>", "secure field was not redacted")
        try require(probe.valueReads == 0, "secure field read the underlying AX value")
    }
}

private func testBoundedCFStringAdapterFailsSecureOnIncompleteIdentity() throws {
    let suffixRole = "AXTextField" + String(repeating: "x", count: 600) + "Secure"
    let truncatedRole = boundedAXStringResult(suffixRole as CFString)
    try require(truncatedRole.status == .truncated, "long role did not preserve truncated status")
    let suffixProbe = HarnessSecureValueProbe(
        role: truncatedRole,
        subrole: BoundedAXStringResult(value: "", status: .complete)
    )
    let suffixNode = AXNodeReader.read(provider: suffixProbe, windowBounds: .zero)
    try require(suffixNode.value == "<redacted>" && suffixProbe.valueReads == 0, "truncated role read AXValue")

    let suffixSubrole = "AXTextField" + String(repeating: "x", count: 600) + "Secure"
    let truncatedSubrole = boundedAXStringResult(suffixSubrole as CFString)
    try require(truncatedSubrole.status == .truncated, "long subrole did not preserve truncated status")
    let subroleProbe = HarnessSecureValueProbe(
        role: BoundedAXStringResult(value: "AXTextField", status: .complete),
        subrole: truncatedSubrole
    )
    let subroleNode = AXNodeReader.read(provider: subroleProbe, windowBounds: .zero)
    try require(subroleNode.value == "<redacted>" && subroleProbe.valueReads == 0, "truncated subrole read AXValue")

    let hugeGrapheme = "a" + String(repeating: "\u{0301}", count: 20_000)
    let emptyRole = boundedAXStringResult(hugeGrapheme as CFString)
    try require(emptyRole.status == .truncated && emptyRole.value == nil, "oversized first grapheme was not an empty truncation")
    let emptyProbe = HarnessSecureValueProbe(
        role: emptyRole,
        subrole: BoundedAXStringResult(value: nil, status: .failed)
    )
    let emptyNode = AXNodeReader.read(provider: emptyProbe, windowBounds: .zero)
    try require(emptyNode.value == "<redacted>" && emptyProbe.valueReads == 0, "failed/empty identity read AXValue")
}

private func testBoundedTreeAndSnapshotReferenceContracts() throws {
    let leaf = AXNode(role: "AXStaticText", label: String(repeating: "x", count: 2_000), bounds: .zero)
    let root = AXNode(role: "AXWindow", bounds: .zero, children: Array(repeating: leaf, count: 3_000))
    let tree = AXSerializer.serialize(root, snapshotID: "caps")
    try require(tree.nodeCount <= 2_000, "AX node cap was exceeded")
    try require(tree.jsonByteCount <= 512 * 1024, "AX JSON cap was exceeded")
    let reference = ElementReference(snapshotID: "one", ordinal: 1)
    try require(!reference.isValid(for: "two"), "element reference crossed snapshots")
}

private final class BudgetedAXProvider: AXNodeAttributeProvider {
    let text: String?
    let offeredChildren: Int
    private(set) var requestedChildBudgets: [Int] = []

    init(text: String? = nil, offeredChildren: Int = 0) {
        self.text = text
        self.offeredChildren = offeredChildren
    }

    func stringValue(for attribute: String) -> BoundedAXStringResult {
        let value: String?
        switch attribute {
        case kAXRoleAttribute: value = "AXStaticText"
        case kAXDescriptionAttribute, kAXTitleAttribute, kAXHelpAttribute, kAXValueAttribute: value = text
        default: value = nil
        }
        return BoundedAXStringResult(value: value, status: .complete)
    }

    func boolValue(for _: String) -> Bool? { nil }
    func bounds() -> CGRect { .zero }
    func actions() -> [BoundedAXStringResult] { [] }
    func children(remaining: Int) -> [any AXNodeAttributeProvider] {
        requestedChildBudgets.append(remaining)
        return (0..<min(offeredChildren, remaining)).map { _ in BudgetedAXProvider() }
    }
    func sourceElement() -> AXUIElement? { nil }
}

private final class DepthAXProvider: AXNodeAttributeProvider {
    let remainingDepth: Int
    init(_ remainingDepth: Int) { self.remainingDepth = remainingDepth }
    func stringValue(for attribute: String) -> BoundedAXStringResult {
        BoundedAXStringResult(
            value: attribute == kAXRoleAttribute ? "AXGroup" : nil,
            status: .complete
        )
    }
    func boolValue(for _: String) -> Bool? { nil }
    func bounds() -> CGRect { .zero }
    func actions() -> [BoundedAXStringResult] { [] }
    func children(remaining _: Int) -> [any AXNodeAttributeProvider] {
        remainingDepth > 0 ? [DepthAXProvider(remainingDepth - 1)] : []
    }
    func sourceElement() -> AXUIElement? { nil }
}

private func testAXReaderHonorsFocusedDialogDepthLimit() throws {
    let node = AXNodeReader.read(
        provider: DepthAXProvider(8),
        windowBounds: .zero,
        maximumDepth: 2
    )
    try require(node.children.count == 1, "dialog depth limit dropped direct controls")
    try require(node.children[0].children.isEmpty, "dialog depth limit traversed a deep subtree")
}

private final class MenuAXProvider: AXNodeAttributeProvider {
    let role: String
    let title: String?
    let descendants: [MenuAXProvider]

    init(role: String, title: String? = nil, descendants: [MenuAXProvider] = []) {
        self.role = role
        self.title = title
        self.descendants = descendants
    }

    func stringValue(for attribute: String) -> BoundedAXStringResult {
        let value: String?
        switch attribute {
        case kAXRoleAttribute: value = role
        case kAXTitleAttribute: value = title
        default: value = nil
        }
        return BoundedAXStringResult(value: value, status: .complete)
    }

    func boolValue(for _: String) -> Bool? { nil }
    func bounds() -> CGRect { .zero }
    func actions() -> [BoundedAXStringResult] { [] }
    func children(remaining: Int) -> [any AXNodeAttributeProvider] {
        Array(descendants.prefix(remaining))
    }
    func sourceElement() -> AXUIElement? { nil }
}

private func testAXReaderIncludesBoundedApplicationMenuItems() throws {
    let menuItem = MenuAXProvider(role: "AXMenuItem", title: "输出为PDF...")
    let menu = MenuAXProvider(role: "AXMenu", descendants: [menuItem])
    let file = MenuAXProvider(role: "AXMenuBarItem", title: "文件", descendants: [menu])
    let menuBar = MenuAXProvider(role: "AXMenuBar", descendants: [file])
    let node = AXNodeReader.read(
        provider: menuBar,
        windowBounds: .zero,
        maximumDepth: applicationMenuBarMaximumDepth
    )
    try require(
        node.children.first?.children.first?.children.first?.title == "输出为PDF...",
        "bounded target-application menu observation omitted direct menu items"
    )
}

private final class ActionAXProvider: AXNodeAttributeProvider {
    let actionResults: [BoundedAXStringResult]
    init(actionResults: [BoundedAXStringResult]) { self.actionResults = actionResults }
    func stringValue(for attribute: String) -> BoundedAXStringResult {
        BoundedAXStringResult(value: attribute == kAXRoleAttribute ? "AXButton" : nil, status: .complete)
    }
    func boolValue(for _: String) -> Bool? { nil }
    func bounds() -> CGRect { .zero }
    func actions() -> [BoundedAXStringResult] { actionResults }
    func children(remaining _: Int) -> [any AXNodeAttributeProvider] { [] }
    func sourceElement() -> AXUIElement? { nil }
}

private func testActionAdapterReadsOnlyBoundedCFArrayPrefix() throws {
    let hugeGrapheme = "a" + String(repeating: "\u{0301}", count: 20_000)
    var raw: [Any] = [hugeGrapheme]
    raw.append(contentsOf: (1..<40).map { "AXAction\($0)" })
    raw[5] = NSNumber(value: 5)
    let array = unsafeBitCast(NSArray(array: raw), to: CFArray.self)
    var accessed: [Int] = []
    let results = boundedAXActionResults(array) { accessed.append($0) }

    try require(accessed == Array(0..<32), "action adapter accessed beyond the first 32 entries")
    try require(results.count == 32 && results[5].status == .failed, "action adapter did not preserve the malformed entry status")
    try require(results[0].status == .truncated && results[0].value == nil, "huge action grapheme lost truncation status")
    let node = AXNodeReader.read(provider: ActionAXProvider(actionResults: results), windowBounds: .zero)
    try require(node.actions.count == 30, "reader retained a failed/empty action")
    try require(!node.actions.contains(hugeGrapheme), "AXNode retained the complete huge action")
    try require(node.actions.allSatisfy { $0.count <= maximumAXStringCharacters && $0.utf8.count <= maximumAXReadStringBytes }, "AXNode retained an unbounded action")
}

private func countCollectedNodes(_ node: AXNode) -> Int {
    1 + node.children.reduce(0) { $0 + countCollectedNodes($1) }
}

private func testAXReaderBoundsCollectionBeforeSerialization() throws {
    let huge = String(repeating: "🧑🏽‍💻", count: 200_000)
    let provider = BudgetedAXProvider(text: huge, offeredChildren: 10_000)
    let node = AXNodeReader.read(provider: provider, windowBounds: .zero)

    try require(provider.requestedChildBudgets == [maximumAXNodes - 1], "reader did not request exactly the remaining node budget")
    try require(countCollectedNodes(node) == maximumAXNodes, "reader crossed the 2,000-node hard limit")
    try require(node.label != huge && (node.label?.count ?? 0) <= maximumAXStringCharacters, "intermediate AXNode retained the huge label")
    try require((node.label?.utf8.count ?? 0) <= 16 * 1024, "intermediate AXNode label exceeded its UTF-8 cap")
    try require(node.value != huge && (node.value?.count ?? 0) <= maximumAXStringCharacters, "intermediate AXNode retained the huge value")
}

private final class FaultingArtifactSyscalls: ArtifactSyscalls {
    enum Fault {
        case none
        case writeEINTROnce
        case writeHard
        case fileSync
        case rename(Int32)
        case directorySync
    }

    let directoryFD: Int32
    let fault: Fault
    private var interrupted = false

    init(directoryFD: Int32, fault: Fault) {
        self.directoryFD = directoryFD
        self.fault = fault
    }

    func openFile(at directoryFD: Int32, name: String, flags: Int32, mode: mode_t) -> Int32 {
        name.withCString { Darwin.openat(directoryFD, $0, flags, mode) }
    }

    func writeFile(_ fileDescriptor: Int32, buffer: UnsafeRawPointer, count: Int) -> Int {
        switch fault {
        case .writeEINTROnce where !interrupted:
            interrupted = true
            errno = EINTR
            return -1
        case .writeHard:
            errno = EIO
            return -1
        default:
            return Darwin.write(fileDescriptor, buffer, count)
        }
    }

    func sync(_ fileDescriptor: Int32) -> Int32 {
        switch fault {
        case .fileSync where fileDescriptor != directoryFD:
            errno = EIO
            return -1
        case .directorySync where fileDescriptor == directoryFD:
            errno = EIO
            return -1
        default:
            return Darwin.fsync(fileDescriptor)
        }
    }

    func renameExclusive(at directoryFD: Int32, from temporary: String, to final: String) -> Int32 {
        if case let .rename(code) = fault {
            errno = code
            return -1
        }
        return temporary.withCString { source in
            final.withCString { destination in
                Darwin.renameatx_np(directoryFD, source, directoryFD, destination, UInt32(RENAME_EXCL))
            }
        }
    }

    func unlink(at directoryFD: Int32, name: String) -> Int32 {
        name.withCString { Darwin.unlinkat(directoryFD, $0, 0) }
    }
}

private func withArtifactDirectory(_ body: (URL, Int32) throws -> Void) throws {
    let root = FileManager.default.temporaryDirectory.appendingPathComponent("astra-artifact-\(UUID().uuidString)")
    try FileManager.default.createDirectory(at: root, withIntermediateDirectories: false, attributes: [.posixPermissions: 0o700])
    let descriptor = Darwin.open(root.path, O_RDONLY | O_DIRECTORY | O_CLOEXEC)
    try require(descriptor >= 0, "failed to open artifact directory")
    defer {
        Darwin.close(descriptor)
        try? FileManager.default.removeItem(at: root)
    }
    try body(root, descriptor)
}

private func temporaryEntries(_ root: URL) throws -> [String] {
    try FileManager.default.contentsOfDirectory(atPath: root.path).filter { $0.hasSuffix(".tmp") }
}

private func testArtifactWriteRetriesEINTRAndPublishes0600() throws {
    try withArtifactDirectory { root, descriptor in
        let data = Data("complete".utf8)
        let result = try ArtifactDirectory.publishPNG(
            data,
            named: "capture.png",
            directoryFD: descriptor,
            syscalls: FaultingArtifactSyscalls(directoryFD: descriptor, fault: .writeEINTROnce)
        )
        let final = root.appendingPathComponent("capture.png")
        try require(result == .durable, "successful publish was not durable")
        let finalData = try Data(contentsOf: final)
        try require(finalData == data, "EINTR retry did not publish complete bytes")
        let mode = try FileManager.default.attributesOfItem(atPath: final.path)[.posixPermissions] as? NSNumber
        try require(mode?.intValue == 0o600, "published artifact mode was not 0600")
        let leftovers = try temporaryEntries(root)
        try require(leftovers.isEmpty, "successful publish leaked a temporary file")
    }
}

private func testArtifactWriteFailureLeavesNoFinalOrTemporary() throws {
    try withArtifactDirectory { root, descriptor in
        do {
            _ = try ArtifactDirectory.publishPNG(Data("partial".utf8), named: "capture.png", directoryFD: descriptor, syscalls: FaultingArtifactSyscalls(directoryFD: descriptor, fault: .writeHard))
            throw HarnessFailure.assertion("hard write failure unexpectedly succeeded")
        } catch let error as ArtifactPublicationError {
            try require(error.stage == .write, "hard write failure reported the wrong stage")
        }
        try require(!FileManager.default.fileExists(atPath: root.appendingPathComponent("capture.png").path), "hard write failure exposed a final artifact")
        let leftovers = try temporaryEntries(root)
        try require(leftovers.isEmpty, "hard write failure leaked a temporary file")
    }
}

private func testArtifactFileSyncFailureLeavesNoFinalOrTemporary() throws {
    try withArtifactDirectory { root, descriptor in
        do {
            _ = try ArtifactDirectory.publishPNG(Data("complete".utf8), named: "capture.png", directoryFD: descriptor, syscalls: FaultingArtifactSyscalls(directoryFD: descriptor, fault: .fileSync))
            throw HarnessFailure.assertion("file fsync failure unexpectedly succeeded")
        } catch let error as ArtifactPublicationError {
            try require(error.stage == .fileSync && !error.finalVisible, "file fsync failure state was inaccurate")
        }
        try require(!FileManager.default.fileExists(atPath: root.appendingPathComponent("capture.png").path), "file fsync failure exposed a final artifact")
        let leftovers = try temporaryEntries(root)
        try require(leftovers.isEmpty, "file fsync failure leaked a temporary file")
    }
}

private func testArtifactRenameConflictPreservesExistingFinal() throws {
    try withArtifactDirectory { root, descriptor in
        let final = root.appendingPathComponent("capture.png")
        try Data("existing".utf8).write(to: final)
        do {
            _ = try ArtifactDirectory.publishPNG(Data("replacement".utf8), named: "capture.png", directoryFD: descriptor, syscalls: FaultingArtifactSyscalls(directoryFD: descriptor, fault: .rename(EEXIST)))
            throw HarnessFailure.assertion("rename conflict unexpectedly succeeded")
        } catch let error as ArtifactPublicationError {
            try require(error.stage == .publish && error.errnoCode == EEXIST, "rename conflict state was inaccurate")
        }
        let finalData = try Data(contentsOf: final)
        let leftovers = try temporaryEntries(root)
        try require(finalData == Data("existing".utf8), "rename conflict changed the existing final")
        try require(leftovers.isEmpty, "rename conflict leaked a temporary file")
    }
}

private func testDarwinArtifactPublisherNeverOverwrites() throws {
    try withArtifactDirectory { root, descriptor in
        let final = root.appendingPathComponent("capture.png")
        try Data("existing".utf8).write(to: final)
        do {
            _ = try ArtifactDirectory.publishPNG(Data("replacement".utf8), named: "capture.png", directoryFD: descriptor, syscalls: DarwinArtifactSyscalls())
            throw HarnessFailure.assertion("Darwin publisher overwrote an existing final")
        } catch let error as ArtifactPublicationError {
            try require(error.stage == .publish && error.errnoCode == EEXIST, "Darwin no-overwrite failure was not EEXIST")
        }
        let finalData = try Data(contentsOf: final)
        let leftovers = try temporaryEntries(root)
        try require(finalData == Data("existing".utf8), "Darwin publisher changed the existing final")
        try require(leftovers.isEmpty, "Darwin no-overwrite failure leaked a temporary file")
    }
}

private func testArtifactRenameFailureLeavesNoFinalOrTemporary() throws {
    try withArtifactDirectory { root, descriptor in
        do {
            _ = try ArtifactDirectory.publishPNG(Data("complete".utf8), named: "capture.png", directoryFD: descriptor, syscalls: FaultingArtifactSyscalls(directoryFD: descriptor, fault: .rename(EIO)))
            throw HarnessFailure.assertion("rename failure unexpectedly succeeded")
        } catch let error as ArtifactPublicationError {
            try require(error.stage == .publish && !error.finalVisible, "rename failure state was inaccurate")
        }
        try require(!FileManager.default.fileExists(atPath: root.appendingPathComponent("capture.png").path), "rename failure exposed a final artifact")
        let leftovers = try temporaryEntries(root)
        try require(leftovers.isEmpty, "rename failure leaked a temporary file")
    }
}

private func testArtifactDirectorySyncFailureReportsVisibleCompleteFinal() throws {
    try withArtifactDirectory { root, descriptor in
        let data = Data("complete".utf8)
        do {
            _ = try ArtifactDirectory.publishPNG(data, named: "capture.png", directoryFD: descriptor, syscalls: FaultingArtifactSyscalls(directoryFD: descriptor, fault: .directorySync))
            throw HarnessFailure.assertion("directory fsync failure unexpectedly succeeded")
        } catch let error as ArtifactPublicationError {
            try require(error.stage == .directorySync && error.finalVisible && error.bytesComplete, "directory fsync failure did not report published-but-uncertain state")
        }
        let finalData = try Data(contentsOf: root.appendingPathComponent("capture.png"))
        let leftovers = try temporaryEntries(root)
        try require(finalData == data, "directory fsync failure left incomplete final bytes")
        try require(leftovers.isEmpty, "directory fsync failure leaked a temporary file")
    }
}

private final class HarnessBundleFaultSyscalls: ArtifactSyscalls {
    enum Fault { case secondRename, firstDirectorySync }

    let directoryFD: Int32
    let fault: Fault
    private var renameCalls = 0
    private var directorySyncCalls = 0

    init(directoryFD: Int32, fault: Fault) {
        self.directoryFD = directoryFD
        self.fault = fault
    }

    func openFile(at directoryFD: Int32, name: String, flags: Int32, mode: mode_t) -> Int32 {
        name.withCString { Darwin.openat(directoryFD, $0, flags, mode) }
    }

    func writeFile(_ fileDescriptor: Int32, buffer: UnsafeRawPointer, count: Int) -> Int {
        Darwin.write(fileDescriptor, buffer, count)
    }

    func sync(_ fileDescriptor: Int32) -> Int32 {
        if fileDescriptor == directoryFD {
            directorySyncCalls += 1
            if case .firstDirectorySync = fault, directorySyncCalls == 1 {
                errno = EIO
                return -1
            }
        }
        return Darwin.fsync(fileDescriptor)
    }

    func renameExclusive(at directoryFD: Int32, from temporary: String, to final: String) -> Int32 {
        renameCalls += 1
        if case .secondRename = fault, renameCalls == 2 {
            errno = EIO
            return -1
        }
        return temporary.withCString { source in
            final.withCString { destination in
                Darwin.renameatx_np(directoryFD, source, directoryFD, destination, UInt32(RENAME_EXCL))
            }
        }
    }

    func unlink(at directoryFD: Int32, name: String) -> Int32 {
        name.withCString { Darwin.unlinkat(directoryFD, $0, 0) }
    }
}

private func harnessSmartBundleNames(_ index: Int) -> (image: String, detail: String) {
    let suffix = String(index, radix: 16)
    let token = String(repeating: "0", count: 32 - suffix.count) + suffix
    return ("snapshot-\(token).png", "snapshot-\(token).ax.json")
}

private func testSmartArtifactBundlePOSIXAndRecoveredQuota() throws {
    try withArtifactDirectory { root, descriptor in
        let names = harnessSmartBundleNames(1)
        let publisher = ArtifactBundlePublisher(directoryFD: descriptor)
        let publication = try publisher.publish(
            image: Data("png".utf8), imageName: names.image,
            detail: Data("detail".utf8), detailName: names.detail
        )
        try require(
            publication == .durable,
            "smart artifact bundle was not durable"
        )
        for name in [names.image, names.detail] {
            var value = stat()
            let result = name.withCString { Darwin.fstatat(descriptor, $0, &value, AT_SYMLINK_NOFOLLOW) }
            try require(
                result == 0
                    && (value.st_mode & S_IFMT) == S_IFREG
                    && (value.st_mode & mode_t(0o7777)) == mode_t(0o600)
                    && value.st_uid == getuid()
                    && value.st_nlink == 1,
                "smart artifact final identity/mode was unsafe"
            )
        }
        let recovered = ArtifactBundlePublisher(
            directoryFD: descriptor,
            maximumDetailArtifacts: 1,
            maximumDetailBytes: 64 * 1024 * 1024
        )
        let over = harnessSmartBundleNames(2)
        do {
            _ = try recovered.publish(
                image: Data("png".utf8), imageName: over.image,
                detail: Data("detail".utf8), detailName: over.detail
            )
            throw HarnessFailure.assertion("recovered quota did not reject the second detail")
        } catch ArtifactBundleError.quotaExceeded {}
        recovered.close()
        let retainedEntries = try FileManager.default.contentsOfDirectory(atPath: root.path)
        try require(
            Set(retainedEntries) == [names.image, names.detail],
            "quota/close deleted a still-referenceable artifact"
        )
    }
}

private func testSmartArtifactBundlePoisonAndRollbackLifecycle() throws {
    for fault in [HarnessBundleFaultSyscalls.Fault.secondRename, .firstDirectorySync] {
        try withArtifactDirectory { root, descriptor in
            let syscalls = HarnessBundleFaultSyscalls(directoryFD: descriptor, fault: fault)
            let publisher = ArtifactBundlePublisher(directoryFD: descriptor, syscalls: syscalls)
            let names = harnessSmartBundleNames(3)
            do {
                _ = try publisher.publish(
                    image: Data("png".utf8), imageName: names.image,
                    detail: Data("detail".utf8), detailName: names.detail
                )
                throw HarnessFailure.assertion("uncertain bundle fault unexpectedly succeeded")
            } catch let error as ArtifactPublicationError {
                try require(error.publisherPoisoned, "uncertain bundle fault did not poison its publisher")
            }
            let remainingEntries = try FileManager.default.contentsOfDirectory(atPath: root.path)
            try require(
                remainingEntries.count == 2
                    && remainingEntries.allSatisfy { $0.hasSuffix(".quarantine") },
                "uncertain bundle fault did not isolate both request artifacts"
            )
            let retry = harnessSmartBundleNames(4)
            do {
                _ = try publisher.publish(
                    image: Data("png".utf8), imageName: retry.image,
                    detail: Data("detail".utf8), detailName: retry.detail
                )
                throw HarnessFailure.assertion("poisoned publisher accepted another bundle")
            } catch ArtifactBundleError.poisoned {}
            publisher.close()
        }
    }
}

private func testWindowIdentityStabilityContracts() throws {
    let target = CaptureIdentity(pid: 42, windowID: 7, bounds: CGRect(x: 10, y: 20, width: 300, height: 200), axIdentity: 99)
    try verifyCaptureIdentity(target: target, before: target, after: target)
    let switched = CaptureIdentity(pid: 42, windowID: 8, bounds: target.bounds, axIdentity: 100)
    try require(throwsWindowObservationError { try verifyCaptureIdentity(target: target, before: target, after: switched) }, "same-app window switch was accepted")
    let moved = CaptureIdentity(pid: 42, windowID: 7, bounds: target.bounds.offsetBy(dx: 5, dy: 0), axIdentity: 99)
    try require(throwsWindowObservationError { try verifyCaptureIdentity(target: target, before: target, after: moved) }, "window movement during capture was accepted")
    try require(throwsWindowObservationError { try verifyCaptureIdentity(target: target, before: nil, after: target) }, "disappeared pre-capture window was accepted")
    try require(throwsWindowObservationError { try verifyCaptureIdentity(target: target, before: target, after: nil) }, "disappeared post-capture window was accepted")
}

private final class HarnessExactWindowImageProvider: ExactWindowImageProviding {
    let fallbackImageValue: CGImage?
    private(set) var fallbackWindowIDs: [CGWindowID] = []

    init(fallbackImage: CGImage?) { fallbackImageValue = fallbackImage }

    func primaryImage() throws -> CGImage { throw PrimaryWindowCaptureError.timedOut }
    func fallbackImage(for windowID: CGWindowID) -> CGImage? {
        fallbackWindowIDs.append(windowID)
        return fallbackImageValue
    }
}

private func harnessTestImage() -> CGImage {
    let bytes = Data([0, 0, 0, 255]) as CFData
    let provider = CGDataProvider(data: bytes)!
    return CGImage(
        width: 1, height: 1, bitsPerComponent: 8, bitsPerPixel: 32, bytesPerRow: 4,
        space: CGColorSpaceCreateDeviceRGB(),
        bitmapInfo: CGBitmapInfo(rawValue: CGImageAlphaInfo.premultipliedLast.rawValue),
        provider: provider, decode: nil, shouldInterpolate: false, intent: .defaultIntent
    )!
}

private func harnessPNGData() throws -> Data {
    let data = NSMutableData()
    guard let destination = CGImageDestinationCreateWithData(data, "public.png" as CFString, 1, nil) else {
        throw HarnessFailure.assertion("failed to create PNG destination")
    }
    CGImageDestinationAddImage(destination, harnessTestImage(), nil)
    try require(CGImageDestinationFinalize(destination), "failed to encode harness PNG")
    return data as Data
}

private final class HarnessExactWindowCommandRunner: ExactWindowCommandRunning {
    enum Result {
        case validPNG
        case symlinkPNG
        case invalidPNG
        case exited(Int32)
        case timedOut
    }

    let result: Result
    private(set) var calls: [(String, [String], [String: String], TimeInterval)] = []
    private(set) var outputExistedBeforeRun: Bool?

    init(_ result: Result) { self.result = result }

    func run(
        executable: String,
        arguments: [String],
        environment: [String: String],
        timeout: TimeInterval
    ) -> ExactWindowCommandOutcome {
        calls.append((executable, arguments, environment, timeout))
        outputExistedBeforeRun = FileManager.default.fileExists(atPath: arguments.last!)
        switch result {
        case .validPNG:
            try? harnessPNGData().write(to: URL(fileURLWithPath: arguments.last!))
            return .exited(0)
        case .symlinkPNG:
            try? FileManager.default.createSymbolicLink(atPath: arguments.last!, withDestinationPath: "/dev/null")
            return .exited(0)
        case .invalidPNG:
            try? Data("not a PNG".utf8).write(to: URL(fileURLWithPath: arguments.last!))
            return .exited(0)
        case let .exited(status):
            return .exited(status)
        case .timedOut:
            return .timedOut
        }
    }
}

private func testExactWindowCommandContracts() throws {
    try withArtifactDirectory { root, descriptor in
        let valid = HarnessExactWindowCommandRunner(.validPNG)
        let image = ArtifactDirectory.captureExactWindow(
            windowID: 73,
            directoryFD: descriptor,
            directoryPath: root.path,
            runner: valid
        )
        try require(image != nil, "valid exact-window PNG was rejected")
        try require(valid.calls.count == 1, "exact-window command did not run exactly once")
        try require(valid.outputExistedBeforeRun == false, "exact-window command was given a pre-existing redirectable output path")
        let call = valid.calls[0]
        try require(call.0 == "/usr/sbin/screencapture", "exact-window command executable was not fixed")
        try require(call.1.count == 5, "exact-window command arguments changed")
        try require(Array(call.1.prefix(4)) == ["-x", "-t", "png", "-l73"], "exact-window command did not bind the selected window ID")
        let commandRoot = URL(fileURLWithPath: call.1[4]).deletingLastPathComponent().resolvingSymlinksInPath().path
        try require(commandRoot == root.resolvingSymlinksInPath().path, "exact-window command output escaped the held directory")
        try require(!URL(fileURLWithPath: call.1[4]).lastPathComponent.hasPrefix("."), "exact-window command used a hidden filename rejected by screencapture")
        try require(call.2 == ["LANG": "C", "PATH": "/usr/bin:/bin:/usr/sbin:/sbin"], "exact-window command environment was not minimal")
        try require(call.3 == 5, "exact-window command timeout changed")
        let successEntries = try FileManager.default.contentsOfDirectory(atPath: root.path)
        try require(successEntries.isEmpty, "successful exact-window capture leaked its temporary file")

        for failure in [
            HarnessExactWindowCommandRunner.Result.timedOut,
            .exited(7),
            .invalidPNG,
            .symlinkPNG,
        ] {
            let runner = HarnessExactWindowCommandRunner(failure)
            try require(
                ArtifactDirectory.captureExactWindow(
                    windowID: 73,
                    directoryFD: descriptor,
                    directoryPath: root.path,
                    runner: runner
                ) == nil,
                "failed exact-window command unexpectedly returned an image"
            )
            let failureEntries = try FileManager.default.contentsOfDirectory(atPath: root.path)
            try require(failureEntries.isEmpty, "failed exact-window capture leaked its temporary file")
        }

        let mismatched = HarnessExactWindowCommandRunner(.validPNG)
        try require(
            ArtifactDirectory.captureExactWindow(
                windowID: 73,
                directoryFD: descriptor,
                directoryPath: root.deletingLastPathComponent().path,
                runner: mismatched
            ) == nil,
            "mismatched exact-window directory path unexpectedly succeeded"
        )
        try require(mismatched.calls.isEmpty, "mismatched exact-window directory path launched the command")
    }
}

private func testSystemExactWindowCommandRunner() throws {
    let outcome = SystemExactWindowCommandRunner().run(
        executable: "/usr/bin/true",
        arguments: [],
        environment: ["LANG": "C", "PATH": "/usr/bin:/bin:/usr/sbin:/sbin"],
        timeout: 1
    )
    try require(outcome == .exited(0), "system exact-window command runner could not launch a fixed executable: \(outcome)")
    let started = Date()
    let timedOut = SystemExactWindowCommandRunner().run(
        executable: "/bin/sleep",
        arguments: ["10"],
        environment: ["LANG": "C", "PATH": "/usr/bin:/bin:/usr/sbin:/sbin"],
        timeout: 0.05
    )
    try require(timedOut == .timedOut, "system exact-window command runner did not report timeout")
    try require(Date().timeIntervalSince(started) < 2.5, "system exact-window command runner did not terminate its process group promptly")
}

private func testExactWindowCaptureFallbackContracts() throws {
    let successful = HarnessExactWindowImageProvider(fallbackImage: harnessTestImage())
    _ = try captureExactWindowImage(windowID: 73, provider: successful)
    try require(successful.fallbackWindowIDs == [73], "fallback did not use the exact selected window ID")

    let failed = HarnessExactWindowImageProvider(fallbackImage: nil)
    do {
        _ = try captureExactWindowImage(windowID: 73, provider: failed)
        throw HarnessFailure.assertion("missing exact-window fallback unexpectedly succeeded")
    } catch is WindowObservationError {
        try require(failed.fallbackWindowIDs == [73], "failed fallback broadened beyond the selected window ID")
    }

    let bounds = CGRect(x: 20, y: 30, width: 400, height: 300)
    let target = CaptureIdentity(pid: 42, windowID: 73, bounds: bounds, axIdentity: 91)
    let switched = CaptureIdentity(pid: 42, windowID: 74, bounds: bounds, axIdentity: 92)
    try require(
        throwsWindowObservationError {
            try verifyCaptureIdentity(target: target, before: target, after: switched)
        },
        "target change after exact-window fallback was accepted"
    )
}

private func testContainedTransientWindowBoundary() throws {
    let target = CGRect(x: 100, y: 100, width: 800, height: 600)
    try require(
        trustedFocusedWindow(expectedIdentity: 10, targetBounds: target, focusedIdentity: 10, focusedBounds: target),
        "the locked AX window was rejected"
    )
    try require(
        trustedFocusedWindow(
            expectedIdentity: 10,
            targetBounds: target,
            focusedIdentity: 11,
            focusedBounds: CGRect(x: 250, y: 250, width: 300, height: 80)
        ),
        "a contained app-owned transient was rejected"
    )
    try require(
        !trustedFocusedWindow(expectedIdentity: 10, targetBounds: target, focusedIdentity: 11, focusedBounds: target),
        "a different same-size window was accepted as a transient"
    )
    try require(
        !trustedFocusedWindow(
            expectedIdentity: 10,
            targetBounds: target,
            focusedIdentity: 11,
            focusedBounds: CGRect(x: 50, y: 250, width: 300, height: 80)
        ),
        "an out-of-target transient was accepted"
    )
    try require(
        !trustedFocusedTransition(
            expectedIdentity: 10,
            targetBounds: target,
            beforeIdentity: 11,
            beforeBounds: CGRect(x: 200, y: 180, width: 500, height: 300),
            afterIdentity: 12,
            afterBounds: CGRect(x: 300, y: 220, width: 300, height: 180)
        ),
        "a transition between two contained transient roots was accepted as one snapshot"
    )
    try require(
        reusableFrontmostTarget(
            frontmostPID: 42,
            targetPID: 42,
            expectedIdentity: 10,
            targetBounds: target,
            focusedIdentity: 11,
            focusedBounds: CGRect(x: 250, y: 200, width: 400, height: 250)
        ),
        "a frontmost contained modal could not reuse its locked underlying target"
    )
    try require(
        !reusableFrontmostTarget(
            frontmostPID: 43,
            targetPID: 42,
            expectedIdentity: 10,
            targetBounds: target,
            focusedIdentity: 11,
            focusedBounds: CGRect(x: 250, y: 200, width: 400, height: 250)
        ),
        "a different frontmost application reused the target"
    )

    let textField = BoundedAXStringResult(value: "AXTextField", status: .complete)
    let noSubrole = BoundedAXStringResult(value: nil, status: .complete)
    try require(
        trustedFocusedElement(
            role: textField,
            subrole: noSubrole,
            enabled: nil,
            targetBounds: target,
            focusedBounds: CGRect(x: 250, y: 250, width: 300, height: 30)
        ),
        "a contained complete non-secure focused text element was rejected"
    )
    try require(
        !trustedFocusedElement(
            role: textField,
            subrole: BoundedAXStringResult(value: "AXSecureTextField", status: .complete),
            enabled: true,
            targetBounds: target,
            focusedBounds: CGRect(x: 250, y: 250, width: 300, height: 30)
        ),
        "a secure focused element was accepted as a window fallback"
    )
}

private func testContainedAppOwnedOverlayDetection() throws {
    let target = CGRect(x: 0, y: 33, width: 1512, height: 860)
    let records = [
        VisibleWindowRecord(pid: 42, windowID: 73, bounds: target, layer: 0, alpha: 1),
        VisibleWindowRecord(
            pid: 42,
            windowID: 74,
            bounds: CGRect(x: 316, y: 136, width: 880, height: 448),
            layer: 8,
            alpha: 1
        ),
    ]
    try require(
        containedAppOwnedOverlayActive(
            targetPID: 42,
            targetWindowID: 73,
            targetBounds: target,
            records: records
        ),
        "contained same-app overlay was not detected"
    )
    let layerZeroOverlay = [
        records[0],
        VisibleWindowRecord(
            pid: 42,
            windowID: 75,
            bounds: CGRect(x: 526, y: 187, width: 460, height: 345),
            layer: 0,
            alpha: 1
        ),
    ]
    try require(
        containedAppOwnedOverlayActive(
            targetPID: 42,
            targetWindowID: 73,
            targetBounds: target,
            records: layerZeroOverlay
        ),
        "contained same-app layer-zero sheet was not detected"
    )
    try require(
        !containedAppOwnedOverlayActive(
            targetPID: 99,
            targetWindowID: 73,
            targetBounds: target,
            records: records
        ),
        "different-app overlay was accepted"
    )
    let equalBounds = [
        VisibleWindowRecord(
            pid: 42, windowID: 73, bounds: target, layer: 0, alpha: 1, zOrder: 0
        ),
        VisibleWindowRecord(
            pid: 42, windowID: 74, bounds: target, layer: 0, alpha: 1, zOrder: 1
        ),
    ]
    try require(
        !containedAppOwnedOverlayActive(
            targetPID: 42,
            targetWindowID: 73,
            targetBounds: target,
            records: equalBounds
        ),
        "equal-size same-app sibling behind the target changed foreground overlay policy"
    )
    try require(
        !backgroundVisibleWindowAmbiguityActive(
            targetPID: 42,
            targetWindowID: 73,
            targetBounds: target,
            records: equalBounds
        ),
        "proven-behind same-app sibling changed background ambiguity policy"
    )
    for order in [0, -1] {
        let uncertainOrFront = [equalBounds[0], VisibleWindowRecord(
            pid: 42, windowID: 74, bounds: target, layer: 0, alpha: 1, zOrder: order
        )]
        try require(backgroundVisibleWindowAmbiguityActive(
            targetPID: 42, targetWindowID: 73, targetBounds: target, records: uncertainOrFront
        ), "same-app sibling without proven-behind ordering was accepted")
    }
    try require(
        trustedContainedDialogRoot(
            role: BoundedAXStringResult(value: "AXWindow", status: .complete),
            subrole: BoundedAXStringResult(value: "AXDialog", status: .complete),
            targetBounds: target,
            candidateBounds: records[1].bounds
        ),
        "contained AX dialog root was rejected"
    )
    try require(
        trustedContainedDialogBody(
            role: BoundedAXStringResult(value: "AXSheet", status: .complete),
            targetBounds: target,
            candidateBounds: CGRect(x: 526, y: 187, width: 460, height: 345)
        ),
        "contained AX sheet body was rejected"
    )
    try require(
        trustedContainedDialogBody(
            role: BoundedAXStringResult(value: "AXSplitGroup", status: .complete),
            targetBounds: target,
            candidateBounds: CGRect(x: 316, y: 168, width: 880, height: 416)
        ),
        "contained AX split-group body was rejected"
    )
    try require(
        !trustedContainedDialogBody(
            role: BoundedAXStringResult(value: "AXTextField", status: .complete),
            targetBounds: target,
            candidateBounds: CGRect(x: 526, y: 187, width: 460, height: 345)
        ),
        "focused field was accepted as the dialog body"
    )
    try require(
        !trustedContainedDialogRoot(
            role: BoundedAXStringResult(value: "AXTextField", status: .complete),
            subrole: BoundedAXStringResult(value: nil, status: .complete),
            targetBounds: target,
            candidateBounds: records[1].bounds
        ),
        "non-window AX child was accepted as a dialog root"
    )
}

private func testApplicationMenuBarIsBoundedToSelectedWindowObservation() throws {
    let originalChild = AXNode(role: "AXGroup", bounds: .zero)
    let window = AXNode(
        role: "AXWindow", title: "Document", bounds: CGRect(x: 0, y: 0, width: 800, height: 600),
        children: [originalChild]
    )
    let menu = AXNode(
        role: "AXMenuBar", bounds: CGRect(x: 0, y: -33, width: 800, height: 33),
        children: [AXNode(role: "AXMenuBarItem", title: "文件", bounds: .zero)]
    )
    let combined = appendingApplicationMenuBar(window: window, menuBar: menu)
    try require(combined.role == "AXWindow" && combined.title == "Document", "menu observation replaced selected-window identity")
    try require(combined.children.count == 2 && combined.children[0].role == "AXGroup", "menu observation dropped selected-window children")
    try require(combined.children[1].role == "AXMenuBar", "same-application menu bar was not appended as a bounded child")

    let overlay = AXNode(
        role: "AXDialog", title: "Export", bounds: CGRect(x: 100, y: 100, width: 400, height: 300)
    )
    let overlayCombined = targetApplicationObservationTree(window: overlay, menuBar: menu)
    try require(
        overlayCombined.role == "AXDialog" && overlayCombined.children.last?.role == "AXMenuBar",
        "same-application menu bar disappeared while a contained overlay was observed"
    )
    try require(
        trustedApplicationMenuBarOwner(targetPID: 42, applicationPID: 42, menuBarPID: 42),
        "same-PID target application menu bar was rejected"
    )
    try require(
        !trustedApplicationMenuBarOwner(targetPID: 42, applicationPID: 42, menuBarPID: 99),
        "another process menu bar crossed the target boundary"
    )
    try require(
        focusedObservationMaximumDepth(preference: .selectedWindow) == maximumAXDepth,
        "an independently selected dialog was truncated as if it were a contained overlay"
    )
    try require(
        focusedObservationMaximumDepth(preference: .containedOverlay) == 2,
        "a true contained overlay lost its strict subtree depth"
    )
}

private func testFocusFailureIsRejected() throws {
    let target = CaptureIdentity(pid: 42, windowID: 7, bounds: CGRect(x: 10, y: 20, width: 300, height: 200), axIdentity: 99)
    let wrongSelection = CaptureIdentity(pid: 42, windowID: 8, bounds: target.bounds, axIdentity: 100)
    try require(throwsWindowObservationError { try verifyFocusedIdentity(target: target, selected: wrongSelection) }, "focus failure was accepted")
}

private func testCaptureRejectsFrontmostAppSwitch() throws {
    let target = makeCaptureIdentity(
        frontmostPID: 42,
        windowID: 7,
        bounds: CGRect(x: 10, y: 20, width: 300, height: 200),
        axIdentity: 99
    )!
    let switched = makeCaptureIdentity(
        frontmostPID: 43,
        windowID: 7,
        bounds: target.bounds,
        axIdentity: 99
    )
    try require(throwsWindowObservationError { try verifyFocusedIdentity(target: target, selected: switched) }, "frontmost app switch during capture was accepted")
}

private func throwsWindowObservationError(_ operation: () throws -> Void) -> Bool {
    do { try operation(); return false }
    catch is WindowObservationError { return true }
    catch { return false }
}

private func testWindowCandidateFiltering() throws {
    let candidates = [
        WindowCandidate(pid: 1, windowID: 1, bounds: CGRect(x: 0, y: 0, width: 100, height: 100), isOnScreen: true, isDesktop: false, isRegularApplication: true),
        WindowCandidate(pid: 2, windowID: 2, bounds: CGRect(x: 0, y: 0, width: 100, height: 100), isOnScreen: false, isDesktop: false, isRegularApplication: true),
        WindowCandidate(pid: 3, windowID: 3, bounds: CGRect(x: -10_000, y: -10_000, width: 100, height: 100), isOnScreen: true, isDesktop: false, isRegularApplication: true),
        WindowCandidate(pid: 4, windowID: 4, bounds: CGRect(x: 0, y: 0, width: 100, height: 100), isOnScreen: true, isDesktop: true, isRegularApplication: true),
        WindowCandidate(pid: 5, windowID: 5, bounds: CGRect(x: 0, y: 0, width: 100, height: 100), isOnScreen: true, isDesktop: false, isRegularApplication: false),
    ]
    let eligible = eligibleWindowCandidates(candidates, displays: [CGRect(x: 0, y: 0, width: 1920, height: 1080)])
    try require(eligible.map(\.windowID) == [1], "desktop/offscreen/accessory filtering failed")
}

private func testWindowTitleAndDocumentPathNormalization() throws {
    try require(
        windowTitlesMatch(
            screenCaptureTitle: "/private/tmp/astra copy.docx",
            accessibilityTitle: "astra copy.docx"
        ),
        "absolute ScreenCaptureKit document title did not match AX basename"
    )
    try require(
        !windowTitlesMatch(screenCaptureTitle: "one.docx", accessibilityTitle: "two.docx"),
        "different non-path window titles matched"
    )
    try require(
        normalizedDocumentPath("file:///private/tmp/astra%20copy.docx") == "/private/tmp/astra copy.docx",
        "file URL did not normalize to an exact local path"
    )
    try require(normalizedDocumentPath("https://example.test/private.docx") == nil, "remote URL became a local document path")
    try require(normalizedDocumentPath("relative/private.docx") == nil, "relative text became a trusted document path")
}

private func testSnapshotReferenceRegistryLifecycle() throws {
    let registry = SnapshotReferenceRegistry()
    let element = SnapshotElement(element: nil, bounds: CGRect(x: 1, y: 2, width: 3, height: 4))
    registry.register(snapshotID: "one", references: ["ax_one": element])
    try require(registry.element(for: "ax_one", snapshotID: "one")?.bounds == element.bounds, "registered snapshot ref was not found")
    try require(registry.element(for: "ax_one", snapshotID: "two") == nil, "snapshot ref crossed snapshot IDs")
    for reason in SnapshotInvalidationReason.allCases {
        registry.register(snapshotID: "one", references: ["ax_one": element])
        registry.invalidate(reason)
        try require(registry.element(for: "ax_one", snapshotID: "one") == nil, "snapshot ref survived \(reason)")
    }

    let guardValue = ActionGuard(pid: 1, windowID: 2, bounds: CGRect(x: 0, y: 0, width: 10, height: 10), axIdentity: 3, snapshotID: "take")
    registry.register(snapshotID: "take", references: ["ax_one": element], actionGuard: guardValue)
    let consumed = registry.takeForActions(snapshotID: "take")
    try require(consumed?.guardValue == guardValue && consumed?.references["ax_one"]?.bounds == element.bounds, "act did not atomically take its snapshot context")
    try require(registry.takeForActions(snapshotID: "take") == nil, "same snapshot was reusable for a second act")

    registry.register(snapshotID: "concurrent", references: ["ax_one": element], actionGuard: ActionGuard(pid: 1, windowID: 2, bounds: guardValue.bounds, axIdentity: 3, snapshotID: "concurrent"))
    let countLock = NSLock()
    var takeCount = 0
    DispatchQueue.concurrentPerform(iterations: 20) { _ in
        if registry.takeForActions(snapshotID: "concurrent") != nil {
            countLock.lock(); takeCount += 1; countLock.unlock()
        }
    }
    try require(takeCount == 1, "concurrent act calls consumed the same snapshot more than once")
}

private final class PhasedActionProvider: ActionProviding {
    var states: [ActionTargetState]
    let performance: ActionPerformance?
    let failure: ActionPerformFailure?
    private(set) var performed = 0

    init(states: [ActionTargetState], performance: ActionPerformance? = nil, failure: ActionPerformFailure? = nil) {
        self.states = states
        self.performance = performance
        self.failure = failure
    }

    func currentTargetState() throws -> ActionTargetState {
        guard !states.isEmpty else { throw ActionExecutionError.targetGone }
        return states.removeFirst()
    }
    func element(reference _: String, snapshotID _: String) -> ActionElement? { nil }
    func focusedKeyboardElement(matching _: ActionElement?) throws -> ActionElement { throw ActionExecutionError.targetNotFrontmost }
    func perform(_: ResolvedAction) throws -> ActionPerformance {
        performed += 1
        if let failure { throw failure }
        return performance ?? ActionPerformance(inputStarted: false)
    }
}

private func testInputPhaseControlsUnknownOutcome() throws {
    let bounds = CGRect(x: 0, y: 0, width: 100, height: 100)
    let expected = ActionGuard(pid: 1, windowID: 2, bounds: bounds, axIdentity: 3, snapshotID: "snapshot")
    let stable = ActionTargetState(pid: 1, windowID: 2, bounds: bounds, axIdentity: 3)

    let before = PhasedActionProvider(states: [], failure: nil)
    let beforeResult = ActionExecutor(provider: before).run(expected: expected, actions: [.click(x: 1, y: 1)])
    try require(beforeResult.error == .targetGone && before.performed == 0, "pre-input failure became unknown")

    let notStarted = PhasedActionProvider(states: [stable], failure: ActionPerformFailure(error: .permissionDenied, inputStarted: false))
    let notStartedResult = ActionExecutor(provider: notStarted).run(expected: expected, actions: [.click(x: 1, y: 1)])
    try require(notStartedResult.error == .permissionDenied, "known pre-emission failure became unknown")

    let startedFailure = PhasedActionProvider(states: [stable], failure: ActionPerformFailure(error: .helperFailed, inputStarted: true))
    let startedFailureResult = ActionExecutor(provider: startedFailure).run(expected: expected, actions: [.click(x: 1, y: 1)])
    try require(startedFailureResult.error == .unknownOutcome && startedFailureResult.lastAcknowledgedAction == -1, "started input failure was presented as retryable")

    let postFailure = PhasedActionProvider(states: [stable], performance: ActionPerformance(inputStarted: true))
    let postFailureResult = ActionExecutor(provider: postFailure).run(expected: expected, actions: [.click(x: 1, y: 1)])
    try require(postFailureResult.error == nil && postFailureResult.lastAcknowledgedAction == 0, "completed input dispatch was not acknowledged")

    let waitPostFailure = PhasedActionProvider(states: [stable], performance: ActionPerformance(inputStarted: false))
    let waitResult = ActionExecutor(provider: waitPostFailure).run(expected: expected, actions: [.wait(durationMS: 1)])
    try require(waitResult.error == nil && waitResult.lastAcknowledgedAction == 0, "completed wait was not acknowledged")
}

private final class KeyboardActionProvider: ActionProviding {
    let stable: ActionTargetState
    let focused: ActionElement
    let references: [String: ActionElement]
    private(set) var performed = 0

    init(stable: ActionTargetState, focused: ActionElement, references: [String: ActionElement] = [:]) {
        self.stable = stable
        self.focused = focused
        self.references = references
    }
    func currentTargetState() throws -> ActionTargetState { stable }
    func element(reference: String, snapshotID _: String) -> ActionElement? { references[reference] }
    func focusedKeyboardElement(matching expected: ActionElement?) throws -> ActionElement {
        if let expected, expected.identityToken != focused.identityToken { throw ActionExecutionError.staleSnapshot }
        return focused
    }
    func perform(_: ResolvedAction) throws -> ActionPerformance {
        performed += 1
        return ActionPerformance(inputStarted: true)
    }
}

private func testEveryKeyboardActionFailsSecureBeforeInput() throws {
    let bounds = CGRect(x: 0, y: 0, width: 100, height: 100)
    let stable = ActionTargetState(pid: 1, windowID: 2, bounds: bounds, axIdentity: 3)
    let expected = ActionGuard(pid: 1, windowID: 2, bounds: bounds, axIdentity: 3, snapshotID: "snapshot")
    let secure = ActionElement(
        element: nil,
        identityToken: "secure",
        bounds: CGRect(x: 1, y: 1, width: 20, height: 20),
        roleResult: BoundedAXStringResult(value: "AXTextField", status: .complete),
        subroleResult: BoundedAXStringResult(value: "AXSecureTextField", status: .complete),
        enabled: true,
        actionNames: .complete([])
    )
    for action in [NativeAction.type(text: "x"), .keypress(key: "a"), .keypress(key: "1"), .keypress(key: "return")] {
        let provider = KeyboardActionProvider(stable: stable, focused: secure)
        let result = ActionExecutor(provider: provider).run(expected: expected, actions: [action])
        try require(result.error == .secureTarget && provider.performed == 0, "secure focused element received keyboard action \(action.kind)")
    }

    let referencedProvider = KeyboardActionProvider(stable: stable, focused: secure, references: ["secure": secure])
    let referenced = ActionExecutor(provider: referencedProvider).run(expected: expected, actions: [.keypress(key: "return", elementRef: "secure")])
    try require(referenced.error == .secureTarget && referencedProvider.performed == 0, "keypress element_ref bypassed secure focus validation")

    let missingRole = ActionElement(
        element: nil,
        identityToken: "missing",
        bounds: secure.bounds,
        roleResult: BoundedAXStringResult(value: nil, status: .complete),
        subroleResult: BoundedAXStringResult(value: nil, status: .complete),
        enabled: true,
        actionNames: .complete([])
    )
    let missingProvider = KeyboardActionProvider(stable: stable, focused: missingRole)
    let missing = ActionExecutor(provider: missingProvider).run(expected: expected, actions: [.keypress(key: "a")])
    try require(missing.error == .secureTarget && missingProvider.performed == 0, "missing focused role did not fail secure")

    let enabledUnsupported = ActionElement(
        element: nil,
        identityToken: "terminal-text-area",
        bounds: secure.bounds,
        roleResult: BoundedAXStringResult(value: "AXTextArea", status: .complete),
        subroleResult: BoundedAXStringResult(value: nil, status: .complete),
        enabled: nil,
        actionNames: .complete([])
    )
    let enabledUnsupportedProvider = KeyboardActionProvider(stable: stable, focused: enabledUnsupported)
    let enabledUnsupportedResult = ActionExecutor(provider: enabledUnsupportedProvider).run(
        expected: expected,
        actions: [.type(text: "safe text")]
    )
    try require(
        enabledUnsupportedResult.error == nil && enabledUnsupportedProvider.performed == 1,
        "focused non-secure text area was rejected only because AXEnabled is unsupported"
    )

    let mismatched = ActionElement(
        element: nil, identityToken: "other", bounds: secure.bounds,
        roleResult: BoundedAXStringResult(value: "AXTextField", status: .complete),
        subroleResult: BoundedAXStringResult(value: nil, status: .complete), enabled: true, actionNames: .complete([])
    )
    let mismatchProvider = KeyboardActionProvider(stable: stable, focused: mismatched, references: ["expected": secure])
    let mismatch = ActionExecutor(provider: mismatchProvider).run(expected: expected, actions: [.keypress(key: "a", elementRef: "expected")])
    try require(mismatch.error == .staleSnapshot && mismatchProvider.performed == 0, "keypress ref did not have to match actual focus")

    let ordinary = ActionElement(element: nil, identityToken: "ordinary", bounds: secure.bounds, roleResult: BoundedAXStringResult(value: "AXTextField", status: .complete), subroleResult: BoundedAXStringResult(value: nil, status: .complete), enabled: true, actionNames: .complete([]))
    let pasteProvider = KeyboardActionProvider(stable: stable, focused: ordinary)
    let paste = ActionExecutor(provider: pasteProvider).run(expected: expected, actions: [.keypress(key: "v", modifiers: ["command"])])
    try require(paste.error == .invalidAction && pasteProvider.performed == 0, "Command-V clipboard paste reached input provider")
}

private func testKeyboardIdentityReaderNeverReadsValue() throws {
    let probe = HarnessSecureValueProbe(role: "AXTextField", subrole: "AXSecureTextField")
    let identity = readActionElementIdentity(provider: probe, identityToken: "probe", windowBounds: .zero)
    try require(identity.isSecure && probe.valueReads == 0, "keyboard identity validation read a secure value")
}

private final class HarnessActionProvider: ActionProviding {
    var states: [ActionTargetState]
    var elements: [String: ActionElement]
    var failureAt: Int?
    private(set) var performed: [ResolvedAction] = []

    init(states: [ActionTargetState], elements: [String: ActionElement] = [:], failureAt: Int? = nil) {
        self.states = states
        self.elements = elements
        self.failureAt = failureAt
    }

    func currentTargetState() throws -> ActionTargetState {
        guard !states.isEmpty else { throw ActionExecutionError.targetGone }
        return states.removeFirst()
    }

    func element(reference: String, snapshotID: String) -> ActionElement? {
        guard snapshotID == "snapshot" else { return nil }
        return elements[reference]
    }

    func focusedKeyboardElement(matching expected: ActionElement?) throws -> ActionElement {
        if let expected { return expected }
        return ActionElement(element: nil, bounds: .zero, role: "AXTextField", subrole: nil, enabled: true, actions: [])
    }

    func perform(_ action: ResolvedAction) throws -> ActionPerformance {
        let index = performed.count
        performed.append(action)
        if failureAt == index { throw ActionPerformFailure(error: .actionTimeout, inputStarted: false) }
        return ActionPerformance(inputStarted: action.method != .wait)
    }
}

private func testGuardedActionBatchStopsAndAcknowledgesExactly() throws {
    let bounds = CGRect(x: 100, y: 200, width: 300, height: 200)
    let expected = ActionGuard(pid: 7, windowID: 8, bounds: bounds, axIdentity: 9, snapshotID: "snapshot")
    let stable = ActionTargetState(pid: 7, windowID: 8, bounds: bounds, axIdentity: 9)
    let provider = HarnessActionProvider(states: [stable, stable, stable], failureAt: 1)
    let result = ActionExecutor(provider: provider).run(
        expected: expected,
        actions: [.click(x: 10, y: 20), .wait(durationMS: 1), .type(text: "must-not-run")]
    )
    try require(result.error == .actionTimeout, "batch did not preserve the first action failure")
    try require(result.lastAcknowledgedAction == 0, "last acknowledged action was not exact")
    try require(result.outcomes.count == 2 && provider.performed.count == 2, "actions after first failure were executed")

    let movedAfter = ActionTargetState(pid: 7, windowID: 8, bounds: bounds.offsetBy(dx: 2, dy: 0), axIdentity: 9)
    let postChangeProvider = HarnessActionProvider(states: [stable, movedAfter])
    let postChange = ActionExecutor(provider: postChangeProvider).run(expected: expected, actions: [.click(x: 10, y: 20), .type(text: "must-not-run")])
    try require(postChange.error == .staleSnapshot && postChange.lastAcknowledgedAction == 0, "next-action preflight did not preserve the acknowledged prefix")
    try require(postChangeProvider.performed.count == 1, "batch continued after post-action target change")
}

private func testActionGuardsRejectStaleSecureAndCoordinates() throws {
    let bounds = CGRect(x: 100, y: 200, width: 300, height: 200)
    let expected = ActionGuard(pid: 7, windowID: 8, bounds: bounds, axIdentity: 9, snapshotID: "snapshot")
    let moved = ActionTargetState(pid: 7, windowID: 8, bounds: bounds.offsetBy(dx: 2, dy: 0), axIdentity: 9)
    let movedProvider = HarnessActionProvider(states: [moved])
    let movedResult = ActionExecutor(provider: movedProvider).run(expected: expected, actions: [.click(x: 1, y: 1)])
    try require(movedResult.error == .staleSnapshot && movedProvider.performed.isEmpty, "moved window received input")

    let appearedOverlay = ActionTargetState(
        pid: 7, windowID: 8, bounds: bounds, axIdentity: 9,
        focusedAXIdentity: 9,
        focusedAXBounds: bounds,
        focusedRootPreference: .containedOverlay
    )
    let appearedOverlayProvider = HarnessActionProvider(states: [appearedOverlay])
    let appearedOverlayResult = ActionExecutor(provider: appearedOverlayProvider).run(
        expected: expected,
        actions: [.click(x: 1, y: 1)]
    )
    try require(
        appearedOverlayResult.error == .staleSnapshot && appearedOverlayProvider.performed.isEmpty,
        "a newly appeared contained overlay reused the parent-window snapshot"
    )

    let overlayExpected = ActionGuard(
        pid: 7, windowID: 8, bounds: bounds, axIdentity: 9,
        focusedAXIdentity: 10,
        focusedAXBounds: CGRect(x: 150, y: 240, width: 200, height: 100),
        focusedRootPreference: .containedOverlay,
        snapshotID: "overlay-snapshot"
    )
    let stableOverlay = ActionTargetState(
        pid: 7, windowID: 8, bounds: bounds, axIdentity: 9,
        focusedAXIdentity: 10,
        focusedAXBounds: CGRect(x: 150, y: 240, width: 200, height: 100),
        focusedRootPreference: .containedOverlay
    )
    let stableOverlayProvider = HarnessActionProvider(states: [stableOverlay, stableOverlay])
    let stableOverlayResult = ActionExecutor(provider: stableOverlayProvider).run(
        expected: overlayExpected,
        actions: [.wait(durationMS: 0)]
    )
    try require(
        stableOverlayResult.error == nil && stableOverlayResult.lastAcknowledgedAction == 0,
        "an unchanged contained overlay could not act from its snapshot context"
    )
    let disappearedOverlay = ActionTargetState(
        pid: 7, windowID: 8, bounds: bounds, axIdentity: 9
    )
    let disappearedProvider = HarnessActionProvider(states: [disappearedOverlay])
    let disappearedResult = ActionExecutor(provider: disappearedProvider).run(
        expected: overlayExpected,
        actions: [.click(x: 1, y: 1)]
    )
    try require(
        disappearedResult.error == .staleSnapshot && disappearedProvider.performed.isEmpty,
        "a disappeared contained overlay reused its older snapshot"
    )
    let replacedOverlay = ActionTargetState(
        pid: 7, windowID: 8, bounds: bounds, axIdentity: 9,
        focusedAXIdentity: 11,
        focusedAXBounds: CGRect(x: 160, y: 250, width: 180, height: 90),
        focusedRootPreference: .containedOverlay
    )
    let replacedOverlayProvider = HarnessActionProvider(states: [replacedOverlay])
    let replacedOverlayResult = ActionExecutor(provider: replacedOverlayProvider).run(
        expected: overlayExpected,
        actions: [.click(x: 1, y: 1)]
    )
    try require(
        replacedOverlayResult.error == .staleSnapshot && replacedOverlayProvider.performed.isEmpty,
        "a different focused modal reused an older modal snapshot"
    )
    let movedOverlay = ActionTargetState(
        pid: 7, windowID: 8, bounds: bounds, axIdentity: 9,
        focusedAXIdentity: 10,
        focusedAXBounds: CGRect(x: 152, y: 240, width: 200, height: 100),
        focusedRootPreference: .containedOverlay
    )
    let movedOverlayProvider = HarnessActionProvider(states: [movedOverlay])
    let movedOverlayResult = ActionExecutor(provider: movedOverlayProvider).run(
        expected: overlayExpected,
        actions: [.click(x: 1, y: 1)]
    )
    try require(
        movedOverlayResult.error == .staleSnapshot && movedOverlayProvider.performed.isEmpty,
        "a moved contained overlay reused its older snapshot"
    )

    let stable = ActionTargetState(pid: 7, windowID: 8, bounds: bounds, axIdentity: 9)
    let secure = ActionElement(element: nil, bounds: CGRect(x: 5, y: 5, width: 20, height: 20), role: "AXTextField", subrole: "AXSecureTextField", actions: [])
    let secureProvider = HarnessActionProvider(states: [stable], elements: ["secure": secure])
    let secureResult = ActionExecutor(provider: secureProvider).run(expected: expected, actions: [.click(elementRef: "secure")])
    try require(secureResult.error == .secureTarget && secureProvider.performed.isEmpty, "secure element received fallback input")

    let disabled = ActionElement(element: nil, bounds: CGRect(x: 5, y: 5, width: 20, height: 20), role: "AXButton", subrole: nil, enabled: false, actions: ["AXPress"])
    let disabledProvider = HarnessActionProvider(states: [stable], elements: ["disabled": disabled])
    let disabledResult = ActionExecutor(provider: disabledProvider).run(expected: expected, actions: [.click(elementRef: "disabled")])
    try require(disabledResult.error == .helperFailed && disabledProvider.performed.isEmpty, "disabled element received input")

    for point in [CGPoint(x: -CGFloat.infinity, y: 1), CGPoint(x: CGFloat.nan, y: 1), CGPoint(x: 301, y: 1), CGPoint(x: -1, y: 1)] {
        let provider = HarnessActionProvider(states: [stable])
        let result = ActionExecutor(provider: provider).run(expected: expected, actions: [.click(x: point.x, y: point.y)])
        try require(result.error == .outOfBounds && provider.performed.isEmpty, "invalid local coordinate received input")
    }
}

private func testElementPressAndCenterFallbackRemainSnapshotLocal() throws {
    let bounds = CGRect(x: 100, y: 200, width: 300, height: 200)
    let expected = ActionGuard(pid: 7, windowID: 8, bounds: bounds, axIdentity: 9, snapshotID: "snapshot")
    let stable = ActionTargetState(pid: 7, windowID: 8, bounds: bounds, axIdentity: 9)
    let press = ActionElement(element: nil, bounds: CGRect(x: 5, y: 10, width: 20, height: 30), role: "AXButton", subrole: nil, actions: ["AXPress"])
    let fallback = ActionElement(element: nil, bounds: CGRect(x: 25, y: 30, width: 20, height: 10), role: "AXButton", subrole: nil, actions: [])
    let provider = HarnessActionProvider(states: [stable, stable, stable, stable], elements: ["press": press, "fallback": fallback])
    let result = ActionExecutor(provider: provider).run(expected: expected, actions: [.click(elementRef: "press"), .click(elementRef: "fallback")])
    try require(result.error == nil && result.lastAcknowledgedAction == 1, "valid element actions failed")
    guard provider.performed.count == 2 else { throw HarnessFailure.assertion("valid element actions were not performed") }
    try require(provider.performed[0].method == .accessibilityPress, "AXPress was not preferred")
    try require(provider.performed[1].screenPoint == CGPoint(x: 135, y: 235), "element center was not converted from window-local coordinates")

    let staleProvider = HarnessActionProvider(states: [stable], elements: [:])
    let staleResult = ActionExecutor(provider: staleProvider).run(expected: expected, actions: [.click(elementRef: "old")])
    try require(staleResult.error == .staleSnapshot && staleProvider.performed.isEmpty, "old element reference was accepted")
}

private func testActionSecureIdentityFailsClosed() throws {
    let completeText = BoundedAXStringResult(value: "AXTextField", status: .complete)
    let completeNone = BoundedAXStringResult(value: nil, status: .complete)
    try require(!secureActionIdentity(role: completeText, subrole: completeNone), "ordinary text field was rejected")
    try require(secureActionIdentity(role: completeText, subrole: BoundedAXStringResult(value: "AXSecureTextField", status: .complete)), "secure subrole was accepted")
    try require(secureActionIdentity(role: BoundedAXStringResult(value: "AXText", status: .truncated), subrole: completeNone), "truncated role did not fail secure")
    try require(secureActionIdentity(role: completeText, subrole: BoundedAXStringResult(value: nil, status: .failed)), "failed subrole did not fail secure")
}

private final class HarnessActionObserver: WindowObserving {
    private(set) var planCalls = 0
    private(set) var cooperativeCalls = 0
    func apps() throws -> JSONValue { .object(["apps": .array([])]) }
    func snapshot(appRef _: String, windowRef _: String, scope _: String, artifactName _: String?, textDetail _: SnapshotTextDetailRequest) throws -> JSONValue { .object([:]) }

    func planActions(snapshotID _: String, interactionMode: InteractionMode, actions _: [NativeAction]) -> CooperativePlanResult {
        planCalls += 1
        return CooperativePlanResult(
            summary: DispatchPlanSummary(
                planRef: "plan",
                interactionMode: interactionMode,
                requiresTakeover: false,
                reason: "background_ax_only",
                actionClasses: [],
                lastAcknowledgedAction: -1
            ),
            error: nil
        )
    }

    func cooperativeAct(snapshotID _: String, interactionMode _: InteractionMode, planRef _: String, takeoverRef _: String?, actions _: [NativeAction]) -> CooperativeActionResult {
        cooperativeCalls += 1
        return CooperativeActionResult(
            batch: ActionBatchResult(
                outcomes: [ActionOutcome(index: 0, ok: true, error: nil)],
                lastAcknowledgedAction: 0,
                error: nil
            ),
            error: nil
        )
    }
}

private func testActionDispatcherRoutesBackgroundCooperativeBatches() throws {
    let observer = HarnessActionObserver()
    let dispatcher = Dispatcher(permissions: StaticHarnessPermissions(), windows: observer)
    let plan = dispatcher.handle(
        #"{"protocol_version":4,"request_id":"plan-1","operation":"plan_actions","payload":{"interaction_mode":"background","snapshot_id":"snapshot","actions":[{"type":"wait","duration_ms":0}]}}"#
    )
    try require(plan.ok && observer.planCalls == 1, "cooperative plan did not reach the observer")

    let response = dispatcher.handle(
        #"{"protocol_version":4,"request_id":"act-1","operation":"act","payload":{"interaction_mode":"background","snapshot_id":"snapshot","plan_ref":"plan","actions":[{"type":"wait","duration_ms":0}]}}"#
    )
    try require(response.ok, "background cooperative action was rejected")
    try require(observer.cooperativeCalls == 1, "background cooperative action did not reach the observer")

    let legacy = dispatcher.handle(
        #"{"protocol_version":4,"request_id":"legacy-act","operation":"act","payload":{"app_ref":"app","window_ref":"window","snapshot_id":"snapshot","actions":[{"type":"click","x":1,"y":2},{"type":"wait","duration_ms":1}]}}"#
    )
    try require(!legacy.ok && legacy.error?.code == "protocol_mismatch", "legacy foreground action was accepted")
    try require(observer.cooperativeCalls == 1, "legacy foreground action reached the observer")

    let malformed = dispatcher.handle(
        #"{"protocol_version":4,"request_id":"act-2","operation":"act","payload":{"interaction_mode":"foreground_takeover","snapshot_id":"snapshot","plan_ref":"plan","actions":[]}}"#
    )
    try require(!malformed.ok && malformed.error?.code == "protocol_mismatch", "malformed action batch was accepted")
    try require(observer.cooperativeCalls == 1, "malformed action batch reached the observer")

    let missingMode = dispatcher.handle(
        #"{"protocol_version":4,"request_id":"act-missing-mode","operation":"act","payload":{"snapshot_id":"snapshot","plan_ref":"plan","actions":[]}}"#
    )
    let missingPlan = dispatcher.handle(
        #"{"protocol_version":4,"request_id":"act-missing-plan","operation":"act","payload":{"interaction_mode":"background","snapshot_id":"snapshot","actions":[]}}"#
    )
    let missingTakeover = dispatcher.handle(
        #"{"protocol_version":4,"request_id":"act-missing-takeover","operation":"act","payload":{"interaction_mode":"foreground_takeover","snapshot_id":"snapshot","plan_ref":"plan","actions":[]}}"#
    )
    try require(
        [missingMode, missingPlan, missingTakeover].allSatisfy { !$0.ok && $0.error?.code == "protocol_mismatch" },
        "missing cooperative authority was accepted"
    )
}

private func testNativeActionSchemaMatchesSharedVectors() throws {
    let path = packageRoot().appendingPathComponent("Tests/Fixtures/action_vectors.json")
    let root = try JSONSerialization.jsonObject(with: Data(contentsOf: path)) as! [String: Any]
    func expanded(_ vector: [String: Any]) throws -> JSONValue {
        var action = vector["action"] as! [String: Any]
        if let repeatValue = vector["repeat"] as? [String: Any] {
            let field = repeatValue["field"] as! String
            let scalar = repeatValue["scalar"] as! String
            let count = repeatValue["count"] as! Int
            action[field] = String(repeating: scalar, count: count)
        }
        return try JSONDecoder().decode(JSONValue.self, from: JSONSerialization.data(withJSONObject: action))
    }
    for vector in root["valid"] as! [[String: Any]] {
        do { _ = try NativeAction.parse(expanded(vector)) }
        catch { throw HarnessFailure.assertion("native rejected valid schema vector \(vector["name"] ?? "unknown")") }
    }
    for vector in root["invalid"] as! [[String: Any]] {
        do {
            _ = try NativeAction.parse(expanded(vector))
            throw HarnessFailure.assertion("native accepted invalid schema vector \(vector["name"] ?? "unknown")")
        } catch let failure as HarnessFailure {
            throw failure
        } catch {}
    }
}

private func testEveryActionKindRunsThroughGuardedCore() throws {
    let bounds = CGRect(x: 100, y: 200, width: 300, height: 200)
    let expected = ActionGuard(pid: 7, windowID: 8, bounds: bounds, axIdentity: 9, snapshotID: "snapshot")
    let stable = ActionTargetState(pid: 7, windowID: 8, bounds: bounds, axIdentity: 9)
    let text = ActionElement(element: nil, bounds: CGRect(x: 5, y: 5, width: 100, height: 30), role: "AXTextField", subrole: nil, enabled: true, actions: [])
    let actions: [NativeAction] = [
        .doubleClick(x: 10, y: 20),
        .type(text: "Unicode 🧑🏽‍💻", elementRef: "text"),
        .keypress(key: "a", modifiers: ["command", "shift"]),
        .scroll(deltaX: 2, deltaY: -4, x: 20, y: 30),
        .drag(x: 1, y: 2, endX: 30, endY: 40, durationMS: 20),
        .wait(durationMS: 1),
    ]
    let provider = HarnessActionProvider(states: Array(repeating: stable, count: actions.count * 2), elements: ["text": text])
    let result = ActionExecutor(provider: provider).run(expected: expected, actions: actions)
    try require(result.error == nil && result.lastAcknowledgedAction == actions.count - 1, "complete action set did not finish")
    try require(provider.performed.map(\.method) == [.pointerClick(count: 2, button: .left), .unicodeText, .keypress, .scroll, .drag, .wait], "action kinds resolved incorrectly")
    try require(provider.performed[0].screenPoint == CGPoint(x: 110, y: 220), "double click was not window-local")
    try require(provider.performed[1].source.text == "Unicode 🧑🏽‍💻", "Unicode text changed before provider execution")
    try require(provider.performed[3].screenPoint == CGPoint(x: 120, y: 230), "scroll point was not window-local")
    try require(provider.performed[4].endScreenPoint == CGPoint(x: 130, y: 240), "drag endpoint was not window-local")
}

private final class HarnessSyntheticPoster: SyntheticInputPosting {
    var allowed = true
    var failingCalls: Set<Int> = []
    var failureStarted = true
    private(set) var events: [SyntheticInputEvent] = []

    func preflight() -> Bool { allowed }
    func post(_ event: SyntheticInputEvent) throws {
        let index = events.count
        events.append(event)
        if failingCalls.contains(index) {
            throw SyntheticInputFailure(error: .helperFailed, inputStarted: failureStarted)
        }
    }
}

private final class HarnessSelectedTextWriter: AXSelectedTextWriting {
    var result: AXSelectedTextWriteResult
    var preflight: AXTextMutationPreflight = .settable
    private(set) var calls = 0
    init(_ result: AXSelectedTextWriteResult) { self.result = result }
    func preflightSelectedText(to _: AXUIElement) -> AXTextMutationPreflight { preflight }
    func writeSelectedText(_: String, to _: AXUIElement) -> AXSelectedTextWriteResult {
        calls += 1
        return result
    }
}

private struct HarnessSafeTextInputDetector: BackgroundTextInputSafetyDetecting {
    func detect() -> BackgroundTextInputSafety { .safeASCIIKeyboardLayout }
}

private func testBackgroundDispatcherPostsNoSyntheticEventsForAXOnlyBatch() throws {
    let stable = ActionTargetState(
        pid: 1,
        windowID: 2,
        bounds: CGRect(x: 0, y: 0, width: 100, height: 100),
        axIdentity: 3
    )
    let axElement = AXUIElementCreateSystemWide()
    let text = ActionElement(
        element: axElement,
        bounds: CGRect(x: 10, y: 40, width: 50, height: 20),
        role: kAXTextFieldRole as String,
        subrole: nil,
        actions: []
    )
    let poster = HarnessSyntheticPoster()
    let writer = HarnessSelectedTextWriter(.written)
    let performer = SystemActionPerformer(
        state: { stable },
        lookup: { reference, snapshotID in
            guard snapshotID == "snapshot" else { return nil }
            return reference == "text" ? text : nil
        },
        inputPoster: poster,
        selectedTextWriter: writer,
        focusedKeyboard: { _ in text }
    )
    let dispatcher = InputDispatcher(
        performer: performer,
        backgroundTextInputSafety: HarnessSafeTextInputDetector()
    )
    let context = DispatchContext(
        guardValue: ActionGuard(
            pid: 1,
            windowID: 2,
            bounds: stable.bounds,
            axIdentity: 3,
            snapshotID: "snapshot",
            interactionMode: .background
        )
    )
    let plan = try dispatcher.plan(
        actions: [.type(text: "AX only", elementRef: "text"), .wait(durationMS: 0)],
        context: context
    )
    let result = try dispatcher.execute(plan, context: context)

    try require(
        result.error == nil && result.lastAcknowledgedAction == 1,
        "AX-only batch did not complete: error=\(String(describing: result.error)) ack=\(result.lastAcknowledgedAction) outcomes=\(result.outcomes)"
    )
    try require(writer.calls == 1, "AX text mutation was not executed exactly once")
    try require(poster.events.isEmpty, "AX-only batch posted synthetic events")
}

private func testAccessibilityFirstUnicodeTyping() throws {
    let stable = ActionTargetState(pid: 1, windowID: 2, bounds: CGRect(x: 0, y: 0, width: 100, height: 100), axIdentity: 3)
    let element = AXUIElementCreateSystemWide()
    let safe = ActionElement(element: element, bounds: .zero, role: "AXTextField", subrole: nil, enabled: true, actions: [])
    let secure = ActionElement(element: element, bounds: .zero, role: "AXTextField", subrole: "AXSecureTextField", enabled: true, actions: [])
    let action = ResolvedAction(
        source: .type(text: "w82777.docx"), method: .unicodeText,
        screenPoint: nil, endScreenPoint: nil, element: element, verifiedElement: safe
    )

    let writtenPoster = HarnessSyntheticPoster()
    let written = HarnessSelectedTextWriter(.written)
    let writtenPerformer = SystemActionPerformer(
        state: { stable }, lookup: { _, _ in nil }, inputPoster: writtenPoster,
        selectedTextWriter: written, focusedKeyboard: { _ in safe }
    )
    let performance = try writtenPerformer.perform(action)
    try require(performance.inputStarted && written.calls == 1, "successful AX selected-text write was not acknowledged")
    try require(writtenPoster.events.isEmpty, "successful AX selected-text write also emitted CG input")

    let unsupportedPoster = HarnessSyntheticPoster()
    let unsupported = HarnessSelectedTextWriter(.unsupported)
    let unsupportedPerformer = SystemActionPerformer(
        state: { stable }, lookup: { _, _ in nil }, inputPoster: unsupportedPoster,
        selectedTextWriter: unsupported, focusedKeyboard: { _ in safe }
    )
    _ = try unsupportedPerformer.perform(action)
    try require(
        unsupported.calls == 1 && unsupportedPoster.events.count == 2,
        "unsupported text-field AX write did not fall back to one IME-independent CG Unicode pair"
    )

    let container = ActionElement(
        element: element,
        bounds: .zero,
        role: "AXSplitGroup",
        subrole: nil,
        enabled: true,
        actions: []
    )
    let misleadingContainerWriter = HarnessSelectedTextWriter(.written)
    let containerPoster = HarnessSyntheticPoster()
    let containerPerformer = SystemActionPerformer(
        state: { stable }, lookup: { _, _ in nil }, inputPoster: containerPoster,
        selectedTextWriter: misleadingContainerWriter, focusedKeyboard: { _ in container }
    )
    _ = try containerPerformer.perform(action)
    try require(
        misleadingContainerWriter.calls == 0 && containerPoster.events.count == 2 * "w82777.docx".count,
        "non-text focus trusted a misleading AX selected-text write instead of CG Unicode"
    )

    let physicalPoster = HarnessSyntheticPoster()
    let physicalPerformer = SystemActionPerformer(
        state: { stable }, lookup: { _, _ in nil }, inputPoster: physicalPoster,
        selectedTextWriter: misleadingContainerWriter, focusedKeyboard: { _ in container }
    )
    let physicalAction = ResolvedAction(
        source: .type(text: "A_1"), method: .unicodeText,
        screenPoint: nil, endScreenPoint: nil, element: element, verifiedElement: container
    )
    _ = try physicalPerformer.perform(physicalAction)
    try require(
        physicalPoster.events == [
            .virtualKeyDown(0, .maskShift), .virtualKeyUp(0, .maskShift),
            .virtualKeyDown(27, .maskShift), .virtualKeyUp(27, .maskShift),
            .virtualKeyDown(18, []), .virtualKeyUp(18, []),
        ],
        "ASCII text did not use layout key events for WPS-compatible typing"
    )

    for (result, expectedError, expectedStarted) in [
        (AXSelectedTextWriteResult.failed(.permissionDenied, inputStarted: false), ActionExecutionError.permissionDenied, false),
        (.failed(.staleSnapshot, inputStarted: true), .staleSnapshot, true),
    ] {
        let writer = HarnessSelectedTextWriter(result)
        let poster = HarnessSyntheticPoster()
        let performer = SystemActionPerformer(
            state: { stable }, lookup: { _, _ in nil }, inputPoster: poster,
            selectedTextWriter: writer, focusedKeyboard: { _ in safe }
        )
        do {
            _ = try performer.perform(action)
            throw HarnessFailure.assertion("AX selected-text failure unexpectedly succeeded")
        } catch let failure as ActionPerformFailure {
            try require(failure.error == expectedError && failure.inputStarted == expectedStarted, "AX selected-text failure lost phase/error")
            try require(poster.events.isEmpty, "AX selected-text failure fell through to CG input")
        }
    }

    let secureWriter = HarnessSelectedTextWriter(.written)
    let securePoster = HarnessSyntheticPoster()
    let securePerformer = SystemActionPerformer(
        state: { stable }, lookup: { _, _ in nil }, inputPoster: securePoster,
        selectedTextWriter: secureWriter, focusedKeyboard: { _ in secure }
    )
    do {
        _ = try securePerformer.perform(action)
        throw HarnessFailure.assertion("secure focused text accepted AX selected-text write")
    } catch let failure as ActionPerformFailure {
        try require(failure.error == .secureTarget && !failure.inputStarted, "secure AX selected-text failure was not pre-input")
        try require(secureWriter.calls == 0 && securePoster.events.isEmpty, "secure target reached AX set or CG input")
    }
}

private func testSystemAXSelectedTextWriterPhaseMapping() throws {
    let element = AXUIElementCreateSystemWide()
    let unsupported = SystemAXSelectedTextWriter(
        isSettable: { _ in (.attributeUnsupported, false) },
        setValue: { _, _ in .failure }
    )
    try require(unsupported.writeSelectedText("x", to: element) == .unsupported, "unsupported AX attribute did not allow CG fallback")

    let denied = SystemAXSelectedTextWriter(
        isSettable: { _ in (.apiDisabled, false) },
        setValue: { _, _ in .failure }
    )
    try require(denied.writeSelectedText("x", to: element) == .failed(.permissionDenied, inputStarted: false), "AX permission failure mapping changed")

    let invalid = SystemAXSelectedTextWriter(
        isSettable: { _ in (.success, true) },
        setValue: { _, _ in .invalidUIElement }
    )
    try require(invalid.writeSelectedText("x", to: element) == .failed(.staleSnapshot, inputStarted: true), "post-AXSet invalid element was not started/unknown-capable")
}

private func testBalancedInputCleanupAndPermissionPreflight() throws {
    guard let frontmostPID = NSWorkspace.shared.frontmostApplication?.processIdentifier else {
        throw HarnessFailure.assertion("frontmost PID was unavailable for keyboard cleanup preflight")
    }
    let stable = ActionTargetState(
        pid: frontmostPID,
        windowID: 2,
        bounds: CGRect(x: 0, y: 0, width: 100, height: 100),
        axIdentity: 3
    )
    let safeKeyboard = ActionElement(element: nil, bounds: .zero, role: "AXTextField", subrole: nil, enabled: true, actions: [])
    func performer(_ poster: HarnessSyntheticPoster, _ registry: HeldInputRegistry) -> SystemActionPerformer {
        SystemActionPerformer(
            state: { stable }, lookup: { _, _ in nil }, inputPoster: poster, heldInputs: registry,
            focusedKeyboard: { $0 ?? safeKeyboard }
        )
    }
    let actions: [ResolvedAction] = [
        ResolvedAction(source: .click(x: 1, y: 1), method: .pointerClick(count: 1, button: .left), screenPoint: CGPoint(x: 1, y: 1), endScreenPoint: nil, element: nil),
        ResolvedAction(source: .doubleClick(x: 1, y: 1), method: .pointerClick(count: 2, button: .left), screenPoint: CGPoint(x: 1, y: 1), endScreenPoint: nil, element: nil),
        ResolvedAction(source: .type(text: "x"), method: .unicodeText, screenPoint: nil, endScreenPoint: nil, element: nil),
        ResolvedAction(source: .keypress(key: "a"), method: .keypress, screenPoint: nil, endScreenPoint: nil, element: nil),
        ResolvedAction(source: .drag(x: 1, y: 1, endX: 2, endY: 2, durationMS: 0), method: .drag, screenPoint: CGPoint(x: 1, y: 1), endScreenPoint: CGPoint(x: 2, y: 2), element: nil),
    ]
    for action in actions {
        let poster = HarnessSyntheticPoster()
        let registry = HeldInputRegistry()
        poster.failingCalls = [1]
        do {
            _ = try performer(poster, registry).perform(action)
            throw HarnessFailure.assertion("balanced action unexpectedly succeeded after post-down failure")
        } catch let failure as ActionPerformFailure {
            try require(failure.inputStarted, "post-down failure was not marked started")
            try require(registry.heldCount == 0, "normal failure left a key or button held")
            try require(poster.events.count >= 3, "normal failure did not attempt a release cleanup")
        }
    }

    let cleanupPoster = HarnessSyntheticPoster()
    cleanupPoster.failingCalls = [1, 2]
    let cleanupRegistry = HeldInputRegistry()
    do {
        _ = try performer(cleanupPoster, cleanupRegistry).perform(actions[0])
        throw HarnessFailure.assertion("cleanup failure unexpectedly succeeded")
    } catch let failure as ActionPerformFailure {
        try require(failure.inputStarted && cleanupRegistry.heldCount == 1, "cleanup failure lost unknown held state")
        cleanupPoster.failingCalls = []
        try require(cleanupRegistry.cleanupAll() && cleanupRegistry.heldCount == 0, "later cancellation cleanup did not release held input")
    }

    let signalPoster = HarnessSyntheticPoster()
    let signalRegistry = HeldInputRegistry()
    try signalRegistry.begin(
        token: UUID(), poster: signalPoster,
        down: .virtualKeyDown(0, .maskCommand), release: .virtualKeyUp(0, .maskCommand)
    )
    try require(signalRegistry.cleanupAll() && signalRegistry.heldCount == 0, "SIGTERM-style cleanup did not release held input")

    let deniedPoster = HarnessSyntheticPoster()
    deniedPoster.allowed = false
    let deniedRegistry = HeldInputRegistry()
    do {
        _ = try performer(deniedPoster, deniedRegistry).perform(actions[0])
        throw HarnessFailure.assertion("preflight-denied input reported success")
    } catch let failure as ActionPerformFailure {
        try require(failure.error == .permissionDenied && !failure.inputStarted && deniedPoster.events.isEmpty, "preflight denial emitted input")
    }

    let securePoster = HarnessSyntheticPoster()
    let secureRegistry = HeldInputRegistry()
    let secureKeyboard = ActionElement(element: nil, bounds: .zero, role: "AXTextField", subrole: "AXSecureTextField", enabled: true, actions: [])
    let securePerformer = SystemActionPerformer(
        state: { stable }, lookup: { _, _ in nil }, inputPoster: securePoster, heldInputs: secureRegistry,
        focusedKeyboard: { _ in secureKeyboard }
    )
    do {
        _ = try securePerformer.perform(actions[3])
        throw HarnessFailure.assertion("system keyboard performer accepted a secure current focus")
    } catch let failure as ActionPerformFailure {
        try require(failure.error == .secureTarget && !failure.inputStarted && securePoster.events.isEmpty, "secure revalidation emitted keyboard input")
    }
}

private func testIncompleteActionNamesAndAXErrorsFailClosed() throws {
    let bounds = CGRect(x: 0, y: 0, width: 100, height: 100)
    let stable = ActionTargetState(pid: 1, windowID: 2, bounds: bounds, axIdentity: 3)
    let expected = ActionGuard(pid: 1, windowID: 2, bounds: bounds, axIdentity: 3, snapshotID: "snapshot")
    for actionNames in [
        ActionNameResults(values: [], status: .failed),
        ActionNameResults(values: [BoundedAXStringResult(value: "AXPress", status: .truncated)], status: .complete),
        ActionNameResults(values: [], status: .truncated),
    ] {
        let element = ActionElement(
            element: nil, identityToken: "button", bounds: CGRect(x: 1, y: 1, width: 10, height: 10),
            roleResult: BoundedAXStringResult(value: "AXButton", status: .complete),
            subroleResult: BoundedAXStringResult(value: nil, status: .complete), enabled: true, actionNames: actionNames
        )
        let provider = HarnessActionProvider(states: [stable], elements: ["button": element])
        let result = ActionExecutor(provider: provider).run(expected: expected, actions: [.click(elementRef: "button")])
        try require(result.error == .helperFailed && provider.performed.isEmpty, "incomplete action discovery allowed pointer fallback")
    }
    try require(mapAXActionError(.apiDisabled) == .permissionDenied, "AX apiDisabled mapping drifted")
    try require(mapAXActionError(.notImplemented) == .helperFailed, "AX notImplemented was misreported as a permission error")
    try require(mapAXActionError(.invalidUIElement) == .staleSnapshot, "AX invalid element mapping drifted")
    try require(mapAXActionError(.cannotComplete) == .actionTimeout, "AX cannotComplete mapping drifted")
    try require(mapAXActionError(.actionUnsupported) == .helperFailed, "AX unsupported action mapping drifted")

    for (axError, expectedError) in [(AXError.apiDisabled, ActionExecutionError.permissionDenied), (.notImplemented, .helperFailed)] {
        let actionNames = boundedAXActionNameResults(error: axError, names: nil)
        let element = ActionElement(
            element: nil, identityToken: "button", bounds: CGRect(x: 1, y: 1, width: 10, height: 10),
            roleResult: BoundedAXStringResult(value: "AXButton", status: .complete),
            subroleResult: BoundedAXStringResult(value: nil, status: .complete), enabled: true, actionNames: actionNames
        )
        let provider = HarnessActionProvider(states: [stable], elements: ["button": element])
        let result = ActionExecutor(provider: provider).run(expected: expected, actions: [.click(elementRef: "button")])
        try require(result.error == expectedError && provider.performed.isEmpty, "action-name AX error lost its production mapping")
    }
}

private func testUserActivityClassificationAndCompatibilityDefaults() throws {
    var now: TimeInterval = 0
    let classifier = UserActivityClassifier(marker: 42, clock: { now })
    try require(
        classifier.consume(.pointerMotion(deltaX: 100, deltaY: 100, marker: 42)) == .ignoredSynthetic,
        "Astra marker was classified as physical input"
    )
    try require(
        classifier.consume(.pointerMotion(deltaX: 12, deltaY: 0, marker: 0)) == .jitter,
        "exact 12-point boundary paused"
    )
    now = 0.150
    try require(
        classifier.consume(.pointerMotion(deltaX: 0.001, deltaY: 0, marker: 0)) == .pause(.pointerMotion),
        "motion above 12 points at 150 milliseconds did not pause"
    )
    let application = PIDTargetApplication(bundleIdentifier: "com.example.Editor", version: "1")
    let disabled = PIDInputCompatibilityRegistry()
    try require(!disabled.allows(application: application, action: .click), "empty compatibility registry enabled PID input")
    let enabled = PIDInputCompatibilityRegistry(cells: [
        .init(bundleIdentifier: application.bundleIdentifier, version: application.version, action: .click),
    ])
    try require(enabled.allows(application: application, action: .click), "exact compatibility cell was not enabled")
    try require(!enabled.allows(application: application, action: .drag), "compatibility cell enabled a different action")
    try require(AstraEventMarker.random() != 0, "random Astra marker used the reserved zero marker")
}

private func testBuildValidatesStagedCompatibilityBytesBeforeSigning() throws {
    let root = packageRoot().deletingLastPathComponent().deletingLastPathComponent()
    let script = try String(contentsOf: root.appendingPathComponent("scripts/build_macos_computer_helper.sh"), encoding: .utf8)
    guard script.contains(#"compatibility_resource="$staging_bundle/Contents/Resources/macos_computer_compatibility.json""#),
          let copy = script.range(of: #"install -m 600 "$compatibility_source" "$compatibility_resource""#),
          let stagedValidation = script.range(
              of: #"python3 - "$project_root" "$compatibility_resource""#,
              range: copy.upperBound..<script.endIndex
          ),
          let stagedLoad = script.range(
              of: "_load_cells(compatibility_resource)",
              range: stagedValidation.upperBound..<script.endIndex
          ),
          let comparison = script.range(
              of: #"cmp "$compatibility_source" "$compatibility_resource""#,
              range: stagedLoad.upperBound..<script.endIndex
          ),
          let signing = script.range(of: "codesign --force", range: comparison.upperBound..<script.endIndex)
    else {
        throw HarnessFailure.assertion("build script does not validate the staged compatibility bytes before signing")
    }
    try require(
        copy.lowerBound < stagedValidation.lowerBound &&
            stagedValidation.lowerBound < stagedLoad.lowerBound &&
            stagedLoad.lowerBound < comparison.lowerBound &&
            comparison.lowerBound < signing.lowerBound,
        "the staged compatibility resource is not the validated signing input"
    )

    let fixture = FileManager.default.temporaryDirectory.appendingPathComponent("astra-build-compatibility-\(UUID().uuidString)")
    defer { try? FileManager.default.removeItem(at: fixture) }
    for relativePath in [
        "scripts", "config", "native/macos-computer-helper/Sources/AstraMacComputerHelper",
        "agent/runtime", "fake-tools",
    ] {
        try FileManager.default.createDirectory(
            at: fixture.appendingPathComponent(relativePath),
            withIntermediateDirectories: true
        )
    }
    try Data(script.utf8).write(to: fixture.appendingPathComponent("scripts/build_macos_computer_helper.sh"))
    for relativePath in [
        "agent/__init__.py", "agent/runtime/__init__.py",
        "agent/runtime/computer_protocol.py", "agent/runtime/macos_computer_compatibility.py",
        "config/macos_computer_compatibility.json",
    ] {
        let source = root.appendingPathComponent(relativePath)
        let destination = fixture.appendingPathComponent(relativePath)
        if FileManager.default.fileExists(atPath: source.path) {
            try FileManager.default.copyItem(at: source, to: destination)
        } else {
            try Data().write(to: destination)
        }
    }

    let fakeSwift = #"""
#!/bin/bash
set -euo pipefail
fixture_root="${ASTRA_BUILD_FIXTURE_ROOT:?}"
binary_root="$fixture_root/fake-bin"
mkdir -p "$binary_root"
if [[ " $* " == *" --show-bin-path "* ]]; then
  printf '%s\n' "$binary_root"
  exit 0
fi
if [[ ! -e "$fixture_root/source-mutated" ]]; then
  printf '%s\n' '{"schema_version":1,"applications":"changed-after-validation"}' > "$fixture_root/config/macos_computer_compatibility.json"
  touch "$fixture_root/source-mutated"
fi
printf '%s\n' 'fixture helper' > "$binary_root/AstraMacComputerHelper"
printf '%s\n' 'fixture sidecar' > "$binary_root/AstraVirtualCursorSidecar"
"""#
    let fakeCodesign = #"""
#!/bin/bash
touch "${ASTRA_BUILD_FIXTURE_ROOT:?}/codesign-was-called"
exit 0
"""#
    for (name, contents) in [("swift", fakeSwift), ("codesign", fakeCodesign)] {
        let url = fixture.appendingPathComponent("fake-tools/\(name)")
        try Data(contents.utf8).write(to: url)
        try FileManager.default.setAttributes([.posixPermissions: 0o700], ofItemAtPath: url.path)
    }

    let process = Process()
    let errors = Pipe()
    process.executableURL = URL(fileURLWithPath: "/bin/bash")
    process.arguments = [fixture.appendingPathComponent("scripts/build_macos_computer_helper.sh").path]
    var environment = ProcessInfo.processInfo.environment
    environment["ASTRA_BUILD_FIXTURE_ROOT"] = fixture.path
    environment["PATH"] = fixture.appendingPathComponent("fake-tools").path + ":" + (environment["PATH"] ?? "/usr/bin:/bin")
    process.environment = environment
    process.standardOutput = FileHandle.nullDevice
    process.standardError = errors
    try process.run()
    process.waitUntilExit()
    let stderr = String(decoding: errors.fileHandleForReading.readDataToEndOfFile(), as: UTF8.self)
    try require(
        FileManager.default.fileExists(atPath: fixture.appendingPathComponent("source-mutated").path),
        "fixture did not mutate the source after its initial validation: \(stderr)"
    )
    try require(process.terminationStatus != 0, "post-validation source mutation unexpectedly produced a bundle")
    try require(
        !FileManager.default.fileExists(atPath: fixture.appendingPathComponent("codesign-was-called").path),
        "post-validation source mutation reached codesign before staged input validation: \(stderr)"
    )
}

private func testCompatibilityRegistryRejectsDeepNestingWithoutProcessCrash() throws {
    for kind in ["array", "object"] {
        let process = Process()
        let errors = Pipe()
        process.executableURL = URL(fileURLWithPath: CommandLine.arguments[0])
        process.arguments = [deepCompatibilityProbeArgument, kind]
        process.standardOutput = FileHandle.nullDevice
        process.standardError = errors
        try process.run()
        process.waitUntilExit()
        let stderr = String(decoding: errors.fileHandleForReading.readDataToEndOfFile(), as: UTF8.self)
        try require(
            process.terminationReason == .exit && process.terminationStatus == 0,
            "deep \(kind) compatibility JSON did not fail closed: reason=\(process.terminationReason) status=\(process.terminationStatus) stderr=\(stderr)"
        )
    }
}

private final class HarnessPIDPoster: PIDTargetedInputPosting {
    private(set) var events: [(SyntheticInputEvent, pid_t, UInt64)] = []
    func preflight(targetPID: pid_t, marker: UInt64) -> Bool { targetPID > 0 && marker != 0 }
    func post(_ event: SyntheticInputEvent, to targetPID: pid_t, marker: UInt64) throws {
        events.append((event, targetPID, marker))
    }
}

private final class HarnessPIDValidator: PIDActionGuardValidating {
    private(set) var calls = 0
    func revalidate(expected _: ActionGuard, point _: CGPoint?) throws { calls += 1 }
}

private final class HarnessActivityMonitor: UserActivityMonitoring {
    private(set) var assertions = 0
    var paused = false
    var pauseAfter: Int?
    private var lease: UserActivitySessionLease
    private let scope: HeldInputScope
    private var fragmentActive = false
    init(pauseAfter: Int? = nil, marker: UInt64 = 1234) {
        self.pauseAfter = pauseAfter
        lease = UserActivitySessionLease(marker: marker)
        scope = .cooperative(marker: marker, generation: UUID())
    }
    func arm(marker: UInt64, notification _: UserActivityPauseSignal) throws -> UserActivitySessionLease {
        lease = UserActivitySessionLease(marker: marker)
        return lease
    }
    func currentLeaseForTest() -> UserActivitySessionLease { lease }
    func beginFragment(lease: UserActivitySessionLease) throws {
        guard lease === self.lease, !fragmentActive else { throw UserActivityMonitoringError.notArmed }
        fragmentActive = true
    }
    func endFragment(lease: UserActivitySessionLease) {
        if lease === self.lease { fragmentActive = false }
    }
    func assertNotPaused(lease: UserActivitySessionLease) throws {
        guard lease === self.lease else { throw UserActivityMonitoringError.notArmed }
        assertions += 1
        if assertions >= pauseAfter ?? .max { paused = true }
        if paused { throw UserActivityMonitoringError.paused }
    }
    func heldInputScope(lease: UserActivitySessionLease) throws -> HeldInputScope {
        try assertNotPaused(lease: lease)
        return scope
    }
    func performPIDEvent(
        lease: UserActivitySessionLease,
        validate: () throws -> Void,
        mutation: () throws -> Void
    ) throws {
        try assertNotPaused(lease: lease)
        try validate()
        try assertNotPaused(lease: lease)
        try mutation()
    }
    func performPIDCleanup(lease _: UserActivitySessionLease, _ cleanup: () throws -> Void) throws {
        try cleanup()
    }
    func cleanupHeldInputs(lease _: UserActivitySessionLease) throws -> HeldInputCleanupResult {
        HeldInputCleanupResult(attempted: 0, released: 0, failed: 0)
    }
    func disarm(lease: UserActivitySessionLease) throws -> Bool {
        guard lease === self.lease else { throw UserActivityMonitoringError.notArmed }
        return paused
    }
}

private func harnessPIDPlan(_ actions: [NativeAction]) -> [PlannedDispatchEntry] {
    actions.enumerated().map { index, action in
        let actionClass: DispatchActionClass
        switch action.kind {
        case .click, .rightClick: actionClass = .click
        case .doubleClick: actionClass = .doubleClick
        case .scroll: actionClass = .scroll
        case .drag: actionClass = .drag
        case .type, .keypress, .wait:
            preconditionFailure("PID harness plan accepts pointer actions only")
        }
        let reference = "pointer-\(index)"
        let source = NativeAction(
            kind: action.kind,
            x: action.x,
            y: action.y,
            endX: action.endX,
            endY: action.endY,
            text: action.text,
            key: action.key,
            deltaX: action.deltaX,
            deltaY: action.deltaY,
            durationMS: action.durationMS,
            elementRef: nil,
            targetElementRef: reference,
            modifiers: action.modifiers
        )
        return PlannedDispatchEntry(
            sourceIndex: index,
            source: source,
            backend: .pidPointer,
            actionClass: actionClass,
            resolved: nil,
            pointerSafeRegion: PointerSafeRegionAuthority(
                reference: reference,
                identityToken: reference,
                bounds: CGRect(x: 0, y: 0, width: 300, height: 200)
            )
        )
    }
}

private func testPIDTargetedExecutorGuardsAndPauseCleanup() throws {
    let expected = ActionGuard(
        pid: 77,
        windowID: 88,
        bounds: CGRect(x: 100, y: 200, width: 300, height: 200),
        axIdentity: 99,
        snapshotID: "snapshot",
        interactionMode: .foregroundTakeover
    )
    let application = PIDTargetApplication(bundleIdentifier: "com.example.Editor", version: "1")
    let compatibility = PIDInputCompatibilityRegistry(cells: [
        .init(bundleIdentifier: application.bundleIdentifier, version: application.version, action: .click),
    ])
    let poster = HarnessPIDPoster()
    let validator = HarnessPIDValidator()
    let activity = HarnessActivityMonitor(pauseAfter: 6)
    let lease = activity.currentLeaseForTest()
    let held = HeldInputRegistry()
    let executor = PIDTargetedActionExecutor(
        poster: poster,
        compatibility: compatibility,
        activity: activity,
        validator: validator,
        heldInputs: held,
        element: { reference, snapshotID in
            guard snapshotID == expected.snapshotID, reference.hasPrefix("pointer-") else { return nil }
            return ActionElement(
                element: nil,
                identityToken: reference,
                bounds: CGRect(x: 0, y: 0, width: 300, height: 200),
                roleResult: .init(value: "AXGroup", status: .complete),
                subroleResult: .init(value: nil, status: .complete),
                enabled: true,
                actionNames: .complete([])
            )
        },
        evidence: { _, _ in true },
        delay: { _ in }
    )
    let result = executor.run(
        expected: expected,
        application: application,
        lease: lease,
        actions: harnessPIDPlan([.click(x: 10, y: 10), .click(x: 20, y: 20)])
    )
    try require(result.cooperativeError == .userActivityPaused, "physical activity did not pause PID batch")
    try require(result.lastAcknowledgedAction == -1, "paused action was incorrectly acknowledged")
    try require(poster.events.count == 2, "pause after mouse-down did not emit exactly one paired release")
    try require(
        poster.events[0].0 == .mouseDown(point: CGPoint(x: 110, y: 210), clickCount: 1)
            && poster.events[1].0 == .mouseUp(point: CGPoint(x: 110, y: 210), clickCount: 1),
        "pause cleanup did not balance mouse-down"
    )
    try require(poster.events.allSatisfy { $0.1 == 77 && $0.2 == 1234 }, "PID or marker changed during paired input")
    try require(validator.calls == 2, "normal release guard or guard-free cleanup-only release drifted")
    try require(held.heldCount == 0, "pause cleanup left PID input held")
}

private enum HarnessHelperLoopError: Error { case input, encoding }
private enum HarnessCleanupStepError: Error { case injected }

private func testHelperMainFailuresCleanupHeldInputBeforeExitCode() throws {
    func heldRegistry() throws -> (HeldInputRegistry, HarnessSyntheticPoster) {
        let registry = HeldInputRegistry()
        let poster = HarnessSyntheticPoster()
        try registry.begin(
            token: UUID(), poster: poster,
            down: .virtualKeyDown(0, []), release: .virtualKeyUp(0, [])
        )
        return (registry, poster)
    }

    let (readRegistry, readPoster) = try heldRegistry()
    let readCode = runHelperLoop(
        next: { throw HarnessHelperLoopError.input },
        handle: { _ in Dispatcher.invalidRequest(message: "unused") },
        write: { _ in },
        cleanup: { _ = readRegistry.cleanupAll() },
        report: { _ in }
    )
    try require(readCode == 1, "stdin failure did not return a failing exit code")
    try require(readRegistry.heldCount == 0 && readPoster.events.count == 2, "stdin failure skipped held-input cleanup")

    let (encodingRegistry, encodingPoster) = try heldRegistry()
    var delivered = false
    let encodingCode = runHelperLoop(
        next: {
            guard !delivered else { return nil }
            delivered = true
            return .line(Data("request".utf8))
        },
        handle: { _ in Dispatcher.invalidRequest(message: "encoding probe") },
        write: { _ in throw HarnessHelperLoopError.encoding },
        cleanup: { _ = encodingRegistry.cleanupAll() },
        report: { _ in }
    )
    try require(encodingCode == 1, "encoding failure did not return a failing exit code")
    try require(encodingRegistry.heldCount == 0 && encodingPoster.events.count == 2, "encoding failure skipped held-input cleanup")
}

private func testHelperTerminalCleanupOrderAcrossEOFReadWriteAndSIGTERM() throws {
    enum Path: CaseIterable { case eof, read, write, sigterm }

    for path in Path.allCases {
        var events: [String] = []
        let cleanup = HelperTerminalCleanupCoordinator(steps: [
            .init(name: "dispatcher") { events.append("dispatcher") },
            .init(name: "artifacts") {
                events.append("artifacts")
                throw HarnessCleanupStepError.injected
            },
            .init(name: "cursor") { events.append("cursor") },
            .init(name: "held") { events.append("held") },
        ])
        var failures: [String] = []
        switch path {
        case .eof:
            let code = runHelperLoop(
                next: { nil },
                handle: { _ in Dispatcher.invalidRequest(message: "unused") },
                write: { _ in },
                cleanup: { failures = cleanup.run() },
                report: { _ in }
            )
            try require(code == 0, "EOF cleanup changed the helper exit code")
        case .read:
            let code = runHelperLoop(
                next: { throw HarnessHelperLoopError.input },
                handle: { _ in Dispatcher.invalidRequest(message: "unused") },
                write: { _ in },
                cleanup: { failures = cleanup.run() },
                report: { _ in }
            )
            try require(code == 1, "read failure cleanup changed the helper exit code")
        case .write:
            var delivered = false
            var descriptors = [Int32](repeating: -1, count: 2)
            try require(Darwin.pipe(&descriptors) == 0, "could not create the broken-pipe probe")
            _ = Darwin.close(descriptors[0])
            defer { _ = Darwin.close(descriptors[1]) }
            var reportDescriptors = [Int32](repeating: -1, count: 2)
            try require(Darwin.pipe(&reportDescriptors) == 0, "could not create the report-pipe probe")
            _ = Darwin.close(reportDescriptors[0])
            defer { _ = Darwin.close(reportDescriptors[1]) }
            installHelperBrokenPipeHandling()
            let code = runHelperLoop(
                next: {
                    guard !delivered else { return nil }
                    delivered = true
                    return .line(Data("request".utf8))
                },
                handle: { _ in Dispatcher.invalidRequest(message: "write probe") },
                write: { try writeResponse($0, fileDescriptor: descriptors[1]) },
                cleanup: { failures = cleanup.run() },
                report: { writeHelperDiagnostic($0, fileDescriptor: reportDescriptors[1]) }
            )
            try require(code == 1, "write failure cleanup changed the helper exit code")
        case .sigterm:
            let handled = DispatchSemaphore(value: 0)
            let source = installTerminationInputCleanup(
                { failures = runHelperSIGTERMCleanup(cleanup) },
                terminate: { _ in handled.signal() }
            )
            try require(Darwin.kill(Darwin.getpid(), SIGTERM) == 0, "could not deliver SIGTERM probe")
            try require(
                handled.wait(timeout: .now() + 2) == .success,
                "SIGTERM cleanup source did not run"
            )
            source.cancel()
            _ = Darwin.signal(SIGTERM, SIG_DFL)
        }
        try require(
            events == ["dispatcher", "artifacts", "cursor", "held"],
            "\(path) cleanup order changed or stopped after a failed step: \(events)"
        )
        try require(failures == ["artifacts"], "\(path) cleanup did not report only the failed step")
        try require(cleanup.run().isEmpty, "\(path) cleanup was not single-use")
        try require(events.count == 4, "\(path) cleanup replayed terminal actions")
    }

    let entered = DispatchSemaphore(value: 0)
    let release = DispatchSemaphore(value: 0)
    let callersReady = DispatchSemaphore(value: 0)
    let group = DispatchGroup()
    let resultsLock = NSLock()
    var concurrentRuns = 0
    var concurrentResults: [[String]] = []
    let concurrentCleanup = HelperTerminalCleanupCoordinator(steps: [
        .init(name: "blocking") {
            concurrentRuns += 1
            entered.signal()
            _ = release.wait(timeout: .now() + 2)
            throw HarnessCleanupStepError.injected
        },
    ])
    for _ in 0..<2 {
        group.enter()
        DispatchQueue.global().async {
            callersReady.signal()
            let result = concurrentCleanup.run()
            resultsLock.lock()
            concurrentResults.append(result)
            resultsLock.unlock()
            group.leave()
        }
    }
    try require(callersReady.wait(timeout: .now() + 2) == .success, "first cleanup claimant did not start")
    try require(callersReady.wait(timeout: .now() + 2) == .success, "second cleanup claimant did not start")
    try require(entered.wait(timeout: .now() + 2) == .success, "cleanup action did not start")
    release.signal()
    try require(group.wait(timeout: .now() + 2) == .success, "concurrent cleanup claimants deadlocked")
    try require(concurrentRuns == 1, "concurrent cleanup replayed terminal actions")
    try require(
        concurrentResults.filter { $0 == ["blocking"] }.count == 1
            && concurrentResults.filter(\.isEmpty).count == 1,
        "cleanup did not preserve single-claimant failure ownership: \(concurrentResults)"
    )
}

private func testUnicodeSerializerWorstCaseIsBoundedAndUseful() throws {
    let value = String(repeating: "🧑🏽‍💻", count: 512)
    let tree = AXSerializer.serialize(AXNode(role: "AXTextField", label: value, bounds: .zero), snapshotID: "unicode")
    try require(tree.jsonByteCount <= maximumAXJSONBytes, "multi-byte AX JSON exceeded 512 KiB")
    try require(tree.label?.count == 512, "single useful field did not retain the allowed 512 characters")
    try require(tree.label == value, "Unicode truncation split or changed graphemes")

    let leaf = AXNode(role: "AXStaticText", label: value, title: value, value: value, bounds: .zero)
    let worstCase = AXSerializer.serialize(
        AXNode(role: "AXWindow", bounds: .zero, children: Array(repeating: leaf, count: 3_000)),
        snapshotID: "unicode-worst-case"
    )
    try require(worstCase.jsonByteCount <= maximumAXJSONBytes, "worst-case multi-byte AX tree exceeded 512 KiB")
    try require(worstCase.nodeCount <= maximumAXNodes, "worst-case multi-byte AX tree exceeded the node cap")
}

private func testMalformedCFValuesFailClosed() throws {
    let malformed: CFTypeRef = "not-an-AX-value" as CFString
    try require(decodeAXPoint(malformed) == nil, "malformed AX point was accepted")
    try require(decodeAXSize(malformed) == nil, "malformed AX size was accepted")
    try require(decodeAXElement(malformed) == nil, "malformed focused AX element was accepted")

    var nanPoint = CGPoint(x: CGFloat.nan, y: 10)
    let nanPointValue = AXValueCreate(.cgPoint, &nanPoint)
    try require(decodeAXPoint(nanPointValue) == nil, "NaN AX point was accepted")
    var infinitePoint = CGPoint(x: 10, y: CGFloat.infinity)
    let infinitePointValue = AXValueCreate(.cgPoint, &infinitePoint)
    try require(decodeAXPoint(infinitePointValue) == nil, "infinite AX point was accepted")
    var negativeSize = CGSize(width: -1, height: 10)
    let negativeSizeValue = AXValueCreate(.cgSize, &negativeSize)
    try require(decodeAXSize(negativeSizeValue) == nil, "negative AX size was accepted")
    var infiniteSize = CGSize(width: 10, height: CGFloat.infinity)
    let infiniteSizeValue = AXValueCreate(.cgSize, &infiniteSize)
    try require(decodeAXSize(infiniteSizeValue) == nil, "infinite AX size was accepted")
    var unreasonableSize = CGSize(width: 1_000_001, height: 10)
    let unreasonableSizeValue = AXValueCreate(.cgSize, &unreasonableSize)
    try require(decodeAXSize(unreasonableSizeValue) == nil, "unreasonably large AX size was accepted")
}

private func testJSONEncodingFailureFallsBackExplicitly() throws {
    let invalidBounds = CGRect(x: CGFloat.nan, y: 0, width: 10, height: 10)
    let tree = AXSerializer.serialize(AXNode(role: "AXWindow", bounds: invalidBounds), snapshotID: "bad-json")
    try require(tree.jsonByteCount > 0, "JSON encoding failure was reported as zero bytes")
    try require(tree.jsonByteCount <= maximumAXJSONBytes, "JSON encoding fallback exceeded its cap")
    try require(tree.root.bounds == .zero, "JSON encoding failure did not use the safe fallback")
}

private enum AXBuildProbeError: Error { case failed }

private func testAXFailurePreventsArtifactPublication() throws {
    var published = false
    do {
        _ = try prepareSnapshotForPublication(
            buildAX: { throw AXBuildProbeError.failed },
            publishArtifact: { published = true }
        )
        throw HarnessFailure.assertion("AX failure unexpectedly succeeded")
    } catch AXBuildProbeError.failed {
        try require(!published, "artifact was published before AX preparation succeeded")
    }
}

private final class AXFailingObserver: WindowObserving {
    func apps() throws -> JSONValue { .object(["apps": .array([])]) }
    func snapshot(appRef _: String, windowRef _: String, scope _: String, artifactName _: String?, textDetail _: SnapshotTextDetailRequest) throws -> JSONValue {
        throw WindowObservationError.axSerializationFailed
    }
    func invalidateSnapshots() {}
}

private func testAXFailureReturnsStructuredHelperError() throws {
    let response = Dispatcher(permissions: StaticHarnessPermissions(), windows: AXFailingObserver()).handle(
        #"{"protocol_version":4,"request_id":"ax-failure","operation":"snapshot","payload":{"app_ref":"app","window_ref":"window","scope":"target_window","artifact_name":"capture.png"}}"#
    )
    try require(!response.ok, "AX failure unexpectedly succeeded")
    try require(response.error?.code == "helper_failed", "AX failure used the wrong protocol code")
    try require(response.error?.message.contains("Accessibility tree") == true, "AX failure message was not specific")
}

private struct StaticHarnessPermissions: PermissionStatusProviding {
    var accessibilityTrusted: Bool { true }
    var screenRecordingAllowed: Bool { true }
}

private func testUTF8TruncationUsesLinearByteAccounting() throws {
    let value = String(repeating: "🧑🏽‍💻", count: 1_000)
    let result = truncateUTF8Linearly(value, maximumCharacters: 512, maximumBytes: 7_680)
    try require(result.count == 512, "linear UTF-8 truncation lost allowed graphemes")
    try require(result.lengthOfBytes(using: String.Encoding.utf8) <= 7_680, "linear UTF-8 truncation exceeded byte limit")
}

private func runNamed(_ name: String, _ test: () throws -> Void) throws {
    try test()
    print("PASS \(name)")
}


do {
    try runNamed("cooperative_protocol_v4_types_and_frames", testCooperativeProtocolV4TypesAndStrictFrames)
    try runNamed("helper_happy_path", testHappyPath)
    try runNamed("strict_frame_validation", testStrictFrameValidation)
    try runNamed("recognized_errors_echo_request_ids", testRecognizedRequestErrorsEchoTheirIDs)
    try runNamed("message_boundary", testMessageBoundaryAndRequestID)
    try runNamed("observation_protocol_boundaries", testObservationProtocolBoundaries)
    try runNamed("deterministic_get_app_state_background_invariants", testDeterministicBackgroundFixtureGetAppState)
    try runNamed("secure_fields_never_read_values", testSecureSubroleNeverReadsValue)
    try runNamed("bounded_cfstring_identity_fail_secure", testBoundedCFStringAdapterFailsSecureOnIncompleteIdentity)
    try runNamed("bounded_tree_and_cross_snapshot_refs", testBoundedTreeAndSnapshotReferenceContracts)
    try runNamed("ax_reader_budgeted_collection", testAXReaderBoundsCollectionBeforeSerialization)
    try runNamed("ax_reader_dialog_depth_limit", testAXReaderHonorsFocusedDialogDepthLimit)
    try runNamed("ax_reader_application_menu_items", testAXReaderIncludesBoundedApplicationMenuItems)
    try runNamed("bounded_action_cfarray_prefix", testActionAdapterReadsOnlyBoundedCFArrayPrefix)
    try runNamed("artifact_write_eintr_retry_and_mode", testArtifactWriteRetriesEINTRAndPublishes0600)
    try runNamed("artifact_write_failure_cleanup", testArtifactWriteFailureLeavesNoFinalOrTemporary)
    try runNamed("artifact_file_fsync_failure_cleanup", testArtifactFileSyncFailureLeavesNoFinalOrTemporary)
    try runNamed("artifact_rename_conflict_no_overwrite", testArtifactRenameConflictPreservesExistingFinal)
    try runNamed("artifact_darwin_no_overwrite", testDarwinArtifactPublisherNeverOverwrites)
    try runNamed("artifact_rename_failure_cleanup", testArtifactRenameFailureLeavesNoFinalOrTemporary)
    try runNamed("artifact_directory_fsync_uncertain", testArtifactDirectorySyncFailureReportsVisibleCompleteFinal)
    try runNamed("smart_artifact_bundle_posix_and_quota_recovery", testSmartArtifactBundlePOSIXAndRecoveredQuota)
    try runNamed("smart_artifact_bundle_poison_and_rollback", testSmartArtifactBundlePoisonAndRollbackLifecycle)
    try runNamed("window_capture_identity_stability", testWindowIdentityStabilityContracts)
    try runNamed("window_exact_capture_fallback", testExactWindowCaptureFallbackContracts)
    try runNamed("window_exact_capture_command", testExactWindowCommandContracts)
    try runNamed("window_exact_capture_system_runner", testSystemExactWindowCommandRunner)
    try runNamed("window_contained_transient_boundary", testContainedTransientWindowBoundary)
    try runNamed("window_contained_overlay_detection", testContainedAppOwnedOverlayDetection)
    try runNamed("window_application_menu_bar_boundary", testApplicationMenuBarIsBoundedToSelectedWindowObservation)
    try runNamed("window_focus_failure", testFocusFailureIsRejected)
    try runNamed("window_frontmost_app_switch", testCaptureRejectsFrontmostAppSwitch)
    try runNamed("window_candidate_filtering", testWindowCandidateFiltering)
    try runNamed("window_title_document_path_normalization", testWindowTitleAndDocumentPathNormalization)
    try runNamed("snapshot_reference_registry_lifecycle", testSnapshotReferenceRegistryLifecycle)
    try runNamed("action_input_phase_unknown_outcome", testInputPhaseControlsUnknownOutcome)
    try runNamed("action_keyboard_fail_secure", testEveryKeyboardActionFailsSecureBeforeInput)
    try runNamed("action_keyboard_identity_no_value", testKeyboardIdentityReaderNeverReadsValue)
    try runNamed("guarded_action_batch_stop_ack", testGuardedActionBatchStopsAndAcknowledgesExactly)
    try runNamed("action_stale_secure_coordinate_guards", testActionGuardsRejectStaleSecureAndCoordinates)
    try runNamed("action_element_press_fallback_refs", testElementPressAndCenterFallbackRemainSnapshotLocal)
    try runNamed("action_secure_identity_fail_closed", testActionSecureIdentityFailsClosed)
    try runNamed("action_dispatch_background_cooperative", testActionDispatcherRoutesBackgroundCooperativeBatches)
    try runNamed("action_cross_language_schema_vectors", testNativeActionSchemaMatchesSharedVectors)
    try runNamed("action_all_kinds_guarded_core", testEveryActionKindRunsThroughGuardedCore)
    try runNamed("action_balanced_cleanup_preflight", testBalancedInputCleanupAndPermissionPreflight)
    try runNamed("action_accessibility_first_unicode", testAccessibilityFirstUnicodeTyping)
    try runNamed("action_background_ax_only_no_synthetic", testBackgroundDispatcherPostsNoSyntheticEventsForAXOnlyBatch)
    try runNamed("action_ax_selected_text_phase_mapping", testSystemAXSelectedTextWriterPhaseMapping)
    try runNamed("action_names_ax_errors_fail_closed", testIncompleteActionNamesAndAXErrorsFailClosed)
    try runNamed("user_activity_and_pid_compatibility", testUserActivityClassificationAndCompatibilityDefaults)
    try runNamed("build_validates_staged_compatibility_before_signing", testBuildValidatesStagedCompatibilityBytesBeforeSigning)
    try runNamed("compatibility_deep_nesting_no_crash", testCompatibilityRegistryRejectsDeepNestingWithoutProcessCrash)
    try runNamed("pid_targeted_guard_pause_cleanup", testPIDTargetedExecutorGuardsAndPauseCleanup)
    try runNamed("helper_main_failures_cleanup_held_input", testHelperMainFailuresCleanupHeldInputBeforeExitCode)
    try runNamed("helper_terminal_cleanup_order", testHelperTerminalCleanupOrderAcrossEOFReadWriteAndSIGTERM)
    try runNamed("unicode_serializer_worst_case", testUnicodeSerializerWorstCaseIsBoundedAndUseful)
    try runNamed("malformed_cf_values_fail_closed", testMalformedCFValuesFailClosed)
    try runNamed("json_encoding_failure_fallback", testJSONEncodingFailureFallsBackExplicitly)
    try runNamed("ax_failure_prevents_artifact_publication", testAXFailurePreventsArtifactPublication)
    try runNamed("ax_failure_structured_error", testAXFailureReturnsStructuredHelperError)
    try runNamed("utf8_linear_byte_accounting", testUTF8TruncationUsesLinearByteAccounting)
    print("AstraMacComputerHelperHarness: 62 tests passed")
} catch {
    FileHandle.standardError.write(Data("AstraMacComputerHelperHarness failed: \(error)\n".utf8))
    exit(1)
}

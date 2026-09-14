import ApplicationServices
import Foundation

/// Owned by exactly one observation; never stores action authority between requests.
final class ObservationPIDInventory<Value> {
    private var values: [pid_t: Value] = [:]
    func value(for pid: pid_t, load: () -> Value) -> Value {
        if let value = values[pid] { return value }
        let value = load()
        values[pid] = value
        return value
    }
}

let maximumOrdinaryAXDuration: TimeInterval = 5
let maximumObservationAXCallDuration: TimeInterval = 0.25

final class AXObservationBudget {
    static var current: AXObservationBudget? { Thread.current.threadDictionary["astra.observationBudget"] as? AXObservationBudget }
    func install() -> () -> Void {
        let previous = Thread.current.threadDictionary["astra.observationBudget"]
        Thread.current.threadDictionary["astra.observationBudget"] = self
        return { Thread.current.threadDictionary["astra.observationBudget"] = previous }
    }
    func phaseTimeout(maximum: TimeInterval) throws -> TimeInterval {
        guard available else { throw WindowObservationError.observationTimedOut }
        return min(maximum, remainingDuration)
    }
    func check() throws { guard available else { throw WindowObservationError.observationTimedOut } }
    private let clock: () -> TimeInterval
    private let deadline: TimeInterval
    private var externalRemainingBudget: (() -> TimeInterval)?
    private var remainingDuration: TimeInterval { externalRemainingBudget?() ?? (deadline - clock()) }
    private let setTimeout: (AXUIElement, Float) -> AXError
    private(set) var calls = 0
    private(set) var failed = false
    var expired: Bool { let remaining = remainingDuration; return !remaining.isFinite || remaining <= 0 }
    var available: Bool { !failed && !expired }
    init(duration: TimeInterval = maximumOrdinaryAXDuration,
         clock: @escaping () -> TimeInterval = { ProcessInfo.processInfo.systemUptime },
         setTimeout: @escaping (AXUIElement, Float) -> AXError = { AXUIElementSetMessagingTimeout($0, $1) }) {
        self.clock = clock
        let start = clock()
        self.deadline = start.isFinite && duration.isFinite && duration > 0 ? start + duration : -.infinity
        self.setTimeout = setTimeout
    }
    /// Share an existing transaction deadline and cancellation signal without restarting its clock.
    convenience init(remainingBudget: @escaping () -> TimeInterval,
                     setTimeout: @escaping (AXUIElement, Float) -> AXError = { AXUIElementSetMessagingTimeout($0, $1) }) {
        self.init(setTimeout: setTimeout)
        self.externalRemainingBudget = remainingBudget
    }
    func call<Value>(element: AXUIElement, _ body: () -> Value) -> Value? {
        guard available else { return nil }
        let remaining = min(remainingDuration, maximumObservationAXCallDuration)
        guard remaining.isFinite, remaining > 0 else { return nil }
        var timeout = Float(remaining)
        if Double(timeout) > remaining { timeout = timeout.nextDown }
        guard timeout > 0, setTimeout(element, timeout) == .success else { failed = true; return nil }
        guard available else { _ = setTimeout(element, 0); return nil }
        calls += 1
        let value = body()
        guard setTimeout(element, 0) == .success else { failed = true; return nil }
        return available ? value : nil
    }
}

/// Only fixed-size non-text values are shared between ordinary and detail views.
/// Security metadata and text are always reread: a cached role must never authorize
/// a later fresh value after a control changes to a secure field.
final class AXObservationAttributeCache {
    static var current: AXObservationAttributeCache? { Thread.current.threadDictionary["astra.observationAttributeCache"] as? AXObservationAttributeCache }
    func install() -> () -> Void {
        let previous = Thread.current.threadDictionary["astra.observationAttributeCache"]
        Thread.current.threadDictionary["astra.observationAttributeCache"] = self
        return { Thread.current.threadDictionary["astra.observationAttributeCache"] = previous }
    }
    private struct Entry {
        let element: AXUIElement
        var attributes: [String: (AXError, CFTypeRef?)]
    }
    private var entries: [CFHashCode: [Entry]] = [:]
    private(set) var hits = 0
    private let reusable: Set<String> = [kAXPositionAttribute, kAXSizeAttribute, kAXEnabledAttribute, kAXFocusedAttribute]
    func read(element: AXUIElement, attribute: String, load: () -> (AXError, CFTypeRef?)) -> (AXError, CFTypeRef?) {
        guard reusable.contains(attribute) else { return load() }
        let hash = CFHash(element)
        if let entry = entries[hash]?.first(where: { CFEqual($0.element, element) }),
           let value = entry.attributes[attribute] { hits += 1; return value }
        let value = load()
        // Do not retain attacker-sized CF objects in a supposedly fixed-size cache.
        guard value.0 == .success, let object = value.1,
              CFGetTypeID(object) == AXValueGetTypeID() || CFGetTypeID(object) == CFBooleanGetTypeID()
        else { return value }
        var bucket = entries[hash] ?? []
        if let index = bucket.firstIndex(where: { CFEqual($0.element, element) }) {
            bucket[index].attributes[attribute] = value
        } else { bucket.append(Entry(element: element, attributes: [attribute: value])) }
        entries[hash] = bucket
        return value
    }
}

func helperBuildIdentity(data: Data?) -> JSONValue {
    let raw = data.flatMap { try? JSONSerialization.jsonObject(with: $0) as? [String: Any] } ?? [:]
    func hex(_ key: String, count: Int) -> String {
        guard let value = raw[key] as? String, value.utf8.count == count,
              value.allSatisfy({ "0123456789abcdef".contains($0) }) else { return "unknown" }
        return value
    }
    let buildID = (raw["build_id"] as? String).flatMap(UUID.init(uuidString:))?.uuidString.lowercased() ?? "unknown"
    return .object([
        "helper_git_revision": .string(hex("helper_git_revision", count: 40)),
        "compatibility_registry_sha256": .string(hex("compatibility_registry_sha256", count: 64)),
        "build_id": .string(buildID),
        "helper_source_dirty": .bool(raw["helper_source_dirty"] as? Bool ?? true),
    ])
}

func bundledHelperBuildIdentity() -> JSONValue {
    helperBuildIdentity(data: Bundle.main.url(forResource: "build-info", withExtension: "json").flatMap { try? Data(contentsOf: $0) })
}

final class ObservationStageMetrics {
    enum Stage: String { case inventory, image, png, ax, detail, finalValidation = "final_validation" }
    private let start = ProcessInfo.processInfo.systemUptime
    private var milliseconds: [String: Double] = [:]
    func measure<Value>(_ stage: Stage, _ body: () throws -> Value) rethrows -> Value {
        let before = ProcessInfo.processInfo.systemUptime
        defer { milliseconds[stage.rawValue, default: 0] += (ProcessInfo.processInfo.systemUptime - before) * 1_000 }
        return try body()
    }
    func record(_ stage: Stage, startedAt: TimeInterval) {
        milliseconds[stage.rawValue, default: 0] += (ProcessInfo.processInfo.systemUptime - startedAt) * 1_000
    }
    func emit(budget: AXObservationBudget?, cache: AXObservationAttributeCache?) {
        let payload: [String: Any] = [
            "type": "observation_metrics", "stages_ms": milliseconds,
            "total_ms": (ProcessInfo.processInfo.systemUptime - start) * 1_000,
            "ax_calls": budget?.calls ?? 0, "ax_cache_hits": cache?.hits ?? 0,
            "ax_budget_expired": budget?.expired ?? false, "ax_timeout_setup_failed": budget?.failed ?? false,
        ]
        if let data = try? JSONSerialization.data(withJSONObject: payload, options: [.sortedKeys]) {
            FileHandle.standardError.write(data + Data("\n".utf8))
        }
    }
}

func observationAXCall<Value>(element: AXUIElement, fallback: Value, _ body: () -> Value) -> Value {
    guard let budget = AXObservationBudget.current else { return body() }
    return budget.call(element: element, body) ?? fallback
}

func observationAXAttribute(_ element: AXUIElement, _ attribute: String) -> (AXError, CFTypeRef?) {
    let load = {
        observationAXCall(element: element, fallback: (AXError.cannotComplete, nil as CFTypeRef?)) {
            var value: CFTypeRef?
            let error = AXUIElementCopyAttributeValue(element, attribute as CFString, &value)
            return (error, value)
        }
    }
    guard let cache = AXObservationAttributeCache.current else { return load() }
    return cache.read(element: element, attribute: attribute, load: load)
}

struct CatalogAXWindowRecord {
    let element: AXUIElement
    let bounds: CGRect
    let title: BoundedAXStringResult
}

func catalogAXWindowRecords(pid: pid_t) -> [CatalogAXWindowRecord] {
    (completeObservedAXWindows(AXUIElementCreateApplication(pid)) ?? []).compactMap {
        guard let bounds = AXNodeReader.frameAttribute($0) else { return nil }
        return CatalogAXWindowRecord(element: $0, bounds: bounds, title: AXNodeReader.stringAttribute($0, kAXTitleAttribute))
    }
}

func matchingCatalogAXWindow(target: WindowTarget, records: [CatalogAXWindowRecord]) -> AXUIElement? {
    let matches = records.filter {
        screenCaptureBoundsMatchAXBounds(screenCapture: target.bounds, accessibility: $0.bounds) &&
        $0.title.status == .complete &&
        windowTitlesMatch(screenCaptureTitle: target.title, accessibilityTitle: $0.title.value ?? "") &&
        (target.axIdentity == nil || target.axIdentity == CFHash($0.element)) &&
        (target.axElement == nil || CFEqual(target.axElement!, $0.element))
    }
    return matches.count == 1 ? matches[0].element : nil
}

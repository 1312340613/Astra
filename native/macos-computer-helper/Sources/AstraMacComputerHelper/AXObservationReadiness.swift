@preconcurrency import ApplicationServices
import Foundation

protocol AXReadinessProviding {
    func enabled(_ application: AXUIElement) -> Bool?
    func isSettable(_ application: AXUIElement) -> Bool
    func enable(_ application: AXUIElement) -> AXError
}

struct SystemAXReadinessProvider: AXReadinessProviding {
    private let attribute = "AXEnhancedUserInterface" as CFString
    func enabled(_ application: AXUIElement) -> Bool? {
        var value: CFTypeRef?
        guard AXUIElementCopyAttributeValue(application, attribute, &value) == .success,
              let value, CFGetTypeID(value) == CFBooleanGetTypeID() else { return nil }
        return value as? Bool
    }
    func isSettable(_ application: AXUIElement) -> Bool {
        var settable = DarwinBoolean(false)
        return AXUIElementIsAttributeSettable(application, attribute, &settable) == .success && settable.boolValue
    }
    func enable(_ application: AXUIElement) -> AXError {
        AXUIElementSetAttributeValue(application, attribute, kCFBooleanTrue)
    }
}

// Capability negotiation for the explicitly selected application. Some providers
// return notImplemented after changing the flag; only a fresh read proves it.
final class AXObservationReadiness {
    enum Result { case alreadyEnabled, enabled, unavailable, unconfirmed, alreadyRequested }
    private struct Attempt {
        let launchedAt: TimeInterval
        let application: AXUIElement
    }
    private var attempts: [pid_t: Attempt] = [:]
    private let provider: any AXReadinessProviding
    private let settle: (TimeInterval) -> Void
    private let settleDuration: TimeInterval = 2.25

    init(provider: any AXReadinessProviding = SystemAXReadinessProvider(),
         settle: @escaping (TimeInterval) -> Void = { Thread.sleep(forTimeInterval: $0) }) {
        self.provider = provider
        self.settle = settle
    }

    func prepare(application: AXUIElement, pid: pid_t, launchedAt: TimeInterval,
                 budget: AXObservationBudget) throws -> Result {
        guard pid > 0, launchedAt.isFinite else { return .unavailable }
        func call<T>(_ operation: () -> T) throws -> T {
            guard let value = budget.call(element: application, operation) else {
                throw WindowObservationError.observationTimedOut
            }
            return value
        }
        guard let enabled = try call({ provider.enabled(application) }) else { return .unavailable }
        if enabled { return .alreadyEnabled }
        if let previous = attempts[pid], previous.launchedAt == launchedAt,
           CFEqual(previous.application, application) { return .alreadyRequested }
        guard try call({ provider.isSettable(application) }),
              attempts[pid] != nil || attempts.count < maximumObservedApplications
        else { return .unavailable }
        // Record before dispatch so even an uncertain return cannot cause replay.
        attempts[pid] = Attempt(launchedAt: launchedAt, application: application)
        _ = try call { provider.enable(application) }
        guard try call({ provider.enabled(application) }) == true else { return .unconfirmed }
        guard try budget.phaseTimeout(maximum: settleDuration) >= settleDuration else {
            throw WindowObservationError.observationTimedOut
        }
        settle(settleDuration)
        try budget.check()
        return .enabled
    }
}

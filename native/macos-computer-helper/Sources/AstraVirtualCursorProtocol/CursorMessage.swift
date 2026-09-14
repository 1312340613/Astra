import CoreGraphics
import Foundation

public let maximumCursorMessageBytes = 512
public let maximumVirtualCursorCoordinate = 1_000_000.0

public enum CursorProtocolError: Error, Equatable {
    case oversizedFrame
    case malformedFrame
    case unknownCommand
    case invalidCoordinate
    case unsafePanel
}

public enum CursorMessage: Equatable, Sendable {
    case show(CGPoint)
    case move(CGPoint)
    case click(CGPoint)
    case hide
}

public enum CursorMessageCodec {
    public static func encode(_ message: CursorMessage) throws -> Data {
        let object: [String: Any]
        switch message {
        case let .show(point): object = try pointObject(command: "show", point: point)
        case let .move(point): object = try pointObject(command: "move", point: point)
        case let .click(point): object = try pointObject(command: "click", point: point)
        case .hide: object = ["command": "hide"]
        }
        return try encodeBounded(object)
    }

    public static func decode(_ data: Data) throws -> CursorMessage {
        let object = try decodeObject(data)
        guard let command = strictString(object["command"]) else { throw CursorProtocolError.malformedFrame }
        switch command {
        case "hide":
            guard Set(object.keys) == ["command"] else { throw CursorProtocolError.malformedFrame }
            return .hide
        case "show", "move", "click":
            guard Set(object.keys) == ["command", "x", "y"],
                  let x = strictDouble(object["x"]),
                  let y = strictDouble(object["y"])
            else { throw CursorProtocolError.malformedFrame }
            let point = CGPoint(x: x, y: y)
            try validate(point)
            if command == "show" { return .show(point) }
            if command == "move" { return .move(point) }
            return .click(point)
        default:
            throw CursorProtocolError.unknownCommand
        }
    }

    public static func validate(_ point: CGPoint) throws {
        guard point.x.isFinite, point.y.isFinite,
              abs(Double(point.x)) <= maximumVirtualCursorCoordinate,
              abs(Double(point.y)) <= maximumVirtualCursorCoordinate
        else { throw CursorProtocolError.invalidCoordinate }
    }

    private static func pointObject(command: String, point: CGPoint) throws -> [String: Any] {
        try validate(point)
        return ["command": command, "x": Double(point.x), "y": Double(point.y)]
    }
}

public struct CursorPanelProperties: Equatable, Sendable {
    public let borderlessPanel: Bool
    public let transparent: Bool
    public let nonactivatingPanel: Bool
    public let ignoresMouseEvents: Bool
    public let canBecomeKey: Bool
    public let canBecomeMain: Bool

    public init(
        borderlessPanel: Bool,
        transparent: Bool,
        nonactivatingPanel: Bool,
        ignoresMouseEvents: Bool,
        canBecomeKey: Bool,
        canBecomeMain: Bool
    ) {
        self.borderlessPanel = borderlessPanel
        self.transparent = transparent
        self.nonactivatingPanel = nonactivatingPanel
        self.ignoresMouseEvents = ignoresMouseEvents
        self.canBecomeKey = canBecomeKey
        self.canBecomeMain = canBecomeMain
    }
}

public struct CursorSidecarHello: Equatable, Sendable {
    public let windowID: UInt32
    public let panel: CursorPanelProperties

    public init(windowID: UInt32, panel: CursorPanelProperties) {
        self.windowID = windowID
        self.panel = panel
    }

    public var isSafeForCooperativePresentation: Bool {
        windowID > 0 && panel.borderlessPanel && panel.transparent && panel.nonactivatingPanel &&
            panel.ignoresMouseEvents && !panel.canBecomeKey && !panel.canBecomeMain
    }
}

public enum CursorSidecarHelloCodec {
    public static func encode(_ hello: CursorSidecarHello) throws -> Data {
        guard hello.isSafeForCooperativePresentation else { throw CursorProtocolError.unsafePanel }
        return try encodeBounded([
            "type": "ready",
            "window_id": Int(hello.windowID),
            "panel": [
                "borderless_panel": hello.panel.borderlessPanel,
                "transparent": hello.panel.transparent,
                "nonactivating_panel": hello.panel.nonactivatingPanel,
                "ignores_mouse_events": hello.panel.ignoresMouseEvents,
                "can_become_key": hello.panel.canBecomeKey,
                "can_become_main": hello.panel.canBecomeMain,
            ],
        ])
    }

    public static func decode(_ data: Data) throws -> CursorSidecarHello {
        let object = try decodeObject(data)
        guard Set(object.keys) == ["type", "window_id", "panel"],
              strictString(object["type"]) == "ready",
              let windowNumber = strictInteger(object["window_id"]),
              windowNumber > 0, windowNumber <= Int64(UInt32.max),
              let panel = strictObject(object["panel"]),
              Set(panel.keys) == [
                  "borderless_panel", "transparent", "nonactivating_panel",
                  "ignores_mouse_events", "can_become_key", "can_become_main",
              ],
              let borderless = strictBool(panel["borderless_panel"]),
              let transparent = strictBool(panel["transparent"]),
              let nonactivating = strictBool(panel["nonactivating_panel"]),
              let ignoresMouse = strictBool(panel["ignores_mouse_events"]),
              let canBecomeKey = strictBool(panel["can_become_key"]),
              let canBecomeMain = strictBool(panel["can_become_main"])
        else { throw CursorProtocolError.malformedFrame }
        let hello = CursorSidecarHello(
            windowID: UInt32(windowNumber),
            panel: CursorPanelProperties(
                borderlessPanel: borderless,
                transparent: transparent,
                nonactivatingPanel: nonactivating,
                ignoresMouseEvents: ignoresMouse,
                canBecomeKey: canBecomeKey,
                canBecomeMain: canBecomeMain
            )
        )
        guard hello.isSafeForCooperativePresentation else { throw CursorProtocolError.unsafePanel }
        return hello
    }
}

public protocol CursorRendering: AnyObject {
    func show(at point: CGPoint) throws
    func move(to point: CGPoint) throws
    func click(at point: CGPoint) throws
    func hide()
    func remove()
}

public enum CursorSessionDisposition: Equatable {
    case `continue`
    case exit
}

public final class CursorProtocolSession {
    private let renderer: any CursorRendering
    private var exited = false

    public init(renderer: any CursorRendering) { self.renderer = renderer }

    public func consume(_ frame: Data) -> CursorSessionDisposition {
        guard !exited else { return .exit }
        do {
            switch try CursorMessageCodec.decode(frame) {
            case let .show(point): try renderer.show(at: point)
            case let .move(point): try renderer.move(to: point)
            case let .click(point): try renderer.click(at: point)
            case .hide: renderer.hide()
            }
            return .continue
        } catch {
            return terminate()
        }
    }

    public func endOfFile() -> CursorSessionDisposition { terminate() }

    private func terminate() -> CursorSessionDisposition {
        guard !exited else { return .exit }
        exited = true
        renderer.hide()
        renderer.remove()
        return .exit
    }
}

private func encodeBounded(_ object: [String: Any]) throws -> Data {
    let data: Data
    do { data = try JSONSerialization.data(withJSONObject: object, options: [.sortedKeys]) }
    catch { throw CursorProtocolError.malformedFrame }
    guard !data.isEmpty, data.count <= maximumCursorMessageBytes else { throw CursorProtocolError.oversizedFrame }
    return data
}

private func decodeObject(_ data: Data) throws -> [String: StrictCursorJSONValue] {
    guard !data.isEmpty else { throw CursorProtocolError.malformedFrame }
    guard data.count <= maximumCursorMessageBytes else { throw CursorProtocolError.oversizedFrame }
    var parser = StrictCursorJSONParser(data: data)
    return try parser.parseRootObject()
}

private func strictString(_ value: StrictCursorJSONValue?) -> String? {
    guard case let .string(result)? = value else { return nil }
    return result
}

private func strictObject(_ value: StrictCursorJSONValue?) -> [String: StrictCursorJSONValue]? {
    guard case let .object(result)? = value else { return nil }
    return result
}

private func strictDouble(_ value: StrictCursorJSONValue?) -> Double? {
    guard case let .number(number)? = value, number.doubleValue.isFinite else { return nil }
    return number.doubleValue
}

private func strictInteger(_ value: StrictCursorJSONValue?) -> Int64? {
    guard case let .number(number)? = value else { return nil }
    let bytes = number.lexeme
    guard !bytes.isEmpty else { return nil }
    let negative = bytes[0] == 0x2D
    let digitStart = negative ? 1 : 0
    guard digitStart < bytes.count else { return nil }

    let limit = negative ? UInt64(Int64.max) + 1 : UInt64(Int64.max)
    var magnitude: UInt64 = 0
    for byte in bytes[digitStart...] {
        guard (0x30 ... 0x39).contains(byte) else { return nil }
        let digit = UInt64(byte - 0x30)
        guard magnitude <= (limit - digit) / 10 else { return nil }
        magnitude = magnitude * 10 + digit
    }
    if negative {
        if magnitude == UInt64(Int64.max) + 1 { return Int64.min }
        return -Int64(magnitude)
    }
    return Int64(magnitude)
}

private func strictBool(_ value: StrictCursorJSONValue?) -> Bool? {
    guard case let .bool(result)? = value else { return nil }
    return result
}

private indirect enum StrictCursorJSONValue {
    case object([String: StrictCursorJSONValue])
    case string(String)
    case number(StrictCursorJSONNumber)
    case bool(Bool)
    case null
}

private struct StrictCursorJSONNumber {
    let lexeme: [UInt8]
    let doubleValue: Double
}

private struct StrictCursorJSONParser {
    private let bytes: [UInt8]
    private var index = 0
    private let maximumObjectDepth = 2
    private let maximumObjectFields = 16

    init(data: Data) { bytes = Array(data) }

    mutating func parseRootObject() throws -> [String: StrictCursorJSONValue] {
        skipWhitespace()
        guard case let .object(object) = try parseValue(depth: 0) else {
            throw CursorProtocolError.malformedFrame
        }
        skipWhitespace()
        guard index == bytes.count else { throw CursorProtocolError.malformedFrame }
        return object
    }

    private mutating func parseValue(depth: Int) throws -> StrictCursorJSONValue {
        skipWhitespace()
        guard index < bytes.count else { throw CursorProtocolError.malformedFrame }
        switch bytes[index] {
        case 0x7B:
            guard depth < maximumObjectDepth else { throw CursorProtocolError.malformedFrame }
            return .object(try parseObject(depth: depth + 1))
        case 0x22:
            return .string(try parseString())
        case 0x74:
            try consumeLiteral("true")
            return .bool(true)
        case 0x66:
            try consumeLiteral("false")
            return .bool(false)
        case 0x6E:
            try consumeLiteral("null")
            return .null
        case 0x2D, 0x30 ... 0x39:
            return .number(try parseNumber())
        default:
            throw CursorProtocolError.malformedFrame
        }
    }

    private mutating func parseObject(depth: Int) throws -> [String: StrictCursorJSONValue] {
        try consume(0x7B)
        skipWhitespace()
        if consumeIfPresent(0x7D) { return [:] }
        var result: [String: StrictCursorJSONValue] = [:]
        while true {
            guard result.count < maximumObjectFields else { throw CursorProtocolError.malformedFrame }
            skipWhitespace()
            let key = try parseString()
            guard result[key] == nil else { throw CursorProtocolError.malformedFrame }
            skipWhitespace()
            try consume(0x3A)
            result[key] = try parseValue(depth: depth)
            skipWhitespace()
            if consumeIfPresent(0x7D) { return result }
            try consume(0x2C)
        }
    }

    private mutating func parseString() throws -> String {
        guard index < bytes.count, bytes[index] == 0x22 else { throw CursorProtocolError.malformedFrame }
        let start = index
        index += 1
        while index < bytes.count {
            let byte = bytes[index]
            if byte == 0x22 {
                index += 1
                let token = Data(bytes[start ..< index])
                do { return try JSONDecoder().decode(String.self, from: token) }
                catch { throw CursorProtocolError.malformedFrame }
            }
            guard byte >= 0x20 else { throw CursorProtocolError.malformedFrame }
            if byte == 0x5C {
                index += 1
                guard index < bytes.count else { throw CursorProtocolError.malformedFrame }
                if bytes[index] == 0x75 {
                    guard index + 4 < bytes.count,
                          bytes[(index + 1) ... (index + 4)].allSatisfy(Self.isHexDigit)
                    else { throw CursorProtocolError.malformedFrame }
                    index += 5
                    continue
                }
                guard [0x22, 0x5C, 0x2F, 0x62, 0x66, 0x6E, 0x72, 0x74].contains(bytes[index]) else {
                    throw CursorProtocolError.malformedFrame
                }
            }
            index += 1
        }
        throw CursorProtocolError.malformedFrame
    }

    private mutating func parseNumber() throws -> StrictCursorJSONNumber {
        let start = index
        _ = consumeIfPresent(0x2D)
        guard index < bytes.count else { throw CursorProtocolError.malformedFrame }
        if consumeIfPresent(0x30) {
            guard index == bytes.count || !(0x30 ... 0x39).contains(bytes[index]) else {
                throw CursorProtocolError.malformedFrame
            }
        } else {
            guard index < bytes.count, (0x31 ... 0x39).contains(bytes[index]) else {
                throw CursorProtocolError.malformedFrame
            }
            while index < bytes.count, (0x30 ... 0x39).contains(bytes[index]) { index += 1 }
        }
        if consumeIfPresent(0x2E) {
            guard index < bytes.count, (0x30 ... 0x39).contains(bytes[index]) else {
                throw CursorProtocolError.malformedFrame
            }
            while index < bytes.count, (0x30 ... 0x39).contains(bytes[index]) { index += 1 }
        }
        if index < bytes.count, bytes[index] == 0x65 || bytes[index] == 0x45 {
            index += 1
            if index < bytes.count, bytes[index] == 0x2B || bytes[index] == 0x2D { index += 1 }
            guard index < bytes.count, (0x30 ... 0x39).contains(bytes[index]) else {
                throw CursorProtocolError.malformedFrame
            }
            while index < bytes.count, (0x30 ... 0x39).contains(bytes[index]) { index += 1 }
        }
        let lexeme = Array(bytes[start ..< index])
        guard let value = Double(String(decoding: lexeme, as: UTF8.self)) else {
            throw CursorProtocolError.malformedFrame
        }
        return StrictCursorJSONNumber(lexeme: lexeme, doubleValue: value)
    }

    private mutating func consumeLiteral(_ literal: StaticString) throws {
        let expected = Array(String(describing: literal).utf8)
        guard index + expected.count <= bytes.count,
              Array(bytes[index ..< (index + expected.count)]) == expected
        else { throw CursorProtocolError.malformedFrame }
        index += expected.count
    }

    private mutating func consume(_ expected: UInt8) throws {
        guard consumeIfPresent(expected) else { throw CursorProtocolError.malformedFrame }
    }

    private mutating func consumeIfPresent(_ expected: UInt8) -> Bool {
        guard index < bytes.count, bytes[index] == expected else { return false }
        index += 1
        return true
    }

    private mutating func skipWhitespace() {
        while index < bytes.count, [0x20, 0x09, 0x0A, 0x0D].contains(bytes[index]) { index += 1 }
    }

    private static func isHexDigit(_ byte: UInt8) -> Bool {
        (0x30 ... 0x39).contains(byte) || (0x41 ... 0x46).contains(byte) || (0x61 ... 0x66).contains(byte)
    }
}

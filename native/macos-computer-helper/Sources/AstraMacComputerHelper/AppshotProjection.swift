@preconcurrency import ApplicationServices
import Foundation
import AstraAppshotCore

struct AppshotProjectionMetadata: Codable, Equatable {
  let coverage: AppshotCoverage
  let nodeCount: Int
  let depth: Int
  let truncated: Bool
  let truncationReasons: [String]
  enum CodingKeys: String, CodingKey {
    case coverage, depth, truncated
    case nodeCount = "node_count"
    case truncationReasons = "truncation_reasons"
  }
}
struct AppshotProjection {
  let json: Data
  let metadata: AppshotProjectionMetadata
}

/// Uses the Smart Snapshot bounded AX provider and conservative role/subrole redaction.
/// The serializer carries no action references, snapshot registry or input authority.
struct AppshotProjectionBuilder {
  static let maximumBytes = 256 * 1024
  static let maximumDepth = 64
  static func unavailable() throws -> AppshotProjection {
    let metadata = AppshotProjectionMetadata(
      coverage: .unavailable, nodeCount: 0, depth: 0,
      truncated: true, truncationReasons: ["ax_unavailable"])
    let encoder = JSONEncoder()
    encoder.outputFormatting = [.sortedKeys]
    return .init(
      json: try encoder.encode(Envelope(schemaVersion: 1, metadata: metadata, root: .object([:]))),
      metadata: metadata)
  }
  func build(root: AXUIElement, deadline: AppshotCaptureDeadline) throws -> AppshotProjection {
    let restore = try deadline.axBudget().install()
    defer { restore() }
    return try build(
      provider: SystemAXTextDetailAttributeProvider(
        element: root, remainingTime: { try? deadline.remaining() }), deadline: deadline)
  }
  func build(provider: any AXTextDetailAttributeProvider, deadline: AppshotCaptureDeadline) throws
    -> AppshotProjection
  {
    _ = try deadline.remaining()
    var state = State(deadline: deadline)
    let root = try state.visit(provider, depth: 0) ?? .object([:])
    let metadata = AppshotProjectionMetadata(
      coverage: .reportedAXSubtree, nodeCount: state.count,
      depth: state.depth, truncated: !state.reasons.isEmpty, truncationReasons: state.reasons)
    let encoder = JSONEncoder()
    encoder.outputFormatting = [.sortedKeys]
    let data = try encoder.encode(Envelope(schemaVersion: 1, metadata: metadata, root: root))
    guard data.count <= Self.maximumBytes else {
      throw AXTextDetailSerializationError.finalByteLimitExceeded
    }
    return AppshotProjection(json: data, metadata: metadata)
  }
  private struct Envelope: Encodable {
    let schemaVersion: Int
    let metadata: AppshotProjectionMetadata
    let root: JSONValue
    enum CodingKeys: String, CodingKey {
      case schemaVersion = "schema_version"
      case metadata, root
    }
  }
  private struct State {
    let deadline: AppshotCaptureDeadline
    var count = 0
    var depth = 0
    var reasons: [String] = []
    // Reserve bounded metadata/reasons plus root envelope. Each admitted node separately
    // reserves its object punctuation and the children-array key, brackets and separators.
    var bytes = AppshotProjectionBuilder.maximumBytes - 2048
    var expired = false
    mutating func reason(_ value: String) { if !reasons.contains(value) { reasons.append(value) } }
    mutating func available() throws -> Bool {
      if expired { return false }
      do {
        _ = try deadline.remaining()
        return true
      } catch AppshotCaptureError.timedOut {
        expired = true
        reason("wall_clock_limit")
        return false
      }
    }
    mutating func string(
      _ provider: any AXTextDetailAttributeProvider, _ attribute: String, limit: Int,
      reason why: String
    ) throws -> AXTextDetailStringResult {
      guard try available() else { return .init(value: nil, status: .unreadable) }
      let raw = provider.stringValue(for: attribute, maximumBytes: min(limit, max(0, bytes)))
      if raw.status == .hardFailure || raw.status == .invalid {
        throw AXTextDetailSerializationError.attributeReadFailed
      }
      guard try available() else { return .init(value: nil, status: .unreadable) }
      guard raw.status == .complete || raw.status == .truncated, let value = raw.value else {
        return .init(value: nil, status: raw.status)
      }
      let bounded = truncateUTF8WithStatus(value, maximumCharacters: Int.max, maximumBytes: limit)
      if bounded.truncated || raw.status == .truncated { reason(why) }
      return .init(value: bounded.value, status: bounded.truncated ? .truncated : raw.status)
    }
    mutating func visit(_ provider: any AXTextDetailAttributeProvider, depth level: Int) throws
      -> JSONValue?
    {
      guard count < 2000 else {
        reason("node_limit")
        return nil
      }
      guard bytes >= 128 else {
        reason("final_byte_limit")
        return nil
      }
      guard try available() else { return nil }
      let role = try string(
        provider, kAXRoleAttribute, limit: maximumAXTextDetailStructuralStringBytes,
        reason: "structural_string_limit")
      let subrole = try string(
        provider, kAXSubroleAttribute, limit: maximumAXTextDetailStructuralStringBytes,
        reason: "structural_string_limit")
      let completeRole = role.status == .complete && !(role.value?.isEmpty ?? true)
      let completeSubrole = subrole.status == .complete && !(subrole.value?.isEmpty ?? true)
      // Browser static text commonly has no optional subrole. Absence alone
      // does not make a non-editable text node a password field.
      let optionalTextSubrole = ["AXStaticText", "AXHeading", "AXLink", "AXListMarker"].contains(role.value ?? "")
        && (subrole.status == .missing || (subrole.status == .complete && (subrole.value?.isEmpty ?? true)))
      let secure =
        !completeRole || (!completeSubrole && !optionalTextSubrole) || role.value == canonicalAXUnknownIdentity
        || subrole.value == canonicalAXUnknownIdentity
        || role.value?.localizedCaseInsensitiveContains("secure") == true
        || subrole.value?.localizedCaseInsensitiveContains("secure") == true
      var values: [String: JSONValue] = [
        "role": .string(completeRole ? role.value! : canonicalAXUnknownIdentity)
      ]
      if completeSubrole { values["subrole"] = .string(subrole.value!) }
      if secure { values["redacted"] = .bool(true) }
      let encoder = JSONEncoder()
      let baseBytes = try encoder.encode(JSONValue.object(values)).count + 16
      guard baseBytes <= bytes else {
        reason("final_byte_limit")
        return nil
      }
      bytes -= baseBytes
      count += 1
      depth = max(depth, level)
      for (attribute, key, limit, why) in [
        (
          kAXDescriptionAttribute, "label", maximumAXTextDetailStructuralStringBytes,
          "structural_string_limit"
        ),
        (
          kAXTitleAttribute, "title", maximumAXTextDetailStructuralStringBytes,
          "structural_string_limit"
        ),
        (
          kAXHelpAttribute, "help", maximumAXTextDetailStructuralStringBytes,
          "structural_string_limit"
        ),
        (kAXValueAttribute, "value", maximumAXTextDetailValueBytes, "value_limit"),
      ] {
        if secure && attribute == kAXValueAttribute { continue }
        if let value = try string(provider, attribute, limit: limit, reason: why).value {
          var bounded = value
          func cost(_ text: String) throws -> Int {
            try encoder.encode(JSONValue.object([key: .string(text)])).count
          }
          if try cost(bounded) > bytes {
            reason("final_byte_limit")
            var low = 0
            var high = min(value.utf8.count, bytes)
            while low < high {
              let middle = (low + high + 1) / 2
              let prefix = truncateUTF8WithStatus(
                value, maximumCharacters: Int.max, maximumBytes: middle
              ).value
              if try cost(prefix) <= bytes { low = middle } else { high = middle - 1 }
            }
            bounded =
              truncateUTF8WithStatus(value, maximumCharacters: Int.max, maximumBytes: low).value
          }
          let used = try cost(bounded)
          if used <= bytes {
            bytes -= used
            values[key] = .string(bounded)
          }
        }
      }
      if try available() {
        let bounds = provider.bounds()
        if bounds.status == .hardFailure {
          throw AXTextDetailSerializationError.attributeReadFailed
        }
        if bounds.status == .invalid { throw AXTextDetailSerializationError.invalidGeometry }
        if try available(), bounds.status == .complete, let rect = bounds.value {
          guard rect.origin.x.isFinite, rect.origin.y.isFinite, rect.width.isFinite,
            rect.height.isFinite, rect.width >= 0, rect.height >= 0
          else { throw AXTextDetailSerializationError.invalidGeometry }
          let value = CGRectJSON.encode(rect)
          let used = try encoder.encode(JSONValue.object(["bounds": value])).count
          if used <= bytes {
            values["bounds"] = value
            bytes -= used
          } else {
            reason("final_byte_limit")
          }
        }
      }
      for (attribute, key) in [(kAXEnabledAttribute, "enabled"), (kAXFocusedAttribute, "focused")] {
        guard try available() else { break }
        let value = provider.boolValue(for: attribute)
        if value.status == .hardFailure || value.status == .invalid {
          throw AXTextDetailSerializationError.attributeReadFailed
        }
        if try available(), value.status == .complete, let flag = value.value {
          let used = key.utf8.count + 10
          if used <= bytes {
            values[key] = .bool(flag)
            bytes -= used
          } else {
            reason("final_byte_limit")
          }
        }
      }
      if try available(), bytes >= 128 {
        let maximum = level >= AppshotProjectionBuilder.maximumDepth ? 1 : 2000 - count
        let children = provider.children(maximumCount: maximum)
        guard children.status != .failed, children.values.count <= maximum else {
          throw AXTextDetailSerializationError.childrenReadFailed
        }
        if try available() {
          if level >= AppshotProjectionBuilder.maximumDepth {
            if !children.values.isEmpty || children.status == .truncated { reason("depth_limit") }
          } else {
            if children.status == .truncated { reason("node_limit") }
            var retained: [JSONValue] = []
            for child in children.values {
              guard let result = try visit(child, depth: level + 1) else { break }
              retained.append(result)
            }
            if !retained.isEmpty { values["children"] = .array(retained) }
          }
        }
      } else if bytes < 128 {
        reason("final_byte_limit")
      }
      return .object(values)
    }
  }
}

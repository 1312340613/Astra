import ApplicationServices
import Foundation
import Testing

@testable import AstraMacComputerHelperCore

final class AppshotAXFixture: AXTextDetailAttributeProvider {
  var strings: [String: AXTextDetailStringResult] = [
    kAXRoleAttribute: .init(value: "AXTextField", status: .complete),
    kAXSubroleAttribute: .init(value: "AXStandard", status: .complete),
  ]
  var descendants: [AppshotAXFixture] = []
  var reads: [String] = []
  var onRead: (() -> Void)?
  func stringValue(for attribute: String, maximumBytes: Int) -> AXTextDetailStringResult {
    reads.append(attribute)
    onRead?()
    return strings[attribute] ?? .init(value: nil, status: .missing)
  }
  func boolValue(for attribute: String) -> AXTextDetailBoolResult {
    .init(value: nil, status: .missing)
  }
  func bounds() -> AXTextDetailBoundsResult { .init(value: nil, status: .missing) }
  func children(maximumCount: Int) -> AXTextDetailChildrenResult {
    .init(
      values: Array(descendants.prefix(maximumCount)),
      status: descendants.count > maximumCount ? .truncated : .complete)
  }
}
@Suite struct AppshotProjectionTests {
  @Test func hierarchyRedactionAndNoAuthority() throws {
    let root = AppshotAXFixture()
    let secure = AppshotAXFixture()
    let unknown = AppshotAXFixture()
    secure.strings[kAXSubroleAttribute] = .init(value: "AXSecureTextField", status: .complete)
    unknown.strings[kAXRoleAttribute] = .init(value: nil, status: .unreadable)
    for node in [secure, unknown] {
      node.strings[kAXValueAttribute] = .init(value: "SECRET", status: .complete)
    }
    root.descendants = [secure, unknown]
    let projection = try AppshotProjectionBuilder().build(
      provider: root, deadline: AppshotCaptureDeadline())
    let text = String(decoding: projection.json, as: UTF8.self)
    #expect(!text.contains("SECRET"))
    #expect(!secure.reads.contains(kAXValueAttribute))
    #expect(!unknown.reads.contains(kAXValueAttribute))
    for forbidden in ["element_ref", "action_ref", "snapshot_id", "path", "input_authority"] {
      #expect(!text.contains(forbidden))
    }
    let object = try #require(JSONSerialization.jsonObject(with: projection.json) as? [String: Any])
    let children = try #require((object["root"] as? [String: Any])?["children"] as? [[String: Any]])
    #expect(children.count == 2)
    #expect(children[0]["subrole"] as? String == "AXSecureTextField")
    #expect(projection.metadata.nodeCount == 3)
    #expect(projection.metadata.coverage == .reportedAXSubtree)
  }
  @Test func unicodeBytesAndFirstObservedReasons() throws {
    let root = AppshotAXFixture()
    root.strings[kAXTitleAttribute] = .init(
      value: String(repeating: "😀", count: 2000), status: .complete)
    root.strings[kAXValueAttribute] = .init(
      value: String(repeating: "\"\n😀", count: 100000), status: .truncated)
    let result = try AppshotProjectionBuilder().build(
      provider: root, deadline: AppshotCaptureDeadline())
    #expect(result.json.count <= 256 * 1024)
    #expect(result.metadata.truncationReasons.first == "structural_string_limit")
    #expect(Set(result.metadata.truncationReasons).count == result.metadata.truncationReasons.count)
    #expect(result.metadata.truncated)
    _ = try JSONSerialization.jsonObject(with: result.json)
  }
  @Test func nodeDepthAndEncodedCaps() throws {
    let root = AppshotAXFixture()
    root.descendants = (0..<2100).map { _ in AppshotAXFixture() }
    let result = try AppshotProjectionBuilder().build(
      provider: root, deadline: AppshotCaptureDeadline())
    #expect(result.metadata.nodeCount == 2000)
    #expect(result.metadata.truncationReasons.contains("node_limit"))
    let deep = AppshotAXFixture()
    var node = deep
    for _ in 0..<70 {
      let child = AppshotAXFixture()
      node.descendants = [child]
      node = child
    }
    let depth = try AppshotProjectionBuilder().build(
      provider: deep, deadline: AppshotCaptureDeadline())
    #expect(depth.metadata.depth == 64)
    #expect(depth.metadata.nodeCount == 65)
    #expect(depth.metadata.truncationReasons == ["depth_limit"])
  }
  @Test func sharedDeadlineStopsReadsAndCannotPublishExpiredProjection() throws {
    var now = 0.0
    let deadline = AppshotCaptureDeadline(clock: { now })
    let root = AppshotAXFixture()
    root.onRead = { now = 6 }
    let result = try AppshotProjectionBuilder().build(provider: root, deadline: deadline)
    #expect(result.metadata.truncationReasons.contains("wall_clock_limit"))
    #expect(root.reads.count == 1)
    deadline.cancel()
    #expect(throws: AppshotCaptureError.cancelled) {
      try AppshotProjectionBuilder().build(provider: root, deadline: deadline)
    }
  }
  @Test func hardFailureFailsClosed() throws {
    let root = AppshotAXFixture()
    root.strings[kAXRoleAttribute] = .init(value: nil, status: .hardFailure)
    #expect(throws: AXTextDetailSerializationError.attributeReadFailed) {
      try AppshotProjectionBuilder().build(provider: root, deadline: AppshotCaptureDeadline())
    }
  }
}

extension AppshotProjectionTests {
  @Test func browserBodyBeyondOldDepthRetainsStaticTextWithMissingSubrole() throws {
    let root = AppshotAXFixture()
    var node = root
    for _ in 0..<25 {
      let child = AppshotAXFixture()
      node.descendants = [child]
      node = child
    }
    node.strings[kAXRoleAttribute] = .init(value: "AXStaticText", status: .complete)
    node.strings.removeValue(forKey: kAXSubroleAttribute)
    node.strings[kAXValueAttribute] = .init(value: "Task 2: visualization_of_word_embeddings.ipynb", status: .complete)
    let result = try AppshotProjectionBuilder().build(provider: root, deadline: .init())
    #expect(result.metadata.depth == 25)
    #expect(!result.metadata.truncated)
    #expect(String(decoding: result.json, as: UTF8.self).contains("visualization_of_word_embeddings.ipynb"))
    #expect(node.reads.contains(kAXValueAttribute))
  }

  @Test func staticTextStillRespectsExplicitSecureAndUnreadableIdentity() throws {
    for subrole in [
      AXTextDetailStringResult(value: "AXSecureTextField", status: .complete),
      .init(value: nil, status: .unreadable),
      .init(value: canonicalAXUnknownIdentity, status: .complete),
    ] {
      let node = AppshotAXFixture()
      node.strings[kAXRoleAttribute] = .init(value: "AXStaticText", status: .complete)
      node.strings[kAXSubroleAttribute] = subrole
      node.strings[kAXValueAttribute] = .init(value: "SECRET", status: .complete)
      let result = try AppshotProjectionBuilder().build(provider: node, deadline: .init())
      #expect(!node.reads.contains(kAXValueAttribute))
      #expect(!String(decoding: result.json, as: UTF8.self).contains("SECRET"))
    }
  }

  @Test func missingTruncatedAndUnknownIdentityNeverReadsValue() throws {
    for attribute in [kAXRoleAttribute, kAXSubroleAttribute] {
      for value in [
        AXTextDetailStringResult(value: nil, status: .missing),
        .init(value: "AXTextField", status: .truncated),
        .init(value: canonicalAXUnknownIdentity, status: .complete),
      ] {
        let node = AppshotAXFixture()
        node.strings[attribute] = value
        node.strings[kAXValueAttribute] = .init(value: "SECRET_NEVER_READ", status: .complete)
        let projection = try AppshotProjectionBuilder().build(provider: node, deadline: .init())
        #expect(!node.reads.contains(kAXValueAttribute))
        #expect(!String(decoding: projection.json, as: UTF8.self).contains("SECRET_NEVER_READ"))
      }
    }
  }
}

import Foundation
import Testing
@testable import AstraMacComputerHelperCore

@Test("bootstrap prunes expired segments only, retaining boundary and current buckets")
func recorderRetention() throws {
    let root = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
    defer { try? FileManager.default.removeItem(at: root) }
    let now = Date(timeIntervalSince1970: 1_788_573_600)
    let old = now.addingTimeInterval(-91 * 86400)
    let boundary = now.addingTimeInterval(-90 * 86400)
    func bucket(_ date: Date) throws -> URL {
        let dir = root.appendingPathComponent("segments/" + BucketWriter.bucketName(for: date, widthSeconds: 600))
        try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        try Data("{}".utf8).write(to: dir.appendingPathComponent("metadata.json"))
        return dir
    }
    let expired = try bucket(old)
    let recent = try bucket(boundary)
    let current = try bucket(now)
    let unrelated = root.appendingPathComponent("keep.txt")
    try Data("keep".utf8).write(to: unrelated)
    let outside = root.appendingPathComponent("outside")
    try FileManager.default.createDirectory(at: outside, withIntermediateDirectories: true)
    let link = root.appendingPathComponent("segments/2020-01-01T00-00-00Z")
    try FileManager.default.createSymbolicLink(at: link, withDestinationURL: outside)
    let writer = BucketWriter(root: root)
    try writer.bootstrap(now: now)
    try writer.bootstrap(now: now)
    #expect(!FileManager.default.fileExists(atPath: expired.path))
    for url in [recent, current, unrelated, outside, link] {
        #expect(FileManager.default.fileExists(atPath: url.path))
    }
    #expect(!FileManager.default.fileExists(atPath: outside.appendingPathComponent("metadata.json").path))
}

@Test("retention configuration defaults safely and zero days protects current bucket")
func recorderRetentionConfiguration() throws {
    #expect(BucketWriter.retentionDays(environment: [:]) == 90)
    #expect(BucketWriter.retentionDays(environment: ["ASTRA_ACTIVITY_RETENTION_DAYS": "7"]) == 7)
    for value in ["-1", "bad", "999999999999999999999"] {
        #expect(BucketWriter.retentionDays(environment: ["ASTRA_ACTIVITY_RETENTION_DAYS": value]) == 90)
    }
    let root = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
    defer { try? FileManager.default.removeItem(at: root) }
    let now = Date(timeIntervalSince1970: 1_788_573_601)
    let current = root.appendingPathComponent("segments/" + BucketWriter.bucketName(for: now, widthSeconds: 600))
    try FileManager.default.createDirectory(at: current, withIntermediateDirectories: true)
    try BucketWriter(root: root, retentionDays: 0).bootstrap(now: now)
    #expect(FileManager.default.fileExists(atPath: current.path))
}

@Test("bootstrap refuses a symlinked segments directory")
func recorderRetentionSymlinkRoot() throws {
    let root = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
    defer { try? FileManager.default.removeItem(at: root) }
    let outside = root.appendingPathComponent("outside/2020-01-01T00-00-00Z")
    try FileManager.default.createDirectory(at: outside, withIntermediateDirectories: true)
    try FileManager.default.createSymbolicLink(at: root.appendingPathComponent("segments"), withDestinationURL: outside.deletingLastPathComponent())
    try BucketWriter(root: root).bootstrap(now: Date())
    #expect(FileManager.default.fileExists(atPath: outside.path))
    #expect(!FileManager.default.fileExists(atPath: outside.appendingPathComponent("metadata.json").path))
}

@Test("custom retention expires old buckets and ignores malformed names and linked metadata")
func recorderRetentionCustomAge() throws {
    let root = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
    defer { try? FileManager.default.removeItem(at: root) }
    let now = Date(timeIntervalSince1970: 1_788_573_600)
    let segments = root.appendingPathComponent("segments")
    let old = segments.appendingPathComponent(BucketWriter.bucketName(for: now.addingTimeInterval(-8 * 86400), widthSeconds: 600))
    let recent = segments.appendingPathComponent(BucketWriter.bucketName(for: now.addingTimeInterval(-6 * 86400), widthSeconds: 600))
    let malformed = segments.appendingPathComponent("2000-01-01T00-00-00Z-not-a-bucket")
    let linked = segments.appendingPathComponent("2000-01-01T00-00-00Z")
    for directory in [old, recent, malformed, linked] {
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
    }
    let outside = root.appendingPathComponent("outside.json")
    try Data("untouched".utf8).write(to: outside)
    try FileManager.default.createSymbolicLink(at: linked.appendingPathComponent("metadata.json"), withDestinationURL: outside)
    try BucketWriter(root: root, retentionDays: 7).bootstrap(now: now)
    #expect(!FileManager.default.fileExists(atPath: old.path))
    for directory in [recent, malformed, linked] { #expect(FileManager.default.fileExists(atPath: directory.path)) }
    #expect(try String(contentsOf: outside) == "untouched")
}

import Darwin
import Foundation
import Testing

@testable import AstraMacComputerHelperCore

@Suite struct AppshotSettingsTests {
  @Test func appshotSettingsDefaultsAndChordCanonicalization() throws {
    #expect(AppshotSettings.default.enabled)
    #expect(AppshotSettings.default.shortcut.description == "Control+Shift+Z")
    #expect(
      try AppshotChord.parse("shift+option+control+k").description == "Control+Option+Shift+K")
  }

  @Test func appshotChordRequiresTwoModifiersAndRejectsReservedKeys() {
    #expect(throws: AppshotError.invalidShortcut) { try AppshotChord.parse("Control+K") }
    #expect(throws: AppshotError.invalidShortcut) { try AppshotChord.parse("Control+Shift+Escape") }
    #expect(throws: AppshotError.invalidShortcut) { try AppshotChord.parse("Control+Shift+K+Z") }
  }

  @Test func directChordConstructionRejectsUnsupportedInput() {
    #expect(throws: AppshotError.invalidShortcut) {
      _ = try AppshotChord(modifiers: [], key: "?")
    }
  }

  @Test func chordConstructionAndParsingShareASCIIInvariant() throws {
    for key: Character in ["ß", "ſ", "é", "?"] {
      #expect(throws: AppshotError.invalidShortcut) {
        _ = try AppshotChord(modifiers: [.control, .shift], key: key)
      }
      #expect(throws: AppshotError.invalidShortcut) {
        _ = try AppshotChord.parse("Control+Shift+\(key)")
      }
    }
    #expect(throws: AppshotError.invalidShortcut) {
      _ = try AppshotChord(modifiers: [.control], key: "A")
    }
    #expect(try AppshotChord(modifiers: [.control, .shift], key: "z") == .default)
  }

  @Test func rejectsAncestorSymlinks() throws {
    let root = URL(fileURLWithPath: "/private/tmp", isDirectory: true)
      .appendingPathComponent(UUID().uuidString)
    defer { try? FileManager.default.removeItem(at: root) }
    let target = root.appendingPathComponent("target")
    let link = root.appendingPathComponent("link")
    try FileManager.default.createDirectory(at: target, withIntermediateDirectories: true)
    try FileManager.default.createSymbolicLink(at: link, withDestinationURL: target)
    #expect(throws: AppshotError.insecureSettingsPath) {
      _ = try AppshotSettingsStore(directoryURL: link.appendingPathComponent("nested"))
    }
    #expect(!FileManager.default.fileExists(atPath: target.appendingPathComponent("nested").path))
  }

  @Test(arguments: [true, false]) func rejectsDirectoryReplacement(useSymlink: Bool) throws {
    let root = URL(fileURLWithPath: "/private/tmp", isDirectory: true)
      .appendingPathComponent(UUID().uuidString)
    defer { try? FileManager.default.removeItem(at: root) }
    let directory = root.appendingPathComponent("settings")
    let moved = root.appendingPathComponent("moved")
    let store = try AppshotSettingsStore(directoryURL: directory)
    try store.save(.default)
    try FileManager.default.moveItem(at: directory, to: moved)
    if useSymlink {
      try FileManager.default.createSymbolicLink(at: directory, withDestinationURL: moved)
    } else {
      try FileManager.default.createDirectory(
        at: directory, withIntermediateDirectories: false,
        attributes: [.posixPermissions: 0o700])
    }
    #expect(throws: AppshotError.insecureSettingsPath) { _ = try store.load() }
    #expect(throws: AppshotError.insecureSettingsPath) { try store.save(.default) }
  }

  @Test(arguments: ["mode", "hardlink", "directory", "substitution"])
  func validatesOpenedInode(kind: String) throws {
    let root = URL(fileURLWithPath: "/private/tmp", isDirectory: true)
      .appendingPathComponent(UUID().uuidString)
    defer { try? FileManager.default.removeItem(at: root) }
    let initial = try AppshotSettingsStore(directoryURL: root)
    try initial.save(.default)
    let other = root.appendingPathComponent("other")
    if kind == "directory" {
      try FileManager.default.createDirectory(at: other, withIntermediateDirectories: false)
    } else {
      try Data("{}".utf8).write(to: other)
      #expect(chmod(other.path, kind == "mode" ? 0o644 : 0o600) == 0)
      if kind == "hardlink" {
        #expect(link(other.path, root.appendingPathComponent("alias").path) == 0)
      }
    }
    let store = try AppshotSettingsStore(
      directoryURL: root,
      openSettingsFile: { _ in
        if kind != "substitution" {
          #expect(unlink(initial.settingsURL.path) == 0)
          #expect(rename(other.path, initial.settingsURL.path) == 0)
          return open(initial.settingsURL.path, O_RDONLY | O_NOFOLLOW | O_NONBLOCK)
        }
        return open(other.path, O_RDONLY | O_NOFOLLOW | O_NONBLOCK)
      })
    #expect(throws: AppshotError.insecureSettingsPath) { _ = try store.load() }
  }

  @Test func settingsStoreUsesPrivateModesAndPreservesCompatibleFields() throws {
    let root = URL(fileURLWithPath: "/private/tmp", isDirectory: true).appendingPathComponent(
      UUID().uuidString)
    defer { try? FileManager.default.removeItem(at: root) }
    let store = try AppshotSettingsStore(directoryURL: root)
    try store.save(AppshotSettings.default)
    let firstInode = try inode(root.appendingPathComponent("settings.json"))
    try store.save(AppshotSettings(enabled: false, shortcut: .default))
    let replacementInode = try inode(root.appendingPathComponent("settings.json"))
    let directoryMode = try mode(root)
    let fileMode = try mode(root.appendingPathComponent("settings.json"))
    #expect(directoryMode == 0o700)
    #expect(fileMode == 0o600)
    #expect(firstInode != replacementInode)

    let initial = Data(
      "{\"enabled\":false,\"shortcut\":\"Control+Option+K\",\"future\":{\"flag\":true}}".utf8)
    try initial.write(to: root.appendingPathComponent("settings.json"), options: .atomic)
    try FileManager.default.setAttributes(
      [.posixPermissions: 0o600], ofItemAtPath: root.appendingPathComponent("settings.json").path)
    let loaded = try store.load()
    #expect(loaded.warning == nil)
    #expect(loaded.settings.shortcut.description == "Control+Option+K")
    try store.save(
      AppshotSettings(
        enabled: true, shortcut: loaded.settings.shortcut, extensions: loaded.settings.extensions))
    let object = try #require(
      JSONSerialization.jsonObject(
        with: Data(contentsOf: root.appendingPathComponent("settings.json"))) as? [String: Any])
    #expect((object["future"] as? [String: Bool])?["flag"] == true)
  }

  @Test func malformedSettingsWarnWithoutRewriting() throws {
    let root = URL(fileURLWithPath: "/private/tmp", isDirectory: true).appendingPathComponent(
      UUID().uuidString)
    defer { try? FileManager.default.removeItem(at: root) }
    let store = try AppshotSettingsStore(directoryURL: root)
    let url = root.appendingPathComponent("settings.json")
    let malformed = Data("{broken".utf8)
    try malformed.write(to: url)
    try FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: url.path)
    let loaded = try store.load()
    #expect(loaded.settings == .default)
    #expect(loaded.warning == .malformedFile)
    #expect(try Data(contentsOf: url) == malformed)
  }

  @Test func rejectedSettingsPathDoesNotFollowSymlink() throws {
    let parent = URL(fileURLWithPath: "/private/tmp", isDirectory: true).appendingPathComponent(
      UUID().uuidString)
    let target = parent.appendingPathComponent("target")
    let link = parent.appendingPathComponent("settings")
    try FileManager.default.createDirectory(at: target, withIntermediateDirectories: true)
    try FileManager.default.createSymbolicLink(at: link, withDestinationURL: target)
    defer { try? FileManager.default.removeItem(at: parent) }
    #expect(throws: AppshotError.insecureSettingsPath) {
      _ = try AppshotSettingsStore(directoryURL: link)
    }
  }

}

private func mode(_ url: URL) throws -> mode_t {
  var value = stat()
  guard lstat(url.path, &value) == 0 else { throw AppshotError.settingsIO }
  return value.st_mode & 0o777
}

private func inode(_ url: URL) throws -> ino_t {
  var value = stat()
  guard lstat(url.path, &value) == 0 else { throw AppshotError.settingsIO }
  return value.st_ino
}

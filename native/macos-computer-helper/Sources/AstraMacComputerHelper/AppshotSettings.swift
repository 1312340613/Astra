import Darwin
import Foundation
import AstraAppshotCore

public enum AppshotError: Error, Equatable {
  case invalidShortcut
  case shortcutConflict
  case hotKeyRegistrationFailed
  case insecureSettingsPath
  case settingsIO
}

public enum AppshotModifier: String, Codable, CaseIterable, Hashable {
  case control = "Control"
  case option = "Option"
  case shift = "Shift"
  case command = "Command"
}

public struct AppshotChord: Codable, Equatable, Hashable, CustomStringConvertible {
  public let modifiers: Set<AppshotModifier>
  public let key: Character

  public static let `default` = AppshotChord(
    validatedModifiers: [.control, .shift],
    key: "Z"
  )

  private init(validatedModifiers: Set<AppshotModifier>, key: Character) {
    self.modifiers = validatedModifiers
    self.key = key
  }

  public init(modifiers: Set<AppshotModifier>, key: Character) throws {
    guard modifiers.count >= 2, key.isASCII, key.isLetter || key.isNumber else {
      throw AppshotError.invalidShortcut
    }
    self.init(validatedModifiers: modifiers, key: Character(String(key).uppercased()))
  }

  public static func parse(_ source: String) throws -> AppshotChord {
    let parts = source.split(separator: "+", omittingEmptySubsequences: false)
    guard parts.count >= 3 else { throw AppshotError.invalidShortcut }
    var modifiers = Set<AppshotModifier>()
    for part in parts.dropLast() {
      guard
        let modifier = AppshotModifier.allCases.first(where: {
          $0.rawValue.caseInsensitiveCompare(String(part)) == .orderedSame
        })
      else {
        throw AppshotError.invalidShortcut
      }
      guard modifiers.insert(modifier).inserted else { throw AppshotError.invalidShortcut }
    }
    let keyText = String(parts.last!)
    guard keyText.count == 1,
      let key = keyText.first,
      key.isASCII,
      key.isLetter || key.isNumber,
      modifiers.count >= 2
    else {
      throw AppshotError.invalidShortcut
    }
    return try AppshotChord(modifiers: modifiers, key: key)
  }

  public var description: String {
    let order: [AppshotModifier] = [.control, .option, .shift, .command]
    return (order.filter(modifiers.contains).map(\.rawValue) + [String(key)]).joined(separator: "+")
  }

  public init(from decoder: Decoder) throws {
    let value = try decoder.singleValueContainer().decode(String.self)
    self = try Self.parse(value)
  }

  public func encode(to encoder: Encoder) throws {
    var container = encoder.singleValueContainer()
    try container.encode(description)
  }
}

extension JSONValue: Equatable {
  static func == (lhs: JSONValue, rhs: JSONValue) -> Bool {
    switch (lhs, rhs) {
    case (.null, .null): true
    case (.bool(let left), .bool(let right)): left == right
    case (.number(let left), .number(let right)): left == right
    case (.string(let left), .string(let right)): left == right
    case (.array(let left), .array(let right)): left == right
    case (.object(let left), .object(let right)): left == right
    default: false
    }
  }
}

public struct AppshotSettings: Codable, Equatable {
  public var enabled: Bool
  public var shortcut: AppshotChord
  var extensions: [String: JSONValue]

  public static let `default` = AppshotSettings(enabled: true, shortcut: .default, extensions: [:])

  public init(enabled: Bool, shortcut: AppshotChord) {
    self.init(enabled: enabled, shortcut: shortcut, extensions: [:])
  }

  init(enabled: Bool, shortcut: AppshotChord, extensions: [String: JSONValue]) {
    self.enabled = enabled
    self.shortcut = shortcut
    self.extensions = extensions
  }

  public init(from decoder: Decoder) throws {
    let container = try decoder.container(keyedBy: DynamicCodingKey.self)
    let enabledKey = DynamicCodingKey("enabled")
    let shortcutKey = DynamicCodingKey("shortcut")
    enabled = try container.decode(Bool.self, forKey: enabledKey)
    shortcut = try container.decode(AppshotChord.self, forKey: shortcutKey)
    var unknown: [String: JSONValue] = [:]
    for key in container.allKeys where key.stringValue != "enabled" && key.stringValue != "shortcut"
    {
      unknown[key.stringValue] = try container.decode(JSONValue.self, forKey: key)
    }
    extensions = unknown
  }

  public func encode(to encoder: Encoder) throws {
    var container = encoder.container(keyedBy: DynamicCodingKey.self)
    for (name, value) in extensions where name != "enabled" && name != "shortcut" {
      try container.encode(value, forKey: DynamicCodingKey(name))
    }
    try container.encode(enabled, forKey: DynamicCodingKey("enabled"))
    try container.encode(shortcut, forKey: DynamicCodingKey("shortcut"))
  }
}

private struct DynamicCodingKey: CodingKey {
  let stringValue: String
  let intValue: Int? = nil
  init(_ value: String) { stringValue = value }
  init?(stringValue: String) { self.init(stringValue) }
  init?(intValue: Int) { return nil }
}

public enum AppshotSettingsWarning: Equatable {
  case malformedFile
}

public struct AppshotSettingsLoadResult: Equatable {
  public let settings: AppshotSettings
  public let warning: AppshotSettingsWarning?
  public init(settings: AppshotSettings, warning: AppshotSettingsWarning?) {
    self.settings = settings
    self.warning = warning
  }
}

public protocol AppshotSettingsStoring: AnyObject {
  func load() throws -> AppshotSettingsLoadResult
  func save(_ settings: AppshotSettings) throws
}

public final class AppshotSettingsStore: AppshotSettingsStoring {
  private static let maximumBytes = 64 * 1024
  public let directoryURL: URL
  public var settingsURL: URL {
    directoryURL.appendingPathComponent("settings.json", isDirectory: false)
  }

  public convenience init(fileManager: FileManager = .default) throws {
    let applicationSupport = try fileManager.url(
      for: .applicationSupportDirectory,
      in: .userDomainMask,
      appropriateFor: nil,
      create: false
    )
    try self.init(
      directoryURL: applicationSupport.appendingPathComponent("Astra/appshot", isDirectory: true))
  }

  private let directoryDescriptor: Int32
  private let openSettingsFile: (Int32) -> Int32

  public convenience init(directoryURL: URL) throws {
    try self.init(
      directoryURL: directoryURL,
      openSettingsFile: {
        openat($0, "settings.json", O_RDONLY | O_NOFOLLOW | O_NONBLOCK | O_CLOEXEC)
      })
  }

  // The injected opener lets tests exercise the opened-inode authority boundary.
  init(directoryURL: URL, openSettingsFile: @escaping (Int32) -> Int32) throws {
    self.directoryURL = directoryURL
    self.openSettingsFile = openSettingsFile
    directoryDescriptor = try Self.openDirectory(directoryURL, create: true)
  }

  deinit { close(directoryDescriptor) }

  public func load() throws -> AppshotSettingsLoadResult {
    try validateDirectoryAuthority()
    let descriptor = try openValidatedSettings()
    guard let descriptor else { return .init(settings: .default, warning: nil) }
    defer { close(descriptor) }
    var data = Data()
    var buffer = [UInt8](repeating: 0, count: 4096)
    while true {
      let count = Darwin.read(descriptor, &buffer, buffer.count)
      if count < 0, errno == EINTR { continue }
      guard count >= 0 else { throw AppshotError.settingsIO }
      if count == 0 { break }
      guard data.count + count <= Self.maximumBytes else { throw AppshotError.settingsIO }
      data.append(contentsOf: buffer.prefix(count))
    }
    try validateDirectoryAuthority()
    do {
      return .init(
        settings: try JSONDecoder().decode(AppshotSettings.self, from: data), warning: nil)
    } catch {
      return .init(settings: .default, warning: .malformedFile)
    }
  }

  public func save(_ settings: AppshotSettings) throws {
    try validateDirectoryAuthority()
    if let existing = try openValidatedSettings() { close(existing) }
    let encoder = JSONEncoder()
    encoder.outputFormatting = [.sortedKeys]
    let data: Data
    do { data = try encoder.encode(settings) } catch { throw AppshotError.settingsIO }
    guard data.count <= Self.maximumBytes else { throw AppshotError.settingsIO }

    let temporaryName = ".settings.\(UUID().uuidString).tmp"
    let descriptor = openat(
      directoryDescriptor, temporaryName, O_WRONLY | O_CREAT | O_EXCL | O_NOFOLLOW | O_CLOEXEC,
      0o600)
    guard descriptor >= 0 else { throw AppshotError.settingsIO }
    var committed = false
    defer {
      close(descriptor)
      if !committed { unlinkat(directoryDescriptor, temporaryName, 0) }
    }
    let written = data.withUnsafeBytes { buffer -> Bool in
      guard let base = buffer.baseAddress else { return data.isEmpty }
      var offset = 0
      while offset < data.count {
        let count = Darwin.write(descriptor, base.advanced(by: offset), data.count - offset)
        if count < 0, errno == EINTR { continue }
        guard count > 0 else { return false }
        offset += count
      }
      return true
    }
    guard written, fsync(descriptor) == 0 else { throw AppshotError.settingsIO }
    try validateDirectoryAuthority()
    if let existing = try openValidatedSettings() { close(existing) }
    guard renameat(directoryDescriptor, temporaryName, directoryDescriptor, "settings.json") == 0
    else {
      throw AppshotError.settingsIO
    }
    committed = true
    // Atomic publication has committed. A durability failure must never throw here:
    // callers would otherwise roll back registration despite the new file being visible.
    _ = fsync(directoryDescriptor)
  }

  private func openValidatedSettings() throws -> Int32? {
    let descriptor = openSettingsFile(directoryDescriptor)
    guard descriptor >= 0 else {
      if errno == ENOENT { return nil }
      throw AppshotError.insecureSettingsPath
    }
    do {
      var opened = stat()
      var named = stat()
      guard fstat(descriptor, &opened) == 0,
        opened.st_uid == getuid(),
        opened.st_mode & S_IFMT == S_IFREG,
        opened.st_mode & 0o7777 == 0o600,
        opened.st_nlink == 1,
        fstatat(directoryDescriptor, "settings.json", &named, AT_SYMLINK_NOFOLLOW) == 0,
        named.st_dev == opened.st_dev, named.st_ino == opened.st_ino
      else { throw AppshotError.insecureSettingsPath }
      guard opened.st_size >= 0, opened.st_size <= Self.maximumBytes else {
        throw AppshotError.settingsIO
      }
      return descriptor
    } catch {
      close(descriptor)
      throw error
    }
  }

  private func validateDirectoryAuthority() throws {
    let current = try Self.openDirectory(directoryURL, create: false)
    defer { close(current) }
    var held = stat()
    var named = stat()
    guard fstat(directoryDescriptor, &held) == 0, fstat(current, &named) == 0,
      held.st_uid == getuid(), held.st_mode & S_IFMT == S_IFDIR,
      held.st_mode & 0o7777 == 0o700,
      held.st_dev == named.st_dev, held.st_ino == named.st_ino
    else { throw AppshotError.insecureSettingsPath }
  }

  private static func openDirectory(_ url: URL, create: Bool) throws -> Int32 {
    let components = url.path.split(separator: "/").map(String.init)
    guard url.isFileURL, url.path.hasPrefix("/"), !components.isEmpty,
      !components.contains("."), !components.contains("..")
    else { throw AppshotError.insecureSettingsPath }
    var descriptor = open("/", O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC)
    guard descriptor >= 0 else { throw AppshotError.settingsIO }
    do {
      for component in components {
        var next = openat(descriptor, component, O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC)
        if next < 0, errno == ENOENT, create {
          guard mkdirat(descriptor, component, 0o700) == 0 || errno == EEXIST else {
            throw AppshotError.settingsIO
          }
          next = openat(descriptor, component, O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC)
        }
        guard next >= 0 else { throw AppshotError.insecureSettingsPath }
        close(descriptor)
        descriptor = next
      }
      var metadata = stat()
      guard fstat(descriptor, &metadata) == 0,
        metadata.st_uid == getuid(), metadata.st_mode & S_IFMT == S_IFDIR,
        metadata.st_mode & 0o7777 == 0o700
      else { throw AppshotError.insecureSettingsPath }
      return descriptor
    } catch {
      close(descriptor)
      throw error
    }
  }
}

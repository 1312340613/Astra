@testable import AstraMacComputerHelperCore
import Foundation
import Testing

@Test func approvedKeyChordMatchesCrossLanguageCanonicalVectors() throws {
    struct Vectors: Decodable { let valid: [String]; let invalid: [String] }
    let fixture = URL(fileURLWithPath: #filePath)
        .deletingLastPathComponent()
        .deletingLastPathComponent()
        .appendingPathComponent("Fixtures/key_chord_vectors.json")
    let vectors = try JSONDecoder().decode(Vectors.self, from: Data(contentsOf: fixture))

    #expect(vectors.valid.allSatisfy { ApprovedKeyChord(rawValue: $0) != nil })
    #expect(vectors.invalid.allSatisfy { ApprovedKeyChord(rawValue: $0) == nil })
}

@Test func approvedKeyChordRequiresCanonicalRegistryFormAndCanonicalizesActions() {
    #expect(ApprovedKeyChord(rawValue: "command+shift+s")?.rawValue == "command+shift+s")
    #expect(ApprovedKeyChord(rawValue: "return")?.rawValue == "return")
    #expect(ApprovedKeyChord(rawValue: "shift+command+s") == nil)
    #expect(ApprovedKeyChord(rawValue: "command+command+s") == nil)
    #expect(ApprovedKeyChord(rawValue: "Command+s") == nil)
    #expect(ApprovedKeyChord(rawValue: "command+unknown") == nil)
    #expect(ApprovedKeyChord(rawValue: "command") == nil)
    #expect(ApprovedKeyChord(rawValue: "command+v") == nil)

    #expect(ApprovedKeyChord(action: .keypress(key: "S", modifiers: ["shift", "command"]))?.rawValue == "command+shift+s")
    #expect(ApprovedKeyChord(action: .keypress(key: "enter"))?.rawValue == "return")
    #expect(ApprovedKeyChord(action: .keypress(key: "v", modifiers: ["command"])) == nil)
    #expect(ApprovedKeyChord(action: .click(x: 1, y: 1)) == nil)
}

@Test func compatibilityRegistryDecodesExactBackendAwareSchemaV2() {
    let application = PIDTargetApplication(bundleIdentifier: "com.example.editor", version: "1.2.3")
    let registry = PIDInputCompatibilityRegistry(bytes: Data(#"""
    {
        "schema_version": 2,
        "applications": [{
            "bundle_id": "com.example.editor",
            "app_version": "1.2.3",
            "capabilities": [{
                "backend": "pid_pointer",
                "enabled_actions": ["click", "scroll"]
            }, {
                "backend": "foreground_keyboard",
                "enabled_actions": ["text"],
                "allowed_key_chords": ["command+a", "command+shift+s", "return"],
                "allow_text_entry": true
            }]
        }]
    }
    """#.utf8))

    #expect(registry.allows(application: application, backend: .pidPointer, action: .click))
    #expect(registry.allows(application: application, backend: .pidPointer, action: .scroll))
    #expect(!registry.allows(application: application, backend: .pidPointer, action: .drag))
    #expect(registry.allows(application: application, backend: .foregroundKeyboard, action: .text))
    #expect(registry.allows(application: application, keyChord: ApprovedKeyChord(rawValue: "command+shift+s")!))
    #expect(registry.allowsTextEntry(application: application))
    #expect(!registry.allows(
        application: .init(bundleIdentifier: application.bundleIdentifier, version: "1.2.4"),
        backend: .pidPointer,
        action: .click
    ))
}

@Test func syntheticInputPlanningPolicyRequiresIndependentHostAndRegistryGates() {
    let application = PIDTargetApplication(bundleIdentifier: "com.example.editor", version: "1.2.3")
    let chord = ApprovedKeyChord(rawValue: "command+a")!
    let registry = PIDInputCompatibilityRegistry(cells: [
        PIDInputCompatibilityCell(
            bundleIdentifier: application.bundleIdentifier,
            version: application.version,
            backend: .pidPointer,
            action: .click,
            allowedKeyChords: [],
            allowTextEntry: false
        ),
        PIDInputCompatibilityCell(
            bundleIdentifier: application.bundleIdentifier,
            version: application.version,
            backend: .foregroundKeyboard,
            action: .text,
            allowedKeyChords: [chord],
            allowTextEntry: true
        ),
    ])

    let enabled = SyntheticInputPlanningPolicy(
        pointerCapability: .experimentalAvailable,
        keyboardCapability: .available,
        registry: registry
    )
    #expect(enabled.allows(application: nil, intents: []))
    #expect(enabled.allows(application: application, intents: [.pointer(.click), .keyChord(chord), .textEntry]))
    #expect(!enabled.allows(application: application, intents: [.pointer(.drag)]))
    let mismatchedApplication = PIDTargetApplication(
        bundleIdentifier: application.bundleIdentifier,
        version: "1.2.4"
    )
    #expect(!enabled.allows(application: mismatchedApplication, intents: [.pointer(.click)]))

    let malformedRegistry = PIDInputCompatibilityRegistry(cells: [
        PIDInputCompatibilityCell(
            bundleIdentifier: "",
            version: "",
            backend: .foregroundKeyboard,
            action: .text,
            allowedKeyChords: [chord],
            allowTextEntry: true
        ),
    ])
    let malformedApplication = PIDTargetApplication(bundleIdentifier: "", version: "")
    #expect(!malformedRegistry.allows(application: malformedApplication, keyChord: chord))
    #expect(!malformedRegistry.allowsTextEntry(application: malformedApplication))

    #expect(!SyntheticInputPlanningPolicy(
        pointerCapability: .unavailable,
        keyboardCapability: .available,
        registry: registry
    ).allows(application: application, intents: [.pointer(.click)]))
    #expect(!SyntheticInputPlanningPolicy(
        pointerCapability: .experimentalAvailable,
        keyboardCapability: .unavailable,
        registry: registry
    ).allows(application: application, intents: [.keyChord(chord)]))
}

@Test func compatibilityRegistryRejectsMalformedSchemaV2Wholly() {
    let application = PIDTargetApplication(bundleIdentifier: "com.example.editor", version: "1.2.3")
    let oversizedChords = Array("abcdefghijklmnopqrstuvwxyz0123456").map { "command+\($0)" }
    let oversizedChordPayload = try! JSONSerialization.data(withJSONObject: [
        "schema_version": 2,
        "applications": [[
            "bundle_id": application.bundleIdentifier,
            "app_version": application.version,
            "capabilities": [[
                "backend": "foreground_keyboard",
                "enabled_actions": ["text"],
                "allowed_key_chords": oversizedChords,
                "allow_text_entry": true,
            ]],
        ]],
    ])
    func pointerPayload(schemaVersion: String) -> Data {
        Data("{\"schema_version\":\(schemaVersion),\"applications\":[{\"bundle_id\":\"com.example.editor\",\"app_version\":\"1.2.3\",\"capabilities\":[{\"backend\":\"pid_pointer\",\"enabled_actions\":[\"click\"]}]}]}".utf8)
    }
    let invalidPayloads: [Data?] = [
        nil,
        pointerPayload(schemaVersion: "1"),
        pointerPayload(schemaVersion: "2.0"),
        pointerPayload(schemaVersion: "2e0"),
        pointerPayload(schemaVersion: "true"),
        Data(#"{"schema_version":2,"schema_version":2,"applications":[{"bundle_id":"com.example.editor","app_version":"1.2.3","capabilities":[{"backend":"pid_pointer","enabled_actions":["click"]}]}]}"#.utf8),
        Data(#"{"schema_version":2,"applications":[{"bundle_id":"com.example.editor","app_version":"1.2.3","capabilities":[{"backend":"pid_pointer","enabled_actions":["click"]}]}],"extra":true}"#.utf8),
        Data(#"{"schema_version":2,"applications":[{"bundle_id":"com.example.editor","app_version":"1.2.3","capabilities":[{"backend":"pid_pointer","enabled_actions":["click"]},{"backend":"pid_pointer","enabled_actions":["scroll"]}]}]}"#.utf8),
        Data(#"{"schema_version":2,"applications":[{"bundle_id":"com.example.editor","app_version":"1.2.3","capabilities":[{"backend":"unknown","enabled_actions":["click"]}]}]}"#.utf8),
        Data(#"{"schema_version":2,"applications":[{"bundle_id":"com.example.editor","app_version":"1.2.3","capabilities":[{"backend":"pid_pointer","enabled_actions":["text"]}]}]}"#.utf8),
        Data(#"{"schema_version":2,"applications":[{"bundle_id":"com.example.editor","app_version":"1.2.3","capabilities":[{"backend":"foreground_keyboard","enabled_actions":["text"],"allowed_key_chords":["shift+command+s"],"allow_text_entry":true}]}]}"#.utf8),
        Data(#"{"schema_version":2,"applications":[{"bundle_id":"com.example.editor","app_version":"1.2.3","capabilities":[{"backend":"foreground_keyboard","enabled_actions":["text"],"allowed_key_chords":["return","return"],"allow_text_entry":true}]}]}"#.utf8),
        Data(#"{"schema_version":2,"applications":[{"bundle_id":"com.example.editor","app_version":"1.2.3","capabilities":[{"backend":"foreground_keyboard","enabled_actions":["text"],"allowed_key_chords":["command+v"],"allow_text_entry":false}]}]}"#.utf8),
        Data(#"{"schema_version":2,"applications":[{"bundle_id":"com.example.editor","app_version":"1.2.3","capabilities":[{"backend":"foreground_keyboard","enabled_actions":["text"],"allowed_key_chords":["return"],"allow_text_entry":1}]}]}"#.utf8),
        Data(#"{"schema_version":2,"applications":[{"bundle_id":"com.example.editor","app_version":"1.2.3","capabilities":[{"backend":"pid_pointer","enabled_actions":["click"]}]},{"bundle_id":"com.example.editor","app_version":"1.2.3","capabilities":[{"backend":"pid_pointer","enabled_actions":["scroll"]}]}]}"#.utf8),
        Data(#"{"schema_version":2,"applications":[{"bundle_id":"com.example.editor","app_version":"1.2.3","capabilities":[{"backend":"pid_pointer","enabled_actions":["click"]},{"backend":"foreground_keyboard","enabled_actions":["text"],"allowed_key_chords":[],"allow_text_entry":true},{"backend":"pid_pointer","enabled_actions":["scroll"]}]}]}"#.utf8),
        Data("{\"schema_version\":2,\"applications\":[{\"bundle_id\":\"com.example.editor\",\"app_version\":\"1.2.3\",\"capabilities\":[{\"backend\":\"foreground_keyboard\",\"enabled_actions\":[\"text\"],\"allowed_key_chords\":[\"\(String(repeating: "a", count: 129))\"],\"allow_text_entry\":false}]}]}".utf8),
        oversizedChordPayload,
        Data(repeating: 0x20, count: 64 * 1024 + 1),
    ]

    for payload in invalidPayloads {
        let rejected = PIDInputCompatibilityRegistry(bytes: payload)
        #expect(!rejected.allows(application: application, backend: .pidPointer, action: .click))
        #expect(!rejected.allowsTextEntry(application: application))
    }
}

@Test func compatibilityRegistryRejectsBOMAndNonUTF8Encodings() {
    let application = PIDTargetApplication(bundleIdentifier: "com.example.editor", version: "1.2.3")
    let document = #"{"schema_version":2,"applications":[{"bundle_id":"com.example.editor","app_version":"1.2.3","capabilities":[{"backend":"pid_pointer","enabled_actions":["click"]}]}]}"#
    var utf8BOM = Data([0xEF, 0xBB, 0xBF])
    utf8BOM.append(Data(document.utf8))
    let invalidPayloads = [
        utf8BOM,
        document.data(using: .utf16LittleEndian)!,
        document.data(using: .utf16BigEndian)!,
        document.data(using: .utf32LittleEndian)!,
        document.data(using: .utf32BigEndian)!,
    ]

    for payload in invalidPayloads {
        let registry = PIDInputCompatibilityRegistry(bytes: payload)
        #expect(!registry.allows(application: application, backend: .pidPointer, action: .click))
    }
}

@Test func pidKeyboardWPSProductionCellDecodesAndAllowsThroughFullChain() {
    let application = PIDTargetApplication(bundleIdentifier: "com.kingsoft.wpsoffice.mac", version: "12.1.26026")
    let bytes = Data(#"""
    {
        "schema_version": 2,
        "applications": [{
            "bundle_id": "com.kingsoft.wpsoffice.mac",
            "app_version": "12.1.26026",
            "capabilities": [{
                "backend": "pid_keyboard",
                "enabled_actions": ["text"],
                "allowed_key_chords": ["return"],
                "allow_text_entry": true
            }]
        }]
    }
    """#.utf8)
    let registry = PIDInputCompatibilityRegistry(bytes: bytes)

    #expect(registry.allows(application: application, backend: .pidKeyboard, action: .text))
    #expect(registry.allows(application: application, keyChord: ApprovedKeyChord(rawValue: "return")!))

    let policy = SyntheticInputPlanningPolicy(
        pointerCapability: .experimentalAvailable,
        keyboardCapability: .available,
        registry: registry
    )
    #expect(policy.allows(application: application, intents: [.keyChord(ApprovedKeyChord(rawValue: "return")!)]))
}

@Test func compatibilityRegistryDecodesRequiresActivePointerCell() {
    let application = PIDTargetApplication(bundleIdentifier: "com.example.active", version: "1.0.0")
    let registry = PIDInputCompatibilityRegistry(bytes: Data(#"""
    {
        "schema_version": 2,
        "applications": [{
            "bundle_id": "com.example.active",
            "app_version": "1.0.0",
            "capabilities": [{
                "backend": "pid_pointer",
                "enabled_actions": ["click"],
                "requires_active": true
            }]
        }]
    }
    """#.utf8))

    #expect(registry.pointerRequiresActive(bundleIdentifier: "com.example.active", version: "1.0.0"))
    #expect(!registry.pointerRequiresActive(bundleIdentifier: "com.example.active", version: "9.9.9"))
    #expect(!registry.pointerRequiresActive(bundleIdentifier: "com.example.other", version: "1.0.0"))
    #expect(registry.allows(application: application, action: .click))
}

@Test func compatibilityRegistryDefaultsRequiresActiveFalse() {
    let registry = PIDInputCompatibilityRegistry(bytes: Data(#"""
    {
        "schema_version": 2,
        "applications": [{
            "bundle_id": "com.example.passive",
            "app_version": "1.0.0",
            "capabilities": [{
                "backend": "pid_pointer",
                "enabled_actions": ["click", "scroll"]
            }]
        }]
    }
    """#.utf8))

    #expect(!registry.pointerRequiresActive(bundleIdentifier: "com.example.passive", version: "1.0.0"))
    #expect(registry.allows(
        application: .init(bundleIdentifier: "com.example.passive", version: "1.0.0"),
        action: .click
    ))
}

@Test func requiresActiveCellDisablesBackgroundDelivery() {
    let application = PIDTargetApplication(bundleIdentifier: "com.example.editor", version: "1.2.3")
    let activeRegistry = PIDInputCompatibilityRegistry(cells: [
        PIDInputCompatibilityCell(
            bundleIdentifier: application.bundleIdentifier,
            version: application.version,
            backend: .pidPointer,
            action: DispatchActionClass.click,
            requiresActive: true
        ),
    ])
    let activePolicy = SyntheticInputPlanningPolicy(
        pointerCapability: .experimentalAvailable,
        keyboardCapability: .available,
        registry: activeRegistry,
        backgroundDeliveryEnabled: true
    )
    #expect(!activePolicy.allowsBackgroundDelivery(
        application: application,
        backend: .pidPointer,
        action: .click
    ))
    #expect(activePolicy.pointerRequiresActive(application: application))

    let passiveRegistry = PIDInputCompatibilityRegistry(cells: [
        PIDInputCompatibilityCell(
            bundleIdentifier: application.bundleIdentifier,
            version: application.version,
            backend: .pidPointer,
            action: DispatchActionClass.click
        ),
    ])
    let passivePolicy = SyntheticInputPlanningPolicy(
        pointerCapability: .experimentalAvailable,
        keyboardCapability: .available,
        registry: passiveRegistry,
        backgroundDeliveryEnabled: true
    )
    #expect(passivePolicy.allowsBackgroundDelivery(
        application: application,
        backend: .pidPointer,
        action: .click
    ))
    #expect(!passivePolicy.pointerRequiresActive(application: application))
}

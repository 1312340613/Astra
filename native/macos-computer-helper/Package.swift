// swift-tools-version: 5.9
import Foundation
import PackageDescription

let commandLineToolsLibraries = "/Library/Developer/CommandLineTools/Library/Developer/usr/lib"
let testingLinkerSettings: [LinkerSetting] = FileManager.default.fileExists(
    atPath: "\(commandLineToolsLibraries)/lib_TestingInterop.dylib"
) ? [.unsafeFlags([
    "-L", commandLineToolsLibraries,
    "-Xlinker", "-rpath", "-Xlinker", commandLineToolsLibraries,
])] : []
let includeHelperHarness = ProcessInfo.processInfo.environment["ASTRA_MACOS_COMPUTER_HELPER_HARNESS"] == "1"
let includeE2EHarness = ProcessInfo.processInfo.environment["ASTRA_MACOS_COMPUTER_E2E"] == "1"

var targets: [Target] = [
    .target(
        name: "AstraMacComputerHelperCore",
        dependencies: [
            "AstraVirtualCursorProtocol",
            .product(name: "AstraAppshotCore", package: "appshot-core"),
        ],
        path: "Sources/AstraMacComputerHelper"
    ),
    .target(name: "AstraVirtualCursorProtocol"),
    .executableTarget(
        name: "AstraVirtualCursorSidecar",
        dependencies: ["AstraVirtualCursorProtocol"]
    ),
    .executableTarget(
        name: "AstraMacComputerHelper",
        dependencies: ["AstraMacComputerHelperCore"],
        path: "Sources/AstraMacComputerHelperExecutable"
    ),
    .executableTarget(
        name: "AstraActivityRecorder",
        dependencies: ["AstraMacComputerHelperCore"],
        path: "Sources/AstraActivityRecorder"
    ),
    .executableTarget(
        name: "AstraComputerFixture"
    ),
    .executableTarget(
        name: "AstraComputerProtocolFixture"
    ),
    .testTarget(
        name: "AstraMacComputerSwiftTests",
        dependencies: [
            "AstraMacComputerHelperCore",
            "AstraVirtualCursorProtocol",
            .product(name: "Testing", package: "swift-testing"),
        ],
        path: "Tests/AstraMacComputerHelperTests",
        linkerSettings: testingLinkerSettings
    ),
]

if includeHelperHarness {
    targets.append(.executableTarget(
        name: "AstraMacComputerHelperHarness",
        dependencies: ["AstraMacComputerHelperCore"],
        path: "Tests/AstraMacComputerHelperHarness"
    ))
}

if includeE2EHarness {
    targets.append(.executableTarget(
        name: "AstraMacComputerE2EHarness",
        dependencies: ["AstraMacComputerHelperCore"],
        path: "Tests/AstraMacComputerE2EHarness"
    ))
}

let package = Package(
    name: "AstraMacComputerHelper",
    platforms: [.macOS(.v14)],
    dependencies: [
        .package(path: "../appshot-core"),
        .package(
            url: "https://github.com/swiftlang/swift-testing.git",
            exact: "6.3.2"
        ),
    ],
    targets: targets
)

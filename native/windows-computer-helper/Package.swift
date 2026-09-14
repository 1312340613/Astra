// swift-tools-version: 5.9
import PackageDescription

let package = Package(
    name: "AstraWindowsComputerHelper",
    products: [.executable(name: "AstraWindowsComputerHelper", targets: ["AstraWindowsComputerHelper"]),
        .library(name: "AstraWindowsNative", type: .dynamic, targets: ["AstraWindowsNative"])],
    dependencies: [.package(path: "../appshot-core")],
    targets: [
        .target(name: "AstraWindowsNative", publicHeadersPath: "include",
            cxxSettings: [.define("NOMINMAX"), .define("WIN32_LEAN_AND_MEAN")],
            linkerSettings: [.linkedLibrary("user32"), .linkedLibrary("advapi32"),
                .linkedLibrary("ole32"), .linkedLibrary("oleaut32"), .linkedLibrary("d3d11"),
                .linkedLibrary("windowsapp"), .linkedLibrary("windowscodecs"), .linkedLibrary("dwmapi"),
                .linkedLibrary("bcrypt"), .linkedLibrary("swiftCore")]),
        .executableTarget(name: "AstraWindowsComputerHelper",
            dependencies: ["AstraWindowsNative", .product(name: "AstraAppshotCore", package: "appshot-core")]),
    ],
    cxxLanguageStandard: .cxx20
)

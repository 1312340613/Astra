// swift-tools-version: 5.9
import PackageDescription

let package = Package(
    name: "AstraAppshotCore",
    platforms: [.macOS(.v14)],
    products: [.library(name: "AstraAppshotCore", targets: ["AstraAppshotCore"])],
    targets: [
        .target(name: "AstraAppshotCore"),
        .testTarget(name: "AstraAppshotCoreTests", dependencies: ["AstraAppshotCore"]),
    ]
)

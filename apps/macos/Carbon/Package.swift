// swift-tools-version: 6.0
import PackageDescription

let package = Package(
    name: "Carbon",
    platforms: [
        .macOS(.v14)
    ],
    products: [
        .executable(
            name: "Carbon",
            targets: ["Carbon"]
        )
    ],
    targets: [
        .executableTarget(
            name: "Carbon",
            path: "Sources"
        )
    ]
)

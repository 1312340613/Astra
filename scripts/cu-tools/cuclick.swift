// cuclick — 持久化 CGEvent 点击工具 (left/right/double)
// 编译: swiftc -O cuclick.swift -o cuclick
// 用法: cuclick <x> <y> [left|right|double] [-f <app名>]
//   -f <app名>  点击前激活该 app (进程名/localizedName 匹配; 解决"第一击只激活、焦点不进控件"的坑, v2 新增)
import AppKit
import CoreGraphics
import Foundation

var args = CommandLine.arguments

// 提取 -f <app>
var activateApp: String?
if let i = args.firstIndex(of: "-f"), i + 1 < args.count {
    activateApp = args[i + 1]
    args.removeSubrange(i...(i + 1))
}

guard args.count >= 3, let x = Double(args[1]), let y = Double(args[2]) else {
    print("usage: cuclick x y [left|right|double] [-f app]"); exit(1)
}
let mode = args.count >= 4 ? args[3] : "left"

// 点击前激活目标 app (不抢焦点则跳过)
if let app = activateApp {
    let running = NSWorkspace.shared.runningApplications
    if let proc = running.first(where: {
        $0.localizedName == app || $0.executableURL?.lastPathComponent == app || $0.bundleIdentifier == app
    }) {
        if #available(macOS 14.0, *) {
            proc.activate()
        } else {
            proc.activate(options: [.activateIgnoringOtherApps])
        }
        usleep(350_000)
    } else {
        fputs("warning: app not found: \(app)\n", stderr)
    }
}
let point = CGPoint(x: x, y: y)
let source = CGEventSource(stateID: .hidSystemState)

let downType: CGEventType = mode == "right" ? .rightMouseDown : .leftMouseDown
let upType: CGEventType = mode == "right" ? .rightMouseUp : .leftMouseUp
let button: CGMouseButton = mode == "right" ? .right : .left

func postClick() -> Bool {
    guard let down = CGEvent(mouseEventSource: source, mouseType: downType,
                             mouseCursorPosition: point, mouseButton: button),
          let up = CGEvent(mouseEventSource: source, mouseType: upType,
                           mouseCursorPosition: point, mouseButton: button) else {
        return false
    }
    down.post(tap: .cghidEventTap)
    usleep(60000)
    up.post(tap: .cghidEventTap)
    return true
}

if mode == "double" {
    for _ in 0..<2 {
        guard postClick() else { print("failed"); exit(1) }
        usleep(80000)
    }
} else {
    guard postClick() else { print("failed"); exit(1) }
}
print("\(mode)clicked \(Int(x)),\(Int(y))")

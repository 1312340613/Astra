// cuwin — 查询 macOS 窗口 bounds (通用, 无副作用: 不激活/不抢前台)
// v2 (2026-09-02): 重写。旧版会 set frontmost 抢前台, 查询工具不应有副作用。
// 用法: cuwin -l            列出全部屏幕窗口 (z 序 前->后): "owner<TAB>title<TAB>x,y,w,h"
//       cuwin <关键字>       匹配 owner 名/标题的车口:    "owner<TAB>title<TAB>x,y,w,h"
//       cuwin <关键字> --first  只输出第一个匹配的行 (便于赋值)
// 坐标 = 全局逻辑坐标 (与 cuclick/cushot/cuocr 一比一)
// 编译: swiftc -O cuwin.swift -o cuwin
import CoreGraphics
import Foundation

let args = CommandLine.arguments
let listMode = args.contains("-l") || args.contains("--list")
let firstOnly = args.contains("--first")

func boundsString(_ d: [String: Any]) -> String {
    guard let b = d[kCGWindowBounds as String] as? [String: Any],
          let x = b["X"] as? NSNumber,
          let y = b["Y"] as? NSNumber,
          let w = b["Width"] as? NSNumber,
          let h = b["Height"] as? NSNumber
    else { return "?" }
    return "\(x.intValue),\(y.intValue),\(w.intValue),\(h.intValue)"
}

let opts: CGWindowListOption = [.optionOnScreenOnly, .excludeDesktopElements]
guard let info = CGWindowListCopyWindowInfo(opts, kCGNullWindowID) as? [[String: Any]] else {
    print("can not list windows"); exit(1)
}

var lines: [String] = []
for d in info {
    guard let layer = d[kCGWindowLayer as String] as? Int, layer == 0 else { continue }
    guard let owner = d[kCGWindowOwnerName as String] as? String, !owner.isEmpty else { continue }
    let title = d[kCGWindowName as String] as? String ?? ""
    let bounds = boundsString(d)
    lines.append("\(owner)\t\(title)\t\(bounds)")
}

if listMode {
    for l in lines { print(l) }
} else {
    // args[0] 是程序名, 查询关键字从 dropFirst 找
    let query = args.dropFirst().first { !$0.hasPrefix("-") } ?? ""
    if query.isEmpty {
        print("usage: cuwin -l | cuwin <关键字> [--first]")
        exit(2)
    }
    var matched: [String] = []
    for l in lines {
        let parts = l.split(separator: "\t", maxSplits: 2).map(String.init)
        let hay = (parts.first ?? "") + " " + (parts.count > 1 ? parts[1] : "")
        if hay.localizedCaseInsensitiveContains(query) { matched.append(l) }
    }
    if firstOnly {
        if let m = matched.first { print(m) } else { exit(1) }
    } else {
        for m in matched { print(m) }
        if matched.isEmpty { exit(1) }
    }
}

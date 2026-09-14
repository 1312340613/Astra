// cuclip — 把文件引用或文本放入通用剪贴板 (v2 新增)
// 解决: osascript `set clipboard to (POSIX file ... as alias)` 产生的类型
//       微信等应用不识别; Finder ⌘C 又需要激活 Finder 兜圈。
// 用法: cuclip <path>        文件: 写 NSFilenamesPboardType(路径数组) + public.file-url
//       cuclip -t <text>     文本
// 输出: ok 摘要 或 FAILED (exit 1)
// 编译: swiftc -O cuclip.swift -o cuclip
import AppKit
import Foundation

let args = CommandLine.arguments
guard args.count >= 2 else {
    print("usage: cuclip <path> | cuclip -t <text>")
    exit(2)
}

let pb = NSPasteboard.general
pb.clearContents()

// ---- 文本模式: cuclip -t <text> ----
if args[1] == "-t" || args[1] == "--text" {
    guard args.count >= 3 else { print("usage: cuclip -t <text>"); exit(2) }
    let text = args[2...].joined(separator: " ")
    let ok = pb.setString(text, forType: .string)
    print(ok ? "ok text \(text.count) chars -> clipboard" : "FAILED")
    exit(ok ? 0 : 1)
}

// ---- 文件模式: cuclip <path> ----
let path = args[1]
let fm = FileManager.default
var isDir: ObjCBool = false
guard fm.fileExists(atPath: path, isDirectory: &isDir), !isDir.boolValue else {
    print("not a regular file: \(path) (文件夹请用 -t 不行; 本工具仅文件)")
    exit(1)
}
let url = URL(fileURLWithPath: path)

let item = NSPasteboardItem()
// public.file-url — 标准文件引用(微信 4.x 实测识别, 2026-09-02 端到端验证)
// 注意: 不要写 "NSFilenamesPboardType"(非标准 UTI, macOS 10.13+ 拒绝并告警无效果)
item.setString(url.absoluteString, forType: .fileURL)

let ok = pb.writeObjects([item])
if !ok {
    print("FAILED writing pasteboard"); exit(1)
}
let size = ((try? fm.attributesOfItem(atPath: path))?[.size] as? Int) ?? 0
print("ok file \(path) (\(size) bytes) -> clipboard")

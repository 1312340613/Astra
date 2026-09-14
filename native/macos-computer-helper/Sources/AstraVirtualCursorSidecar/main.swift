import AppKit
import AstraVirtualCursorProtocol
import Darwin
import Foundation

private final class CursorSidecarDelegate: NSObject, NSApplicationDelegate {
    private var panel: CursorPanel?
    private var readerThread: Thread?
    private var session: CursorProtocolSession?

    func applicationDidFinishLaunching(_: Notification) {
        let panel = CursorPanel()
        self.panel = panel
        session = CursorProtocolSession(renderer: panel)
        guard panel.windowNumber > 0,
              let windowID = UInt32(exactly: panel.windowNumber)
        else {
            terminate()
            return
        }
        let hello = CursorSidecarHello(windowID: windowID, panel: panel.safetyProperties)
        guard hello.isSafeForCooperativePresentation,
              let frame = try? CursorSidecarHelloCodec.encode(hello),
              writeFrame(frame)
        else {
            terminate()
            return
        }
        startReader()
    }

    func applicationWillTerminate(_: Notification) { cleanup() }

    private func startReader() {
        let thread = Thread { [weak self] in
            var frame = Data()
            var oversized = false
            var byte: UInt8 = 0
            while true {
                let count = Darwin.read(STDIN_FILENO, &byte, 1)
                if count < 0, errno == EINTR { continue }
                if count <= 0 {
                    DispatchQueue.main.async { self?.finishEOF() }
                    return
                }
                if byte == 0x0A {
                    let line = frame
                    let invalid = oversized
                    frame.removeAll(keepingCapacity: true)
                    oversized = false
                    let keepRunning = DispatchQueue.main.sync { [weak self] in
                        self?.consume(line, oversized: invalid) ?? false
                    }
                    if !keepRunning { return }
                } else if !oversized {
                    if frame.count == maximumCursorMessageBytes {
                        frame.removeAll(keepingCapacity: false)
                        oversized = true
                    } else {
                        frame.append(byte)
                    }
                }
            }
        }
        thread.name = "astra.virtual-cursor-reader"
        readerThread = thread
        thread.start()
    }

    private func consume(_ frame: Data, oversized: Bool) -> Bool {
        guard !oversized, let session else {
            terminate()
            return false
        }
        guard session.consume(frame) == .continue else {
            terminate()
            return false
        }
        return true
    }

    private func finishEOF() {
        _ = session?.endOfFile()
        terminate()
    }

    private func cleanup() {
        _ = session?.endOfFile()
        session = nil
        panel = nil
    }

    private func terminate() {
        cleanup()
        NSApplication.shared.terminate(nil)
    }

    private func writeFrame(_ frame: Data) -> Bool {
        let bytes = frame + Data([0x0A])
        return bytes.withUnsafeBytes { raw in
            guard let base = raw.baseAddress else { return false }
            var sent = 0
            while sent < raw.count {
                let count = Darwin.write(STDOUT_FILENO, base.advanced(by: sent), raw.count - sent)
                if count > 0 { sent += count; continue }
                if count < 0, errno == EINTR { continue }
                return false
            }
            return true
        }
    }
}

signal(SIGPIPE, SIG_IGN)
let application = NSApplication.shared
private let delegate = CursorSidecarDelegate()
application.delegate = delegate
_ = application.setActivationPolicy(.accessory)
application.run()

import AppKit
import AstraVirtualCursorProtocol
import CoreGraphics

final class CursorPanel: NSPanel, CursorRendering {
    private static let cursorSize = CGSize(width: 28, height: 28)
    private let cursorView = CursorGlyphView(frame: CGRect(origin: .zero, size: cursorSize))

    init() {
        super.init(
            contentRect: CGRect(origin: .zero, size: Self.cursorSize),
            styleMask: [.borderless, .nonactivatingPanel],
            backing: .buffered,
            defer: false
        )
        contentView = cursorView
        backgroundColor = .clear
        isOpaque = false
        hasShadow = false
        level = .floating
        ignoresMouseEvents = true
        hidesOnDeactivate = false
        collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary, .stationary, .ignoresCycle]
        isReleasedWhenClosed = false
    }

    override var canBecomeKey: Bool { false }
    override var canBecomeMain: Bool { false }

    var safetyProperties: CursorPanelProperties {
        let decorations = styleMask.subtracting(.nonactivatingPanel)
        return CursorPanelProperties(
            borderlessPanel: decorations.isEmpty,
            transparent: !isOpaque && backgroundColor.alphaComponent == 0 && contentView?.isOpaque == false,
            nonactivatingPanel: styleMask.contains(.nonactivatingPanel),
            ignoresMouseEvents: ignoresMouseEvents,
            canBecomeKey: canBecomeKey,
            canBecomeMain: canBecomeMain
        )
    }

    func show(at point: CGPoint) throws {
        try CursorMessageCodec.validate(point)
        setCursorPosition(point)
        cursorView.clicking = false
        orderFrontRegardless()
    }

    func move(to point: CGPoint) throws {
        try CursorMessageCodec.validate(point)
        setCursorPosition(point)
    }

    func click(at point: CGPoint) throws {
        try CursorMessageCodec.validate(point)
        setCursorPosition(point)
        cursorView.clicking = true
        orderFrontRegardless()
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.12) { [weak cursorView] in
            cursorView?.clicking = false
        }
    }

    func hide() { orderOut(nil) }

    func remove() {
        orderOut(nil)
        contentView = nil
        close()
    }

    private func setCursorPosition(_ point: CGPoint) {
        let top = NSScreen.screens.map(\.frame.maxY).max() ?? 0
        let appKitPoint = CGPoint(x: point.x, y: top - point.y)
        setFrameOrigin(CGPoint(
            x: appKitPoint.x - Self.cursorSize.width / 2,
            y: appKitPoint.y - Self.cursorSize.height / 2
        ))
    }
}

private final class CursorGlyphView: NSView {
    var clicking = false { didSet { needsDisplay = true } }

    override var isOpaque: Bool { false }

    override func draw(_ dirtyRect: NSRect) {
        super.draw(dirtyRect)
        let inset: CGFloat = clicking ? 5 : 3
        let circle = bounds.insetBy(dx: inset, dy: inset)
        NSColor(calibratedWhite: 0.78, alpha: clicking ? 0.72 : 0.48).setFill()
        NSBezierPath(ovalIn: circle).fill()
        NSColor(calibratedWhite: 1, alpha: 0.86).setStroke()
        let outline = NSBezierPath(ovalIn: circle)
        outline.lineWidth = 1.5
        outline.stroke()
    }
}

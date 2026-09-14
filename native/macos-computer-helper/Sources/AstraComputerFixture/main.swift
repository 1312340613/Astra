import AppKit

class StateMarker: NSTextField {
    func mark(_ value: String) { stringValue = value }
}

final class CounterMarker: StateMarker {
    private(set) var count = 0

    init(identifier: String) {
        super.init(frame: .zero)
        isEditable = false
        isBordered = false
        drawsBackground = false
        setAccessibilityIdentifier(identifier)
        update()
    }

    required init?(coder: NSCoder) { nil }

    func increment() {
        count += 1
        update()
    }

    private func update() { stringValue = "\(accessibilityIdentifier()):\(count)" }
}

final class ClickSurface: NSView {
    let counter: CounterMarker

    init(counter: CounterMarker) {
        self.counter = counter
        super.init(frame: .zero)
        setAccessibilityIdentifier("astra.double_click")
        setAccessibilityLabel("Double click surface")
        setAccessibilityRole(.button)
        setAccessibilitySubrole(NSAccessibility.Subrole(rawValue: "AXStandardButton"))
        wantsLayer = true
        layer?.backgroundColor = NSColor.systemOrange.withAlphaComponent(0.2).cgColor
    }

    required init?(coder: NSCoder) { nil }
    override func isAccessibilityElement() -> Bool { true }
    override func mouseDown(with event: NSEvent) {
        if event.clickCount == 2 { counter.increment() }
    }
}

final class CanvasSurface: NSView {
    let counter: CounterMarker

    init(counter: CounterMarker) {
        self.counter = counter
        super.init(frame: .zero)
        setAccessibilityIdentifier("astra.canvas")
        setAccessibilityLabel("Custom canvas")
        setAccessibilityRole(.button)
        setAccessibilitySubrole(NSAccessibility.Subrole(rawValue: "AXStandardButton"))
        wantsLayer = true
        layer?.backgroundColor = NSColor.systemPurple.withAlphaComponent(0.2).cgColor
    }

    required init?(coder: NSCoder) { nil }
    override func isAccessibilityElement() -> Bool { true }
    override func mouseDown(with _: NSEvent) { counter.increment() }
}

final class DragSurface: NSView {
    let marker: StateMarker
    let counter: CounterMarker

    init(marker: StateMarker, counter: CounterMarker) {
        self.marker = marker
        self.counter = counter
        super.init(frame: .zero)
        setAccessibilityIdentifier("astra.drag")
        setAccessibilityLabel("Drag surface")
        setAccessibilityRole(.button)
        setAccessibilitySubrole(NSAccessibility.Subrole(rawValue: "AXStandardButton"))
        wantsLayer = true
        layer?.backgroundColor = NSColor.systemBlue.withAlphaComponent(0.2).cgColor
    }

    required init?(coder: NSCoder) { nil }
    override func isAccessibilityElement() -> Bool { true }
    override func mouseDown(with _: NSEvent) { marker.mark("drag:start") }
    override func mouseDragged(with event: NSEvent) { marker.mark("drag:\(Int(event.locationInWindow.x)),\(Int(event.locationInWindow.y))") }
    override func mouseUp(with _: NSEvent) {
        counter.increment()
        marker.mark("drag:complete")
    }
}

final class FixtureScrollView: NSScrollView {
    let marker: StateMarker
    let counter: CounterMarker
    init(marker: StateMarker, counter: CounterMarker) {
        self.marker = marker
        self.counter = counter
        super.init(frame: .zero)
    }
    required init?(coder: NSCoder) { nil }
    override func scrollWheel(with event: NSEvent) {
        super.scrollWheel(with: event)
        counter.increment()
        marker.mark("scroll:\(Int(event.scrollingDeltaX)),\(Int(event.scrollingDeltaY))")
    }
}

final class FixtureDelegate: NSObject, NSApplicationDelegate, NSTextFieldDelegate {
    private var window: NSWindow!
    private let marker = StateMarker(labelWithString: "ready")
    private let clickCounter = CounterMarker(identifier: "astra.click_count")
    private let doubleClickCounter = CounterMarker(identifier: "astra.double_click_count")
    private let scrollCounter = CounterMarker(identifier: "astra.scroll_count")
    private let dragCounter = CounterMarker(identifier: "astra.drag_count")
    private let canvasCounter = CounterMarker(identifier: "astra.canvas_count")

    func applicationDidFinishLaunching(_: Notification) {
        window = NSWindow(
            contentRect: NSRect(x: 100, y: 100, width: 640, height: 680),
            styleMask: [.titled, .closable, .miniaturizable, .resizable],
            backing: .buffered,
            defer: false
        )
        window.title = "Astra Computer Fixture"
        window.setAccessibilityIdentifier("astra.window")

        let content = NSView()
        let stack = NSStackView()
        stack.orientation = .vertical
        stack.alignment = .leading
        stack.spacing = 12
        stack.translatesAutoresizingMaskIntoConstraints = false
        content.addSubview(stack)
        window.contentView = content
        NSLayoutConstraint.activate([
            stack.leadingAnchor.constraint(equalTo: content.leadingAnchor, constant: 20),
            stack.trailingAnchor.constraint(equalTo: content.trailingAnchor, constant: -20),
            stack.topAnchor.constraint(equalTo: content.topAnchor, constant: 20),
            stack.bottomAnchor.constraint(equalTo: content.bottomAnchor, constant: -20),
        ])

        marker.setAccessibilityIdentifier("astra.state")
        marker.font = .monospacedSystemFont(ofSize: 16, weight: .semibold)
        stack.addArrangedSubview(marker)
        let appStateMarker = NSTextField(labelWithString: "app-state:primary")
        appStateMarker.setAccessibilityIdentifier("astra.app_state_marker")
        appStateMarker.setAccessibilityLabel("App state transaction marker")
        stack.addArrangedSubview(appStateMarker)
        let counters = NSStackView(views: [clickCounter, doubleClickCounter, scrollCounter, dragCounter, canvasCounter])
        counters.orientation = .horizontal
        counters.spacing = 8
        stack.addArrangedSubview(counters)

        let button = NSButton(title: "Fixture button", target: self, action: #selector(buttonPressed))
        button.setAccessibilityIdentifier("astra.button")
        stack.addArrangedSubview(button)

        let doubleClick = ClickSurface(counter: doubleClickCounter)
        doubleClick.widthAnchor.constraint(equalToConstant: 420).isActive = true
        doubleClick.heightAnchor.constraint(equalToConstant: 30).isActive = true
        stack.addArrangedSubview(doubleClick)

        let text = NSTextField(string: "")
        text.placeholderString = "Unicode text"
        text.setAccessibilityIdentifier("astra.text")
        text.delegate = self
        text.widthAnchor.constraint(equalToConstant: 420).isActive = true
        let nextText = NSTextField(string: "")
        nextText.placeholderString = "Checkpoint target"
        nextText.setAccessibilityIdentifier("astra.checkpoint_target")
        nextText.setAccessibilityLabel("Checkpoint target")
        nextText.delegate = self
        nextText.widthAnchor.constraint(equalToConstant: 160).isActive = true
        let textRow = NSStackView(views: [text, nextText])
        textRow.orientation = .horizontal
        textRow.spacing = 8
        stack.addArrangedSubview(textRow)

        let secure = NSSecureTextField(string: "ASTRA-FIXTURE-SECRET-DO-NOT-EXPOSE")
        secure.placeholderString = "Secure text (manual handoff only)"
        secure.setAccessibilityIdentifier("astra.secure")
        secure.delegate = self
        secure.widthAnchor.constraint(equalToConstant: 420).isActive = true
        stack.addArrangedSubview(secure)

        let scroll = FixtureScrollView(marker: marker, counter: scrollCounter)
        scroll.setAccessibilityIdentifier("astra.scroll")
        scroll.setAccessibilityLabel("Scroll area")
        scroll.hasVerticalScroller = true
        scroll.documentView = NSTextView(frame: NSRect(x: 0, y: 0, width: 400, height: 500))
        (scroll.documentView as? NSTextView)?.string = (1...40).map { "Scroll row \($0)" }.joined(separator: "\n")
        scroll.widthAnchor.constraint(equalToConstant: 420).isActive = true
        scroll.heightAnchor.constraint(equalToConstant: 100).isActive = true
        stack.addArrangedSubview(scroll)

        let drag = DragSurface(marker: marker, counter: dragCounter)
        drag.widthAnchor.constraint(equalToConstant: 420).isActive = true
        drag.heightAnchor.constraint(equalToConstant: 70).isActive = true
        stack.addArrangedSubview(drag)

        let canvas = CanvasSurface(counter: canvasCounter)
        canvas.widthAnchor.constraint(equalToConstant: 420).isActive = true
        canvas.heightAnchor.constraint(equalToConstant: 40).isActive = true
        stack.addArrangedSubview(canvas)

        let modal = NSButton(title: "Open modal", target: self, action: #selector(openModal))
        modal.setAccessibilityIdentifier("astra.modal")
        stack.addArrangedSubview(modal)

        window.initialFirstResponder = text
        text.nextKeyView = nextText
        nextText.nextKeyView = secure
        window.makeKeyAndOrderFront(nil)
        window.makeFirstResponder(text)
        NSApp.activate(ignoringOtherApps: true)
    }

    func applicationShouldHandleReopen(
        _: NSApplication,
        hasVisibleWindows _: Bool
    ) -> Bool {
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
        return true
    }

    @objc private func buttonPressed() {
        clickCounter.increment()
        marker.mark("button:pressed")
    }

    @objc private func openModal() {
        marker.mark("modal:opened")
        let alert = NSAlert()
        alert.messageText = "Fixture modal"
        alert.informativeText = "Deterministic modal state"
        alert.addButton(withTitle: "Close")
        alert.beginSheetModal(for: window) { [weak self] _ in self?.marker.mark("modal:closed") }
    }

    func controlTextDidChange(_ notification: Notification) {
        if let secure = notification.object as? NSSecureTextField {
            marker.mark("secure:changed:\(secure.stringValue.count)")
        } else if let text = notification.object as? NSTextField {
            marker.mark("text:\(text.stringValue)")
        }
    }
}

let application = NSApplication.shared
let delegate = FixtureDelegate()
application.delegate = delegate
application.setActivationPolicy(.regular)
application.run()

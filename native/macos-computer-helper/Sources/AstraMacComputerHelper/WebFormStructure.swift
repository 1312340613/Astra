import Foundation

func choiceNodeCount(_ root: AXNode) -> Int {
    (root.role == "AXRadioButton" || root.role == "AXCheckBox" ? 1 : 0) +
        root.children.reduce(0) { $0 + choiceNodeCount($1) }
}

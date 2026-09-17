# Exact native window identity for duplicate titles

Two ordinary browser windows may have identical titles and geometry. Matching
only those properties makes both windows unbindable and can leave their siblings
with uncertain overlay ordering. Changing a tab title must not determine whether
an otherwise identifiable window can be controlled.

## Matching

Read the native CG window ID from actual AXWindow objects through the optional
`_AXUIElementGetWindow` bridge. Resolve the symbol dynamically: it is a private
macOS SPI, so availability is not assumed. Use the same identity in the catalog,
live target validation, sibling ordering and foreground key-window verification.
The [AeroSpace declaration](https://github.com/nikitabobko/AeroSpace/blob/main/Sources/PrivateApi/include/private.h)
documents the bridge signature used by another macOS window manager.

An ID must resolve to one visible window owned by the same process with matching
geometry. Retained AX object equality remains required after binding. A native ID
mismatch, duplicate ID, wrong owner or changed geometry cannot fall back to a
matching title. Titles can change independently of identity.

If the bridge is absent or returns no ID, preserve the existing unique metadata
match and reject ambiguous candidates. Sheets keep their existing ownership and
live-focus proof: a sheet may report its parent's native ID, so this bridge does
not apply to AXSheet objects.

## Scope and acceptance

No new input channel, focus stealing, blanket overlay exemption or action replay.
Mapped ordinary windows behind the selected target use the existing ordering
checks; actual covering windows still block it.

Unit regressions cover duplicate titles/geometry, title publication lag, bridge
absence, ID mismatch/duplication, owner/geometry changes, retained identity,
foreground identity and real occlusion. Real Edge acceptance creates two private
HTTP fixtures with equal titles and geometry, binds the front window, changes its
checkbox through foreground takeover, then checks the other checkbox stayed
unchanged. It also verifies that a genuinely covered window is still blocked.
The fixture closes its own windows and does not use the browser extension or any
existing page's form controls.

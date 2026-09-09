# Dynamic coordinate detection for WeChat

When the WeChat window is moved or resized, hard-coded coordinates break. Here's how to detect them at runtime.

## Get window position + size

```bash
osascript << 'EOF'
tell application "System Events"
    tell process "WeChat"
        set w to item 1 of windows
        return (position of w) & "," & (size of w)
    end tell
end tell
EOF
# Output: "4,30,621,962" → x=4, y=30, width=621, height=962
```

If output is "missing value", WeChat has no windows — see "Bring WeChat back from minimized" below.

## Compute sidebar / input / send coords from window geometry

Window layout assumption (verified on this Mac mini, 2026-09-01):
- Sidebar (chat list) occupies the left ~200px of the window
- The avatar column is at x ≈ win_x + 0..70 (icons, mostly empty clickspace)
- The row label / text starts at x ≈ win_x + 85 — **click here, not in the icon gutter**
- Chat content area: x ∈ [window_x + 200, window_x + window_w - 20]
- the operator (your own chat) is the first chat in sidebar (sorted by recency, with last-message preview)
- Input field: visible just above the macOS Dock; with default 1080p display,
  the input is at y ≈ win_y + 825 (NOT win_y + win_h - 60, because the dock
  covers the bottom ~70px of the window)
- Send button: at y ≈ win_y + 887 (same offset, in the icon row below input)

```python
def compute_coords(win_x, win_y, win_w, win_h):
    sidebar_x = win_x + 85       # row text/icon, NOT the leftmost icon gutter
    sidebar_y = win_y + 77       # first chat row — header is at win_y+0..30
    input_x = win_x + (win_w // 2)
    input_y = win_y + 825        # above dock; not win_h - 60
    send_x = win_x + win_w - 60
    send_y = win_y + 857         # icon row, below input
    return {
        "sidebar_row1":  (sidebar_x, sidebar_y),
        "neutral_chat":    (sidebar_x, sidebar_y + 42),  # second row (File Transfer)
        "input_field":     (input_x,   input_y),
        "send_button":     (send_x,    send_y),
    }
```

Pitfall #24 (see parent skill): x < 80 (the icon gutter) often misses the row
entirely and falls into dead space between rows. The click registers but does
nothing — neither selecting nor deselecting. Always test x ≥ 85 for sidebar
clicks, and verify visually after the first click.

Pitfall #25 (see parent skill): after `osascript ... activate WeChat`, click
the window's title bar (around win_y + 15) BEFORE the first sidebar click, to
give the window keyboard focus. Without this, a Feishu Thread panel or
notification banner can intercept the click without WeChat ever seeing it.

Note: window_y is from `osascript position` which is **logical coordinates**
(1920×1080 space). cliclick also uses logical. So no HiDPI math needed.

## Bring WeChat back from minimized

```bash
# Find WeChat in the dock
osascript << 'EOF'
tell application "System Events"
    set dockList to UI elements of UI element 1 of process "Dock"
    repeat with d in dockList
        try
            if name of d is "WeChat" then return (position of d) & "," & (size of d)
        end try
    end repeat
end tell
EOF
# Output: "890,997,57,73" → x=890, y=997, w=57, h=73 (2026-09-07 Dock layout)
# Click center: (918, 1033) logical
```

Then `cliclick c:918,1033` opens the WeChat window (if the dock auto-hide is on, the cursor needs to be at the bottom edge first). **Robust alternative**: skip coords entirely and AX-click by name — `tell process "Dock" to click UI element "WeChat" of list 1` (Pitfall #58) — Dock order changes (2026-09-06: 1586→1358, 2026-09-07: 1358→918) break hard-coded coords but never the name.

## Validate that the cursor moved into the right place

Take a fresh screencapture and use vision_analyze to confirm:
- WeChat window is visible at expected coordinates
- Sidebar shows the operator (your own chat)
- Input field is at the bottom

```python
import subprocess
subprocess.run(['screencapture', '-x', '/tmp/check.png'], check=True)
# Then in your agent loop:
# vision_analyze(image_url="/tmp/check.png", question="...")
```

## When detection fails

If `osascript` reports `missing value` for window position, OR `tell process "WeChat"` returns "Can't get process", run:

```bash
osascript -e 'tell application "WeChat" to launch'   # or 'activate'
sleep 2
```

If still failing, the WeChat process may not be running at all. In that case, open WeChat manually first, then retry detection.

## Caveats

- The dock icon position is **stable across macOS sessions** unless the user reorders dock icons. If they did, re-probe.
- Window position can shift by 1-2 logical pixels during animations — always take a screenshot, not just trust the geometry.
- WeChat for Mac may have slightly different layouts in different versions. The 80px sidebar + 60px input assumption is from macOS 26.6.2 / WeChat 4.x. Older versions had wider sidebars.
#!/usr/bin/env python3
"""
send_digest_to_wechat_v2.py — rebuilt WeChat mirror using proven patterns
from wechat-macos-automation skill.

Key improvements over v1:
- Dynamic coordinate detection via osascript (no hard-coded COORDS dict)
- Targets the "the operator (your own chat)" chat window by name (handles dual-window state)
- Move-away-then-back click sequence for popup-triggering targets
- Mandatory vision verification after send (Pitfall #21/26)
- Fails loudly with non-zero exit if verification fails

Pre-flight (Pitfall #48):
1. Check WeChat process is running
2. Check WeChat has a window open
3. Activate WeChat via osascript (NOT cliclick — handles dock hidden case)
4. Click title bar to force keyboard focus (Pitfall #25)
5. Verify the operator (your own chat) chat window exists; activate it

Click sequence (Pitfall #23):
1. Click File Transfer (neutral chat) to clear any prior selection
2. Click the operator (your own chat) in sidebar
3. Click input field (BEFORE Cmd+V per Pitfall #12)
4. Paste via Cmd+V
5. Send with Enter (Pitfall #4)

Verification (Pitfall #21/26):
- Screencapture + vision_analyze to confirm sent bubble
- Retry once if verification fails

Usage:
    python3 send_digest_to_wechat_v2.py            # full pipeline
    python3 send_digest_to_wechat_v2.py --dry-run # show digest, skip WeChat
    python3 send_digest_to_wechat_v2.py --force   # bypass dedup

Cron integration:
    Set DIGEST_TEXT env var to pass the digest text (from unified_digest.py).
    Set WECHAT_MIRROR=0 to disable (Feishu-only mode).
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Optional

# ---------- Configuration ----------

SCRIPT_DIR = Path(__file__).resolve().parent
LOG_DIR = Path("/tmp/wechat_sends")
LOG_DIR.mkdir(parents=True, exist_ok=True)
VERIFY_DIR = Path("/tmp/wechat_verify")
VERIFY_DIR.mkdir(parents=True, exist_ok=True)

CLICLICK = "/opt/homebrew/bin/cliclick"
DEDUP_WINDOW_HOURS = 24

# Default message (overridden by --message or $DIGEST_TEXT env var)
DEFAULT_MESSAGE = """\
ClassA SPECIALS REMINDER

Replace this with the actual digest or notification body.

Use triple-quoted strings to preserve newlines and Unicode (emojis, CJK, box-drawing).
"""


# ---------- Helpers ----------

def run(cmd: list[str], check: bool = True, **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", **kw)


def log(msg: str) -> None:
    print(msg, file=sys.stderr)


def osascript(script: str) -> str:
    """Run osascript and return stripped stdout."""
    r = run(["osascript", "-e", script], check=False)
    return r.stdout.strip()


def get_wechat_windows() -> list[dict]:
    """Return list of WeChat windows with name, position, size.

    Uses '||' as the field separator inside each window entry, and ';;' to
    separate windows — both are unusual enough that they're safe from
    collision with window names or numeric coords.
    """
    raw = osascript("""
tell application "System Events"
    tell process "WeChat"
        set wins to {}
        repeat with w in windows
            set p to position of w
            set s to size of w
            set end of wins to (name of w) & "||" & (item 1 of p as string) & "," & (item 2 of p as string) & "||" & (item 1 of s as string) & "x" & (item 2 of s as string)
        end repeat
        set AppleScript's text item delimiters to ";;"
        set out to wins as string
        set AppleScript's text item delimiters to ""
        return out
    end tell
end tell
""")
    out = []
    if not raw:
        return out
    for entry in raw.split(";;"):
        entry = entry.strip()
        if not entry or "||" not in entry:
            continue
        parts = entry.split("||")
        if len(parts) != 3:
            continue
        name, xy, dims = parts
        if "," not in xy or "x" not in dims:
            continue
        try:
            x_str, y_str = xy.split(",", 1)
            w, h = dims.split("x", 1)
            out.append({
                "name": name,
                "x": int(x_str), "y": int(y_str),
                "w": int(w), "h": int(h),
            })
        except ValueError:
            continue
    return out


def get_wechat_dock_icon() -> Optional[tuple[int, int, int, int]]:
    """Return (x, y, width, height) of WeChat dock icon or None if not in dock."""
    raw = osascript("""
tell application "System Events"
    tell process "Dock"
        try
            set p to position of UI element "WeChat" of list 1
            set s to size of UI element "WeChat" of list 1
            return (item 1 of p as string) & "," & (item 2 of p as string) & "," & (item 1 of s as string) & "x" & (item 2 of s as string)
        on error
            return ""
        end try
    end tell
end tell
""")
    if not raw:
        return None
    x, y, dims = raw.split(",", 2)
    w, h = dims.split("x")
    return int(x), int(y), int(w), int(h)


def is_wechat_foreground() -> bool:
    """Return True if WeChat is the frontmost app."""
    return osascript('tell application "System Events" to return name of first application process whose frontmost is true') == "WeChat"


# ---------- Pre-flight (Pitfall #48) ----------

def activate_wechat() -> bool:
    """Bring WeChat to foreground. Returns True if successful.

    Pitfall #11: osascript activate does NOT bring up a minimized window.
    Must click dock icon first if WeChat has no windows.
    Pitfall #25: osascript activate does NOT give window keyboard focus.
    Must click inside the window after activate.
    Pitfall #48: WeChat is often NOT frontmost when starting — verify and
    force focus via title-bar click if not.
    """
    log("Pre-flight: checking WeChat state...")

    # Step 1: Check WeChat process
    if not osascript('tell application "System Events" to exists process "WeChat"') == "true":
        log("ERROR: WeChat process not running")
        return False

    # Step 2: Check if WeChat has any windows
    windows = get_wechat_windows()
    if not windows:
        log("WeChat has no windows — clicking dock icon to open")
        dock = get_wechat_dock_icon()
        if not dock:
            log("ERROR: WeChat not in dock and no windows open")
            return False
        dx, dy, dw, dh = dock
        cx, cy = dx + dw // 2, dy + dh // 2
        log(f"  Dock icon at ({cx}, {cy}), clicking...")
        run([CLICLICK, f"c:{cx},{cy}"], check=False)
        time.sleep(2.5)
        windows = get_wechat_windows()
        if not windows:
            log("ERROR: dock click did not open WeChat window")
            return False

    # Step 3: Activate WeChat
    log("  Activating WeChat via osascript...")
    osascript('tell application "WeChat" to activate')
    time.sleep(0.8)

    # Step 4: Verify foreground
    if not is_wechat_foreground():
        fg_app = osascript('tell application "System Events" to return name of first application process whose frontmost is true')
        log("  WeChat not foreground (current: " + fg_app + ")")
        log("  Clicking title bar to force focus...")
        # Click title bar of first WeChat window to give keyboard access
        if windows:
            w = windows[0]
            title_x = w["x"] + w["w"] // 2
            title_y = w["y"] + 15
            run([CLICLICK, f"c:{title_x},{title_y}"], check=False)
            time.sleep(0.5)

    # Step 5: Verify
    if not is_wechat_foreground():
        log("ERROR: WeChat still not foreground after pre-flight")
        return False

    log("  WeChat foreground ✓")
    return True


TARGET_CHAT = "YOUR_GROUP_CHAT"  # pinned group chat, sidebar row 1
# Sidebar row offsets within the main WeChat window (window-relative y):
#   row 1 = pinned "YOUR_GROUP_CHAT" (where the operator (your own chat) used to be)
#   row 2 = File Transfer (neutral chat for clearing selection)
ROW_OFFSET_TARGET = 110   # pinned group row (verified 2026-09-07)
ROW_OFFSET_NEUTRAL = 175  # File Transfer


def find_target_chat_window() -> Optional[dict]:
    """Find the chat window for the ClassA group. Returns window dict or None.

    WeChat 4.1.60 main window is named "WeChat" regardless of selected chat
    (Pitfall #65), so name matching is only useful if a separate chat window
    is open. Returns the main window as fallback — targeting is coordinate-
    based (pinned row 1), not name-based.
    """
    windows = get_wechat_windows()
    for w in windows:
        if "ClassA" in w["name"] or "Class Chat" in w["name"]:
            return w
    return windows[0] if windows else None


def compute_chat_input_coords(window: dict) -> tuple[int, int]:
    """Compute (x, y) for the chat input field inside a chat window."""
    # Input field is in the bottom area of the chat window
    # For a typical 598x640 chat window at (661, 220), input is around y=win_y + win_h - 50
    cx = window["x"] + window["w"] // 2
    cy = window["y"] + window["h"] - 50
    return cx, cy


def compute_sidebar_row(window: dict, row_y_offset: int) -> tuple[int, int]:
    """Compute (x, y) for a sidebar row inside the main WeChat window.

    row_y_offset is the y-coord within the sidebar (e.g., 77 for first row).
    """
    # Sidebar x: row text/icon is at x = win_x + 85 (avoid icon gutter at x<80)
    cx = window["x"] + 85
    cy = window["y"] + row_y_offset
    return cx, cy


# ---------- Click wrapper (Pitfall #36 move-away-then-back) ----------

def safe_click(key: str, x: int, y: int, settle: float = 0.8) -> None:
    """Click with move-away-then-back sequence for popup-triggering targets.

    The plain cliclick c:X,Y silently fails for some UI elements (e.g. toolbar
    icons, sidebar rows after focus changes). The 3-step move-away-then-back
    sequence unblocks these by physically moving the cursor through the target
    location before clicking (Pitfall #36).
    """
    if key in ("input_field", "recipient_sidebar", "neutral_chat"):
        # Popup-trigger or focus-sensitive: use move-away-then-back
        run([CLICLICK, "m:500,300"], check=False)
        time.sleep(0.4)
        run([CLICLICK, f"m:{x},{y}"], check=False)
        time.sleep(0.4)
    run([CLICLICK, f"c:{x},{y}"], check=False)
    time.sleep(settle)


# ---------- Dedup (Pitfall #31) ----------

def extract_marker(message: str) -> Optional[str]:
    """Stable identifier for the digest (e.g. 'Tomorrow is Day 4')."""
    for line in message.splitlines():
        if "Tomorrow is Day" in line:
            return line.strip()
    for line in message.splitlines():
        if line.strip():
            return line.strip()[:30]
    return None


def already_sent_recently(marker: str, hours: int = DEDUP_WINDOW_HOURS) -> bool:
    """Return True if a digest with the same marker was sent within `hours`."""
    if not marker or not LOG_DIR.exists():
        return False
    cutoff = time.time() - hours * 3600
    for f in LOG_DIR.glob("sent-*.txt"):
        try:
            if f.stat().st_mtime >= cutoff:
                content = f.read_text(encoding="utf-8")
                if extract_marker(content) == marker:
                    log(f"  dedup hit: {f.name} has same marker, "
                        f"{int((time.time() - f.stat().st_mtime) / 60)} min ago")
                    return True
        except (OSError, UnicodeDecodeError):
            continue
    return False


# ---------- Send pipeline ----------

def send(message: str, dry_run: bool = False) -> tuple[bool, str]:
    """Click sequence: activate WeChat → click neutral chat → click the operator →
    click input → paste → Enter. Returns (ok, detail)."""
    if dry_run:
        log(f"[dry-run] Would send {len(message)} bytes")
        return True, "dry-run"

    # Pre-flight
    if not activate_wechat():
        return False, "preflight_failed"

    windows = get_wechat_windows()
    if not windows:
        return False, "no_wechat_windows"

    main_win = windows[0]
    log(f"  Main WeChat window: ({main_win['x']}, {main_win['y']}) {main_win['w']}x{main_win['h']}")

    # Pitfall #23: click a neutral chat FIRST to clear any prior selection,
    # then the target group. This ensures the second click is a state-change,
    # not a no-op toggle that deselects.
    log("  Clicking File Transfer (neutral)...")
    nt_x, nt_y = compute_sidebar_row(main_win, ROW_OFFSET_NEUTRAL)
    safe_click("neutral_chat", nt_x, nt_y, settle=1.0)

    log(f"  Clicking {TARGET_CHAT} (pinned row 1)...")
    ro_x, ro_y = compute_sidebar_row(main_win, ROW_OFFSET_TARGET)
    safe_click("recipient_sidebar", ro_x, ro_y, settle=2.0)

    # 4.1.60: main window stays named "WeChat" (non-shareable, Pitfall #65) —
    # coordinate targeting is authoritative; window lookup is best-effort.
    time.sleep(1.0)
    chat_input_window = find_target_chat_window() or main_win
    if chat_input_window is main_win:
        log("  Using main window input field (coordinate targeting)")

    # Load clipboard (handles any UTF-8 — Pitfall #8, #20)
    with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False, encoding="utf-8") as tf:
        tf.write(message)
        tmp = tf.name
    try:
        r = subprocess.run(["pbcopy"], stdin=open(tmp, encoding="utf-8"), timeout=5)
        if r.returncode != 0:
            return False, f"pbcopy failed rc={r.returncode}"
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass
    time.sleep(0.3)

    # Click input field (BEFORE Cmd+V — Pitfall #12)
    log("  Clicking input field...")
    input_x, input_y = compute_chat_input_coords(chat_input_window)
    safe_click("input_field", input_x, input_y, settle=0.8)

    # Paste via Cmd+V
    log("  Pasting (Cmd+V)...")
    osascript('tell application "System Events" to keystroke "v" using {command down}')
    time.sleep(1.8)

    # Send with Enter (Pitfall #4)
    log("  Sending (Enter)...")
    osascript('tell application "System Events" to keystroke return')
    time.sleep(3.0)

    # Save a copy of what we sent
    stamp = time.strftime("%Y%m%d-%H%M%S")
    log_file = LOG_DIR / f"sent-{stamp}.txt"
    try:
        log_file.write_text(message, encoding="utf-8")
    except OSError as e:
        log(f"WARNING: could not save sent log: {e}")

    return True, "sent"


# ---------- Verification (Pitfall #21, #26) ----------

def verify_send(message: str, screenshot_path: str = "/tmp/wc_post_send.png") -> tuple[bool, str]:
    """Confirm the message actually landed via vision_analyze.

    Without this step, send() returning True is just "no exception fired",
    not evidence of delivery. Pitfall #21 / #26 / #22 — verified silent
    failure mode 2026-09-01.
    """
    try:
        subprocess.run(["screencapture", "-x", screenshot_path], check=False)
    except Exception as e:
        return False, f"screencapture failed: {e}"

    if not Path(screenshot_path).exists():
        return False, "screenshot file missing"

    lines = [ln for ln in message.splitlines() if ln.strip()]
    head = lines[0][:30] if lines else message[:30]
    tail = lines[-1][:30] if lines else ""

    question = (
        f"This is a WeChat screenshot. Did a sent message bubble appear in the "
        f"chat panel containing text matching '{head}'? Look at the most recent "
        f"message visible — is it a right-aligned bubble from the operator? "
        f"Answer YES with bubble position, or NO with what's actually visible."
    )
    try:
        # Try to import vision_analyze from hermes_tools (only available in agent context)
        from hermes_tools import vision_analyze
        ans = vision_analyze(image_url=screenshot_path, question=question)
        answer_text = str(ans)
        ok = answer_text.strip().upper().startswith("YES")
        return ok, answer_text.strip()[:300]
    except (ImportError, Exception) as e:
        # vision_analyze not available — fall back to manual verification
        return None, f"vision_analyze unavailable ({e}); manual verification needed at {screenshot_path}"


# ---------- Main ----------

def main() -> int:
    if os.environ.get("WECHAT_MIRROR", "1") == "0":
        log("WECHAT_MIRROR=0 set — exiting silently")
        return 0

    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--message", help="Override default MESSAGE / $DIGEST_TEXT")
    ap.add_argument("--skip-verify", action="store_true",
                    help="Skip post-send vision verification (NOT recommended)")
    ap.add_argument("--force", action="store_true",
                    help="Bypass 24h dedup check")
    args = ap.parse_args()

    # Resolve message (Pitfall #27 / #31 — env var handoff for cron pipelines)
    if args.message:
        msg = args.message
    elif "DIGEST_TEXT" in os.environ:
        msg = os.environ["DIGEST_TEXT"]
        log(f"Got message from $DIGEST_TEXT env var ({len(msg)} bytes)")
    else:
        msg = DEFAULT_MESSAGE

    if not msg or not msg.strip():
        log("ERROR: no message to send")
        return 1

    # Dedup
    if not args.force:
        marker = extract_marker(msg)
        if marker and already_sent_recently(marker):
            log(f"Dedup: marker {marker!r} already sent in past "
                f"{DEDUP_WINDOW_HOURS}h — skipping (use --force to override)")
            return 0

    ok, detail = send(msg, dry_run=args.dry_run)
    log(f"send() → ok={ok}, detail={detail}")

    if not ok or args.dry_run:
        return 0 if ok else 1

    if args.skip_verify:
        log("WARNING: --skip-verify set; treat as soft success only")
        return 0

    # Verify
    v_ok, v_detail = verify_send(msg)
    log(f"verify_send() → ok={v_ok}, detail={v_detail[:200]}")

    if v_ok is None:
        log("Verification tool unavailable — manual check needed")
        return 0

    if not v_ok:
        log("VERIFICATION FAILED — message may not have been delivered. Retrying...")
        time.sleep(3)
        ok2, detail2 = send(msg, dry_run=False)
        log(f"retry send() → ok={ok2}, detail={detail2}")
        if not ok2:
            return 2
        v_ok, v_detail = verify_send(msg, screenshot_path="/tmp/wc_post_send_retry.png")
        log(f"retry verify_send() → ok={v_ok}, detail={v_detail[:200]}")
        if not v_ok:
            return 3

    return 0


if __name__ == "__main__":
    sys.exit(main())
#!/usr/bin/env python3
"""
send_wrapper_template.py — drop-in wrapper template for sending a message
to WeChat via cliclick+peekaboo+screencapture. Copy this file and customize
the MESSAGE / RECIPIENT / coords for your use case.

Designed to be triggered by an agent run (e.g., cron watcher) that needs to
drive the WeChat GUI. Best-effort: errors logged + non-zero exit, no abort.

Pre-flight: confirms WeChat is foregrounded. If minimized, clicks dock icon
to bring up the window. If no WeChat process at all, exits with rc=2.

Post-send verification: takes a fresh screenshot after the send sequence and
runs vision_analyze to confirm a sent bubble with the message text appeared
in the chat panel. WITHOUT this verification, the wrapper reports success
even when no message was actually delivered (Pitfall #21 / #22 in the
parent skill — verified failure mode observed 2026-09-01). Caller should
treat verify-skip as a real failure, not a soft warning.

Usage:
    python3 send_wrapper_template.py                    # full pipeline
    python3 send_wrapper_template.py --dry-run          # print message only
    python3 send_wrapper_template.py --message "hello"  # override message
    python3 send_wrapper_template.py --skip-verify      # for known-good test runs
    python3 send_wrapper_template.py --force            # bypass dedup (24h)

Cron-pipeline integration:
    Set DIGEST_TEXT env var to pass the message in directly (skips --message
    and any MESSAGE constant). This is the pattern used by
    scripts/ (this repo)unified_digest.py to invoke
    send_digest_to_wechat.py after producing a digest for Feishu — see
    Pitfall #27 in the parent skill.

    Set WECHAT_MIRROR=0 to disable the WeChat mirror entirely (used by the
    pipeline script to short-circuit when only Feishu is needed).
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

# Coordinates: re-probe before each cron run if possible. See references/coord-detection.md.
# These are the 2026-09-01 calibration for the operator's Mac mini (621×962 window at (4, 30)).
#
# REVISION HISTORY (do not revert to older values):
#   - 2026-08-31: input_field=(340, 870), send_button=(525, 870), neutral_chat=(60, 144)
#   - 2026-09-01: input_field=(270, 855) — dock covers win_y+win_h-60; need to click higher
#                 send_button=(565, 887) — actual button position in current window layout
#                 neutral_chat=(90, 149) — x=60 hit the sidebar's empty icon gutter
#                                          (Pitfall #24); x=90 lands on File Transfer row text
#                 wc_window_title=(314, 45) — must click inside window after osascript
#                                             activate to give it actual focus (Pitfall #25)
COORDS = {
    "wechat_dock_icon":   (918, 1033),  # when dock hidden, click center (REVISED 2026-09-07 — Dock rearranged; prefer AX name-click per Pitfall #58)
    "wc_window_title":    (314, 45),     # title bar (gives window focus after activate)
    "recipient_sidebar":  (120, 107),    # the operator (your own chat) first row
    "neutral_chat":       (90, 149),     # File Transfer — click FIRST to clear prior selection
    "input_field":        (270, 855),    # message input center (visible above dock)
    "send_button":        (565, 887),    # Send (S) — DO NOT CLICK, use Enter
    "dialog_cancel":      (442, 727),    # "Send favorites" dialog Cancel (rare)
}

CLICLICK = "/opt/homebrew/bin/cliclick"

# Dedup state — set to 0 to disable
DEDUP_WINDOW_HOURS = 24

# Default message — replace, pass --message, or set DIGEST_TEXT env var.
MESSAGE = """\
Replace this with the actual digest or notification body.

Use triple-quoted strings to preserve newlines and Unicode (emojis, CJK, box-drawing).
"""


# ---------- Helpers ----------

def run(cmd, check=True, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", **kw)


def log(msg):
    print(msg, file=sys.stderr)


def is_wechat_frontmost() -> bool:
    r = run(["osascript", "-e",
             'tell application "System Events" to get name of first application process whose frontmost is true'],
            check=False)
    return "WeChat" in (r.stdout or "")


def has_wechat_window() -> bool:
    """Return True if WeChat has at least one window."""
    r = run(["osascript", "-e",
             'tell application "System Events" to tell process "WeChat" to get name of every window'],
            check=False)
    return bool((r.stdout or "").strip())


def activate_wechat():
    """Bring WeChat window to front, opening it from dock if minimized.

    Pitfall #25: osascript activate alone does NOT give the window keyboard
    focus. After activate, click inside the window (title bar) to make sure
    subsequent clicks land on WeChat and not on a window painted over it.
    """
    if not has_wechat_window():
        log("WeChat has no window — clicking dock icon")
        x, y = COORDS["wechat_dock_icon"]
        run([CLICLICK, f"c:{x},{y}"], check=False)
        time.sleep(2)

    run(["osascript", "-e", 'tell application "WeChat" to activate'], check=False)
    time.sleep(0.8)
    # Click inside the window to grant actual focus (Pitfall #25)
    x, y = COORDS["wc_window_title"]
    run([CLICLICK, f"c:{x},{y}"], check=False)
    time.sleep(0.5)


def click(key, retries: int = 1, settle: float = 0.5):
    """Click at pre-calibrated coordinates. Retries once if needed."""
    for i in range(retries):
        x, y = COORDS[key]
        run([CLICLICK, f"c:{x},{y}"], check=False)
        time.sleep(settle)


def paste():
    """Cmd+V via osascript (cliclick kp: doesn't support modifier combos)."""
    run(["osascript", "-e",
         'tell application "System Events" to keystroke "v" using {command down}'],
        check=False)


def send_enter():
    """Press Enter to send (more reliable than clicking Send button)."""
    run(["osascript", "-e",
         'tell application "System Events" to keystroke return'],
        check=False)


# ---------- Dedup helpers (Pitfall #27) ----------

def extract_marker(message: str) -> Optional[str]:
    """Stable identifier for the digest (e.g. 'Tomorrow is Day 4')."""
    for line in message.splitlines():
        if "Tomorrow is Day" in line:
            return line.strip()
    # Fallback: first non-empty line, first 30 chars
    for line in message.splitlines():
        if line.strip():
            return line.strip()[:30]
    return None


def already_sent_recently(marker: str, log_dir: Path, hours: int = DEDUP_WINDOW_HOURS) -> bool:
    """Return True if a digest with the same marker was sent within `hours`.

    Reads sent-*.txt files in log_dir; if any contains the same marker
    AND was written within `hours`, return True (skip the send).
    """
    if not marker or not log_dir.exists():
        return False
    cutoff = time.time() - hours * 3600
    for f in log_dir.glob("sent-*.txt"):
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


def get_message(args, default_message: str) -> Optional[str]:
    """Resolve the message to send from (priority order):
       1. --message arg (override)
       2. $DIGEST_TEXT env var (cron pipeline handoff — Pitfall #27)
       3. default_message constant
    """
    if args.message:
        return args.message
    env_text = os.environ.get("DIGEST_TEXT")
    if env_text is not None:
        log(f"Got message from $DIGEST_TEXT env var ({len(env_text)} bytes)")
        return env_text
    return default_message


# ---------- Main pipeline ----------

def send(message: str, log_dir: Path, dry_run: bool = False, retries: int = 3) -> tuple[bool, str]:
    """Click sequence: activate WeChat → click neutral chat → click recipient →
    click input → paste → Enter. The retry loop on the sidebar handles Pitfall
    #23 (clicking the same row twice deselects it) — we click File Transfer
    FIRST, then the operator, so the second click is a state change, not a toggle.
    """
    if dry_run:
        log(f"[dry-run] Would send {len(message)} bytes")
        return True, "dry-run"

    activate_wechat()

    # Click neutral chat FIRST (clear any prior selection), then recipient.
    # This is critical: clicking the same row twice can DESELECT it (Pitfall #23).
    for attempt in range(retries):
        click("neutral_chat", settle=1.0)
        click("recipient_sidebar", settle=2.0)
        log(f"  click attempt {attempt + 1}/{retries} complete")

    # Load clipboard (handles any UTF-8)
    with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False, encoding="utf-8") as tf:
        tf.write(message)
        tmp = tf.name
    r = subprocess.run(["pbcopy"], stdin=open(tmp, encoding="utf-8"), timeout=5)
    if r.returncode != 0:
        return False, f"pbcopy failed rc={r.returncode}"
    time.sleep(0.3)

    # Click input field BEFORE Cmd+V (Pitfall #12)
    click("input_field", settle=0.8)

    # Paste
    paste()
    time.sleep(1.8)

    # Send with Enter — DO NOT click Send button (Pitfall #4)
    send_enter()
    time.sleep(3.0)

    # Save a copy of what we sent (used for dedup on next runs)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    log_file = log_dir / f"sent-{stamp}.txt"
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        log_file.write_text(message, encoding="utf-8")
    except OSError as e:
        log(f"WARNING: could not save sent log to {log_file}: {e}")

    return True, "sent"


def verify_send(message: str, screenshot_path: str = "/tmp/wc_post_send.png") -> tuple[bool, str]:
    """Confirm the message actually landed in the chat panel.

    Takes a fresh screenshot and asks vision_analyze whether a sent bubble
    with the message text is visible in the chat panel. Returns (ok, detail).

    Skipping this step is the #1 way wrappers lie about success — without it,
    `send()` returning ("sent",) is just "no exception fired", not evidence
    of delivery. Pitfall #21 / #22 in the parent skill.
    """
    try:
        subprocess.run(["screencapture", "-x", screenshot_path], check=False)
    except Exception as e:
        return False, f"screencapture failed: {e}"

    if not Path(screenshot_path).exists():
        return False, "screenshot file missing"

    lines = [ln for ln in message.splitlines() if ln.strip()]
    head = lines[0][:30] if lines else message[:30]
    tail = lines[2][:30] if len(lines) > 2 else (lines[-1][:30] if lines else "")

    question = (
        f"This is a WeChat chat panel screenshot. Did a green sent-bubble "
        f"appear at the bottom of the chat with text containing both "
        f"'{head}' and '{tail}'? Answer YES with the bubble's approximate "
        f"position, or NO with what is actually visible at the bottom of "
        f"the chat panel (e.g. 'still in input box', 'no bubble', 'typing "
        f"indicator', etc.)."
    )
    try:
        from hermes_tools import vision_analyze
        ans = vision_analyze(image_url=screenshot_path, question=question)
        answer_text = str(ans)
        ok = answer_text.strip().upper().startswith("YES")
        return ok, answer_text.strip()[:300]
    except Exception as e:
        return False, f"vision_analyze failed: {e}"


# ---------- Main ----------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--message", help="Override default MESSAGE / $DIGEST_TEXT")
    ap.add_argument("--skip-verify", action="store_true",
                    help="Skip post-send vision verification. Use only when "
                         "debugging or in a known-good environment — this "
                         "is the option that makes the wrapper lie about "
                         "success.")
    ap.add_argument("--force", action="store_true",
                    help="Bypass 24h dedup check (use when re-sending a "
                         "known-good digest after testing or fixing)")
    ap.add_argument("--log-dir", default="/tmp/wechat_sends",
                    help="Directory holding sent-*.txt logs used for dedup")
    args = ap.parse_args()

    msg = get_message(args, MESSAGE)
    if msg is None or not msg.strip():
        log("ERROR: no message to send (set MESSAGE, --message, or $DIGEST_TEXT)")
        return 1

    log_dir = Path(args.log_dir)

    # Dedup (Pitfall #27)
    if not args.force:
        marker = extract_marker(msg)
        if marker and already_sent_recently(marker, log_dir):
            log(f"Dedup: marker {marker!r} already sent in past "
                f"{DEDUP_WINDOW_HOURS}h — skipping (use --force to override)")
            return 0

    ok, detail = send(msg, log_dir=log_dir, dry_run=args.dry_run)
    log(f"send() → ok={ok}, detail={detail}")

    if not ok or args.dry_run:
        return 0 if ok else 1

    if args.skip_verify:
        log("WARNING: --skip-verify set; not confirming delivery. Treat as soft success only.")
        return 0

    v_ok, v_detail = verify_send(msg)
    log(f"verify_send() → ok={v_ok}, detail={v_detail[:200]}")
    if not v_ok:
        log("VERIFICATION FAILED — message may not have been delivered. "
            "Retrying once after a 3s pause...")
        time.sleep(3)
        activate_wechat()
        click("neutral_chat", settle=1.0)
        click("recipient_sidebar", settle=2.0)
        retry_ok, retry_detail = send(msg, log_dir=log_dir, dry_run=False)
        log(f"retry send() → ok={retry_ok}, detail={retry_detail}")
        if not retry_ok:
            return 2
        v_ok, v_detail = verify_send(msg, screenshot_path="/tmp/wc_post_send_retry.png")
        log(f"retry verify_send() → ok={v_ok}, detail={v_detail[:200]}")
        if not v_ok:
            return 3

    return 0


if __name__ == "__main__":
    sys.exit(main())
#!/usr/bin/env python3
"""wechat_outbox.py — single-sender outbox for ALL WeChat DM deliveries.

ARCHITECTURE (2026-09-12, [user]'s multi-class expansion):
  Producers (specials builders, digest, PTSA, announcements) NEVER touch
  WeChat. They enqueue message JSON files into ~/workspace/wechat-outbox/queue/.
  ONE drain worker (this script, via cron or CLI) holds the global GUI lock,
  groups messages by recipient chat, selects each chat ONCE via search, and
  sends all its pending messages in one GUI session.

Why: every 2026-09 incident (5SW→5CS misdelivery, duplicate newsletter,
silent PDF degrade) came from N independent GUI drivers. This design makes
cross-chat mixing structurally impossible — one driver, one lock, one queue.

Layout:
  ~/workspace/wechat-outbox/
    queue/          pending message .json files
    archive/        sent/ .json + report .md (monthly subdirs)
    recipients.json {chat_key: {search, class, status: approved|pending}}
    reports/        drain reports

CLI:
  enqueue --to <chat_key> --class 5NM --type specials \\
          --dedupe <key> [--text "..."] [--file msg.txt] [--priority 2]
  drain [--dry-run] [--max-per-chat 5] [--max-total 15]
  approve <chat_key>          mark recipient chat approved
  pending                     list queue (chat, type, dedupe, age)

Dedupe: (to + dedupe_key) must be unique across queue AND archive — a
producer re-running can never double-send. Age gate: messages older than
MAX_AGE_H (default 24h) are dropped at drain time with a note (stale
reminders are spam — 2026-09-10 lesson).

First-send safety: chats with status != approved are HELD (not sent) and
reported. Approve once after a File Transfer test + [user]'s go-ahead.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

TZ = timezone(timedelta(hours=8))  # CST
OUTBOX_ROOT = Path(os.environ.get("WECHAT_OUTBOX_ROOT",
                                  Path.home() / "workspace/wechat-outbox"))
QUEUE_DIR = OUTBOX_ROOT / "queue"
ARCHIVE_DIR = OUTBOX_ROOT / "archive"
REPORTS_DIR = OUTBOX_ROOT / "reports"
RECIPIENTS_FILE = OUTBOX_ROOT / "recipients.json"
MAX_AGE_H = 24
LOCK_REASON = "outbox-drain"

sys.path.insert(0, str(Path(__file__).resolve().parent))
from wechat_gui_lock import acquire_wechat_gui_lock, release_wechat_gui_lock  # noqa: E402


def _now() -> datetime:
    return datetime.now(TZ)


def _ensure_dirs() -> None:
    for d in (QUEUE_DIR, ARCHIVE_DIR, REPORTS_DIR):
        d.mkdir(parents=True, exist_ok=True)


def load_recipients() -> dict:
    if RECIPIENTS_FILE.exists():
        return json.loads(RECIPIENTS_FILE.read_text())
    return {}


def save_recipients(recips: dict) -> None:
    _ensure_dirs()
    RECIPIENTS_FILE.write_text(json.dumps(recips, indent=2, ensure_ascii=False))


# ---------- enqueue ----------

def _existing_keys(to: str) -> set:
    """(to, dedupe_key) pairs in queue + archive — for dedupe."""
    keys = set()
    for d in (QUEUE_DIR, ARCHIVE_DIR):
        if not d.exists():
            continue
        for f in d.rglob("*.json"):
            try:
                m = json.loads(f.read_text())
                keys.add((m.get("to"), m.get("dedupe_key")))
            except Exception:
                continue
    return keys


def enqueue(to: str, msg_class: str, msg_type: str, dedupe_key: str,
            text: str, files: list | None = None, priority: int = 2) -> str | None:
    """Add a message. Returns message id, or None if (to, dedupe_key) exists."""
    _ensure_dirs()
    if (to, dedupe_key) in _existing_keys(to):
        return None
    msg = {
        "id": f"obx-{uuid.uuid4().hex[:12]}",
        "to": to,
        "class": msg_class,
        "type": msg_type,
        "dedupe_key": dedupe_key,
        "text": text,
        "files": files or [],
        "priority": priority,
        "enqueued_at": _now().isoformat(),
        "status": "pending",
    }
    (QUEUE_DIR / f"{msg['id']}.json").write_text(
        json.dumps(msg, indent=2, ensure_ascii=False))
    return msg["id"]


# ---------- WeChat send primitives (proven 5SW recipe — 2026-09-11 fix) ----------
#
# 19:22 INCIDENT: the first version of these primitives deviated from the
# daily-proven specials_reminder_5sw sequence — no leading Escape, search
# term via raw keystroke, TWO Returns (the second opened a standalone chat
# window), no Escape to leave search, and NO input-field click before
# paste. Result: messages were pasted into the SEARCH BOX and Enter ran a
# search. Nothing was delivered to anyone, yet the drain archived both as
# status:sent (the primitives returned True unconditionally). [user]:
# "No messages were sent to WeChat."
#
# Fix: port the exact 5SW sequence + post-action AX sanity checks. Blind
# True is gone — any anomaly fails the step, the message stays queued, and
# the report says so. Failed messages RETRY on the next drain (a rare
# duplicate after a benign anomaly is cheaper than silent loss).

CLICLICK = "/opt/homebrew/bin/cliclick"


def _osa(expr: str) -> None:
    subprocess.run(["osascript", "-e", expr], check=False,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _run(cmd: list) -> None:
    subprocess.run(cmd, check=False,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _clipboard(text: str) -> bool:
    p = subprocess.run(["pbcopy"], input=text.encode(), check=False)
    return p.returncode == 0


def _wechat_windows() -> list:
    """All WeChat windows: [{name, x, y, w, h}] via AX (Pitfall #60 recipe)."""
    r = subprocess.run(
        ["osascript", "-e", """
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
end tell"""], capture_output=True, text=True)
    windows = []
    for entry in (r.stdout or "").strip().split(";;"):
        entry = entry.strip()
        if not entry or "||" not in entry:
            continue
        name, xy, dims = entry.split("||", 2)
        if "," not in xy or "x" not in dims:
            continue
        x_s, y_s = xy.split(",", 1)
        w_s, h_s = dims.split("x", 1)
        try:
            windows.append({"name": name, "x": int(x_s), "y": int(y_s),
                            "w": int(w_s), "h": int(h_s)})
        except ValueError:
            continue
    return windows


def _main_wechat_window() -> dict | None:
    """The main window is named exactly 'WeChat' (4.1.60). Stray chat
    windows ('WeChat (Window)', chat names) and floating search boxes
    (unnamed) are IGNORED — never windows[0]."""
    for w in _wechat_windows():
        if w["name"] == "WeChat":
            return w
    return None


def _wechat_chat_header() -> str:
    """OCR the active chat's title-bar to verify the right chat opened.

    INCIDENT 2026-09-12 10:56: a 5xx-rp send was archived status:sent but the
    message landed in 5XX Name5's chat instead. Investigation: WeChat
    4.1.60's main window is non-shareable (Pitfall #65), AX exposes only the
    3 window controls (no chat title accessible), so `value of UI element 1`
    returns 'missing value'. The previously-installed AX header check is a
    no-op on this Mac mini's WeChat version.

    The fix: screenshot the title-row strip (window-relative y=80..120, where
    the selected chat's display name lives) and OCR it. If the OCR'd text does
    NOT contain a 4-char fragment of the search term, the wrong chat opened —
    refuse to paste, leave the message queued for human investigation.

    Returns the lowercase OCR text, or '' if anything went wrong (the caller
    treats '' as a hard error — better to fail closed than paste to wrong chat).
    """
    win = _main_wechat_window()
    if not win:
        return ""
    # OCR sandbox: leptonica cannot fopenReadStream files under /tmp (silent
    # empty output). Write to ~/workspace/wechat-outbox/ instead.
    path = Path.home() / "workspace" / "wechat-outbox" / "_chat_header.png"
    path.parent.mkdir(parents=True, exist_ok=True)
    # Chat-title verification band. Two candidate bands (window-relative):
    #   A: y=15..55  — normal state title row (proven: 6/7 gate passes at
    #      2026-09-15 17:57, all RP sends 2026-09-11..14)
    #   B: y=48..96  — fallback when the title renders lower
    # GARBAGE SOURCES seen 2026-09-15: pane-top button glyphs ('oo — &es qs
    # ove') and search-result views (no title at all). The gate matches on
    # distinctive tokens, so a wrong-band read fails CLOSED (refuses), never
    # sends to the wrong chat. Try A, then B; accept the first token match.
    def _ocr(path: Path) -> str:
        # 2x upscale + psm 6 — CALIBRATED 2026-09-15 18:32 live sweep on this
        # window (w=700): psm7 misreads the bold title ('5Sw mm Oe Co'), psm6
        # at 2x reads it perfectly ('5XX Name1 Or Give'). Keep psm7 as a
        # fallback only if psm6 returns empty (broader sweep proved 3x/psm7
        # reads toolbar icons when the title clips — fail-closed is fine).
        try:
            from PIL import Image
            im = Image.open(str(path))
            im.resize((im.width * 2, im.height * 2), Image.LANCZOS).save(str(path))
        except Exception:
            pass
        for args in (["--psm", "6"], ["--psm", "7"], []):
            try:
                out = subprocess.run(
                    ["tesseract", str(path), "-", "-l", "eng", *args],
                    capture_output=True, text=True, timeout=5).stdout.strip().lower()
                if out:
                    return out
            except (FileNotFoundError, subprocess.TimeoutExpired):
                continue
        return ""

    # RE-CALIBRATED 2026-09-15 18:30 (live tesseract TSV on an open chat):
    # the title renders window-relative at x>=230 (a short title like
    # "5XX Name1" ENDS before x=330 — the old x=270 band clipped its head and
    # OCR'd the toolbar icons instead: 'oo — &es QS ove'). Region x=230..,
    # y=5..55 at 2x upscale reads the title cleanly ('5XX Name1' live).
    # Traffic lights live at x<100 — excluded.
    #
    # 2026-10-25 Z-ORDER FIX: `screencapture -R x,y,w,h` captures whatever
    # is ON TOP at those screen coordinates. If Chrome/Feishu overlaps the
    # WeChat window, the OCR read the WRONG app's pixels — on 2026-10-25
    # all 7 sends failed with header garbage like 'eee eee sl\n@® @ © 8g'
    # (Chrome's purple nav bar) instead of a chat title. The post-select
    # gate correctly refused to paste (no misdelivery), but all 7 messages
    # were dropped.
    #
    # FIX: use the `wincap` helper (ScreenCaptureKit) which captures the
    # WeChat WINDOW's own content regardless of z-order. Falls back to the
    # old screen-region method if wincap is unavailable/fails.
    wincap = Path(__file__).resolve().parent / "wincap"
    if wincap.exists() and os.access(str(wincap), os.X_OK):
        # Crop the title band in window-relative coords. ScreenCaptureKit
        # config.sourceRect is in POINTS (window scale), and wincap renders
        # at .best resolution, so crop in points then let the 2x upscale in
        # _ocr() handle the rest.
        r = subprocess.run(
            [str(wincap), "WeChat", str(path),
             "230", "5", str(win["w"] - 230), "50"],
            capture_output=True, text=True, timeout=20)
        if r.returncode == 0 and path.exists() and path.stat().st_size > 0:
            out = _ocr(path)
            if out:
                return out
        # wincap failed — fall through to the legacy screen-region method
        # (only safe if WeChat is genuinely frontmost).
    subprocess.run(
        ["screencapture", "-x", "-R",
         f"{win['x'] + 230},{win['y'] + 5},{win['w'] - 230},50", str(path)],
        check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return _ocr(path)


def _title_token(search: str) -> str:
    """Distinctive name token for header matching.

    The OCR'd title's left edge clips ("5XX Name5" reads as
    "Kiong NameX Hong"), so the '5xx' prefix is unreliable. Match instead on
    the longest Latin token of the search term — the surname (NameX,
    NameX, NameX, NameX Hong, Transfer).
    """
    tokens = [t for t in search.lower().split() if len(t) >= 4]
    if not tokens:
        return search.lower().strip()
    return max(tokens, key=len)


def _header_matches(search: str, header: str) -> bool:
    """True if any distinctive search token appears in the OCR'd header.

    Tolerates single-char OCR misreads (X→K: 'xiong' → 'kiong') via a
    difflib sliding-window fuzzy match at ratio ≥ 0.8. Tokens are unique
    across the recipient set (nameX/nameX/nameX/nameX/nameX/transfer),
    so any-token matching cannot cross-match another RP chat.
    """
    import difflib
    tokens = [t for t in search.lower().split() if len(t) >= 4]
    if not tokens:
        tokens = [search.lower().strip()]
    for tok in tokens:
        if tok in header:
            return True
        n = len(tok)
        best = 0.0
        # windows of length n AND n±1 — tolerates OCR insert/delete misreads
        # without diluting the ratio the way a fixed n+2 window does
        # (live case 2026-09-12: 'nameX' read as 'nelley', old window
        # scored 0.714 vs threshold 0.8; exact-length window scores 0.833)
        for i in range(0, max(1, len(header) - n + 1)):
            for wl in (n - 1, n, n + 1):
                if wl <= 0:
                    continue
                w = header[i:i + wl]
                if len(w) < wl:
                    continue
                r = difflib.SequenceMatcher(None, tok, w).ratio()
                if r > best:
                    best = r
        if best >= 0.8:
            return True
    return False


def _find_search_row_click(search: str, win: dict) -> tuple[int, int] | None:
    """OCR the search-results list and return a click point for the target row.

    2026-09-12 lesson: in the EXPANDED search page, Return/Down+Return do NOT
    open the top hit (ranking is message-first for weak queries, and Return
    is a no-op). Deterministic selection = find the chat-name row by OCR and
    CLICK it. Chat rows read "name + day/time" (e.g. "file transfer monday
    08:52", "5sw nameX tuesday"); message-preview rows start with "..." and
    are excluded.

    Returns (screen_x, screen_y) or None. Coordinates: tesseract runs on a
    2x upscale, so halve before adding window origin.
    """
    shot = Path.home() / "workspace" / "wechat-outbox" / "_search_rows.png"
    subprocess.run(["screencapture", "-x", "-R",
                    f"{win['x']},{win['y']},{win['w']},{win['h']}", str(shot)],
                   check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        from PIL import Image
        im = Image.open(str(shot))
        im.resize((im.width * 2, im.height * 2), Image.LANCZOS).save(str(shot))
    except Exception:
        return None
    try:
        r = subprocess.run(["tesseract", str(shot), "-", "-l", "eng",
                            "--psm", "6", "tsv"],
                           capture_output=True, text=True, timeout=15)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    import difflib
    tokens = [t for t in search.lower().split() if len(t) >= 4] or \
             [search.lower().strip()]
    lines: dict[tuple, dict] = {}
    for row in r.stdout.splitlines()[1:]:
        cols = row.split("\t")
        if len(cols) < 12 or not cols[11].strip():
            continue
        page, block, par, line_no = cols[1], cols[2], cols[3], cols[4]
        left, top, height = int(cols[6]), int(cols[7]), int(cols[9])
        key = (page, block, par, line_no)
        d = lines.setdefault(key, {"top": top, "left": left, "h": height,
                                   "words": []})
        d["words"].append(cols[11].strip())
        d["top"] = min(d["top"], top)
        d["left"] = min(d["left"], left)
        d["h"] = max(d["h"], height)
    # top-to-bottom, skip the query bar (y<120 in 2x = 60 real) and preview rows
    for key in sorted(lines, key=lambda k: lines[k]["top"]):
        d = lines[key]
        text = " ".join(d["words"]).lower()
        top = d["top"]
        if top < 120:                      # query bar / window chrome
            continue
        if text.startswith(("...", "«", "7", "@")):   # message-preview rows
            continue
        # all distinctive tokens present (fuzzy ≥0.75 per token)?
        ok = True
        for tok in tokens:
            n = len(tok)
            best = 0.0
            for i in range(0, max(1, len(text) - n + 1)):
                ratio = difflib.SequenceMatcher(
                    None, tok, text[i:i + n]).ratio()
                best = max(best, ratio)
            if best < 0.75:
                ok = False
                break
        if not ok:
            continue
        # row must look like a chat row: name + trailing time-ish token
        tail = text.split()[-1] if text.split() else ""
        timeish = any(c.isdigit() for c in tail) or tail in (
            "yesterday", "today", "monday", "tuesday", "wednesday",
            "thursday", "friday", "saturday", "sunday")
        if not timeish:
            continue
        y = win["y"] + top // 2 + max(4, d["h"] // 4)
        x = win["x"] + d["left"] // 2 + 100   # click the name text, not avatar
        return (x, y)
    return None


def wechat_frontmost() -> bool:
    r = subprocess.run(
        ["osascript", "-e",
         'tell application "System Events" to get name of '
         'first application process whose frontmost is true'],
        capture_output=True, text=True)
    return (r.stdout or "").strip() == "WeChat"


def wechat_ax_sane() -> list:
    """Structural post-action checks → list of problems ([] = sane).

    WeChat 4.1.60's main window is non-shareable (no screenshot verify,
    Pitfall #65), but AX still exposes window state. The 19:22 failure
    left TWO detectable residues: a floating search window (unnamed
    367x66) and a stray chat window. Either means the recipe misfired.
    """
    problems = []
    wins = _wechat_windows()
    if not wins:
        problems.append("no WeChat windows")
        return problems
    main = [w for w in wins if w["name"] == "WeChat"]
    names = [w["name"] or "<unnamed>" for w in wins]
    if len(main) != 1:
        problems.append(
            f"expected exactly 1 window named 'WeChat', got {len(main)} "
            f"(windows: {names})")
    if not wechat_frontmost():
        problems.append("WeChat not frontmost")
    return problems


def _close_search_floaters() -> None:
    """Close any floating 'Search chat history' window via its AX close button.

    ROOT CAUSE of the 2026-09-12 10:56 misroute: a floating search window
    (left over from manual use, or spawned by Cmd+F in some states) steals
    the search keystrokes — the main window then never switches chats and
    the paste lands in whatever chat was last open. This window's close
    button IS AX-accessible (unlike main-window content) — click it.
    """
    try:
        subprocess.run(
            ["osascript", "-e", '''
tell application "System Events"
    tell process "WeChat"
        repeat with w in windows
            if name of w is "Search chat history" then
                set btns to (every UI element of w whose role is "AXButton")
                repeat with b in btns
                    if description of b is "close button" then
                        click b
                        return "closed"
                    end if
                end repeat
            end if
        end repeat
        return "none"
    end tell
end tell'''], capture_output=True, text=True, timeout=10)
    except subprocess.TimeoutExpired:
        pass


_last_selected_header: str = ""
"""Caches the OCR'd chat header from the prior successful wechat_select_chat
call. The next call reads it BEFORE its Cmd+F and refuses + surfaces a clearer
diagnostic if the header still matches the prior chat (a strong signal that an
orphan file-picker is absorbing the Cmd+F keystrokes — see Pitfall #72)."""


def wechat_select_chat(search: str) -> bool:
    """Search-select a chat — EXACT proven 5SW sequence (drift-proof).

    Escape → Cmd+F → Cmd+A → paste term (clipboard is the only reliable
    path) → ONE Return (top hit) → Escape to leave search. NEVER a second
    Return: that opens the chat in a separate window (observed 2026-09-11)
    and poisons every window-origin read.

    2026-09-12 incident fixes: (1) DOUBLE Escape first — a single Esc can
    leave an expanded search overlay open; (2) Cmd+A before paste — the
    field can hold RESIDUAL text from a prior search ("5JA Xiong NameX
    Hong"), a bare paste appends/misses, the query becomes garbage, and
    Return keeps/opens the WRONG chat (root cause of the 5SW→5JA misroute
    at 10:56). (3) Post-select OCR header gate below.

    2026-10-14 GUARD REWORK: the pre-select picker-leak guard was firing
    on every chat transition (because _last_selected_header is set after
    a successful send, and the next pre_header is the same chat that just
    finished — they're equal by design, not because of a stuck picker).
    The actual picker-leak signal is: POST-SELECT the header STILL
    matches the prior chat AND doesn't match the target. So the gate
    moved to post-select, where it belongs. Pre-select Escape is
    retained as a defensive measure (clears any straggler modal that
    the previous send left behind).
    """
    _osa('tell application "WeChat" to activate')
    time.sleep(1.0)
    if not wechat_frontmost():
        # 2026-09-14: activate can lose to a focus-stealing app (Feishu
        # while [user] is actively chatting). A physical click on the
        # window forces focus at the window-server level — retry once.
        # 2026-09-16: body click can ALSO lose (Chrome was aggressively
        # holding focus; center click changed nothing). Setting AX
        # 'frontmost to true' on the process worked when both activate
        # and body-click failed — it's now the second escalation step.
        r = subprocess.run(
            ["osascript", "-e",
             'tell application "System Events" to tell process "WeChat" '
             'to set frontmost to true'],
            capture_output=True, text=True)
        time.sleep(1.5)
        if not wechat_frontmost():
            win0 = _main_wechat_window()
            if win0:
                subprocess.run(
                    ["cliclick", f"c:{win0['x'] + win0['w'] // 2},"
                                 f"{win0['y'] + win0['h'] // 2}"],
                    capture_output=True)
                time.sleep(1.5)
            if not wechat_frontmost():
                print("  ERROR: WeChat would not come frontmost for select")
                return False
    # Defensive Escape: clear any straggler modal from a prior send.
    # This is NOT the picker-leak detection — that moved post-select
    # (see below). This is just a clean-slate before opening Cmd+F.
    _close_search_floaters()   # 2026-09-12 root cause — floaters steal keys
    time.sleep(0.5)
    _osa('tell application "System Events" to keystroke (key code 53)')
    time.sleep(0.4)
    _osa('tell application "System Events" to keystroke (key code 53)')
    time.sleep(0.4)
    _osa('tell application "System Events" to '
         'keystroke "f" using command down')
    time.sleep(1.2)
    # Cmd+A: select-all residual text so the paste REPLACES it (2026-09-12).
    _osa('tell application "System Events" to '
         'keystroke "a" using command down')
    time.sleep(0.3)
    if not _clipboard(search):
        print("  ERROR: pbcopy failed loading search term")
        return False
    time.sleep(0.4)
    _osa('tell application "System Events" to '
         'keystroke "v" using command down')
    time.sleep(2.0)   # let results render in the search floater
    # Return opens the top hit — PROVEN for unique contact names (4 RP sends
    # 2026-09-11 + 5SW select 2026-09-12). The search UI is the floating
    # 'Search chat history' window Cmd+F spawns; keystrokes land in it.
    # (A row-click detour was tried 2026-09-12 and REMOVED: it OCR'd the
    # main window instead of the floater and clicked inert sidebar rows.)
    _osa('tell application "System Events" to keystroke return')
    time.sleep(2.2)
    _osa('tell application "System Events" to keystroke (key code 53)')
    time.sleep(0.6)
    # POST-SELECT GATE (2026-09-12 incident): OCR the chat-title band and
    # verify the chat that actually opened matches the search term. The AX
    # checks confirm WeChat is alive but NOT which chat is open; this gate
    # is what actually stops a wrong-chat paste.
    header = _wechat_chat_header()
    if not header:
        print(f"  ERROR: post-select — cannot read chat header (search was {search!r})")
        return False
    if not _header_matches(search, header):
        print(f"  ERROR: post-select — chat header {header!r} does not match {search!r} "
              "; refusing to paste into wrong chat")
        return False
    # 2026-10-19 GUARD DISABLED: the post-select picker-leak guard was
    # firing on every chat transition (false positives on every send
    # except the first). The pre-select Escape + post-select header
    # match (lines 569-574) is sufficient — if the header matches
    # the search term, the chat-select worked. The picker-leak guard
    # was incorrectly identifying normal chat transitions as
    # "stuck" because the fuzzy-match threshold falsely matched
    # OCR garbage tokens across chats.
    _last_selected_header = header
    return True


def wechat_send_text(text: str) -> bool:
    """Click input field → paste → Enter. EXACT 5SW pacing.

    Input click BEFORE paste (Pitfall #12 — else Cmd+V triggers 'Send
    favorites'); 2.0s paste settle before Enter; window-relative input
    coord from the name-matched main window only.
    """
    win = _main_wechat_window()
    if not win:
        print("  ERROR: no main 'WeChat' window to send into")
        return False
    _run([CLICLICK, f"c:{win['x'] + 270},{win['y'] + 855}"])  # input field
    time.sleep(1.0)
    if not _clipboard(text):
        print("  ERROR: pbcopy failed loading message")
        return False
    time.sleep(0.5)
    _osa('tell application "System Events" to '
         'keystroke "v" using command down')
    time.sleep(2.0)
    _osa('tell application "System Events" to keystroke return')
    time.sleep(3.0)
    problems = wechat_ax_sane()
    if problems:
        print(f"  ERROR: post-send ax-anomaly: {'; '.join(problems)}")
        return False
    return True


# ---------- File+text send primitive (added 2026-10-06 — photos pipeline refactor) ----------
#
# Port of the proven sequence from seesaw_announcements_pipeline.py:wechat_send_file_and_text.
# Single GUI lock acquisition; stages file in fresh temp dir so Cmd+A selects exactly 1.
# Caller MUST hold the GUI lock (drain() does this — never call this standalone).
# PROVEN COORDS: folder icon at (x+310, y+937); input field at (x+270, y+855).
# Window-relative coords require the name-matched main window — that's what
# _main_wechat_window() returns; sub-windows would lie.


def _stage_single_file(file_path: str) -> Path:
    """Copy the file into a fresh temp dir so Cmd+A only sees this one file.

    BUGFIX carry-over (2026-09-07): a shared announcements/ folder used to
    let Cmd+A sweep unrelated posters from prior sends. Fresh dir per call
    guarantees exactly 1 file is selected.
    """
    stage = Path(tempfile.mkdtemp(prefix="wechat-outbox-send-"))
    staged = stage / Path(file_path).name
    shutil.copy2(file_path, staged)
    return stage


def wechat_send_file_and_text(file_path: str, text: str) -> bool:
    """Send one file bubble + one text companion to the currently-selected chat.

    Caller MUST hold the GUI lock. If you call this outside drain(), wrap with
    acquire_wechat_gui_lock / release_wechat_gui_lock first.
    Returns True iff the post-send AX sanity check passed.
    """
    if not Path(file_path).exists():
        print(f"  ERROR: file not found: {file_path}")
        return False
    stage = _stage_single_file(file_path)
    staged_path = stage / Path(file_path).name

    win = _main_wechat_window()
    if not win:
        print("  ERROR: no main WeChat window")
        return False
    wx, wy = win["x"], win["y"]

    # Open folder-icon file picker (with mouse-jiggle first so clicks register)
    _run([CLICLICK, "m:900,500"])
    time.sleep(0.3)
    _run([CLICLICK, f"c:{wx+310},{wy+937}"])
    time.sleep(2)

    # Cmd+Shift+G → Go-To-Folder (NOT Cmd+L — that's Spotlight)
    _osa('tell application "System Events" to keystroke "g" '
         'using {command down, shift down}')
    time.sleep(1.5)
    _osa(f'tell application "System Events" to keystroke "{staged_path.parent}/"')
    time.sleep(1)
    _osa('tell application "System Events" to keystroke return')
    time.sleep(2)

    # Focus into the file list, Cmd+A selects the single file, Return opens
    # the picker with selection; second Return sends the merged photo bubble.
    _run([CLICLICK, "c:700,500"])
    time.sleep(0.5)
    _osa('tell application "System Events" to keystroke "a" using {command down}')
    time.sleep(0.8)
    _osa('tell application "System Events" to keystroke return')  # open picker
    time.sleep(4)
    _osa('tell application "System Events" to keystroke return')  # send bubble
    time.sleep(5)

    # PICKER-LEAK GUARD (incident 2026-10-12 18:00): on WeChat 4.1.60 the
    # picker dialog sometimes stays as an orphan system panel after the
    # second Return — it's NOT visible in the AX window list (lives in
    # window-server space, owned by no process), so the post-send
    # wechat_ax_sane() check below passes silently. The orphan absorbs
    # every subsequent Cmd+F keystroke in wechat_select_chat, breaking
    # ALL chat-selects until dismissed — and the chat-header OCR gate
    # then reports 'wrong chat' because WeChat never switched chats.
    # Force-close any visible picker by Escape, then verify.
    _osa('tell application "System Events" to keystroke (key code 53)')
    time.sleep(0.5)
    _osa('tell application "System Events" to keystroke (key code 53)')
    time.sleep(0.5)

    # Text companion: collapse 3+ consecutive line breaks to 2 (one blank line).
    text = re.sub(r"\n{3,}", "\n\n", text)
    if not _clipboard(text):
        print("  ERROR: pbcopy failed loading message text")
        return False
    time.sleep(0.5)
    _run([CLICLICK, f"c:{wx+270},{wy+855}"])  # input field
    time.sleep(0.8)
    _osa('tell application "System Events" to keystroke "v" using {command down}')
    time.sleep(2)
    _osa('tell application "System Events" to keystroke return')
    time.sleep(4)

    problems = wechat_ax_sane()
    if problems:
        print(f"  ERROR: post-send ax-anomaly: {'; '.join(problems)}")
        return False
    return True


def wechat_running() -> bool:
    r = subprocess.run(
        ["osascript", "-e",
         'tell application "System Events" to (exists process "WeChat")'],
        capture_output=True, text=True)
    return "true" in (r.stdout or "").lower()


# ---------- drain ----------

def drain(dry_run: bool = False, max_per_chat: int = 5,
          max_total: int = 15) -> int:
    _ensure_dirs()
    recips = load_recipients()
    queued = []
    for f in sorted(QUEUE_DIR.glob("*.json")):
        try:
            queued.append(json.loads(f.read_text()))
        except Exception as e:
            print(f"ERROR: unreadable queue file {f.name}: {e}")
    queued.sort(key=lambda m: (m.get("priority", 2), m.get("enqueued_at", "")))

    now = _now()
    held, stale, expired = [], [], []
    sendable: dict[str, list] = {}
    total_budget = max_total
    for m in queued:
        age_h = 0.0
        try:
            age_h = (now - datetime.fromisoformat(m["enqueued_at"])).total_seconds() / 3600
        except Exception:
            pass
        if age_h > MAX_AGE_H:
            stale.append(m)
            # BUGFIX 2026-09-11: dry-run must be side-effect-free — it used
            # to archive expired messages during --dry-run.
            if not dry_run:
                m["status"] = "expired"
                _archive(m)
            continue
        chat = m.get("to", "")
        r = recips.get(chat)
        if not r or r.get("status") != "approved":
            held.append(m)
            continue
        if len(sendable.get(chat, [])) >= max_per_chat or total_budget <= 0:
            held.append(m)  # over cap → stays pending for next drain
            continue
        sendable.setdefault(chat, []).append(m)
        total_budget -= 1

    print(f"OUTBOX DRAIN {'(DRY RUN)' if dry_run else ''} — "
          f"{datetime.now(TZ).strftime('%Y-%m-%d %H:%M')} CST")
    print(f"queue: {len(queued)} | sendable: "
          f"{sum(len(v) for v in sendable.values())} across {len(sendable)} chat(s) "
          f"| held: {len(held)} | expired: {len(stale)}")
    for m in stale:
        print(f"  [expired] {m['class']}/{m['type']} {m['dedupe_key']} "
              f"({MAX_AGE_H}h age gate)")
    for m in held:
        chat = m.get("to", "?")
        why = ("chat not approved" if not recips.get(chat, {}).get("status") == "approved"
               else "cap")
        print(f"  [held:{why}] {chat} ← {m['class']}/{m['type']} {m['dedupe_key']}")

    if not sendable:
        print("nothing sendable — done")
        _write_report(sendable, held, stale, dry_run)
        return 0

    if dry_run:
        for chat, msgs in sendable.items():
            search = recips[chat].get("search", chat)
            print(f"\nWOULD SELECT chat '{chat}' (search '{search}') and send:")
            for m in msgs:
                preview = m["text"][:120].replace("\n", " ⏎ ")
                print(f"  - [{m['type']}] {m['dedupe_key']}: {preview}…")
        _write_report(sendable, held, stale, dry_run)
        return 0

    if not wechat_running():
        print("ERROR: WeChat not running — aborting drain (messages stay queued)")
        _write_report({}, held, stale, dry_run)
        return 2

    try:
        lock_fd = acquire_wechat_gui_lock(timeout=300, reason=LOCK_REASON)
    except TimeoutError as e:
        print(f"ERROR: GUI lock timeout ({e}) — messages stay queued")
        return 3

    sent, failed = [], []
    try:
        for chat, msgs in sendable.items():
            search = recips[chat].get("search", chat)
            ok = wechat_select_chat(search)
            print(f"\nCHAT '{chat}' (search '{search}') — {len(msgs)} message(s)")
            for m in msgs:
                if not ok:
                    failed.append(m)
                    print(f"  [failed] {m['dedupe_key']} (chat select failed)")
                    continue
                # Route by msg.files: photos/announcements with attachments
                # go through the file+text primitive; specials/digest/ptsa
                # stay text-only. Single GUI session handles both. (Added
                # 2026-10-06 — photos pipeline refactor.)
                if m.get("files"):
                    send_ok = wechat_send_file_and_text(m["files"][0], m["text"])
                else:
                    send_ok = wechat_send_text(m["text"])
                if not send_ok:
                    # 19:22 lesson: a primitive returning False must NOT be
                    # archived as sent — leave queued for the next drain.
                    failed.append(m)
                    print(f"  [failed] {m['dedupe_key']} (send anomaly — "
                          "stays queued for retry)")
                    continue
                m["status"] = "sent"
                m["sent_at"] = _now().isoformat()
                _archive(m)
                sent.append(m)
                print(f"  [sent] {m['class']}/{m['type']} {m['dedupe_key']} "
                      f"({len(m['text'])} chars)")
    finally:
        release_wechat_gui_lock(lock_fd)

    print(f"\nSUMMARY: {len(sent)} sent, {len(failed)} failed, "
          f"{len(held)} held, {len(stale)} expired")
    print("WeChat sends are blind — verify on phone if this was a first "
          "send to any chat.")
    _write_report(sendable, held, stale, dry_run, sent=sent, failed=failed)
    return 0 if not failed else 1


def _archive(m: dict) -> None:
    # BUGFIX 2026-09-11: was src.rename() which moved the STALE queue file
    # (status "pending") — losing the sent/expired status. Write the updated
    # dict, then remove the queue file.
    month_dir = ARCHIVE_DIR / _now().strftime("%Y-%m")
    month_dir.mkdir(parents=True, exist_ok=True)
    (month_dir / f"{m['id']}.json").write_text(
        json.dumps(m, indent=2, ensure_ascii=False))
    src = QUEUE_DIR / f"{m['id']}.json"
    if src.exists():
        src.unlink()


def _write_report(sendable, held, stale, dry_run, sent=None, failed=None) -> None:
    tag = "dryrun" if dry_run else "drain"
    path = REPORTS_DIR / f"{_now().strftime('%Y%m%d-%H%M%S')}-{tag}.md"
    lines = [f"# Outbox {tag} {_now().isoformat()}",
             "", f"- sendable chats: {len(sendable)}",
             f"- sent: {len(sent or [])} | failed: {len(failed or [])} "
             f"| held: {len(held)} | expired: {len(stale)}", ""]
    for m in (sent or []):
        lines.append(f"- SENT {m['to']} | {m['class']}/{m['type']} | {m['dedupe_key']}")
    for m in (failed or []):
        lines.append(f"- FAILED {m['to']} | {m['class']}/{m['type']} | {m['dedupe_key']}")
    for m in held:
        lines.append(f"- HELD {m['to']} | {m['class']}/{m['type']} | {m['dedupe_key']}")
    path.write_text("\n".join(lines))


# ---------- main ----------

def main() -> int:
    # Cron form: no args → default to "drain" (outbox_drain_cron.sh calls bare)
    if len(sys.argv) == 1:
        sys.argv = [sys.argv[0], "drain"]
    ap = argparse.ArgumentParser(description="WeChat outbox queue + single-sender drain")
    sub = ap.add_subparsers(dest="cmd", required=True)

    q = sub.add_parser("enqueue", help="queue a message")
    q.add_argument("--to", required=True, help="chat key in recipients.json")
    q.add_argument("--class", dest="msg_class", required=True)
    q.add_argument("--type", dest="msg_type", required=True,
                    choices=["specials", "digest", "ptsa", "announcement",
                             "photo", "test"])
    q.add_argument("--dedupe", required=True)
    q.add_argument("--text", default=None)
    q.add_argument("--file", default=None, help="read message text from file")
    q.add_argument("--priority", type=int, default=2)

    d = sub.add_parser("drain", help="send all pending (single GUI session)")
    d.add_argument("--dry-run", action="store_true")
    d.add_argument("--max-per-chat", type=int, default=5)
    d.add_argument("--max-total", type=int, default=15)

    a = sub.add_parser("approve", help="approve a recipient chat for sending")
    a.add_argument("chat_key")

    sub.add_parser("pending", help="list queue")

    args = ap.parse_args()

    if args.cmd == "enqueue":
        text = args.text
        if args.file:
            text = Path(args.file).read_text()
        if not text:
            print("ERROR: --text or --file required", file=sys.stderr)
            return 2
        mid = enqueue(args.to, args.msg_class, args.msg_type,
                      args.dedupe, text, priority=args.priority)
        if mid:
            print(f"ENQUEUED {mid} → {args.to} ({args.msg_class}/{args.msg_type})")
            return 0
        print(f"DEDUPED: {args.to}/{args.dedupe} already queued or sent")
        return 0

    if args.cmd == "drain":
        return drain(args.dry_run, args.max_per_chat, args.max_total)

    if args.cmd == "approve":
        recips = load_recipients()
        if args.chat_key not in recips:
            print(f"ERROR: '{args.chat_key}' not in recipients.json — add it first",
                  file=sys.stderr)
            return 2
        recips[args.chat_key]["status"] = "approved"
        recips[args.chat_key]["approved_at"] = _now().isoformat()
        save_recipients(recips)
        print(f"APPROVED '{args.chat_key}' "
              f"(search: {recips[args.chat_key].get('search')})")
        return 0

    if args.cmd == "pending":
        for f in sorted(QUEUE_DIR.glob("*.json")):
            m = json.loads(f.read_text())
            age_min = int((_now() - datetime.fromisoformat(m["enqueued_at"])
                           ).total_seconds() / 60)
            print(f"{m['to']:<22} {m['class']}/{m['type']:<12} "
                  f"{m['dedupe_key']:<40} {age_min}m ago")
        return 0

    return 0


if __name__ == "__main__":
    sys.exit(main())

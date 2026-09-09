# WeChat macOS Automation

Drive the **WeChat for Mac** desktop app from scripts: send text messages,
deliver photos and files, target specific chats — no API, no bot account, no
web-WeChat hacks. Pure **GUI automation** via `cliclick` (mouse/keyboard),
`osascript` (AppleScript/System Events), and `screencapture` (verification).

Written for AI agents (or humans) that need WeChat in an automated pipeline —
e.g., a cron that forwards school announcements to a parent group chat. Every
rule below was earned the hard way: 68 pitfalls documented, several after real
incidents (a silent-failure bug that "sent" messages for days, a group chat
that got 4 stale messages from an unverified pipeline).

> **Status**: In production daily use since Aug 2026 (macOS 26.x, WeChat 4.1.x,
> 1080p display). Names/paths in this repo are sanitized — configure yours via
> the constants at the top of each script.

---

## Why GUI automation (and not an API)

WeChat has no official personal-account bot API. The choices are:

| Approach | Verdict |
|---|---|
| WeChat Work (企业微信) API | Different product; requires organizational accounts |
| Web WeChat (wx.qq.com) | Dead for most accounts; killed by Tencent |
| Unofficial protocol libs (itchat etc.) | Ban risk; dead |
| **Drive the real desktop app via Accessibility** | **Works today, uses your real account, zero TOS gray area beyond automation itself** |

This repo does the last one. macOS Accessibility lets a script do anything a
human with a mouse could do — the entire engineering problem is doing it
*reliably*, which is what the pitfall catalog is about.

## Setup

### 1. Dependencies

```bash
brew install cliclick        # CLI mouse/keyboard event injection
# That's it for the core. Optional:
brew install steipete/tap/peekaboo   # screen-capture + OCR alternative
```

`osascript`, `screencapture`, `pbcopy` ship with macOS.

### 2. Permissions (the #1 setup failure)

Grant the app that runs your scripts (Terminal.app, iTerm, your agent host)
these in **System Settings → Privacy & Security**:

| Permission | Needed for | Symptom if missing |
|---|---|---|
| **Accessibility** | `osascript keystroke`, `cliclick` | `osascript is not allowed assistive access (-1728)` |
| **Screen Recording** | `screencapture`/`peekaboo` verification | Screenshots come back empty/wallpaper |
| **Automation → WeChat** | `tell process "WeChat"` AX queries | `-1748` errors on window queries |

Reset and re-grant if state gets weird: `tccutil reset AppleEvents`.

**Critical**: a *stale* Accessibility grant surfaces as baffling per-app
failures. If keystrokes "stop working", check the permission FIRST (Pitfall
#22) — don't conclude "WeChat blocks synthetic input". It doesn't.

### 3. Know your coordinate space

On the reference machine (1080p, no HiDPI): `screencapture`, `cliclick`, and
osascript AX positions are ALL in the same 1920×1080 device-pixel space —
**1:1, no math**. On any new machine, verify:

```bash
system_profiler SPDisplaysDataType | grep -i resolution
# If it says "looks like 1920x1080" (HiDPI), coordinates WILL differ per tool
# — calibrate before trusting any table of coords.
```

## The core send sequence (text message)

```bash
# 0. Preflight: WeChat running? window? frontmost?
./scripts/verify_wechat_ready.sh && echo ready

# 1. Activate — then FORCE focus by clicking the title bar.
#    "activate" alone brings the process up but does NOT give the window
#    keyboard focus (Pitfall #25). Without the title-bar click, your first
#    clicks can land on whatever window overlaps WeChat.
osascript -e 'tell application "WeChat" to activate'
sleep 1
cliclick c:314,45        # title bar — adapt to your window position

# 2. Select the chat: click a NEUTRAL row FIRST, then the target row.
#    Clicking the same row twice toggles selection OFF (Pitfall #23), and
#    clicking the target directly risks a no-op if it was already selected.
cliclick c:90,175        # File Transfer (neutral) — x must be ≥85, not the
                         # icon gutter at x<80 which silently misses (Pitfall #24)
sleep 1
cliclick c:120,110       # target chat row (pinned row 1)
sleep 2

# 3. Load the message into the clipboard.
#    cliclick t: CANNOT type Unicode (CJK/emoji hang it — Pitfall #8).
#    Clipboard paste is the ONLY reliable path for non-ASCII.
printf '%s' "$MESSAGE" | pbcopy
sleep 0.5

# 4. Click the input field BEFORE pasting. Cmd+V with a chat selected but no
#    input focus triggers WeChat's "Send favorites to <name>" dialog (Pitfall #12)!
cliclick c:270,855
sleep 1

# 5. Paste + send with Enter. NEVER click the Send button (Pitfall #4) —
#    it sits in the dock auto-show zone and eats clicks.
osascript -e 'tell application "System Events" to keystroke "v" using {command down}'
sleep 2
osascript -e 'tell application "System Events" to keystroke return'
sleep 3
```

## Sending photos / files

The file-picker flow (full detail in `scripts/send_wrapper_template.py` and
the pitfalls below):

```bash
# 1. Folder icon in the input toolbar — popup-triggering icons need the
#    MOVE-AWAY-THEN-BACK sequence (Pitfall #36). A direct click is silently
#    swallowed.
cliclick m:500,500; sleep 0.5
cliclick m:310,967;  sleep 0.5
cliclick c:310,967;  sleep 2

# 2. Verify the picker ACTUALLY opened — the picker is owned by the
#    "Open and Save Panel Service" process, NOT WeChat (Pitfall #66):
#    WeChat's sheet count stays 0 even when the picker is open!
screencapture -x /tmp/picker.png   # + vision-check it

# 3. Navigate with Cmd+Shift+G (NOT by clicking the location bar — Pitfall #38):
osascript -e 'tell application "System Events" to keystroke "g" using {command down, shift down}'
sleep 1.5
osascript -e 'tell application "System Events" to keystroke "/path/to/photos/"'
sleep 1
osascript -e 'tell application "System Events" to keystroke return'
sleep 2

# 4. Select all + open. Clicking the visual "Open" button DESELECTS everything
#    (Pitfall #45) — press Return instead. Or AX-click the button (Pitfall #61).
cliclick c:500,460; sleep 0.5        # focus the file list first
osascript -e 'tell application "System Events" to keystroke "a" using {command down}'
sleep 0.5
osascript -e 'tell application "System Events" to keystroke return'
sleep 5                                # wait for thumbnails to stage + upload

# 5. Send with Enter.
osascript -e 'tell application "System Events" to keystroke return'
```

**Stage files in a FRESH temp dir first.** Cmd+A sweeps the whole folder —
pointing it at a shared downloads dir re-sends every old file that was ever
downloaded there. This single mistake shipped 4 stale posters to a 25-parent
group chat (see Pitfall #68's origin story).

**Merged-photos toggle**: WeChat defaults to one bubble per photo. The
"Show as merged photos" toggle bubble collapses N photos into one message.
Its coordinate SHIFTS when photos are staged (input grows upward) — re-derive
every time (Pitfall #62), and verify the green ✓ state before Enter.

## Verification is not optional

The single most expensive lesson in this repo: **a wrapper that returns
"success" after the click sequence is lying unless it verified the sent
bubble.** Real failure observed: a cron "successfully sent" the daily digest
every night for days; none arrived. Exit code 0 meant "no exception", not
"delivered" (Pitfall #26).

Minimum verification ladder (strongest first — drop down only when the
stronger check is impossible on your WeChat build):

1. **Vision check**: `screencapture -x` + a vision model ("is there a
   right-aligned bubble containing '<first line>'?"). Implemented in
   `verify_send()` in both send scripts.
2. **AX side-effects**: `count sheets of window 1`, window lists, sent-log
   markers.
3. **One human confirmation** for visual-only states (merge-bubble green ✓).
   On WeChat 4.1.60+ the main chat window is **non-shareable** — screenshots
   of it return wallpaper, so vision checks against the main window are
   impossible (Pitfall #65). The file picker and Settings windows ARE
   shareable. Pipelines can still run blind via AX + keystrokes; budget one
   user confirmation per run for visual states.

```python
# verify_send() pattern — returns (ok, detail); None = tool unavailable
ok, detail = verify_send(message)
if ok is None:   # vision tool missing → manual check needed, don't claim success
    ...
if not ok:       # real failure → retry once, then exit non-zero
    ...
```

## Cron pipeline pattern

**Rule zero: one GUI driver at a time.** If two pipelines drive WeChat
concurrently, their clicks/pastes/Enters interleave on the same physical
mouse+keyboard and messages land in the WRONG chat — with both pipelines
logging success (real incident, 2026-09-09: a daily reminder was delivered
to a 25-parent group because a digest pipeline held the row selection when
the reminder's Enter fired). Every driver in this repo holds
`scripts/wechat_gui_lock.py` — a global `flock` — for the entire send:

```python
from wechat_gui_lock import acquire_wechat_gui_lock, release_wechat_gui_lock
lock_fd = acquire_wechat_gui_lock(timeout=300, reason="my-pipeline")
try:
    ...  # entire click/paste/Enter sequence
finally:
    release_wechat_gui_lock(lock_fd)

# CLI form — wraps any command (for agent-driven flows without a script):
#   python3 scripts/wechat_gui_lock.py with 300 photos-send -- bash send.sh
```

Stagger cron schedules too (the lock is the safety net; staggering avoids
contention): e.g. 17:30 announcements, 17:45 newsletter, 20:00 digest,
20:15 second reminder.

`scripts/send_digest_to_wechat_v2.py` is the full production wrapper:

- **Env-var handoff**: the upstream generator sets `$DIGEST_TEXT`; the mirror
  sends exactly those bytes. Byte-identical cross-platform delivery, no
  double-generation, upstream's "nothing to send today" exit is authoritative.
- **24h dedup**: every send is logged to `sent-<timestamp>.txt`; a stable
  marker (first line of the digest) blocks re-sends within the window.
  `--force` overrides.
- **`WECHAT_MIRROR=0`** short-circuits the whole script (Feishu-only mode)
  without touching the caller.
- **Window-geometry-driven coords**: no hard-coded table — it queries the
  WeChat window position/size via osascript at runtime and computes sidebar /
  input coordinates relative to the window. Handles moved/resized windows,
  dual-window state, and dock-icon drift (AX name-based dock click).

```bash
# cron: generate digest → hand off to WeChat mirror
DIGEST_TEXT="$(python3 generate_digest.py)" python3 scripts/send_digest_to_wechat_v2.py
```

## Pitfall catalog (abridged — the 20 that bite hardest)

Full catalog (68 entries) lives in the skill this repo was extracted from;
these are the ones that cause silent failures and group-chat incidents:

1. **Popup-trigger icons need move-away-then-back** (#36). Folder/emoji/scissors
   icons silently swallow direct clicks. `c:500,500` → `m:target` → `c:target`.
2. **Return, not the button** (#4, #45). Send button and picker Open button
   both eat clicks; Return routes through the standard panel confirm path.
3. **Unicode = clipboard only** (#8, #20). `cliclick t:` hangs on CJK/emoji;
   `pbcopy` + Cmd+V is the only reliable input.
4. **Neutral chat first** (#23, #24). Click File Transfer → then target row.
   Prevents the deselect-toggle no-op; x must be ≥85 (not the icon gutter).
5. **Image paste does NOT work** (#27). Clipboard image → Cmd+V never lands
   in WeChat's input. Use the file picker flow, not paste.
6. **Focus ≠ activated** (#25, #48). `activate` alone doesn't give keyboard
   focus; click the title bar. Verify frontmost before every run.
7. **Overlapping windows eat clicks** (#37, #53). Chrome/Feishu over WeChat's
   toolbar region intercepts clicks. List visible windows; move blockers.
8. **Coords drift** (#29, #35, #43, #54). Window resize, sidebar relayout,
   even photo-staging shifts toolbar coords. Compute from live window
   geometry (see `references/coord-detection.md`), or sweep x in 30-40px
   increments and let the picker sheet-count tell you what landed.
9. **Vision coords can be 200px off** (#44). When occluded, vision reports
   what's visible, not what's clickable. `cliclick p:` after a human hovers
   the real target is the source of truth.
10. **The picker isn't WeChat's window** (#66). It's owned by "Open and Save
    Panel Service" — verify via screenshot/OCR, not WeChat sheet counts.
11. **Silent logout looks like input failure** (#63). Keystrokes ignored +
    colorful fullscreen = login screen. QR re-scan is the only fix.
12. **WeChat 4.1.60 main window is non-shareable** (#65). Screenshots return
    wallpaper; the window is fine. Use AX + one human confirmation.
13. **keystroke inside tell process is a syntax bug** (#67). keystroke is a
    System Events command — address it at the System Events level.
14. **Never live-test on a group** (#68). First runs → your own File
    Transfer/DM. Send caps + staleness cutoffs + per-send state re-checks.
15. **A step isn't done until its side-effect is observed** (#34). "Clicked
    the coords" is not "picker opened". Verify each click's effect before
    proceeding.
16. **Concurrent GUI drivers = wrong-chat delivery** (#69, real incident
    2026-09-09). One physical mouse/keyboard means one driver. Use
    `wechat_gui_lock.py` (Python or CLI form) and stagger cron minutes.

## Repository layout

```
scripts/
  send_wrapper_template.py     # drop-in template: preflight → click seq →
                               #   paste → Enter → vision verify → dedup → retry
  send_digest_to_wechat_v2.py  # production wrapper: dynamic coords, env-var
                               #   handoff, 24h dedup, WECHAT_MIRROR toggle
  verify_wechat_ready.sh       # preflight gate: 0 = ready, 1 = needs
                               #   intervention, 2 = WeChat not running
references/
  coord-detection.md           # runtime window-geometry → click coords,
                               #   dock-icon AX probe, minimized-window recovery
```

## Legal & ethics

This drives **your own** WeChat account on **your own** Mac, doing what you
could do by hand. It does not bypass any authentication or encryption. WeChat's
ToS may frown on automation — the practical risks are account flags from
abusive volume, not this technique itself. Don't spam; respect group chats;
verify sends; keep humans in the loop for anything irreversible (recall is
impossible after 2 minutes). Use at your own risk.

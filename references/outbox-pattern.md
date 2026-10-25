# Outbox + Drain Architecture

Detailed reference for the producer/drain pattern used by all WeChat
pipelines since 2026-09-15. Complements the README's high-level
overview.

## Why

A single pipeline driving the WeChat GUI for one-off sends is fine. With
**five** pipelines (specials, daily announcements, photos, PTSA,
digests) all wanting to send at overlapping times, the GUI lock alone
isn't enough — you also need:

1. **Decoupling** — pipelines that don't know about each other
2. **Dedup** — no double-sends if a producer runs twice
3. **Backpressure** — if the drain fails, the queue grows; if the queue
   grows, alarms fire (not silent failure)
4. **Observability** — every send has a JSON record of what was supposed
   to go where

The outbox is the standard solution. Producers write JSON; the drain
sends.

## Directory layout

```
~/workspace/wechat-outbox/
  queue/           # pending sends (the only state the drain reads)
  archive/         # sent log, organized by month
  reports/         # drain run logs (stdout)
  recipients.json  # recipient key → WeChat chat name + display + role
```

### recipients.json

```json
{
  "5cs-rp": {
    "chat_name": "5XX Class Chat",
    "display": "5CS",
    "role": "primary",
    "approved": true,
    "approved_at": "2026-09-15"
  },
  "5xx-rp": {
    "chat_name": "5XX Name2",
    "display": "5FM",
    "role": "mirror",
    "approved": true
  },
  "file-transfer": {
    "chat_name": "File Transfer",
    "display": "[user] (self)",
    "role": "test",
    "approved": true
  }
}
```

`approved: true` is required for the drain to send. New recipients must
be reviewed and `approved: true` set before any producer can target them.

### Queue file

```json
{
  "id": "obx-<uuid12>",
  "to": "5cs-rp",
  "class": "5CS",
  "type": "specials",
  "dedupe_key": "specials:2026-10-08:5cs",
  "text": "...",
  "files": ["/path/to/photo.jpg"],
  "priority": 2,
  "enqueued_at": "ISO8601",
  "status": "pending"
}
```

| Field | Required | Notes |
|---|---|---|
| `id` | yes | UUID-12; used as filename and in archive |
| `to` | yes | Recipient key (must exist in recipients.json + approved) |
| `class` | yes | Logical class (`5CS`/`5SW`/`5FM`/etc) — informational |
| `type` | yes | Message type for routing |
| `dedupe_key` | yes | Producer-defined; outbox dedupes on (to, dedupe_key) |
| `text` | yes | Message body (Unicode-safe via clipboard paste) |
| `files` | no | List of absolute file paths; empty for text-only |
| `priority` | no | 1 (urgent) / 2 (normal) / 3 (low); default 2 |
| `enqueued_at` | yes | Auto-filled on enqueue |
| `status` | auto | `pending` → `sent` (after drain) or `expired` |

### msg_type taxonomy

| Type | Producer(s) | Notes |
|---|---|---|
| `specials` | `specials_calendar.py` | Daily rotation reminder |
| `digest` | `unified_digest.py` | Weekly unified digest |
| `ptsa` | `ptsa_prep_reminder.py` | PTSA meeting prep |
| `announcement` | `seesaw_announcements_pipeline.py` | Seesaw Messages inbox |
| `photo` | `seesaw_photos_pipeline.py` | Seesaw feed posts |
| `test` | manual | Test sends; always to `file-transfer` |

`type` doesn't change the send mechanism today — it's used for logging
and future routing (e.g. a photos drain that respects quiet hours).

## Drain lifecycle

```
drain.py --max-per-chat 5 --max-total 30
  │
  ├── 1. acquire_wechat_gui_lock(timeout=300)
  │
  ├── 2. for each pending queue file (sorted by priority, enqueued_at):
  │     ├── 2a. resolve recipient (recipients.json lookup; skip if !approved)
  │     ├── 2b. select chat (wechat_select_chat(chat_name))
  │     │     └── if header doesn't match → log error, skip (msg stays queued)
  │     ├── 2c. send (file+text primitive OR text-only)
  │     │     └── on send success → move to archive/{YYYY-MM}/
  │     │     └── on send failure → leave in queue, continue
  │     ├── 2d. honor --max-per-chat cap (move remainder back to top of queue)
  │     └── 2e. honor --max-total cap (stop drain; remaining waits for next run)
  │
  └── 3. release lock
```

## Post-select header gate (the fail-closed safety net)

Every `wechat_select_chat(search)` call performs a **post-select header
verification**: after the keystrokes finish, the drain OCRs the chat
header at the top-right of the WeChat window and compares to the
expected chat name. If they don't match, the function returns `False`
and the drain **does not paste**.

```python
def wechat_select_chat(search: str) -> bool:
    """Select a chat by name; verify post-select via header OCR."""
    # ... keystrokes: Cmd+F, paste, Return ...
    time.sleep(2.0)
    header = _wechat_chat_header()  # OCR top-right of WeChat window
    return _header_matches(search, header)


def _header_matches(search: str, header: str) -> bool:
    """Token-overlap match. Lenient — accepts substring of any token ≥4 chars."""
    s_tokens = [t for t in re.split(r'\W+', search.lower()) if len(t) >= 4]
    h = header.lower()
    return any(t in h for t in s_tokens)
```

This catches:

- `Cmd+F` opening Search Chat History (Pitfall #71) instead of Search
  Chats — header stays at the previous chat
- Direct row-click mis-targeting the wrong chat
- WeChat focus loss mid-select

If the gate fires, the message stays in the queue. It is NOT lost.
The next drain run retries it.

## Cron schedule (the canonical pattern)

| Time | Job | Why this slot |
|---|---|---|
| 07:50 | drain (morning) | Clears anything queued overnight |
| 17:10 | 7 producers (Sun-Thu) | Compute + enqueue; no GUI |
| **17:20** | **drain (evening)** | **Before announcements; releases GUI lock before 17:30** |
| 17:30 | announcements cron | Old direct-GUI send (still works in parallel) |
| 20:00 | digest | Weekly unified |

**Critical**: 17:20 drain must complete (or fail) before 17:30
announcements. Real incident 2026-09-16: drain at 17:55 held Chrome
focus, announcements at 17:35 couldn't bring WeChat frontmost. Two
days in a row. Now: 17:10 producers → 17:20 drain → 17:30
announcements.

## Stale queue recovery

If a queue file sits for >24h, it's probably stuck (recipient typo,
sender bug, recipient removed). The drain's `--expire-hours N` flag
(default 24) moves such files to `archive/expired/` with a reason.
Inspect before manually retrying.

## Dedupe

The outbox dedupes on `(to, dedupe_key)`. If a producer writes a queue
file with the same key for the same recipient within the dedupe window
(default 7 days), the second write is **silently skipped**.

Producers should generate stable `dedupe_key`s:

- `specials:YYYY-MM-DD:<class>` — daily specials reminder
- `announcement:<seesaw_msg_id>` — each Seesaw message once
- `photo:<seesaw_item_id>` — each Seesaw feed post once
- `digest:YYYY-Www` — weekly digest by ISO week

A dedupe_key that's too tight (e.g. includes a timestamp that changes
each run) defeats dedupe — every run re-enqueues.

## CLI examples

```bash
# Enqueue (from a producer script)
python3 scripts/wechat_outbox.py enqueue \
    --to 5cs-rp \
    --class 5CS \
    --type specials \
    --dedupe-key "specials:2026-10-08:5cs" \
    --text "$(cat /tmp/specials.txt)"

# Enqueue with file
python3 scripts/wechat_outbox.py enqueue \
    --to 5cs-rp \
    --class 5CS \
    --type photo \
    --dedupe-key "photo:item.58ff67ba" \
    --text "$TEXT" \
    --files /tmp/photo.jpg

# Drain (from cron)
python3 scripts/wechat_outbox.py drain \
    --max-per-chat 5 \
    --max-total 30 \
    --expire-hours 24

# Inspect queue
ls ~/workspace/wechat-outbox/queue/ | head
cat ~/workspace/wechat-outbox/queue/obx-*.json | jq .

# Inspect archive (last week's sends)
ls ~/workspace/wechat-outbox/archive/2026-10/
```

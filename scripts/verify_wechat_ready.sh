#!/usr/bin/env bash
# verify_wechat_ready.sh — preflight check that returns 0 if WeChat is
# foregrounded and visible. Returns 1 if WeChat needs intervention,
# 2 if WeChat is not running at all. Use this in cron wrappers to fail
# fast when WeChat is unavailable.
#
# Usage:
#   verify_wechat_ready.sh && python3 send_wrapper.py || echo "skipping send"

set -e

# 1. Is WeChat process even running?
if ! pgrep -x "WeChat" > /dev/null; then
    echo "WeChat process not running" >&2
    exit 2
fi

# 2. Does WeChat have any windows?
WINDOWS=$(osascript -e 'tell application "System Events" to tell process "WeChat" to get name of every window' 2>/dev/null || echo "")
if [ -z "$WINDOWS" ]; then
    echo "WeChat has no windows (minimized)" >&2
    exit 1
fi

# 3. Is WeChat the foreground app?
FRONT=$(osascript -e 'tell application "System Events" to get name of first application process whose frontmost is true' 2>/dev/null || echo "")
if [ "$FRONT" != "WeChat" ]; then
    echo "WeChat is running but not foreground (currently: $FRONT)" >&2
    exit 1
fi

echo "WeChat ready: foreground + has window"
exit 0
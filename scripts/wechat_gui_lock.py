#!/usr/bin/env python3
"""wechat_gui_lock.py — mutual exclusion for ALL WeChat GUI drivers.

INCIDENT (real, 2026-09-09): two cron pipelines (a digest mirror and a
daily reminder) drove the same WeChat instance concurrently. Their
click/paste/Enter sequences interleaved; one pipeline's Enter fired while
the other pipeline's target group was selected → wrong-group delivery. Both scripts logged
success. One physical GUI = one driver at a time, enforced by this file.

Usage (in any script that drives WeChat via cliclick/osascript):

    from wechat_gui_lock import acquire_wechat_gui_lock, release_wechat_gui_lock
    lock_fd = acquire_wechat_gui_lock(timeout=300, reason="my-reminder")
    try:
        ... click/paste/Enter sequence ...
    finally:
        release_wechat_gui_lock(lock_fd)

The lock is an fcntl.flock on a lock file: released automatically if the
process crashes; blocks up to `timeout` s waiting for the current driver.
"""
import errno
import fcntl
import os
import time
from pathlib import Path

LOCK_PATH = Path(os.environ.get(
    "WECHAT_GUI_LOCK",
    Path.home() / ".wechat_gui.lock",
))


def acquire_wechat_gui_lock(timeout: float = 300, poll: float = 2,
                            reason: str = "") -> int:
    """Acquire the global WeChat GUI lock. Blocks up to `timeout` seconds.

    Returns an fd; pass it to release_wechat_gui_lock() when done.
    Raises TimeoutError if another driver holds the lock too long.
    """
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(LOCK_PATH), os.O_CREAT | os.O_RDWR, 0o600)
    start = time.time()
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            os.ftruncate(fd, 0)
            os.write(fd, f"{os.getpid()} {reason} {time.strftime('%F %T')}\n"
                     .encode("utf-8"))
            return fd
        except OSError as e:
            if e.errno not in (errno.EACCES, errno.EAGAIN):
                raise
            if time.time() - start > timeout:
                os.close(fd)
                raise TimeoutError(
                    f"WeChat GUI lock held by another driver for >{timeout}s "
                    f"(lock file: {LOCK_PATH})")
            time.sleep(poll)


def release_wechat_gui_lock(fd: int) -> None:
    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


if __name__ == "__main__":
    # CLI form for agent-driven (non-script) WeChat automation:
    #   python3 wechat_gui_lock.py with <timeout> <reason> -- <command...>
    # Holds the lock for the duration of <command>, e.g.:
    #   python3 wechat_gui_lock.py with 300 photos-send -- bash send_photos.sh
    import subprocess as _sp
    import sys as _sys
    if len(_sys.argv) >= 4 and _sys.argv[1] == "with":
        _timeout = float(_sys.argv[2])
        _reason = _sys.argv[3]
        dash = _sys.argv.index("--") if "--" in _sys.argv else 4
        cmd = _sys.argv[dash + 1:]
        if not cmd:
            print("usage: wechat_gui_lock.py with <timeout> <reason> -- <cmd...>",
                  file=_sys.stderr)
            _sys.exit(2)
        fd = acquire_wechat_gui_lock(timeout=_timeout, reason=_reason)
        try:
            r = _sp.run(cmd)
            _sys.exit(r.returncode)
        finally:
            release_wechat_gui_lock(fd)
    print("usage: wechat_gui_lock.py with <timeout> <reason> -- <cmd...>",
          file=_sys.stderr)
    _sys.exit(2)

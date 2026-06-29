#!/usr/bin/env python3
"""
Claude Code Stop hook — sends a Telegram notification when a task completes
in an interactive CLI session, but only if the terminal window is not focused.

Registered in ~/.claude/settings.json under hooks.Stop.
Claude Code calls this script after every response, passing JSON on stdin:
  {
    "session_id": "...",
    "cwd": "/path/to/project",
    "hook_event_name": "Stop",
    "stop_hook_active": false,
    "last_assistant_message": "..."
  }
"""

import ctypes
import json
import os
import subprocess
import sys
from ctypes import wintypes
from pathlib import Path

# Minimum characters in the response to bother sending a notification.
# Filters out trivial one-liners like "Sure!" or "Done."
MIN_RESPONSE_LEN = 80

NOTIFIER = Path(__file__).parent.parent / "notifier.py"

# How to announce a completed task in an interactive CLI session:
#   "telegram" → send a Telegram message via notifier.py (default)
#   "bell"     → ring the terminal bell (WezTerm shows its visual bell), no Telegram
# Both paths are kept fully intact — set this to "bell" to use the terminal
# bell instead of Telegram notifications.
NOTIFY_CHANNEL = "telegram"


# ──────────────────────────────────────────────────────────────────────────────
# Windows focus check (no external deps)
# ──────────────────────────────────────────────────────────────────────────────

class _PROCESSENTRY32(ctypes.Structure):
    """Win32 PROCESSENTRY32 — layout matches MSVC 64-bit ABI."""
    _fields_ = [
        ("dwSize",              wintypes.DWORD),
        ("cntUsage",            wintypes.DWORD),
        ("th32ProcessID",       wintypes.DWORD),
        ("th32DefaultHeapID",   ctypes.c_size_t),   # ULONG_PTR, 8 bytes on x64
        ("th32ModuleID",        wintypes.DWORD),
        ("cntThreads",          wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD),
        ("pcPriClassBase",      wintypes.LONG),
        ("dwFlags",             wintypes.DWORD),
        ("szExeFile",           ctypes.c_char * 260),
    ]


def _parent_pid_map() -> dict[int, int]:
    """Return {pid: parent_pid} for all running processes."""
    TH32CS_SNAPPROCESS = 0x00000002
    k32 = ctypes.windll.kernel32
    snap = k32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snap == wintypes.HANDLE(-1).value:
        return {}

    result: dict[int, int] = {}
    entry = _PROCESSENTRY32()
    entry.dwSize = ctypes.sizeof(_PROCESSENTRY32)
    try:
        if k32.Process32First(snap, ctypes.byref(entry)):
            result[entry.th32ProcessID] = entry.th32ParentProcessID
            while k32.Process32Next(snap, ctypes.byref(entry)):
                result[entry.th32ProcessID] = entry.th32ParentProcessID
    finally:
        k32.CloseHandle(snap)
    return result


def is_terminal_focused() -> bool:
    """Return True if any process in our process tree owns the foreground window."""
    user32 = ctypes.windll.user32
    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        return False

    fg_pid = wintypes.DWORD(0)
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(fg_pid))
    fg_pid = fg_pid.value
    if fg_pid == 0:
        return False

    parent_map = _parent_pid_map()
    current = os.getpid()
    visited: set[int] = set()
    while current and current not in visited:
        if current == fg_pid:
            return True
        visited.add(current)
        current = parent_map.get(current, 0)
    return False


# ──────────────────────────────────────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────────────────────────────────────

def _escape_md(text: str) -> str:
    """Minimal Telegram legacy Markdown escaping for inline text."""
    out = []
    for ch in text:
        if ch in r"\*_`[]()":
            out.append("\\")
        out.append(ch)
    return "".join(out)


def ring_terminal_bell() -> None:
    """Ring the terminal bell so WezTerm fires its (visual) bell.

    The hook's stdout/stderr are captured by Claude Code and never reach the
    terminal, so writing a BEL there would be swallowed. Instead we open the
    Windows console device (CONOUT$) directly — it is attached to the same
    ConPTY that WezTerm drives, so the BEL (0x07) actually gets through.
    """
    try:
        with open("CONOUT$", "w") as con:
            con.write("\a")
            con.flush()
    except OSError:
        # Fallback: stderr may still reach the terminal in some setups.
        sys.stderr.write("\a")
        sys.stderr.flush()


def send_telegram(data: dict, last_msg: str) -> None:
    """Forward a completion notification to Telegram via notifier.py."""
    # Build a readable preview (first ~200 chars, stripped of leading whitespace)
    preview = last_msg.strip()[:200]
    if len(last_msg.strip()) > 200:
        preview += "…"

    # Show the working directory as context so the user knows which project.
    cwd = data.get("cwd", "")
    cwd_label = ""
    if cwd:
        cwd_label = f"\n📁 `{_escape_md(os.path.basename(cwd) or cwd)}`"

    msg = (
        f"🔔 *Claude Code fertig*{cwd_label}\n\n"
        f"{_escape_md(preview)}"
    )

    subprocess.run(
        [sys.executable, str(NOTIFIER), msg],
        capture_output=True,
        timeout=15,
    )


def main() -> None:
    raw = sys.stdin.read()
    try:
        data = json.loads(raw)
    except Exception:
        sys.exit(0)

    # Guard: stop_hook_active prevents infinite loops when Claude continues
    # processing because of this hook's output.
    if data.get("stop_hook_active"):
        sys.exit(0)

    last_msg: str = data.get("last_assistant_message", "")

    # Skip trivially short responses (acknowledgments, single-word replies, etc.)
    if len(last_msg) < MIN_RESPONSE_LEN:
        sys.exit(0)

    if NOTIFY_CHANNEL == "telegram":
        # Don't notify if the user is already looking at this terminal window.
        if is_terminal_focused():
            sys.exit(0)
        send_telegram(data, last_msg)
    else:
        # Terminal bell: ring on every non-trivial completion. No focus guard —
        # a visual bell is only perceptible when you ARE looking at the window.
        ring_terminal_bell()

    sys.exit(0)


if __name__ == "__main__":
    main()

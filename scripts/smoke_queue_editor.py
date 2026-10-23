"""Smoke test of the dashboard queue editor (B1) over real HTTP -- against a TEMP queue only.

Before any project import ORCH_QUEUE_FILE / ORCH_QUEUE_BACKUP_DIR / ORCH_VAULT_PATH are pointed at a
fresh temp directory; the REAL queue is only read (SHA256 before and after, exit 1 if it changed).
Steps: GET /api/queue, lint, add, file check (CRLF kept), foreign change + edit with a stale ref
(409), delete, undo (byte-identical). Prints one PASS/FAIL line per step. Exit 0 = all PASS.

Usage: python scripts/smoke_queue_editor.py
"""

from __future__ import annotations

import contextlib
import hashlib
import http.client
import importlib
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
FIXTURE = (
    b"## Queue\r\n- [ ] Alpha\r\n- [ ] Beta\r\n"
    b"- [x] Fertig \xe2\x9c\x85 2026-09-01 10:00 (claude)\r\n"
)
STALE_TEXT = "Inzwischen geändert — neu laden."
results: list[tuple[str, bool, str]] = []


def real_queue_path() -> Path:
    """Where config would look for the real queue (separate process, so this one stays clean)."""
    code = "import config; print(config.QUEUE_FILE)"
    out = subprocess.run([sys.executable, "-X", "utf8", "-c", code], cwd=ROOT,
                         capture_output=True, text=True, timeout=60, check=False)
    return Path(out.stdout.strip().splitlines()[-1]) if out.returncode == 0 and out.stdout.strip() else Path()


def real_sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else "missing"


def step(name: str, ok: bool, info: str = "") -> bool:
    results.append((name, ok, info))
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  ({info})" if info else ""))
    return ok


class Client:
    def __init__(self, port: int, token: str) -> None:
        self.port, self.token = port, token

    def call(self, method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
        h = {"Host": f"127.0.0.1:{self.port}"}
        data = None
        if method == "POST":
            data = json.dumps(body or {}).encode()
            h |= {"Origin": f"http://127.0.0.1:{self.port}", "X-Orch-Token": self.token,
                  "Sec-Fetch-Site": "same-origin", "Content-Type": "application/json",
                  "Content-Length": str(len(data))}
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=15)
        try:
            conn.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
            for k, v in h.items():
                conn.putheader(k, v)
            conn.endheaders(data)
            r = conn.getresponse()
            text = r.read().decode("utf-8", "replace")
            return r.status, (json.loads(text) if text.startswith("{") else {"_text": text})
        finally:
            conn.close()


def run(tmp: Path) -> None:
    qfile = tmp / "agent-queue.md"
    qfile.write_bytes(FIXTURE)
    dashboard = importlib.import_module("dashboard")  # only now: the env vars above must come first
    queue_manager: Any = importlib.import_module("queue_manager")

    queue_manager.QUEUE_EVENTS_LOG_FILE = tmp / "queue-events.log"  # bound at import: redirect by hand
    if not step("Fixture: nur die tmp-Queue ist aktiv", queue_manager.queue_file().resolve() == qfile.resolve()):
        return
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server, port = dashboard._bind_server(port)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        status, page = Client(port, "").call("GET", "/")
        m = re.search(r'name="orch-token" content="([^"]+)"', page.get("_text", ""))
        if not step("GET / liefert den Schlüssel", status == 200 and m is not None) or m is None:
            return
        c = Client(port, m.group(1))
        status, q = c.call("GET", "/api/queue")
        step("GET /api/queue", status == 200 and q.get("file", {}).get("eol") == "crlf"
             and q.get("counts", {}).get("offen") == 2, f"HTTP {status}, offen={q.get('counts', {}).get('offen')}")
        t0 = time.perf_counter()
        status, lint = c.call("POST", "/api/queue/lint", {"fields": {"text": "DONE-2"}})
        ms = (time.perf_counter() - t0) * 1000
        step("POST lint (erste Anfrage)", status == 200 and lint.get("valid") is not False, f"{ms:.0f} ms")
        status, res = c.call("POST", "/api/queue/add", {"fields": {"text": "DONE-2"}, "position": "top"})
        data = qfile.read_bytes()
        crlf_only = data.count(b"\n") == data.count(b"\r\n")
        step("POST add", status == 200 and res.get("changed") is True
             and data == b"## Queue\r\n- [ ] DONE-2\r\n" + FIXTURE.split(b"\r\n", 1)[1],
             f"HTTP {status}, CRLF {'ja' if crlf_only else 'nein'}")
        ref = next((e["ref"] for e in res.get("queue", {}).get("entries", []) if e["text"] == "DONE-2"), None)
        qfile.write_bytes(data.replace(b"DONE-2", b"DONE-2 extern"))  # simulates Obsidian
        status, err = c.call("POST", "/api/queue/edit", {"ref": ref, "text": "DONE-2 neu"})
        step("POST edit mit altem ref -> 409", status == 409 and err.get("error") == STALE_TEXT
             and b"DONE-2 extern" in qfile.read_bytes(), f"HTTP {status}")
        q = c.call("GET", "/api/queue")[1]
        ref = next((e["ref"] for e in q.get("entries", []) if e["text"] == "DONE-2 extern"), None)
        before_delete = qfile.read_bytes()
        status, res = c.call("POST", "/api/queue/delete", {"ref": ref, "confirm_unknown_running": True})
        step("POST delete (standalone fragt nach)", status == 200 and b"DONE-2" not in qfile.read_bytes(), f"HTTP {status}")
        undo = res.get("queue", {}).get("undo", {})
        status, _ = c.call("POST", "/api/queue/undo", {"backup_id": undo.get("id", ""), "confirm_unknown_running": True})
        step("POST undo -> byte-gleich wie vor delete", status == 200 and qfile.read_bytes() == before_delete,
             f"HTTP {status}")
        backups = list((tmp / "queue-backups").glob("*.json"))
        step("Sicherungen im tmp-Ordner", len(backups) >= 3, f"{len(backups)} Paare")
    finally:
        server.shutdown()
        server.server_close()


def main() -> int:
    real = real_queue_path()
    before = real_sha(real)
    tmp = Path(tempfile.mkdtemp(prefix="smoke_queue_editor_"))
    os.environ["ORCH_QUEUE_FILE"] = str(tmp / "agent-queue.md")
    os.environ["ORCH_QUEUE_BACKUP_DIR"] = str(tmp / "queue-backups")
    os.environ["ORCH_VAULT_PATH"] = str(tmp)
    sys.path.insert(0, str(ROOT))
    try:
        run(tmp)
    except Exception as exc:
        step("Lauf ohne Ausnahme", False, f"{type(exc).__name__}: {exc}")
    finally:
        with contextlib.suppress(Exception):
            shutil.rmtree(tmp, ignore_errors=True)
    after = real_sha(real)
    intact = before == after
    step("Echte Queue unverändert", intact, f"{real} sha {before[:12]}")
    if not intact:
        print("!!! ECHTE QUEUE WURDE VERÄNDERT !!!", file=sys.stderr)
    ok = intact and all(r[1] for r in results)
    print(f"{'ALLE PASS' if ok else 'FEHLER'}: {sum(r[1] for r in results)}/{len(results)} Schritte")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())

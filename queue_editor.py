"""Queue-Editor (HTTP-frei): Lesen/Zustaende, Zeilenbau/Pruefung, Mutationen und Sicherungen.

Zerlegt agent-queue.md wie der Orchestrator (``splitlines``), bildet Bloecke
(Zeile + eingerueckte Folgezeilen bis zur Leerzeile), klassifiziert jeden Block und
baut die Antwort fuer ``GET /api/queue``. Sektionslogik, Faelligkeit und ``#needs:``
sind NICHT nachgebaut, sondern kommen aus ``queue_manager`` (dieselben Helfer wie
``read_queue_items``), damit Editor und Orchestrator sich nicht widersprechen
koennen. Zustandsnamen sind ASCII (``laeuft`` statt ``läuft``).

Aktionen (``apply_edit``): add, edit, delete, retry, duplicate, move, undo, raw. ``lint_request``
prueft einen Entwurf, ohne zu schreiben. Vor jeder Aenderung entsteht eine Sicherung
(``logs/queue-backups``, nur die neueste ist per undo erreichbar); die Laufsperre schuetzt
Zeilen, die der Orchestrator gerade bearbeitet.
"""

from __future__ import annotations

import difflib
import hashlib
import importlib
import json
import logging
import os
import re
import threading
import unicodedata
from collections import Counter
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, NamedTuple

import config
import dispatcher
import profiles
import queue_manager
from queue_manager import (
    AT_TAG_RE,
    HANG_COUNT_RE,
    ID_TAG_RE,
    OPEN_TASK_RE,
    PARALLEL_TAG_RE,
    RETRY_TAG_RE,
    TASK_DONE_MARK,
    TASK_FAILED_MARK,
    _collect_completed_ids,
    _parse_subtask_line,
    _resolve_scheduled_dt,
    _retry_is_due,
    extract_at_tag,
    extract_every_tag,
    extract_id_tag,
    extract_needs_tags,
    line_is_failed,
    strip_failure_stamp,
    strip_metadata_tags,
)

logger = logging.getLogger(__name__)
qm = queue_manager

STATES = ("laeuft", "offen", "wartet", "geplant", "fehlgeschlagen", "erledigt", "verworfen", "weitere")
_OPEN_STATES = ("offen", "geplant", "wartet")

# Stempel am Zeilenende (erledigt ODER fehlgeschlagen), gleiche Form wie queue_manager.
_STAMP_RE = re.compile(
    r"\s[" + TASK_DONE_MARK + TASK_FAILED_MARK + r"] \d{4}-\d{2}-\d{2} \d{2}:\d{2} \([^)]+\)\s*$"
)

_NOT_UTF8_NOTE = "Datei ist kein UTF-8 - Eintraege nicht lesbar (Orchestrator liest sie lossy als cp1252)."


@dataclass
class Entry:
    """Ein Block unter ``## Queue``. ``lines`` ohne Zeilenende, ``eols`` je Zeile das exakte Ende."""

    start: int                      # 1-basiert, nur informativ (Identitaet laeuft ueber ref)
    lines: list[str]
    eols: list[str]
    kind: str                       # open | done | cancelled | other
    state: str                      # siehe STATES
    task_text: str                  # open: wie read_queue_items; done/cancelled: ohne Praefix+Stempel; other: Zeile
    children: list[str] = field(default_factory=list)   # eingerueckte Folgezeilen (Bodies)
    subtasks: list[str] = field(default_factory=list)   # nur #parallel, wie der Orchestrator
    marker_tail: str = ""           # unveraenderter Zeilenschwanz hinter dem Text (nur wenn er "<!--" enthaelt)
    retry_at: str | None = None
    hang: int = 0
    at: str | None = None
    every_sec: int | None = None
    due_at: str | None = None       # aufgeloester retry- (sonst at-)Zeitpunkt, ISO
    id: str | None = None
    needs: list[str] = field(default_factory=list)
    missing_needs: list[str] = field(default_factory=list)
    stamp: str | None = None
    occurrence: int = 0             # 0-basiert unter identischen Bloecken
    occurrences: int = 1
    comment_in_body: bool = False   # fremder HTML-Kommentar im Text -> edit gesperrt
    actions: list[str] = field(default_factory=list)


def allowed_actions(state: str, *, comment_in_body: bool = False) -> list[str]:
    """Aktionen, die der Server je Zustand erlaubt (das JavaScript entscheidet das nie)."""
    if state in _OPEN_STATES:
        return ["delete", "move"] if comment_in_body else ["edit", "delete", "duplicate", "move"]
    if state == "fehlgeschlagen":
        return ["retry", "delete"] if comment_in_body else ["retry", "delete", "duplicate"]
    if state in ("erledigt", "verworfen"):
        return ["delete"] if comment_in_body else ["delete", "duplicate"]
    return []  # laeuft, weitere


def split_lines(content: str) -> tuple[list[str], list[str]]:
    """(bodies, eols): wie der Orchestrator zerlegt, Zeilenende je Zeile exakt erhalten."""
    bodies = content.splitlines()
    kept = content.splitlines(keepends=True)
    return bodies, [k[len(b):] for k, b in zip(kept, bodies, strict=True)]


def _blocks(bodies: list[str]) -> Iterator[tuple[int, int]]:
    """Halboffene Zeilenbereiche der Bloecke unter ``## Queue`` (Sektion wie read_queue_items)."""
    in_queue = False
    i, n = 0, len(bodies)
    while i < n:
        body = bodies[i]
        if body.startswith("## "):
            in_queue = body.strip() == "## Queue"
            i += 1
            continue
        if not body.strip():
            i += 1
            continue
        j = i + 1
        while j < n and bodies[j].strip() and bodies[j][0] in " \t":
            j += 1
        if in_queue:
            yield i, j
        i = j


def _foreign_comment(text: str, tail: str) -> bool:
    """Kommentar im Text oder ein anderer als retry/hang im Schwanz."""
    if "<!--" in text:
        return True
    rest = HANG_COUNT_RE.sub("", RETRY_TAG_RE.sub("", tail))
    return bool(rest.strip())


def _is_running(in_flight: Any, text: str, subtasks: list[str]) -> bool:
    if in_flight is None or text != in_flight.task_text:
        return False
    wanted = tuple(getattr(in_flight, "subtasks", None) or ())
    return not wanted or tuple(subtasks) == wanted


def _open_entry(entry: Entry, m: re.Match[str], completed: set[str], now: datetime, in_flight: Any) -> None:
    first = entry.lines[0]
    text = m.group(1).strip()
    tail = first[m.end(1):]
    retry_m = RETRY_TAG_RE.search(first)
    at_m = None if retry_m else AT_TAG_RE.search(text)
    entry.task_text = text
    entry.marker_tail = tail if "<!--" in tail else ""
    entry.retry_at = retry_m.group(1) if retry_m else None
    hang_m = HANG_COUNT_RE.search(first)
    entry.hang = int(hang_m.group(1)) if hang_m else 0
    entry.at = extract_at_tag(text)
    entry.every_sec = extract_every_tag(text)
    sched_raw = retry_m.group(1) if retry_m else (at_m.group(1) if at_m else None)
    due = _resolve_scheduled_dt(sched_raw, now) if sched_raw else None
    entry.due_at = due.isoformat(timespec="minutes") if due else None
    entry.id = extract_id_tag(text)
    entry.needs = extract_needs_tags(text)
    entry.missing_needs = [d for d in entry.needs if d not in completed]
    entry.comment_in_body = _foreign_comment(text, tail)
    if PARALLEL_TAG_RE.search(text):
        for body in entry.children:
            st = _parse_subtask_line(body.rstrip())
            if st is None:
                break
            entry.subtasks.append(st)
    # Vorrang wie im Plan: laeuft > geplant > wartet > offen (geplant exakt wie read_queue_items)
    scheduled = retry_m is not None and not _retry_is_due(retry_m.group(1), now)
    if at_m:
        scheduled = not _retry_is_due(at_m.group(1), now)
    if _is_running(in_flight, text, entry.subtasks):
        entry.state = "laeuft"
    elif scheduled:
        entry.state = "geplant"
    elif entry.missing_needs:
        entry.state = "wartet"
    else:
        entry.state = "offen"


def parse_entries(content: str, *, now: datetime, in_flight: Any = None) -> list[Entry]:
    """Alle Bloecke unter ``## Queue``. ``in_flight``: Objekt mit task_text/subtasks oder None."""
    bodies, eols = split_lines(content)
    ranges = list(_blocks(bodies))
    totals = Counter(tuple(bodies[a:b]) for a, b in ranges)
    seen: Counter[tuple[str, ...]] = Counter()
    completed = _collect_completed_ids(content)
    entries: list[Entry] = []
    for a, b in ranges:
        lines = bodies[a:b]
        key = tuple(lines)
        first = lines[0]
        entry = Entry(
            start=a + 1, lines=lines, eols=eols[a:b], kind="other", state="weitere",
            task_text=first.strip(), children=lines[1:], occurrence=seen[key], occurrences=totals[key],
            comment_in_body="<!--" in first,
        )
        seen[key] += 1
        m = OPEN_TASK_RE.match(first)
        if m:
            entry.kind = "open"
            _open_entry(entry, m, completed, now, in_flight)
        elif first.startswith(("- [x] ", "- [-] ")):
            done = first.startswith("- [x] ")
            entry.kind = "done" if done else "cancelled"
            stamp_m = _STAMP_RE.search(first)
            entry.stamp = stamp_m.group(0).strip() if stamp_m else None
            entry.task_text = (_STAMP_RE.sub("", first) if stamp_m else first)[6:].strip()
            entry.id = extract_id_tag(entry.task_text)
            if not done:
                entry.state = "verworfen"
            else:
                entry.state = "fehlgeschlagen" if line_is_failed(first) else "erledigt"
                if entry.state == "fehlgeschlagen":
                    entry.task_text = strip_failure_stamp(first)[6:].strip()
        entry.actions = allowed_actions(entry.state, comment_in_body=entry.comment_in_body)
        if "retry" in entry.actions and "<!--" in HANG_COUNT_RE.sub("", RETRY_TAG_RE.sub("", first)):
            entry.actions.remove("retry")  # wie _reopen: die offene Zeile haette einen Kommentar im Body
        entries.append(entry)
    return entries


def _entry_json(e: Entry, pos: int, active_runs: Any = None) -> dict[str, Any]:
    return {
        "ref": {"start": e.start, "lines": e.lines, "occurrence": e.occurrence, "occurrences": e.occurrences},
        "state": e.state, "kind": e.kind, "text": e.task_text, "line": e.start, "pos": pos,
        "children": e.children, "subtasks": e.subtasks,
        "markers": {"retry": e.retry_at, "hang": e.hang},
        "schedule": {"at": e.at, "every_sec": e.every_sec, "due_at": e.due_at},
        "waiting_for": e.missing_needs, "id": e.id, "needs": e.needs, "stamp": e.stamp,
        "comment_in_body": e.comment_in_body, "actions": e.actions,
        "lock_reason": "running" if e.state == "laeuft" or (e.kind == "open" and _active_match(e, active_runs)) else None,
    }


def _eol_kind(raw: bytes) -> str:
    crlf = raw.count(b"\r\n")
    lf = raw.count(b"\n") - crlf
    if crlf and lf:
        return "mixed"
    return "crlf" if crlf else ("lf" if lf else "none")


def queue_state(
    *, in_flight: Any, mode: str, now: datetime | None = None, include_raw: bool = False, active_runs: Any = None
) -> dict[str, Any]:
    """Payload fuer GET /api/queue (ohne ``ok``/``instance``: das setzt die HTTP-Schicht).

    ``in_flight``: Objekt mit task_text/subtasks (+ optional started_at/tool) oder None;
    ``mode``: ``inprocess`` | ``standalone``. Liest die Datei als Bytes ohne Lock.
    """
    now = now or datetime.now()
    path = queue_manager.queue_file()
    exists = path.is_file()
    raw = path.read_bytes() if exists else b""
    try:
        content: str | None = raw.decode("utf-8")
    except UnicodeDecodeError:
        content = None
    bodies = content.splitlines() if content is not None else []
    entries = parse_entries(content, now=now, in_flight=in_flight) if content is not None else []
    counts = dict.fromkeys(STATES, 0)
    for e in entries:
        counts[e.state] += 1
    task = None
    if in_flight is not None:
        task = {"text": in_flight.task_text, "started_at": getattr(in_flight, "started_at", None),
                "tool": getattr(in_flight, "tool", None)}
    payload: dict[str, Any] = {
        "file": {
            "path": str(path), "exists": exists, "sha": hashlib.sha256(raw).hexdigest(),
            "mtime": path.stat().st_mtime if exists else None, "size": len(raw),
            "utf8": content is not None, "eol": _eol_kind(raw),
            "has_queue_section": any(b.startswith("## ") and b.strip() == "## Queue" for b in bodies),
        },
        "running": {"mode": mode, "task": task},
        "entries": [_entry_json(e, i, active_runs) for i, e in enumerate(entries)],
        "counts": counts,
        "undo": _undo_info(path, raw),
    }
    if content is None:
        payload["note"] = _NOT_UTF8_NOTE
    if include_raw:
        payload["raw"] = content
    return payload


# --- Zeilenbau, Text-Pruefung, Live-Pruefung, Formular-Metadaten ------------------------

class FieldError(NamedTuple):
    field: str
    code: str
    message: str


_STAMP_SHAPE_RE = re.compile(r"[✅❌]\s*\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}")
_CHECKBOX_RE = re.compile(r"^\s*(?:[-*+]\s+)?\[[ xX/\-]\]")
_BIDI_RE = re.compile("[\\u202a-\\u202e\\u2066-\\u2069]")
_WORD_RE = re.compile(r"[\w-]+")
_OFF = (None, False, 0, "", "0", "false", "False", "no")
_DANGERS = (
    (qm.SHUTDOWN_TAG_RE, "#shutdown", "Faehrt den Rechner nach dem Task herunter."),
    (qm.PREAPPROVE_TAG_RE, "#approve:", "Gibt Aktionen vorab frei, die sonst Telegram-Freigabe brauchen."),
    (qm.TOOL_PROVIDERS_TAG_RE, "#tool_providers:", "Ueberschreibt die erlaubten Provider (auch bezahlte)."),
)
# (Feld, Tag, Hinweis, Gefahr) fuer queue_meta; _TAG_OF liefert den Tag fuer den Zeilenbau
_SWITCHES = (
    ("grace", "#grace:", "Nur mit #every:. Wie spaet nach dem Slot der Lauf noch zaehlt (z.B. 2h).", False),
    ("freshonly", "#freshonly", "Nur mit #every:. Verpasster Slot wird uebersprungen statt nachgeholt.", False),
    ("timeout", "#timeout:", "Harte Obergrenze der Laufzeit (30s, 5m, 1h).", False),
    ("verify", "#verify:", "Pruefskript fuer das Ergebnis - empfohlen, sonst zaehlt nur das Wort des Providers.", False),
    ("parallel", "#parallel", "Subtasks stehen als eingerueckte Zeilen im Rohtext.", False),
    ("worktree", "#worktree", "Nur zusammen mit #parallel.", False),
    ("keep_worktree", "#keep-worktree", "Nur zusammen mit #parallel.", False),
    ("no_commit", "#no-commit", "Kein automatischer Commit fuer diese Zeile.", False),
    ("allow_dirty", "#allow-dirty", "Erlaubt Tools wie dev-loop in einem Repo mit offenen Aenderungen.", False),
    ("shutdown", "#shutdown", "Faehrt den Rechner nach dem Task herunter.", True),
)
_TAG_OF = {f: t for f, t, _, _ in _SWITCHES}
_FLAG_RES = {
    "parallel": qm.PARALLEL_TAG_RE, "worktree": qm.WORKTREE_TAG_RE, "keep_worktree": qm.KEEP_WORKTREE_TAG_RE,
    "no_commit": qm.NO_COMMIT_TAG_RE, "allow_dirty": qm.ALLOW_DIRTY_TAG_RE, "shutdown": qm.SHUTDOWN_TAG_RE,
}


def _value_errors(s: str, field: str) -> list[FieldError]:
    """Ablehnungen, die in JEDEM Feld gelten (ein Zeilenumbruch kommt allein zurueck)."""
    if len(("x" + s + "x").splitlines()) != 1:
        return [FieldError(field, "newline", "Zeilenumbruch ist nicht erlaubt.")]
    cats = {unicodedata.category(c) for c in s if c != "\t"}
    return [FieldError(field, code, msg) for code, msg, hit in (
        ("html_comment", "HTML-Kommentare kuerzen den Task still ab.", "<!--" in s),
        ("stamp_shape", "Die Form eines Erledigt-/Fehler-Stempels ist nicht erlaubt.", bool(_STAMP_SHAPE_RE.search(s))),
        ("control", "Steuerzeichen sind nicht erlaubt.", bool(cats & {"Cc", "Cs"})),
        ("bidi", "Bidi-Steuerzeichen sind nicht erlaubt.", bool(_BIDI_RE.search(s))),
    ) if hit]


def validate_task_text(text: str, field: str = "text") -> list[FieldError]:
    """Pruefung fuer Freitext (add/edit): leer, Checkbox am Anfang plus die Feld-Ablehnungen."""
    if not isinstance(text, str) or not text.strip():
        return [FieldError(field, "empty", "Der Task-Text ist leer.")]
    errs = _value_errors(text, field)
    if _CHECKBOX_RE.match(text):
        errs.append(FieldError(field, "checkbox", "Der Text darf nicht mit einer Checkbox beginnen."))
    return errs


def detect_dangers(text: str) -> list[dict[str, str]]:
    """Gefaehrliche Tags (Feld oder Freitext), erkannt mit den Regexen des Orchestrators."""
    return [{"tag": tag, "message": msg} for rx, tag, msg in _DANGERS if rx.search(text)]


def _schedule_hint(text: str, now: datetime) -> str:
    at, every = extract_at_tag(text), qm.EVERY_TAG_RE.search(text)
    dt = _resolve_scheduled_dt(at, now) if at else None
    if dt is None:
        return "Faellig sofort (kein #at:)." if every and not at else ""
    first = "Faellig sofort." if dt <= now else f"Erster Lauf: {dt:%Y-%m-%d %H:%M}."
    return first + (f" Danach alle {every.group(1)}{every.group(2).lower()}." if every else "")


def _read_model_tag(task: str) -> str | None:
    m, alias = qm.PROVIDER_TAG_RE.search(task), qm.extract_model_tag(task)
    return m.group(0).lower() if m else (f"#{alias}" if alias else None)


def _has(rx: re.Pattern[str]) -> Callable[[str], bool]:
    return lambda task: rx.search(task) is not None


class _Builder:
    """Tag-Teile in Zeilenreihenfolge + wie jedes Feld zurueckgelesen werden muss."""

    def __init__(self, fields: dict[str, Any]) -> None:
        self.fields = fields
        self.errs: list[FieldError] = []
        self.parts: list[str] = []
        self.checks: list[tuple[str, Callable[[str], Any], Any]] = []

    def get(self, k: str) -> str:
        v = self.fields.get(k)
        return "" if v is None else str(v).strip()

    def bad(self, k: str, code: str, msg: str) -> None:
        self.errs.append(FieldError(k, code, msg))

    def add(self, k: str, tag: str, fn: Callable[[str], Any], expected: Any) -> None:
        self.parts.append(tag)
        self.checks.append((k, fn, expected))

    def word(self, k: str, v: str) -> bool:
        if _WORD_RE.fullmatch(v):
            return True
        self.bad(k, "invalid", "Nur Buchstaben, Ziffern, _ und - sind erlaubt.")
        return False

    def quoted(self, k: str, v: str) -> str | None:
        errs = _value_errors(v, k) or ([FieldError(k, "quote", 'Anfuehrungszeichen (") sind nicht erlaubt.')] if '"' in v else [])
        self.errs.extend(errs)
        return None if errs else (f'"{v}"' if re.search(r"[\s#]", v) else v)

    def duration(self, k: str, units: str) -> tuple[str, int] | None:
        v = self.get(k).lower()
        if v and (not re.fullmatch(rf"\d+[{units}]", v) or int(v[:-1]) <= 0):
            self.bad(k, "invalid", f"Erwartet Zahl + Einheit ({'/'.join(units)}), z.B. 30m.")
            return None
        return (v, int(v[:-1]) * {"s": 1, "m": 60, "h": 3600, "d": 86400}[v[-1]]) if v else None

    def head(self) -> None:
        if v := self.get("tool").removeprefix("#tool:"):
            try:
                names = set(importlib.import_module("tools.registry").list_tools())
            except Exception:
                logger.warning("tool registry not loadable", exc_info=True)
                names = {v}
            if v not in names:
                self.bad("tool", "unknown_tool", "Unbekanntes Tool.")
            elif self.word("tool", v):
                self.add("tool", f"#tool:{v}", qm.TOOL_TAG_RE.findall, [f"#tool:{v}"])
        if v := self.get("model").lower():
            v = v if v.startswith("#") else f"#{v}"
            if v in qm._PROVIDER_TAG_MAP:
                self.add("model", v, _read_model_tag, v)
            else:
                self.bad("model", "unknown_model", "Unbekannter Provider-/Modell-Tag.")
        if (v := self.get("profile")) and self.word("profile", v):
            self.add("profile", f"#agent:{v}", qm.extract_profile_tag, v)
        if v := self.get("effort").lower():
            if v in config.CLAUDE_EFFORT_LEVELS:
                self.add("effort", f"#effort:{v}", qm.extract_effort_tag, v)
            else:
                self.bad("effort", "invalid", "Unbekannte Stufe.")

    def idents(self) -> None:
        if (v := self.get("cwd")) and (q := self.quoted("cwd", v)) is not None:  # CWD_RE, nicht extract_cwd (print + Dateisystem)
            self.add("cwd", f"cwd:{q}", lambda t: [g for tup in qm.CWD_RE.findall(t)[:1] for g in tup], [v, ""] if q != v else ["", v])
        if (v := self.get("id")) and self.word("id", v):
            self.add("id", f"#id:{v}", extract_id_tag, v.lower())
        raw = self.fields.get("needs") or []
        deps = [str(d).strip() for d in (raw.split(",") if isinstance(raw, str) else raw) if str(d).strip()]
        ok = [self.word("needs", d) for d in deps]
        if deps and all(ok):
            self.add("needs", "#needs:" + ",".join(deps), extract_needs_tags, [d.lower() for d in deps])

    def schedule(self, now: datetime) -> None:
        every = self.duration("every", "smhd")
        at = re.sub(r"^(\d{4}-\d{2}-\d{2}) (\d{2}:\d{2})$", r"\1T\2", self.get("at"))
        if at:
            try:
                if not re.fullmatch(r"(\d{4}-\d{2}-\d{2}T)?\d{2}:\d{2}", at):
                    raise ValueError(at)
                slot = datetime.strptime(at, "%Y-%m-%dT%H:%M" if "T" in at else "%H:%M")
            except ValueError:
                self.bad("at", "invalid", "Erwartet YYYY-MM-DDTHH:MM oder HH:MM.")
            else:
                if every and "T" not in at:  # bares HH:MM + #every waere nachmittags sofort faellig
                    nxt = now.replace(hour=slot.hour, minute=slot.minute, second=0, microsecond=0)
                    at = (nxt if nxt > now else nxt + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M")
                self.add("at", f"#at:{at}", extract_at_tag, at)
        if every:
            self.add("every", f"#every:{every[0]}", extract_every_tag, every[1])
        if grace := self.duration("grace", "smhd"):
            self.add("grace", f"#grace:{grace[0]}", qm.extract_grace_tag, grace[1])
        if self.fields.get("freshonly") not in _OFF:
            self.add("freshonly", "#freshonly", qm.has_freshonly_tag, True)
        if timeout := self.duration("timeout", "smh"):
            self.add("timeout", f"#timeout:{timeout[0]}", qm.extract_timeout, timeout[1])

    def tail(self) -> None:
        if (v := self.get("verify")) and (q := self.quoted("verify", v)) is not None:
            self.add("verify", f"#verify:{q}", qm.extract_verify_tag, v)
        for k, rx in _FLAG_RES.items():
            if self.fields.get(k) not in _OFF:
                self.add(k, _TAG_OF[k], _has(rx), True)


def build_task_line(
    fields: dict[str, Any], now: datetime | None = None
) -> tuple[str | None, list[FieldError], list[dict[str, str]], str]:
    """Felder -> ``- [ ] ...``: (line|None, field_errors, dangers, schedule_hint); Rueckleseprobe = ``roundtrip``."""
    now = now or datetime.now()
    b = _Builder(fields)
    text = b.get("text")
    b.errs.extend(validate_task_text(text))
    if text:
        b.parts.append(text)
    b.head()
    b.idents()
    b.schedule(now)
    b.tail()
    body = " ".join(b.parts)
    dangers = detect_dangers(body)
    if not b.errs:
        m = OPEN_TASK_RE.match("- [ ] " + body)
        task = m.group(1) if m else ""
        if task != body:
            b.bad("text", "roundtrip", "Der Orchestrator liest die Zeile anders, als sie gebaut wurde.")
        for k, fn, expected in (b.checks if task == body else []):
            if fn(task) != expected:
                b.bad(k, "roundtrip", f"Der Orchestrator liest '{k}' anders, als es geschrieben wurde.")
    if b.errs:
        return None, b.errs, dangers, ""
    return "- [ ] " + body, [], dangers, _schedule_hint(body, now)


def _place_draft(
    content: str, line: str, replace_ref: dict[str, Any] | None, position: str
) -> tuple[list[str], int] | None:
    """-> (Zeilen, Index) | None. edit ersetzt nur die erste Blockzeile; add: top = hinter ``## Queue``."""
    bodies = content.splitlines()
    ranges = list(_blocks(bodies))
    if replace_ref is not None:
        want = tuple(replace_ref.get("lines") or ())
        hits = [a for a, b in ranges if tuple(bodies[a:b]) == want]
        occ = int(replace_ref.get("occurrence") or 0)
        if occ >= len(hits):
            return None
        return [*bodies[:hits[occ]], line, *bodies[hits[occ] + 1:]], hits[occ]
    head = next((i for i, b in enumerate(bodies) if b.startswith("## ") and b.strip() == "## Queue"), None)
    if head is None:
        return [*bodies, "", "## Queue", line], len(bodies) + 2
    idx = ranges[-1][1] if position == "bottom" and ranges else head + 1
    return [*bodies[:idx], line, *bodies[idx:]], idx


def _reduce(lines: list[str], keep: int) -> str:
    """Jede andere offene Zeile -> ``- [ ] x`` (+ ``#id:``). Zeilenzahl und alles andere bleiben gleich."""
    out: list[str] = []
    in_queue = False
    for i, body in enumerate(lines):
        if body.startswith("## "):
            in_queue = body.strip() == "## Queue"
        m = OPEN_TASK_RE.match(body) if in_queue and i != keep else None
        tid = extract_id_tag(m.group(1)) if m else None
        out.append(body if m is None else "- [ ] x" + (f" #id:{tid}" if tid else ""))
    return "\n".join(out)


def lint_draft(
    content: str, *, line: str, replace_ref: dict[str, Any] | None = None, position: str = "top",
    now: datetime | None = None,
) -> dict[str, Any]:
    """Live-Pruefung gegen das reduzierte Dokument. ``valid`` = keine Feldfehler, kein Befund der Stufe error."""
    now = now or datetime.now()
    text = (line[6:] if line.startswith("- [ ] ") else line).strip()
    ferrs = validate_task_text(text)
    res: dict[str, Any] = {
        "ok": True, "line": None, "valid": False, "field_errors": [e._asdict() for e in ferrs],
        "findings": [], "file_findings": [], "dangers": detect_dangers(text),
        "schedule_hint": "" if ferrs else _schedule_hint(text, now), "lint_available": True,
    }
    if ferrs:
        return res
    placed = _place_draft(content, "- [ ] " + text, replace_ref, position)
    if placed is None:
        res["field_errors"].append(FieldError("ref", "changed", "Inzwischen geaendert - neu laden.")._asdict())
        return res
    res["line"] = "- [ ] " + text
    lines, idx = placed
    try:
        found = importlib.import_module("queue_linter").lint_queue(_reduce(lines, idx))
    except Exception:
        logger.warning("queue_linter unavailable or failed", exc_info=True)
        res["lint_available"] = False
        found = []
    for f in found:
        row = {"level": f.level, "code": f.code, "message": f.message}
        if f.line_no == idx + 1:
            res["findings"].append(row)
        elif f.line_no is None:
            res["file_findings"].append(row)
    res["valid"] = not any(r["level"] == "error" for r in res["findings"])
    return res


def lint_content(content: str) -> dict[str, Any]:
    """Voller Lint eines Rohtexts: Befunde mit Zeilennummer (``line``), Datei-Befunde ohne."""
    res: dict[str, Any] = {"ok": True, "findings": [], "file_findings": [], "lint_available": True}
    try:
        found = importlib.import_module("queue_linter").lint_queue(content)
    except Exception:
        logger.warning("queue_linter unavailable or failed", exc_info=True)
        res["lint_available"] = False
        return res
    for f in found:
        res["file_findings" if f.line_no is None else "findings"].append(
            {"level": f.level, "code": f.code, "message": f.message, "line": f.line_no})
    return res


def lint_request(body: dict[str, Any]) -> dict[str, Any]:
    """POST /api/queue/lint: ``{content}`` | ``{fields[, position]}`` | ``{text[, ref, position]}`` -> Antwort (schreibt nie)."""
    fields, text, content, pos = body.get("fields"), body.get("text"), body.get("content"), body.get("position", "top")
    if isinstance(content, str) and fields is None and text is None:
        return lint_content(content)
    if pos not in ("top", "bottom") or not (isinstance(fields, dict) or isinstance(text, str)):
        raise QueueEditError("bad_request")
    current = queue_manager.read_queue_text()
    if isinstance(fields, dict):
        line, errs, dangers, _hint = build_task_line(fields)
        if line is None:
            return {"ok": True, "line": None, "valid": False, "field_errors": [e._asdict() for e in errs],
                    "findings": [], "file_findings": [], "dangers": dangers, "schedule_hint": "",
                    "lint_available": True}
        return lint_draft(current, line=line, position=pos)
    ref = body.get("ref")
    if ref is not None:
        _check_ref(ref)
    return lint_draft(current, line=str(text), replace_ref=ref, position=pos)


def _recent_cwds(limit: int = 15) -> list[dict[str, Any]]:
    """Zuletzt genutzte cwd aus Queue + Erledigt-Datei (keine Logs), ohne Doppelte."""
    seen: dict[str, None] = {}
    queue = queue_manager.queue_file()
    for path in (queue, queue.with_name("agent-queue-erledigt.md")):
        try:
            text = path.read_bytes().decode("utf-8", errors="replace")
        except OSError:
            continue
        for ln in (x for x in text.splitlines() if x.lstrip().startswith("- [")):
            for m in qm.CWD_RE.finditer(ln):
                seen.setdefault((m.group(1) or m.group(2)).strip(), None)
    return [{"path": p, "exists": os.path.exists(p)} for p in list(seen)[:limit]]


def queue_meta() -> dict[str, Any]:
    """Payload fuer GET /api/queue/meta (Formular-Datenquellen; ``ok`` setzt die HTTP-Schicht)."""
    tools: list[dict[str, Any]] = []
    try:
        registry = importlib.import_module("tools.registry")
        for name, desc in registry.list_tools().items():
            clean = bool(getattr(registry.get_tool(name), "requires_clean_worktree", False))
            hint = "Braucht cwd: in einem sauberen Git-Repo (Ausnahme: #allow-dirty)." if clean else ""
            tools.append({"name": name, "description": desc, "requires_clean_worktree": clean, "hint": hint})
    except Exception:
        logger.warning("tool registry not loadable", exc_info=True)
    providers: list[dict[str, Any]] = []
    for prov in dict.fromkeys(dispatcher._TAG_MAP.values()):
        providers.append({
            "name": prov, "registered": prov in dispatcher._providers,
            "allowed": dispatcher.policy_allows_provider(prov, None),
            "tags": [t for t, p in dispatcher._TAG_MAP.items() if p == prov],
        })
    return {
        "tools": tools, "providers": providers, "profiles": profiles.list_profile_names(config.VAULT_PATH),
        "effort": {"levels": list(config.CLAUDE_EFFORT_LEVELS), "note": "Wirkt nur bei Claude oder ohne Provider-Tag."},
        "cwd": {"roots": [str(r) for r in config.ALLOWED_CWD_ROOTS], "recent": _recent_cwds()},
        "switches": [
            {"field": f, "tag": t, "hint": h, "danger": d, "kind": "value" if t.endswith(":") else "flag"}
            for f, t, h, d in _SWITCHES
        ],
    }


# --- Mutationen (add/edit/delete/retry/duplicate/move/undo/raw), Sicherungen, Rueckgaengig-Verfuegbarkeit -------------

ERROR_TEXTS = {
    "bad_request": "Ungültige Anfrage.",
    "changed": "Inzwischen geändert — neu laden.",
    "ambiguous": "Die Zeile kommt mehrfach identisch vor — neu laden.",
    "not_utf8": "Die Queue-Datei ist kein UTF-8 — das Dashboard ändert sie nicht.",
    "edit_locked": "Die Zeile enthält einen Kommentar — nur im Rohtext bearbeitbar.",
    "confirm_dangerous": "Gefährlicher Tag — bitte ausdrücklich bestätigen.",
    "backup_failed": "Sicherung nicht möglich — die Queue wurde nicht geändert.",
    "unavailable": "Die Queue-Datei ist gerade nicht beschreibbar — später erneut versuchen.",
    "roundtrip": "Der Orchestrator würde die Zeile anders lesen, als sie gebaut wurde.",
    "running": "Die Zeile läuft gerade — jetzt nicht änderbar.",
    "confirm_unknown_running": "Laufstatus unbekannt — bitte ausdrücklich bestätigen.",
    "no_undo": "Es gibt nichts zum Rückgängigmachen.",
    "undo_changed": "Die Sicherung ist nicht mehr die neueste — neu laden.",
    "undo_stale": "Die Queue wurde seitdem geändert — Rückgängig nicht mehr möglich.",
    "backup_corrupt": "Die Sicherung ist beschädigt — die Queue wurde nicht geändert.",
    "sha_mismatch": "Die Queue wurde seitdem geändert — Rohtext neu laden.",
    "stamp_in_raw": "Neue Erledigt-/Fehlgeschlagen-Stempel setzt nur der Orchestrator.",
    "comment_in_raw": "In offenen Zeilen ist kein HTML-Kommentar erlaubt — er würde den Task verbergen.",
}
_OPS = ("add", "edit", "delete", "retry", "duplicate", "move", "undo", "raw")
_EDIT_LOCK = threading.Lock()
_TOOL_RE = re.compile(r"#tool:([\w-]+)")  # wie tools/registry.py (dort nicht importierbar: zieht alle Tools)
_TAIL_RE = re.compile(r"(?:\s*(?:" + RETRY_TAG_RE.pattern + "|" + HANG_COUNT_RE.pattern + r"))+\s*$")
_ID_STRIP_RE = re.compile(r"\s*" + ID_TAG_RE.pattern.removeprefix("(?i)"), re.IGNORECASE)


class QueueEditError(Exception):
    """``code`` (siehe ERROR_TEXTS); Feldfehler tragen ``field_errors`` und deren Code (HTTP 422)."""

    def __init__(self, code: str, field_errors: list[FieldError] | None = None) -> None:
        self.code, self.field_errors = code, list(field_errors or [])
        super().__init__(ERROR_TEXTS.get(code) or (self.field_errors[0].message if self.field_errors else code))


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _read_raw(path: Path) -> bytes:
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return b""


def _backup_metas(qfile: Path) -> list[dict[str, Any]]:
    """Vollstaendige Sicherungen (``.json`` ist der Commit-Marker) DIESER Queue, aelteste zuerst."""
    d, want, metas = Path(config.QUEUE_BACKUP_DIR), str(qfile.resolve()), []
    for p in sorted(d.glob("*.json")) if d.is_dir() else []:
        try:
            m = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(m, dict) and m.get("queue_file") == want and m.get("id") == p.stem:
            metas.append(m)
    return metas


def _drop_backup(stem: str) -> None:
    for suffix in (".json", ".md"):  # Marker zuerst: ein Rest ist dann nur eine verwaiste .md
        try:
            (Path(config.QUEUE_BACKUP_DIR) / f"{stem}{suffix}").unlink(missing_ok=True)
        except OSError:
            logger.warning("backup file not removable: %s%s", stem, suffix)


def _write_backup(qfile: Path, raw: bytes, new: str, action: str, summary: str) -> str:
    """Paar ``<UTC-Zeit>-<aktion>.md`` (Bytes VOR der Aenderung) + ``.json`` (zuletzt, atomar).

    Der Name traegt UTC: Ortszeit wuerde bei der Zeitumstellung rueckwaerts springen und die Reihenfolge brechen."""
    d, stem, t = Path(config.QUEUE_BACKUP_DIR), "", datetime.now(UTC)
    try:
        d.mkdir(parents=True, exist_ok=True)
        while any(d.glob(f"{t:%Y%m%d-%H%M%S-%f}-*.json")):
            t += timedelta(microseconds=1)
        stem = f"{t:%Y%m%d-%H%M%S-%f}-{action}"
        qm._write_bytes_atomic(d / f"{stem}.md", raw)
        meta = {
            "version": 1, "id": stem, "action": action, "created_at": t.astimezone().isoformat(timespec="seconds"),
            "queue_file": str(qfile.resolve()), "before_sha": _sha(raw),
            "after_sha": _sha(new.encode("utf-8")), "summary": summary[:120],
        }
        qm._write_bytes_atomic(d / f"{stem}.json", json.dumps(meta, ensure_ascii=False).encode("utf-8"))
    except Exception as exc:
        logger.warning("queue backup failed: %s", type(exc).__name__)
        if stem:
            _drop_backup(stem)
        raise QueueEditError("backup_failed") from None
    return stem


def _rotate(qfile: Path) -> None:
    """Neueste ``QUEUE_BACKUP_KEEP`` Sicherungen dieser Queue bleiben; verwaiste .md davor gehen."""
    metas = _backup_metas(qfile)
    for m in metas[: max(0, len(metas) - int(config.QUEUE_BACKUP_KEEP))]:
        _drop_backup(m["id"])
    if metas:
        for p in Path(config.QUEUE_BACKUP_DIR).glob("*.md"):
            if p.stem < metas[-1]["id"] and not p.with_suffix(".json").exists():
                p.unlink(missing_ok=True)


def _undo_info(qfile: Path, raw: bytes) -> dict[str, Any]:
    """Rueckgaengig nur fuer die neueste Sicherung, nie nach einem undo, nie nach Aenderung von aussen."""
    metas = _backup_metas(qfile)
    n = metas[-1] if metas else None
    if n and n.get("action") != "undo" and n.get("after_sha") == _sha(raw):
        return {"available": True, "id": n["id"], "action": n["action"], "at": n.get("created_at"),
                "summary": n.get("summary", "")}
    return {"available": False}


def _check_ref(ref: Any) -> None:
    lines = ref.get("lines") if isinstance(ref, dict) else None
    if not (isinstance(lines, list) and lines and all(isinstance(x, str) for x in lines)
            and all(type(ref.get(k)) is int for k in ("occurrence", "occurrences"))
            and 0 <= ref["occurrence"] < ref["occurrences"]):
        raise QueueEditError("bad_request")


def _strs(*values: Any) -> tuple[str, ...]:
    """Alle Werte muessen str und als UTF-8 kodierbar sein (kein Surrogat), sonst bad_request."""
    try:
        if all(isinstance(v, str) for v in values):
            return tuple(v.encode("utf-8").decode("utf-8") for v in values)
    except UnicodeEncodeError:
        pass
    raise QueueEditError("bad_request")


_REF_OPS = ("edit", "delete", "retry", "duplicate", "move")


def _prepare(op: str, body: Any, now: datetime) -> dict[str, Any]:
    """Schritte 1-3 der Pipeline: Schema, Felder, gefaehrliche Tags (rein, ausserhalb des Locks)."""
    if op not in _OPS or not isinstance(body, dict) or not all(
        isinstance(body.get(k, False), bool) for k in ("confirm_dangerous", "confirm_unknown_running")
    ):
        raise QueueEditError("bad_request")
    plan: dict[str, Any] = {
        "ref": body.get("ref") if op in _REF_OPS else None, "confirm": body.get("confirm_unknown_running", False)
    }
    if op == "undo":
        (plan["backup_id"],) = _strs(body.get("backup_id"))
    elif op == "raw":
        plan["sha"], plan["content"] = _strs(body.get("sha"), body.get("content"))
        plan["danger_ok"] = body.get("confirm_dangerous", False)
    elif op == "add":
        fields, plan["position"] = body.get("fields"), body.get("position", "top")
        if not isinstance(fields, dict) or plan["position"] not in ("top", "bottom"):
            raise QueueEditError("bad_request")
        line, errs, _, _ = build_task_line(fields, now)
        if errs or line is None:
            raise QueueEditError(errs[0].code, errs)
        plan["line"], plan["text"] = line, line[6:]
    else:
        _check_ref(plan["ref"])
    if op == "move" and body.get("direction") not in ("up", "down"):
        raise QueueEditError("bad_request")
    plan["direction"] = body.get("direction")
    if op == "edit":
        if not isinstance(body.get("text"), str):
            raise QueueEditError("bad_request")
        errs = validate_task_text(body["text"])
        if errs:
            raise QueueEditError(errs[0].code, errs)
        plan["text"] = body["text"].strip()
    danger_text = plan["ref"]["lines"][0] if op in ("retry", "duplicate") else plan.get("text", "")
    if detect_dangers(danger_text) and not body.get("confirm_dangerous", False):
        raise QueueEditError("confirm_dangerous")
    return plan


def _active_match(entry: Entry, active_runs: Any) -> bool:
    """Positivsignal: laufender Tool-Run mit gleichem Tool und Task-Text (tools/base_tool.py, dev_loop.py:974)."""
    m = _TOOL_RE.search(entry.task_text)
    clean = strip_metadata_tags(entry.task_text)[:200]
    return m is not None and any(
        isinstance(r, dict) and r.get("status") == "running" and r.get("tool") == m.group(1) and r.get("task") == clean
        for r in active_runs or ()
    )


def _lock_gate(
    op: str, entry: Entry | None, *, in_flight: Any, mode: str, active_runs: Any, confirm: bool = False
) -> None:
    """Laufsperre 423 ``running`` / Nachfrage 428 ``confirm_unknown_running`` (Plan Abschn. 3, Owner A).

    Gilt nur fuer offene Bloecke; ``add`` hat keinen. Das Positivsignal wirkt in BEIDEN Modi; nachgefragt
    wird nur im eigenstaendigen Betrieb (Laufstatus unbekannt) bei edit/delete/move/undo auf offenen Zeilen.
    """
    if entry is None or entry.kind != "open":
        return
    if entry.state == "laeuft" or _active_match(entry, active_runs):
        raise QueueEditError("running")
    if mode == "standalone" and op in ("edit", "delete", "move", "undo", "raw") and entry.state in _OPEN_STATES and not confirm:
        raise QueueEditError("confirm_unknown_running")


def _eol_of(content: str) -> str:
    return "\r\n" if content.count("\r\n") * 2 > content.count("\n") else "\n"


def _open_line(first: str, kind: str, failed: bool, *, drop_id: bool = False) -> str:
    """Erste Zeile als frische offene Zeile: ohne retry/hang-Schwanz und Stempel, optional ohne ``#id:``."""
    text = _TAIL_RE.sub("", first)
    if kind != "open":
        text = _TAIL_RE.sub("", strip_failure_stamp(text) if failed else _STAMP_RE.sub("", text))
    line = "- [ ] " + (_ID_STRIP_RE.sub("", text[6:]) if drop_id else text[6:])
    if not OPEN_TASK_RE.match(line):
        raise QueueEditError("roundtrip", [FieldError("text", "roundtrip", ERROR_TEXTS["roundtrip"])])
    return line


def _neighbour(entries: list[Entry], i: int, direction: str) -> int | None:
    """Naechster offener/geplanter/wartender (oder laufender) Block in Richtung; sonst None."""
    step = -1 if direction == "up" else 1
    j = i + step
    while 0 <= j < len(entries) and entries[j].state not in (*_OPEN_STATES, "laeuft"):
        j += step  # erledigte, fehlgeschlagene und weitere Bloecke bleiben liegen
    return j if 0 <= j < len(entries) else None


def _reopen(op: str, entry: Entry, kept: list[str], span: tuple[int, int], eol: str) -> str:
    """retry: Zeile wieder offen (gleiche Stelle). duplicate: frische Kopie des Blocks direkt dahinter."""
    a, b = span
    line = _open_line(entry.lines[0], entry.kind, entry.state == "fehlgeschlagen", drop_id=op == "duplicate")
    if "<!--" in HANG_COUNT_RE.sub("", RETRY_TAG_RE.sub("", line)):  # neue offene Zeile mit Kommentar: Falle
        raise QueueEditError("edit_locked")
    if op == "retry":
        return "".join([*kept[:a], line + entry.eols[0], *kept[a + 1:]])
    copy = [line + entry.eols[0], *(c + e for c, e in zip(entry.children, entry.eols[1:], strict=True))]
    out = list(kept)
    if not entry.eols[-1]:  # Block endet ohne Zeilenende: Original bekommt eines, die Kopie behaelt keins
        out[b - 1] += eol
    return "".join(out[:b] + copy + out[b:])


def _swap(kept: list[str], bodies: list[str], first: tuple[int, int], second: tuple[int, int], eol: str) -> str:
    """Vertauscht zwei Bloecke; was dazwischen liegt, bleibt. Eine EOF-Zeile ohne Ende bleibt die letzte."""
    (a, b), (c, d) = sorted((first, second))
    early, late = kept[a:b], kept[c:d]
    if late[-1] == bodies[d - 1]:  # letzte Zeile der Datei ohne Zeilenende: das Ende wandert mit
        late[-1] += eol
        early[-1] = bodies[b - 1]
    return "".join(kept[:a] + late + kept[b:c] + early + kept[d:])


def _mutate(content: str, op: str, plan: dict[str, Any], now: datetime, gate: Callable[[Entry | None], None]) -> str:
    """Neuer Inhalt; nur die betroffenen Zeilen aendern sich (Zeilenenden bleiben, Rest byte-gleich)."""
    bodies = content.splitlines()
    kept = content.splitlines(keepends=True)
    if op == "add":
        gate(None)
        lines, idx = _place_draft(content, plan["line"], None, plan["position"]) or ([], 0)
        at = idx - (len(lines) - len(bodies)) + 1  # Einfuegestelle; eingefuegt wird lines[at:idx + 1]
        eol = _eol_of(content)
        out = list(kept)
        if at and kept[at - 1] == bodies[at - 1]:  # letzte Zeile ohne Zeilenende
            out[at - 1] += eol
        return "".join(out[:at] + [x + eol for x in lines[at:idx + 1]] + out[at:])
    ranges, ref = list(_blocks(bodies)), plan["ref"]
    hits = [i for i, (a, b) in enumerate(ranges) if bodies[a:b] == ref["lines"]]
    if not hits:
        raise QueueEditError("changed")
    if len(hits) != ref["occurrences"]:
        raise QueueEditError("ambiguous")
    a, b = ranges[hits[ref["occurrence"]]]
    entries, i = parse_entries(content, now=now, in_flight=plan["in_flight"]), hits[ref["occurrence"]]
    entry = entries[i]
    if entry.state != "laeuft" and op not in entry.actions:  # laeuft: Entscheidung liegt beim Hook
        locked = (op == "edit" and entry.comment_in_body and entry.kind == "open") or (
            op == "retry" and entry.state == "fehlgeschlagen"  # Aktion wurde wegen <!-- im Body nicht angeboten
        )
        raise QueueEditError("edit_locked" if locked else "changed")
    gate(entry)
    if op == "delete":
        return "".join(kept[:a] + kept[b:])
    if op == "move":
        j = _neighbour(entries, i, plan["direction"])
        if j is None:
            return content  # am Rand: nichts zu tun
        gate(entries[j])  # der Nachbar wandert mit: laeuft er, gilt 423
        return _swap(kept, bodies, ranges[i], ranges[j], _eol_of(content))
    if op in ("retry", "duplicate"):
        return _reopen(op, entry, kept, (a, b), _eol_of(content))
    first = "- [ ] " + plan["text"] + entry.marker_tail
    m = OPEN_TASK_RE.match(first)
    if not m or m.group(1) != plan["text"]:
        raise QueueEditError("roundtrip", [FieldError("text", "roundtrip", ERROR_TEXTS["roundtrip"])])
    return "".join([*kept[:a], first + entry.eols[0], *kept[a + 1:]])


def _restore(
    text: str, raw: bytes, plan: dict[str, Any], gate: Callable[[Entry | None], None], *, now: datetime, qfile: Path
) -> str:
    """undo (Plan Abschn. 4): nur die neueste Sicherung, nie nach einem undo, nie nach Aenderung von aussen."""
    metas = _backup_metas(qfile)
    newest = metas[-1] if metas else None
    if newest is None or newest.get("action") == "undo":
        raise QueueEditError("no_undo")
    if plan["backup_id"] != newest["id"]:
        raise QueueEditError("undo_changed")
    if newest.get("after_sha") != _sha(raw):
        raise QueueEditError("undo_stale")
    try:
        old = (Path(config.QUEUE_BACKUP_DIR) / f"{newest['id']}.md").read_bytes()
        if _sha(old) != newest.get("before_sha"):
            raise ValueError("sha")
        new = old.decode("utf-8")
    except (OSError, ValueError):
        raise QueueEditError("backup_corrupt") from None
    _gate_changes(text, new, plan, gate, now)
    return new


def _gate_changes(text: str, new: str, plan: dict[str, Any], gate: Callable[[Entry | None], None], now: datetime) -> None:
    """Gate fuer jeden Block, der durch den Wechsel text -> new entsteht oder verschwindet (Multiset-Differenz)."""
    now_e = parse_entries(text, now=now, in_flight=plan["in_flight"])
    then_e = parse_entries(new, now=now, in_flight=plan["in_flight"])
    for src, other in ((now_e, then_e), (then_e, now_e)):
        gone = Counter(tuple(e.lines) for e in src) - Counter(tuple(e.lines) for e in other)
        for e in src:
            if gone[tuple(e.lines)] > 0:
                gone[tuple(e.lines)] -= 1
                gate(e)


def _new_stamp_lines(old: list[str], new: list[str]) -> list[int]:
    """Indizes von Zeilen in ``new`` mit Erledigt-/Fehlgeschlagen-Stempel, die es so vorher nicht gab.

    Erlaubt: dieselbe gestempelte Zeile an anderer Stelle (Multiset, exakt gleiche Zeile) und eine geaenderte
    Zeile mit unveraendertem Stempel, Checkbox-Praefix UND #id (Tippfehler im Text; Zuordnung ueber ``replace``-Bloecke).
    """
    pool = Counter(x for x in old if _STAMP_RE.search(x))
    cands = []
    for j, line in enumerate(new):
        if _STAMP_RE.search(line):
            if pool[line] > 0:
                pool[line] -= 1
            else:
                cands.append(j)
    if cands:
        paired: set[int] = set()
        for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, old, new, autojunk=False).get_opcodes():
            if tag == "replace":
                for k in range(min(i2 - i1, j2 - j1)):
                    o, n = _STAMP_RE.search(old[i1 + k]), _STAMP_RE.search(new[j1 + k])
                    if (
                        o and n and o.group(0) == n.group(0)
                        and old[i1 + k][:6] == new[j1 + k][:6]
                        and extract_id_tag(old[i1 + k]) == extract_id_tag(new[j1 + k])
                    ):
                        paired.add(j1 + k)  # nur Beschreibungstext geaendert: Stempel und Checkbox identisch
        cands = [j for j in cands if j not in paired]
    return cands


def _rewrite(text: str, raw: bytes, plan: dict[str, Any], gate: Callable[[Entry | None], None], now: datetime) -> str:
    """raw (Plan Abschn. 4/9): ganzer Inhalt aus dem Textfeld (LF); Zeilenende und BOM der Datei bleiben."""
    if plan["sha"] != _sha(raw):
        raise QueueEditError("sha_mismatch")
    new: str = plan["content"].replace("\r\n", "\n").replace("\n", _eol_of(text))
    if text.startswith("\ufeff") and not new.startswith("\ufeff"):
        new = "\ufeff" + new
    if new == text:
        return text
    old_l, new_l = text.splitlines(), new.splitlines()
    if _new_stamp_lines(old_l, new_l):
        raise QueueEditError("stamp_in_raw")
    # verbrauchend: jede alte Blockzeile eines Queue-Eintrags deckt genau EIN Vorkommen im neuen Text;
    # Zeilen ausserhalb von ## Queue (z.B. ## Notes) zaehlen nicht als vorhanden
    known = Counter(ln for e in parse_entries(text, now=now, in_flight=plan["in_flight"]) for ln in e.lines)
    for e in parse_entries(new, now=now, in_flight=plan["in_flight"]):
        for ln in e.lines:
            if known[ln] > 0:
                known[ln] -= 1  # bekannt: Umsortieren ist frei, eine zusaetzliche Kopie gilt als neu
                continue
            if e.kind == "open" and "<!--" in HANG_COUNT_RE.sub("", RETRY_TAG_RE.sub("", ln)):
                raise QueueEditError("comment_in_raw")
            if e.kind == "open" and detect_dangers(ln) and not plan["danger_ok"]:
                raise QueueEditError("confirm_dangerous")  # nur neue/geaenderte/zusaetzliche Zeilen, wie add/edit
    _gate_changes(text, new, plan, gate, now)
    return new


def apply_edit(
    op: str, body: Any, *, in_flight: Any, mode: str, active_runs: Any = None, now: datetime | None = None
) -> dict[str, Any]:
    """Eine Aenderung (add|edit|delete|retry|duplicate|move|undo|raw) in einem Lock-Durchgang -> ``{"ok","changed","queue"}`` oder QueueEditError.

    Pipeline wie Plan Abschn. 2: Pruefung ausserhalb des Locks; unter ``_EDIT_LOCK`` + Queue-Lock ein
    transform mit frischem Holder je Aufruf (er kann wiederholt laufen, Exceptions schluckt der Wrapper
    und werden hier neu geworfen): Bytes lesen, ref aufloesen, neu berechnen, Sicherung schreiben,
    ZWEITER Byte-Vergleich unmittelbar vor der Rueckgabe. Schreibt nichts, wenn nichts zu aendern ist.
    """
    now = now or datetime.now()
    plan = _prepare(op, body, now)
    plan["in_flight"] = in_flight
    holders: list[dict[str, Any]] = []
    made: dict[str, str] = {}  # before_sha -> Sicherung dieses Aufrufs (Wiederholungen legen keine zweite an)
    after: dict[str, str] = {}  # after_sha -> before_sha: steht das schon in der Datei, ist es gelandet
    qfile = queue_manager.queue_file()
    summary = plan.get("text") or (" / ".join(plan["ref"]["lines"]) if plan["ref"] else plan.get("backup_id", "Rohtext"))

    def gate(entry: Entry | None) -> None:
        _lock_gate(op, entry, in_flight=in_flight, mode=mode, active_runs=active_runs, confirm=plan["confirm"])

    def transform(content: str) -> str | None:
        h: dict[str, Any] = {"err": None, "new": None, "sha": None, "landed": False}
        holders.append(h)
        try:
            raw = _read_raw(qfile)
            if _sha(raw) in after:  # Wiederholung nach erfolgreichem Schreiben (z. B. Freigabe-Fehler): nie zweimal
                h["sha"], h["landed"] = after[_sha(raw)], True
                return None
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError:
                raise QueueEditError("not_utf8") from None
            if text != content:
                raise QueueEditError("changed")
            if op == "undo":
                new = _restore(text, raw, plan, gate, now=now, qfile=qfile)
            elif op == "raw":
                new = _rewrite(text, raw, plan, gate, now)
            else:
                new = _mutate(text, op, plan, now, gate)
            if new == text:
                return None
            h["sha"] = _sha(raw)
            if h["sha"] not in made:
                made[h["sha"]] = _write_backup(qfile, raw, new, op, summary)
            if _read_raw(qfile) != raw:  # Obsidian hat zwischen Lesen und Schreiben geschrieben
                raise QueueEditError("changed")
            h["new"] = new
            after[_sha(new.encode("utf-8"))] = h["sha"]
            return new
        except OSError:
            # Eigenes Lesen scheiterte: durchreichen, _apply_update wiederholt; bleibt es dabei -> 503
            h["err"] = QueueEditError("unavailable")
            raise
        except Exception as exc:
            h["err"] = exc
            return None

    with _EDIT_LOCK:
        written = queue_manager.apply_queue_update(transform)
        h = holders[-1] if holders else None
        written = written or bool(h and h["landed"])
        for sha, stem in made.items():
            if not (written and h is not None and sha == h["sha"]):
                _drop_backup(stem)
        if h is not None and h["err"] is not None:
            raise h["err"]
        if not written and (h is None or h["new"] is not None):  # fertiger Inhalt, aber nicht geschrieben
            raise QueueEditError("unavailable")
        if written:
            try:
                _rotate(qfile)
            except OSError:
                logger.warning("queue backup rotation failed", exc_info=True)
    if written:
        queue_manager.append_log(f"Dashboard: {op} — {summary[:80]}")
    return {"ok": True, "changed": written, "queue": queue_state(in_flight=in_flight, mode=mode, now=now, active_runs=active_runs)}

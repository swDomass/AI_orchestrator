"""Knowledge Transfer Tool — discovers cross-domain applications of vault expertise.

4-phase workflow:
  Phase 0 (Python):  Smart-sample vault notes by content richness (TF-IDF-style scoring)
  Phase 1 (LLM):    Extract one deep know-how domain — reads between the lines
  Phase 2 (LLM):    Find concrete cross-domain applications (Claude uses WebSearch)
  Phase 3 (LLM):    Synthesize best idea into a complete Obsidian note
  Phase 4 (Python):  Write note to VAULT_PATH/01_Ideen/<slug>/

Queue usage:
    - [ ] Know-How Transfer #tool:knowledge-transfer
    - [ ] Know-How Transfer: Bremsquitschen #tool:knowledge-transfer
"""

import os
import re
import time
from datetime import date
from pathlib import Path

from config import (
    TOOL_INTER_STEP_SLEEP_SEC,
    TOOL_KT_APPLICATIONS_TIMEOUT_SEC,
    TOOL_KT_KNOWHOW_TIMEOUT_SEC,
    TOOL_KT_SYNTHESIS_TIMEOUT_SEC,
    TOOL_KT_VAULT_SCAN_MAX_CHARS,
    VAULT_PATH,
)
from notifier import notify_tool_done, notify_tool_progress
from providers.base import BaseProvider
from queue_manager import strip_metadata_tags
from tools.base_tool import BaseTool, ToolResult, _build_system_prompt, _write_tool_file

_KT_OUTPUT_DIR = "01_Ideen"

# Vault dirs to skip during scan
_SCAN_EXCLUDED_DIRS = {
    ".obsidian", ".trash", "99_System", "01_Ideen",
    "templates", "Templates", ".git", "assets", "attachments",
    "Archive", "Archiv",
}

# Technical keywords that signal expertise depth (boost note score)
_TECHNICAL_KEYWORDS = [
    "analyse", "berechnung", "methode", "algorithmus", "simulation", "modell",
    "framework", "implementierung", "architektur", "experiment", "messung",
    "kalibrierung", "validierung", "protokoll", "eigenvalue", "matrix",
    "regression", "spektrum", "resonanz", "optimierung", "frequenz",
    "finite", "element", "modal", "schwingung", "dämpfung", "impedanz",
    "analysis", "calculation", "method", "algorithm", "model", "implementation",
    "architecture", "api", "schema", "datenbank", "deployment",
]


# ─── Vault Scanning ───────────────────────────────────────────────────────────

def _score_note(filename: str, content: str, topic: str | None) -> float:
    """Score a note by content richness. Higher = more likely to contain deep expertise."""
    wikilinks = content.count("[[")
    length = len(content)
    content_lower = content.lower()
    technical = sum(1 for kw in _TECHNICAL_KEYWORDS if kw in content_lower)
    score = length * 0.5 + wikilinks * 150 + technical * 50
    if topic:
        topic_lower = topic.lower()
        if topic_lower in content_lower or topic_lower in filename.lower():
            score *= 3.5
    return score


def _scan_vault(topic: str | None, max_chars: int) -> str:
    """Walk vault, score notes, return top excerpts up to max_chars."""
    if not VAULT_PATH.exists():
        return f"[Vault nicht gefunden: {VAULT_PATH}]"

    # Cap per-file read to limit memory usage on large vaults.
    # _score_note only needs content for keyword matching; full text
    # is only used for the final excerpt (re-capped in the output loop).
    _SCAN_PER_FILE_CAP = 10_000

    scored: list[tuple[float, str, str]] = []
    try:
        for root, dirnames, filenames in os.walk(VAULT_PATH):
            dirnames[:] = [
                d for d in dirnames
                if d not in _SCAN_EXCLUDED_DIRS and not d.startswith(".")
            ]
            for fname in filenames:
                if not fname.endswith(".md"):
                    continue
                fpath = Path(root) / fname
                try:
                    content = fpath.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
                if len(content.strip()) < 80:
                    continue
                # Keep only first N chars for scoring + excerpt to bound memory
                content = content[:_SCAN_PER_FILE_CAP]
                scored.append((_score_note(fname, content, topic), fname, content))
    except OSError as exc:
        return f"[Vault-Scan-Fehler: {exc}]"

    scored.sort(reverse=True, key=lambda x: x[0])

    parts: list[str] = []
    used = 0
    for _, fname, content in scored:
        if used >= max_chars:
            break
        cap = min(8_000, max_chars - used)
        excerpt = content[:cap]
        block = f"\n\n--- NOTE: {fname} ---\n{excerpt}"
        parts.append(block)
        used += len(block)

    return "".join(parts) if parts else "[Keine Notizen gefunden]"


# ─── Task Parsing ─────────────────────────────────────────────────────────────

def _extract_topic(task: str) -> str | None:
    """Extract optional topic: 'Transfer: Bremsquitschen #tool:...' → 'Bremsquitschen'.

    Returns None when no meaningful topic remains after stripping tags
    (e.g. 'Knowledge Transfer: #tool:knowledge-transfer' → empty → None).
    """
    clean = strip_metadata_tags(task)
    if ":" in clean:
        after = clean.split(":", 1)[1].strip()
        if after:
            return after
    return None


# ─── LLM Prompts ─────────────────────────────────────────────────────────────

_TOPIC_WITH = "Du fokussierst auf das vom Nutzer angegebene Thema: **{topic}**\n"
_TOPIC_AUTO = (
    "Wähle autonom die Domäne mit dem tiefsten und spezifischsten Wissen.\n"
    "Bevorzuge Wissen, das nicht sofort offensichtlich ist — verborgen in Projekten, "
    "Methoden, Vokabular.\n"
)

_KNOWHOW_PROMPT = """\
Du bist ein Wissens-Analyst. Du bekommst Auszüge aus dem persönlichen Obsidian-Vault \
einer Fachperson. Deine Aufgabe: Identifiziere EINE tiefe Wissensdomäne dieser Person.

{topic_instruction}

VAULT-AUSZÜGE:
{vault_content}

WICHTIG — Lies ZWISCHEN DEN ZEILEN:
- Die Person hat ihr Wissen oft NICHT explizit niedergeschrieben.
- Schliesse aus Projekten, gelösten Problemen, verwendeten Methoden und Vokabular.
- Beispiel: "Ich habe eine FEM-Simulation für Scheibenbremsen aufgebaut" → diese Person
  kennt nicht nur FEM, sondern spezifisch: Kontaktsteifigkeit, nichtlineare Eigenwerte,
  reibungsinduzierte Modeninstabilität, MAC-Korrelation, usw.
- Suche das SPEZIFISCHSTE, TIEFSTE Wissensgebiet — nicht "Ingenieur" oder "Programmierung".

Ausgabeformat (ALLE Abschnitte PFLICHT):

## Domäne
[Präziser Name, z.B. "Bremsquietschen-Simulation (NVH / Reibungsinstabilität)"]

## Belege aus dem Vault
[3-5 konkrete Textstellen oder Paraphrasen, die dieses Fachwissen belegen]

## Spezielles Fachwissen
[8-12 konkrete Techniken/Methoden/mathematische Konzepte/Werkzeuge, spezifisch
genug als Suchbegriffe für Cross-Domain-Transfer:
- ✅ "Komplexe Eigenwertanalyse (unsymmetrische Steifigkeitsmatrizen)"
- ✅ "Modal Assurance Criterion (MAC²)"
- ✅ "Kraftangeregte Modeninstabilität durch Reibungskopplung"
- ❌ NICHT: "Mathematik", "Simulation", "Engineering"]

## Wissenstiefe
[1–5 mit Begründung: Oberflächliche Bekanntschaft (1-2) oder echte Expertise (4-5)?]

## Warum diese Domäne
[Warum ist dies das interessanteste/tiefste/spezifischste Wissensgebiet im Vault?]
"""

_APPLICATIONS_PROMPT = """\
Du bist ein Innovation-Consultant für Cross-Domain-Wissenstransfer.

FACHWISSEN DER PERSON:
{knowhow}

Deine Aufgabe: Finde KONKRETE Probleme in ANDEREN Branchen/Domänen, wo dieses \
spezifische Fachwissen innovativ eingesetzt werden könnte.

NUTZE DEINE WEB-SEARCH-TOOLS (falls verfügbar):
1. Recherchiere aktuelle, ungelöste Probleme in anderen Branchen
2. Suche nach Firmen/Startups, die ähnliche Mathematik/Methoden verwenden
3. Finde relevante Paper, die Verbindungen aufzeigen
4. Prüfe den Stand der Technik — was existiert bereits, was fehlt noch?
Falls WebSearch nicht verfügbar ist, nutze dein bestehendes Wissen.

STRENGE REGELN FÜR ANWENDUNGEN:
- KONKRET, nie generisch:
  ❌ "könnte in der Finanzbranche genutzt werden"
  ✅ "Eigenwertzersetzung von Korrelationsmatrizen zur Regime-Erkennung in
     Hochfrequenz-Orderbuch-Daten (ähnlich Flatterinstabilität)"
- Jede Anwendung muss SPEZIFISCHE TECHNIKEN auf ein SPEZIFISCHES PROBLEM mappen
- Es muss keine physische Bremse sein — Instabilitäten, Kopplungen, Eigenmoden
  sind abstrakte Konzepte, die überall auftreten
- Bevorzuge überraschende, nicht-offensichtliche Verbindungen
- Denke: andere Branchen, andere Größenskalen, andere Zeitdomänen

AUSGABEFORMAT (3–5 Anwendungen):

## Anwendung 1: [Titel]
**Zielbranche**: [Spezifische Branche/Domäne]
**Konkretes Problem**: [2–4 Sätze: welches spezifische Problem wird gelöst?]
**Wissens-Mapping**:
- [Technik A aus Quell-Domäne] → [Wie sie hier angewendet wird]
- [Technik B] → [Konkrete Anwendung]
**Warum nicht-offensichtlich**: [Was macht diese Verbindung überraschend?]
**Neuheitsgrad**: [1–5, 5 = noch nie so gemacht]
**Recherche-Quellen**: [Links/Paper/Firmen via WebSearch]

[Wiederhole für Anwendungen 2–5]
"""

_SYNTHESIS_PROMPT = """\
Du bist ein technischer Innovations-Architekt. Basierend auf der Analyse unten, \
entwickle die BESTE Idee zu einer vollständigen, konkreten Obsidian-Notiz.

FACHWISSEN:
{knowhow}

CROSS-DOMAIN-ANWENDUNGEN:
{applications}

AUSWAHLKRITERIEN — wähle die Anwendung mit der besten Kombination aus:
- Neuheit (noch nicht existierend oder noch nicht so gemacht)
- Machbarkeit (Person hat wirklich das nötige Fachwissen)
- Impact (bedeutendes, reales Problem)
- Konkretheit (spezifisch genug zum Handeln)

Schreibe die VOLLSTÄNDIGE OBSIDIAN-NOTIZ auf Deutsch.
Beginne DIREKT mit dem YAML-Frontmatter, keine Einleitung davor.

---
date: {today}
tags: [knowledge-transfer, innovation, kreativ]
tool: knowledge-transfer
status: Idee
---

# 💡 [Spezifischer, einprägsamer Titel]

> [Ein-Satz-Kernidee — das "Warum mich das interessieren sollte"]

## Know-How Herkunft

### Quell-Domäne
[Spezifisches Fachwissens-Gebiet der Person]

### Eingesetztes Spezialwissen
- [Konkrete Technik/Methode 1]
- [Konkrete Technik/Methode 2]
- [...]

## Ziel-Domäne & Problem

### Branche / Feld
[Spezifische Zielbranche oder Domäne]

### Konkretes Problem
[3–5 Sätze: Das spezifische, gut definierte Problem.
Was ist der Pain-Point? Warum bisher ungelöst? Wer leidet darunter?]

### Stand der Technik
[Was existiert bereits? Wo ist die Lücke? Warum reicht das nicht?]

## Innovative Lösung

### Kernidee
[Die zentrale Innovation — der "Aha-Moment" in 2–3 Sätzen]

### Wie das Know-How angewendet wird
[Konkret: Methode X aus Domäne A wird eingesetzt um Problem Y in Domäne B zu lösen,
indem... — technische Beschreibung, kein Marketing-Sprech]

### Beispielhafte Umsetzung
[Was würde man konkret bauen/entwickeln? Eingabedaten, Algorithmus, Output?
Skizziere einen konkreten Prototyp oder Proof-of-Concept.]

## Markt & Potenzial

### Mögliche Nutzer / Kunden
[Wer hat dieses Problem? Wer würde dafür zahlen?]

### Warum besser als bestehende Lösungen
[Differenzierung — was macht diesen Ansatz einzigartig?]

### Größenordnung des Problems
[Schätzung: Wie groß ist dieser Markt / wie oft tritt das Problem auf?]

## Nächste Schritte

- [ ] [Erster konkreter Validierungsschritt — z.B. Experteninterview, Datensatz finden]
- [ ] [Proof-of-Concept skizzieren]
- [ ] [Weitere Recherche: ...]
- [ ] [Potenzielle Kollaboratoren / Kunden identifizieren]

## Quellen & Recherche
[Konkrete Links, Paper, Firmen, die bei der Web-Recherche gefunden wurden]

---
*Erstellt mit Knowledge-Transfer-Tool am {today}*
"""


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _make_slug(text: str) -> str:
    s = re.sub(r"[^\w\s-]", "", text)
    s = re.sub(r"[\s_]+", "-", s).strip("-")
    return s[:50]


def _extract_note_title(note_content: str) -> str:
    """Pull the # 💡 title from synthesized note content."""
    m = re.search(r"^#\s+💡\s*(.+)", note_content, re.MULTILINE)
    if m:
        return m.group(1).strip()
    m = re.search(r"^#\s+(.+)", note_content, re.MULTILINE)
    if m:
        return m.group(1).strip()
    return "Knowledge-Transfer-Idee"


# ─── Tool ─────────────────────────────────────────────────────────────────────

class KnowledgeTransferTool(BaseTool):
    name = "knowledge-transfer"
    description = (
        "Cross-Domain Know-How-Transfer: Vault-Expertise analysieren, "
        "kreative Branchenanwendungen per WebSearch finden, Obsidian-Ideen-Notiz schreiben"
    )

    def run(
        self,
        task: str,
        provider: BaseProvider,
        cwd: str | None = None,
        timeout: int | None = None,
        memory_context: str = "",
        **kwargs,
    ) -> ToolResult:
        print("  [knowledge-transfer] Starte Know-How-Transfer")
        topic = _extract_topic(task)
        if topic:
            print(f"  [knowledge-transfer] Thema vorgegeben: {topic!r}")
        else:
            print("  [knowledge-transfer] Thema: auto-discovery")

        system_prompt = _build_system_prompt(provider.name, memory_context, tool_name=self.name, cwd=cwd)
        total_in = total_out = 0

        # ── Phase 0: Vault-Scan ──────────────────────────────────────────────
        print("  [knowledge-transfer] === Phase 0: VAULT-SCAN ===")
        notify_tool_progress(self.name, 0, 4, "Vault wird intelligent durchsucht...")
        vault_content = _scan_vault(topic, TOOL_KT_VAULT_SCAN_MAX_CHARS)
        note_count = vault_content.count("--- NOTE:")
        print(f"  [knowledge-transfer] {note_count} Notizen, {len(vault_content):,} Zeichen")
        time.sleep(TOOL_INTER_STEP_SLEEP_SEC)

        # ── Phase 1: Know-How-Extraktion ─────────────────────────────────────
        print("  [knowledge-transfer] === Phase 1: KNOW-HOW-EXTRAKTION ===")
        notify_tool_progress(self.name, 1, 4, "Know-How wird aus Vault-Notizen extrahiert...")
        topic_instr = _TOPIC_WITH.format(topic=topic) if topic else _TOPIC_AUTO
        p1 = system_prompt + "\n\n" + _KNOWHOW_PROMPT.format(
            topic_instruction=topic_instr,
            vault_content=vault_content,
        )
        r1 = provider.run(p1, cwd=cwd, timeout=timeout or TOOL_KT_KNOWHOW_TIMEOUT_SEC)
        total_in += r1.input_tokens
        total_out += r1.output_tokens
        if not r1.success:
            msg = f"Know-How-Extraktion fehlgeschlagen: {r1.error}"
            notify_tool_done(self.name, 1, False, msg)
            return ToolResult(
                success=False, output="", iterations=1, error=msg,
                retryable=True, input_tokens=total_in, output_tokens=total_out,
            )
        knowhow = r1.output.strip()
        print("  [knowledge-transfer] Know-How-Extraktion abgeschlossen")
        time.sleep(TOOL_INTER_STEP_SLEEP_SEC)

        # ── Phase 2: Cross-Domain-Recherche ──────────────────────────────────
        print("  [knowledge-transfer] === Phase 2: CROSS-DOMAIN-RECHERCHE ===")
        notify_tool_progress(self.name, 2, 4, "Branchenrecherche (WebSearch)...")
        p2 = system_prompt + "\n\n" + _APPLICATIONS_PROMPT.format(knowhow=knowhow)
        r2 = provider.run(p2, cwd=cwd, timeout=timeout or TOOL_KT_APPLICATIONS_TIMEOUT_SEC)
        total_in += r2.input_tokens
        total_out += r2.output_tokens
        if not r2.success:
            msg = f"Cross-Domain-Recherche fehlgeschlagen: {r2.error}"
            notify_tool_done(self.name, 2, False, msg)
            return ToolResult(
                success=False, output=knowhow, iterations=2, error=msg,
                retryable=True, input_tokens=total_in, output_tokens=total_out,
            )
        applications = r2.output.strip()
        print("  [knowledge-transfer] Cross-Domain-Recherche abgeschlossen")
        time.sleep(TOOL_INTER_STEP_SLEEP_SEC)

        # ── Phase 3: Synthese → Obsidian-Notiz ───────────────────────────────
        print("  [knowledge-transfer] === Phase 3: SYNTHESE ===")
        notify_tool_progress(self.name, 3, 4, "Beste Idee wird ausgearbeitet...")
        today = date.today().isoformat()
        p3 = system_prompt + "\n\n" + _SYNTHESIS_PROMPT.format(
            knowhow=knowhow,
            applications=applications,
            today=today,
        )
        r3 = provider.run(p3, cwd=cwd, timeout=timeout or TOOL_KT_SYNTHESIS_TIMEOUT_SEC)
        total_in += r3.input_tokens
        total_out += r3.output_tokens
        if not r3.success:
            msg = f"Synthese fehlgeschlagen: {r3.error}"
            notify_tool_done(self.name, 3, False, msg)
            return ToolResult(
                success=False, output=f"{knowhow}\n\n{applications}", iterations=3,
                error=msg, retryable=True, input_tokens=total_in, output_tokens=total_out,
            )
        note_content = r3.output.strip()
        print("  [knowledge-transfer] Synthese abgeschlossen")

        # ── Phase 4: Notiz schreiben ──────────────────────────────────────────
        print("  [knowledge-transfer] === Phase 4: OBSIDIAN-NOTIZ SCHREIBEN ===")
        notify_tool_progress(self.name, 4, 4, "Notiz wird in 01_Ideen geschrieben...")
        title = _extract_note_title(note_content)
        slug = _make_slug(title)
        folder_name = f"KT_{today}_{slug}"
        note_dir = VAULT_PATH / _KT_OUTPUT_DIR / folder_name
        # Dedup: append hash suffix if folder already exists (same topic, same day)
        if note_dir.exists():
            import hashlib
            hash_suffix = hashlib.sha256(note_content.encode("utf-8")).hexdigest()[:6]
            folder_name = f"{folder_name}_{hash_suffix}"
            note_dir = VAULT_PATH / _KT_OUTPUT_DIR / folder_name
        note_filename = f"{folder_name}.md"
        try:
            _write_tool_file(note_dir, note_filename, note_content + "\n")
        except OSError as exc:
            msg = f"Notiz konnte nicht geschrieben werden: {exc}"
            notify_tool_done(self.name, 4, False, msg)
            return ToolResult(
                success=False, output=note_content, iterations=4,
                error=msg, input_tokens=total_in, output_tokens=total_out,
            )

        note_path = note_dir / note_filename
        msg = f"💡 {title}\nGespeichert: 01_Ideen/{folder_name}/{note_filename}"
        print(f"  [knowledge-transfer] {msg}")
        notify_tool_done(self.name, 4, True, msg)
        return ToolResult(
            success=True,
            output=(
                f"Idee '{title}' gespeichert unter:\n{note_path}\n\n"
                f"--- Know-How ---\n{knowhow}\n\n"
                f"--- Anwendungen ---\n{applications}\n\n"
                f"--- Notiz ---\n{note_content}"
            ),
            iterations=4,
            input_tokens=total_in,
            output_tokens=total_out,
        )

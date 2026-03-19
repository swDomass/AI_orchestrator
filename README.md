# AI Orchestrator

Autonomer Task-Executor für `claude`, `gemini` und `codex` CLI mit Queue-Datei, Provider-Fallback, Telegram-Steuerung und Sicherheits-/Freigabe-Layer.

Ziel: Routinearbeit aus einer Markdown-Queue ausführen lassen (Code, Reviews, Tests, Doku, Refactors), ohne API-Keys im Projekt zu verwalten. Es werden die bestehenden CLI-Logins (OAuth/Subscription) genutzt.

## Überblick

- Multi-Provider Routing mit Fallback (`Claude -> Gemini -> Codex`)
- Limit-/Kapazitätsprüfung via `cclimits` (mit lokalem JSONL-Fallback bei HTTP 429)
- Retry-Handling bei Rate-Limits / Provider-Ausfällen
- Obsidian-Queue mit `cwd:`, `#tool:`, `#agent:`, `#parallel`, `#shutdown`, `#approve:*`
- Tool-Loops (`dev-loop`, `review-loop`, `test-loop`, `research-qa`, `security-audit`)
- Skills/`SKILL.md` Discovery + Requirements-Gating
- Memory (TF-IDF + Temporal Decay) für wiederkehrende Tasks
- Execution Profiles (Provider-Reihenfolge, erlaubte Skills, Timeout, Policy-Overrides)
- Execution Policy (`AUTO` / `APPROVE` / `DENY`) mit Telegram-Freigaben
- Telegram Listener (Queue steuern, Status prüfen, Plain-Text Chat)
- Heartbeat + Doctor (Monitoring / Onboarding Checks)
- SOUL.md als zentrale Prompt-/Verhaltens-Konfiguration

## Voraussetzungen

- Python `3.10+`
- `cclimits` CLI (`npm install -g cclimits`)
- Installierte CLIs in `PATH`
  - `claude`
  - `gemini`
  - `codex`
- Vorhandene Authentifizierung in den jeweiligen CLIs

## Installation

```bash
git clone <repo-url>
cd AI_orchestrator
pip install -r requirements.txt
```

`requirements.txt` enthält:

- `pyyaml>=6.0`
- `claude-monitor>=3.0.0` *(optional — aktiviert lokalen JSONL-Fallback für Claude HTTP 429; benötigt `CLAUDE_PLAN` in `.env`)*

## Konfiguration (`.env` / Environment)

Die `.env` im Projektroot wird automatisch geladen (ohne externe dotenv-Library).

Wichtige Variablen:

- `ORCH_VAULT_PATH`
  - Pfad zum Obsidian-Vault (Standard-Fallback: `~/obsidian_vault`)
- `ORCH_QUEUE_FILE`
  - Optionaler direkter Pfad zur Queue-Datei (überschreibt Vault-Standardpfad)
- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_ID`
- `CLAUDE_PLAN` *(optional — Claude-Abo-Plan für HTTP 429 JSONL-Fallback; Werte: `pro`, `max5`, `max20`, `custom`)*

Standardpfad der Queue-Datei (wenn `ORCH_QUEUE_FILE` nicht gesetzt ist):

- `99_System/AI/agent-queue.md` im Vault

## Schnellstart

```bash
# Setup prüfen
python orchestrator.py --doctor

# Queue-Datei einmal verarbeiten
python orchestrator.py

# Watch-Modus (Heartbeat + Telegram Listener + Auto-Retry)
python orchestrator.py --watch
```

## CLI-Kommandos

```bash
python orchestrator.py                 # Einmalige Queue-Verarbeitung
python orchestrator.py --watch         # Dauerbetrieb
python orchestrator.py --dry-run       # Nur Parsing/Planung, keine Ausführung
python orchestrator.py --check-limits  # cclimits-Status anzeigen
python orchestrator.py --list-tools    # Verfügbare #tool Handler anzeigen
python orchestrator.py --doctor        # Setup validieren
python orchestrator.py --doctor --fix  # Auto-Fixes anbieten/anwenden
python orchestrator.py --doctor --fix --yes
python orchestrator.py --dashboard     # Analytics-Dashboard im Browser
```

## Queue-Datei (Syntax)

Die Queue wird aus Markdown gelesen. Offene Aufgaben sind normale Checkbox-Zeilen:

```md
- [ ] Fix bug in parser cwd:D:\projects\app #codex #timeout:10m
- [ ] Review + fix repo #tool:review-loop cwd:"D:\projects\my repo" #agent:work
- [ ] Fix login bug #tool:dev-loop cwd:D:\projects\app
- [ ] Add CSV export to dashboard #tool:dev-loop cwd:D:\projects\app #agent:work
- [ ] Add OAuth2 login flow #tool:research-qa cwd:D:\projects\app
- [ ] Architecture audit #tool:critical-review cwd:D:\projects\app
- [ ] Security audit #tool:security-audit cwd:D:\projects\app
```

Der Orchestrator ergänzt automatisch:

- `## Ergebnisse` (Resultate)
- `## Log` (interne Log-Einträge als HTML-Kommentare)

### Unterstützte Tags / Metadaten

| Feature | Syntax | Beispiel |
|---|---|---|
| Provider erzwingen | `#claude`, `#gemini`, `#codex` | `- [ ] Task #codex` |
| Claude-Modell wählen | `#claude_haiku`, `#claude_sonnet`, `#claude_opus` | `- [ ] Task #claude_haiku` |
| Tool ausführen | `#tool:<name>` | `- [ ] Review #tool:review-loop` |
| Provider einschränken (Task-Level) | `#tool_providers:<p1,p2>` | `#tool_providers:claude,gemini` |
| Working Directory | `cwd:<pfad>` | `cwd:D:\projects\repo` |
| Working Directory mit Leerzeichen | `cwd:"<pfad mit spaces>"` | `cwd:"D:\My Projects\App"` |
| Timeout | `#timeout:<n>[s|m|h]` | `#timeout:30s`, `#timeout:15m`, `#timeout:1h` |
| Profil | `#agent:<name>` | `#agent:work` |
| Parallel-Task | `#parallel` | Parent-Task mit eingerückten Subtasks |
| Task-ID vergeben | `#id:<name>` | `- [ ] Build backend #id:build` |
| Task-Abhängigkeit | `#needs:<id1,id2>` | `- [ ] Test #needs:build` |
| Shutdown nach Task | `#shutdown` | `- [ ] Backup #shutdown` |
| Preapproval | `#approve:<kategorie,...>` | `#approve:push,publish` |

### Parallel-Tasks (`#parallel`)

Parent-Task mit eingerückten Subtasks:

```md
- [ ] Release-Prep #parallel #agent:work
  - run tests #tool:test-loop cwd:D:\proj
  - review code #tool:review-loop cwd:D:\proj
  - update changelog cwd:D:\proj #codex
```

Verhalten:

- Subtasks mit **gleichem `cwd`** laufen sequentiell in einer Gruppe.
- Subtasks mit **verschiedenen `cwd`s** laufen parallel in getrennten Threads.
- Fehler in einem Subtask werden als Ergebnis erfasst; andere Subtasks laufen weiter.

### Task-Abhängigkeiten (`#id:` / `#needs:`)

Jeder Task kann eine eindeutige ID tragen und auf andere Tasks warten:

```md
- [ ] Build backend #id:build cwd:D:\projects\app
- [ ] Run integration tests #id:tests #needs:build cwd:D:\projects\app
- [ ] Deploy to staging #needs:build,tests cwd:D:\projects\app
```

Verhalten:

- Ein Task mit `#needs:` bleibt **blockiert**, solange nicht alle genannten IDs als `[x]` (erledigt) oder `[-]` (fehlgeschlagen) in der Queue stehen.
- Blockierte Tasks werden **nicht aus der Queue entfernt** — sie bleiben offen und werden beim nächsten Zyklus automatisch erneut geprüft.
- Die Queue-Kopfzeile zeigt `(N ausführbar, M blockiert)` wenn blockierte Tasks vorhanden sind.
- **Zwei-Phasen-Parsing:** Pass 1 sammelt alle offenen Tasks und ihre IDs, Pass 2 löst `#needs:` gegen erledigte IDs auf. Short-circuit wenn keine `#needs:`-Tags vorhanden sind.
- Auch `[-]`-Tasks (fehlgeschlagen) lösen Abhängigkeiten auf — das Ergebnis des Vorgängers entscheidet der nachfolgende Task selbst.

### Retry-Marker

Der Orchestrator nutzt Retry-Kommentare, um Tasks später erneut auszuführen:

```md
- [ ] Task <!-- retry: 2026-02-26 23:10 -->
```

Unterstützt auch legacy `HH:MM`; Mitternacht wird berücksichtigt.

## Built-in Tools

Aktuell registrierte `#tool:`-Handler:

- `dev-loop`
  - Research -> Execute -> Dual-Review Loop (Code Quality + Issue Resolution)
  - Beide Reviews müssen bestehen; kein Auto-Push
  - Output in `{cwd}/.dev-loop/` (research.md, round-NNN.md, summary.md)
- `review-loop`
  - Iterativer Review -> Fix -> Re-Review Loop (P1/P2/P3, alle Findings werden gefixt)
  - Max 20 Iterationen, Infinite-Loop-Detection via Signature-Vergleich
- `test-loop`
  - Iterativer Test/Fix Loop (bis Tests grün oder Max-Iterationen)
- `research-qa`
  - Read-only Pre-Implementation Research (Discovery -> Analysis -> Fragen-Katalog)
  - Output in `{cwd}/.research-qa/` (01-discovery.md, 02-analysis.md, 03-questions.md, research-qa-complete.md)
  - Keine Code-Änderungen — nur Analyse und Fragen mit [BLOCKING]-Markierungen
- `knowledge-transfer`
  - Cross-Domain Know-How-Transfer (Vault-Expertise analysieren -> Branchenanwendungen per WebSearch finden -> Obsidian-Ideen-Notiz schreiben)
  - Output in `01_Ideen/KT_YYYY-MM-DD_<slug>/`
  - Nutzt 4-Phasen-Workflow (Scan, Extraktion, Recherche, Synthese)
- `critical-review`
  - Radical-Honesty Architektur-Review — hinterfragt Konzept, Methodik und Code
  - Single-Pass, Read-only
  - Output in `{cwd}/docs/critical-review-YYYYMMDD-HHMMSS.md`
- `security-audit`
  - Zwei-Phasen Security-Workflow: Audit (read-only) → Fix + Verify
  - Phase 1 scannt: Hardcoded Secrets, Command Injection, Path Traversal, Input-Validation-Lücken (Null-Bytes, Newlines), Log Injection, unsichere Deserialisierung, SSRF
  - Phase 2 implementiert alle gefundenen Fixes und führt `pytest` aus
  - Output in `{cwd}/docs/security-audit-YYYYMMDD-HHMMSS.md`

Tool-Liste anzeigen:

```bash
python orchestrator.py --list-tools
```

## Dev-Loop (`#tool:dev-loop`)

Der `dev-loop` ist ein drei-phasiger Workflow für Issues und neue Features:

```
Phase 1 — Research
  Liest relevanten Code, versteht Problem/Feature-Anforderung,
  erstellt einen konkreten Implementierungsplan.
  Web-Suche nur wenn lokale Quellen nicht ausreichen.
  → Gespeichert in {cwd}/.dev-loop/research.md

Phase 2 — Execution
  Implementiert die Lösung anhand des Research-Plans.
  Bei Iteration > 1: beinhaltet Findings beider vorheriger Reviews.

Phase 3a — Code Quality Review  (P1/P2/P3)
  Prüft: Correctness, Clean, Secure, Performant, Maintainable,
         Testable, Robust, Documented, Compliant.
  P1/P2 = blockierend | P3 = non-blocking

Phase 3b — Issue Resolution Review  (RESOLVED/PARTIAL/UNRESOLVED)
  Prüft nur: Löst der Code den ursprünglichen Task zu 100%?
  Ignoriert Code-Qualität vollständig.

→ Beide Reviews müssen bestehen → Loop endet, kein Auto-Push.
→ Ergebnisse pro Iteration in {cwd}/.dev-loop/round-NNN.md
→ Abschlussdatei: {cwd}/.dev-loop/summary.md
```

**Abbruchbedingungen:**

| Bedingung | Verhalten |
|---|---|
| Beide Reviews bestanden | `success=True`, Telegram-Notification |
| Quality-Findings wiederholen sich | Abbruch (Infinite-Loop-Detection) |
| Review-Ergebnis (gesamt) wiederholt sich | Abbruch |
| Max-Iterationen (`TOOL_MAX_ITERATIONS`) | Abbruch mit offenem Status |
| Provider-Fehler in beliebiger Phase | `success=False`, retryable |

**Konfiguration in `config.py`:**

| Konstante | Default | Phase |
|---|---|---|
| `TOOL_DEV_RESEARCH_TIMEOUT_SEC` | 3600 (60 min) | Research |
| `TOOL_DEV_EXEC_TIMEOUT_SEC` | 7200 (2 h) | Execution |
| `TOOL_DEV_QUALITY_REVIEW_TIMEOUT_SEC` | 3600 (60 min) | Quality Review |
| `TOOL_DEV_RESOLUTION_REVIEW_TIMEOUT_SEC` | 1800 (30 min) | Resolution Review |

## Research-QA (`#tool:research-qa`)

Der `research-qa` ist ein drei-phasiger Read-only Workflow für Pre-Implementation Research:

```
Phase 1 — Discovery
  Erkundet Codebase: Docs, Verzeichnisstruktur, relevante Source-Files,
  Tests, Configs, Git-History. Kein Code wird verändert.
  → Gespeichert in {cwd}/.research-qa/01-discovery.md

Phase 2 — Analysis
  Tiefenanalyse: 2-3 Implementierungsansätze (Pros/Cons/Effort/Risk),
  Security, Performance, Testing-Strategie, Risiken, Edge Cases.
  → Gespeichert in {cwd}/.research-qa/02-analysis.md

Phase 3 — Questions
  Priorisierter Fragen-Katalog (8-20 Fragen) mit:
  - [BLOCKING]-Markierungen für kritische Blocker
  - Konkreten Code-Referenzen
  - Vorgeschlagenen Optionen (Option A / Option B)
  Kategorien: Requirements, Architecture, Scope, Technical Unknowns,
  Risk & Rollback, Testing & Validation.
  → Gespeichert in {cwd}/.research-qa/03-questions.md

→ Kombiniertes Dokument: {cwd}/.research-qa/research-qa-complete.md
→ Keine Code-Änderungen — reine Analyse und Fragen.
```

**Queue-Beispiele:**

```md
- [ ] Add OAuth2 login flow #tool:research-qa cwd:D:\projects\app
- [ ] Migrate DB from SQLite to Postgres #tool:research-qa cwd:D:\projects\backend
```

**Konfiguration in `config.py`:**

| Konstante | Default | Phase |
|---|---|---|
| `TOOL_RQA_DISCOVERY_TIMEOUT_SEC` | 1200 (20 min) | Discovery |
| `TOOL_RQA_ANALYSIS_TIMEOUT_SEC` | 1200 (20 min) | Analysis |
| `TOOL_RQA_QUESTIONS_TIMEOUT_SEC` | 600 (10 min) | Questions |

## Knowledge-Transfer (`#tool:knowledge-transfer`)

Der `knowledge-transfer` ist ein vier-phasiger Workflow zur Entdeckung neuer Anwendungen für bestehendes Fachwissen:

```
Phase 0 — Vault-Scan
  Durchsucht den Obsidian-Vault intelligent nach Notizen mit hoher Wissenstiefe.
  Bewertung nach Wikilinks, technischem Vokabular und Projekttiefe.
  → Top-Notizen werden als Kontext geladen.

Phase 1 — Know-How-Extraktion (LLM)
  Identifiziert eine spezifische, tiefe Fachdomäne der Person.
  Liest "zwischen den Zeilen" (Methoden, Vokabular, Projekttyp).

Phase 2 — Cross-Domain-Recherche (LLM + WebSearch)
  Findet konkrete Probleme in ANDEREN Branchen, die mit diesem
  Wissen gelöst werden könnten (z.B. Bremsschwingungs-Mathematik 
  angewendet auf Finanzmarkt-Instabilitäten).

Phase 3 — Synthese
  Arbeitet die beste Idee zu einer vollständigen Obsidian-Notiz aus.
  Speichert diese unter 01_Ideen/KT_YYYY-MM-DD_<slug>/.
```

**Konfiguration in `config.py`:**

| Konstante | Default | Phase |
|---|---|---|
| `TOOL_KT_VAULT_SCAN_MAX_CHARS` | 80.000 | Scan-Budget |
| `TOOL_KT_KNOWHOW_TIMEOUT_SEC` | 600 (10 min) | Extraktion |
| `TOOL_KT_APPLICATIONS_TIMEOUT_SEC` | 900 (15 min) | Recherche |
| `TOOL_KT_SYNTHESIS_TIMEOUT_SEC` | 600 (10 min) | Synthese |

## Critical Review (`#tool:critical-review`)

Der `critical-review` ist ein einmaliger Read-only Workflow, der nicht nur Code-Qualität,
sondern die gesamte Idee, Methodik und Architektur hinterfragt.

```
Dimension 0 — Concept & Fundamental Premise  ← wichtigste Dimension
  Sollte dieses Projekt überhaupt existieren?
  Was ist die Grundannahme, die — wenn falsch — das ganze Vorhaben sinnlos macht?
  Wer hat das Problem bereits gelöst, und warum ist dieser Ansatz besser?

Dimension 1 — Problem–Solution Fit
  Ist die Komplexität gerechtfertigt? Welche Annahme wurde nie hinterfragt?

Dimension 2 — Architecture & Design
  Wo bricht das Design unter realen Bedingungen? Welche versteckte Kopplung?

Dimension 3 — Code Quality
  Wo wird Komplexität versteckt statt eliminiert? Welche Tests geben falsches Vertrauen?

Dimension 4 — Operational Reality
  Was passiert um 2 Uhr morgens wenn etwas schiefläuft?

Dimension 5 — Methodology & Process
  Wo akkumuliert Tech-Debt schneller als abgebaut wird?

Dimension 6 — Risk & Blind Spots
  Was weiß der Autor nicht, dass er es nicht weiß?

→ Output: {cwd}/docs/critical-review-YYYYMMDD-HHMMSS.md
→ Keine Code-Änderungen — reine Analyse.
```

**Output-Format (immer):**
1. **Concept Verdict** (2–3 Sätze: Soll das existieren?)
2. **TL;DR** (3–5 Sätze, unverblümtes Gesamturteil)
3. **Critical Findings (P0/P1)** — Problem + Konsequenz + Mindestanforderung
4. **Significant Concerns (P2)**
5. **Methodology Critique**
6. **What's Actually Good** (konkret, kein Padding)
7. **Recommended Action** (eine Sache, keine Liste)

**Verhaltensregeln:** Kein Sandwiching, keine Hedging-Sprache, kein Loben von Aufwand oder Absicht.

**Queue-Beispiele:**

```md
- [ ] Architecture audit #tool:critical-review cwd:D:\projects\app
- [ ] Review auth module #tool:critical-review cwd:D:\projects\backend
```

**Konfiguration in `config.py`:**

| Konstante | Default | Beschreibung |
|---|---|---|
| `TOOL_CR_REVIEW_TIMEOUT_SEC` | 2400 (40 min) | Timeout für den Review-Aufruf |

## Security Audit (`#tool:security-audit`)

Der `security-audit` ist ein zwei-phasiger Workflow: zuerst werden Schwachstellen gefunden, dann direkt gefixt und mit Tests verifiziert.

```
Phase 1 — Audit (read-only, 40 min)
  Scannt systematisch alle Quelldateien nach:
  CRITICAL: Hardcoded Secrets (API-Keys, Tokens in Source-Code — nicht .env)
            Command Injection (shell=True + user-controlled Input)
            Path Traversal (user Input in Dateipfaden ohne .resolve() + Bounds-Check)
  HIGH:     Input Validation Gaps (Null-Bytes \x00, Newlines nicht gefiltert)
            Log Injection (unsanitisierter User-Input in Log-Dateien)
            Unsafe Deserialization (yaml.load ohne SafeLoader, pickle, eval)
  MEDIUM:   SSRF (user-controlled URLs an HTTP-Clients)
            TOCTOU Race Conditions (Exist-Check → Use Fenster)
            Fehlende Timeouts
  LOW:      Übermäßig breite Exception-Handler
            Sensible Daten in Logs
  → Jedes Finding mit Datei:Zeile, Angriffsvektor und konkretem Fix

Phase 2 — Fix + Verify (2 h)
  Implementiert alle Findings nach Severity (CRITICAL zuerst).
  Führt `python -m pytest tests/ -q` aus.
  Tests müssen grün sein — ggf. werden auch Tests gefixt.
  Schreibt "## Manual Actions Required" für Dinge die nicht automatisierbar sind
  (z. B. API-Keys rotieren).

→ Output: {cwd}/docs/security-audit-YYYYMMDD-HHMMSS.md
→ Bei Kapazitätserschöpfung nach Phase 1: Audit-only Report gespeichert, Fixes
  werden beim nächsten Retry fortgesetzt.
```

**Queue-Beispiele:**

```md
- [ ] Security audit #tool:security-audit cwd:D:\projects\app
- [ ] Check providers/ for injection risks #tool:security-audit cwd:D:\projects\app
```

**Konfiguration in `config.py`:**

| Konstante | Default | Phase |
|---|---|---|
| `TOOL_CR_REVIEW_TIMEOUT_SEC` | 2400 (40 min) | Audit (Phase 1) |
| `TOOL_DEV_EXEC_TIMEOUT_SEC` | 7200 (2 h) | Fix + Verify (Phase 2) |

## Skills (`SKILL.md`)

Zusätzlich zu den Built-in Tools können Skills aus `SKILL.md` entdeckt werden.

Suchreihenfolge (höhere Priorität überschreibt niedrigere):

1. `<cwd>/.orchestrator/skills/<name>/SKILL.md`
2. `./skills/<name>/SKILL.md`
3. `<vault>/99_System/AI/Skills/<name>/SKILL.md`
4. `./tools/<name>/SKILL.md`

Skills können Requirements definieren (Bins, Env, OS, Provider). Nicht erfüllte Skills werden gegatet statt blind ausgeführt.

## Execution Profiles (`#agent:<name>`)

Profile sind YAML-Dateien und bündeln Ausführungsregeln pro Task-Typ.

Typische Inhalte:

- Provider-Reihenfolge
- erlaubte / gesperrte Skills
- Timeout-Override
- Safety-/Sandbox-Level (konfigurierbar)
- Profile-spezifische Policy-Regeln (`auto/approve/deny`)

Suchorte:

- `<vault>/99_System/AI/profiles/<name>.yaml`
- `./profiles/<name>.yaml`

## Execution Policy & Freigaben

Die Policy klassifiziert Tasks in:

- `AUTO` -> läuft ohne Rückfrage
- `APPROVE` -> Telegram-Freigabe erforderlich
- `DENY` -> Task wird blockiert

Policy-Datei:

- `<vault>/99_System/AI/policy.yaml`

Telegram-Freigabe-Flow:

- `/approve`
- `/approve-all <category>`
- `/deny`
- `/skip`

Zusätzlich kann ein Task per `#approve:push,publish` Preapprovals mitgeben.

## Telegram-Steuerung

Im `--watch` Modus läuft ein Telegram Long-Poll Listener (wenn `TELEGRAM_*` gesetzt ist).

Befehle:

- `/task <beschreibung>` -> Task zur Queue hinzufügen
- `/status` -> Queue-Größe + Provider-Status
- `/limits` -> detaillierte Limits inkl. Per-Window-Breakdown (z. B. `five_hour`, `seven_day` für Claude)
- `/pause` / `/resume`
- `/approve`, `/approve-all <cat>`, `/deny`, `/skip`
- `/pick N` -> Usage-Vorschlag auswählen (1-3)
- `/decline` -> Vorschläge ablehnen
- `/cancel-shutdown`
- `/help`

Plain-Text:

- beliebiger Text -> AI-Chat (Antwort via best available provider)
- `#shutdown` als eigenständiges Tag im Text -> Shutdown planen

Rate-Limits (Telegram-seitig, gegen Spam):

| Kategorie | Limit |
|---|---|
| Commands | 20/min |
| AI-Chats | 5/min |
| Task-Adds | 10/min |

Max. Task-Länge via `/task`: 500 Zeichen (konfigurierbar via `TELEGRAM_MAX_TASK_LENGTH`).

## Memory, Heartbeat, SOUL.md

- **Memory (`memory.py`)**
  - **Drei-Layer Architektur:**
    1. **Curated (`MEMORY.md`)**: Langfristige Muster, Konventionen, Entscheidungen. Immer im Prompt (Layer 1).
    2. **Daily Logs (`daily/`)**: Append-only Verlauf von heute + gestern für zeitliche Lokalität (Layer 2).
    3. **TF-IDF Deep Search (`task_results/`)**: Keyword-Matching + Temporal Decay über alle vergangenen Tasks (Layer 3).
  - Top-K relevante Erinnerungen werden intelligent in den Prompt injiziert.
  - Automatische Archivierung nach 180 Tagen in `memory/archive/`.
  - **Layer 4 (Lessons) — deaktiviert**: `append_lesson()`/Injection vorhanden aber nicht aktiv — gespeicherte "letzte Findings" veralteten zu schnell. TODO: Neu implementieren mit LLM-generierter Summary über alle Loop-Iterationen.
- **Heartbeat (`heartbeat.py`)**
  - proaktive Checks im `--watch` Modus, konfiguriert über `99_System/AI/HEARTBEAT.md`
  - 7 Built-in Handler: `queue-idle`, `git-status`, `disk-space`, `check-limits`, `summarize`, `stale-branch`, `usage-suggest`
  - Mtime-cached Config — Änderungen an HEARTBEAT.md werden automatisch übernommen
  - Läuft zusätzlich in einem **Daemon-Thread** (60s Poll), damit geplante Checks (`log-capacity`, `usage-suggest`, `check-limits`) pünktlich feuern, auch wenn der Hauptthread stundenlang in einem langen Task blockiert ist. `run_due()` ist re-entrancy-sicher (non-blocking Lock).
- **Usage Suggester (`usage_suggester.py`)**
  - erkennt wenn Claude-Limits bald zurückgesetzt werden und noch >30% Kapazität übrig ist
  - schlägt proaktiv 2-3 Tasks via Telegram vor (Skills, Git-Changes, fehlgeschlagene Retries, Vault-Tasks)
  - Auswahl per `/pick N` oder `/decline`
  - Details und Beispiele: siehe [Usage Suggester](#usage-suggester) weiter unten
- **SOUL.md**
  - zentrale Prompt-/Persönlichkeitsdefinition im Vault (`99_System/AI/SOUL.md`)
  - provider-spezifische Abschnitte möglich (`### claude`, `### gemini`, `### codex`)
  - Mtime-cached — Änderungen wirken ab dem nächsten Task (kein Neustart nötig)
  - Enthält: Safety Rules, Projektkontext-Anweisungen, Qualitätsregeln, Error Handling, Vault-Konventionen

## Usage Suggester

Im `--watch` Modus prüft der Usage Suggester alle 5 Minuten, ob Claude-Kapazität ungenutzt verfallen würde. Wenn das Limit bald zurückgesetzt wird (< 15 Min) und noch ausreichend Kapazität übrig ist (> 30%), werden proaktiv 2–3 sinnvolle Tasks per Telegram vorgeschlagen.

### Wann feuert der Suggester?

Alle Bedingungen müssen gleichzeitig erfüllt sein:

- Queue ist **leer** (keine wartenden Tasks)
- Claude-Remaining **> 30%**
- Reset in **< 15 Minuten**
- Kein ausstehender Policy-Approval
- Letzte Vorschläge liegen mindestens **20 Minuten** zurück (Cooldown)

### Vorschlag-Strategien

Der Suggester sammelt Kandidaten aus vier Quellen und wählt die 3 besten nach Score:

| Strategie | Quelle | Score | Beispiel |
|---|---|---|---|
| **Vault Skills** | Skills aus `99_System/AI/Skills/` die nicht kürzlich (< 7 Tage) liefen | 1.0 (3.0 montags, 2.5 am Monatsanfang) | `Skill: vault-gardener` |
| **Git Changes** | Repos in `ALLOWED_CWD_ROOTS` mit uncommitted Changes | 0.8 – 1.3 (je nach Anzahl Änderungen) | `Git: my-project (12 Änderungen)` |
| **Failed Retries** | Fehlgeschlagene Tasks der letzten 3 Tage aus Memory | 0.6 | `Retry: Fix parser bug in…` |
| **Vault Tasks** | Offene Tasks aus dem Obsidian-Vault (Tasks-Plugin), per LLM auf Autonomie-Eignung bewertet | 0.5 – 2.5 (Heuristik × 0.3 + LLM-Score × 0.7) | `Vault: Datenmodell für Lastkollektiv…` |

**Vault Tasks im Detail:**

Die Vault-Task-Strategie scannt `01_Tasks/01_Tasks_Lake.md`, `01_Tasks/02_recTasks.md` und `01_Tasks/01_Projekte/**/*.md` nach offenen `- [ ]` Tasks mit Tasks-Plugin-Metadaten. Harte Filter entfernen physische/manuelle Tasks (`#Rolle/haus`, `#Rolle/Fam` etc.), blockierte (`#wait`), wiederkehrende (`🔁`) und zu lange (`#Dauer/proj`, `#Dauer/d`) Tasks. Die verbleibenden Kandidaten werden nach Urgency, Priorität, Fälligkeit und Dauer heuristisch vorbewertet. Die Top-10 werden per LLM-Call auf Autonomie-Eignung (0–10) bewertet. Falls kein Provider verfügbar ist, wird nur die Heuristik verwendet.

### Telegram-Nachricht (Beispiel)

So sieht eine typische Vorschlag-Nachricht aus:

```text
💡 Freie Kapazität verfügbar

Claude: 45% übrig, Reset in ~12 Min

Vorschläge:
  1. Skill: vault-gardener
  2. Git: AI_orchestrator (3 Änderungen)
  3. Retry: Fix Unicode-Bug in parser

/pick 1-3 — Auswahl treffen
/decline — Nichts davon
```

### Vorschläge beantworten

| Befehl | Wirkung |
|---|---|
| `/pick 1` | Wählt Vorschlag 1 — der Task wird automatisch zur Queue hinzugefügt und vom Orchestrator ausgeführt |
| `/pick 2` | Wählt Vorschlag 2 |
| `/decline` | Lehnt alle Vorschläge ab — der Suggester wartet mindestens 20 Min bis zum nächsten Versuch |
| *(keine Antwort)* | Nach 5 Minuten Timeout wird automatisch abgebrochen |

Nach `/pick N` bestätigt der Bot:

```text
👍 Vorschlag 1 angenommen…
✅ Task zur Queue hinzugefügt:
`Führe den Skill 'vault-gardener' aus: Bereinigt …`
```

### Welche Tasks eignen sich als Vorschläge?

Gute Kandidaten sind **wartungsarme, risikoarme** Aufgaben, die der Orchestrator selbständig durchführen kann:

**Ideal:**
- Vault-Skills (vault-gardener, smart-search, etc.) — brauchen keine manuelle Eingabe
- Code-Reviews für Repos mit Änderungen — der Orchestrator committet nicht ohne Freigabe
- Retry fehlgeschlagener Tasks — oft hilft ein frischer Versuch bei temporären Fehlern

**Weniger geeignet:**
- Tasks die manuelle Entscheidungen erfordern
- Destruktive Operationen (werden ohnehin durch Policy/Guardrails gefiltert)
- Tasks mit langer Laufzeit (> 10 Min) — die Kapazität könnte mitten im Task resetten

### Konfiguration

Die Schwellenwerte sind in `config.py` einstellbar:

| Konstante | Default | Beschreibung |
|---|---|---|
| `USAGE_SUGGEST_MIN_REMAINING_PCT` | `30` | Mindest-Remaining in % damit Vorschläge kommen |
| `USAGE_SUGGEST_RESET_WINDOW_SEC` | `900` (15 Min) | Reset muss innerhalb dieses Fensters liegen |
| `USAGE_SUGGEST_TIMEOUT_SEC` | `300` (5 Min) | Wartezeit auf Antwort per Telegram |
| `USAGE_SUGGEST_SKILL_COOLDOWN_DAYS` | `7` | Skills die innerhalb dieses Zeitraums liefen werden nicht vorgeschlagen |
| `USAGE_SUGGEST_RETRY_WINDOW_DAYS` | `3` | Nur fehlgeschlagene Tasks der letzten N Tage |
| `USAGE_SUGGEST_LLM_TIMEOUT_SEC` | `60` | Timeout für den LLM-Autonomie-Assessment-Call |
| `USAGE_SUGGEST_VAULT_TASK_DIRS` | `[01_Tasks/...]` | Vault-Pfade für Task-Scanning (relativ zum Vault) |

Heartbeat-Intervall wird über `HEARTBEAT.md` im Vault gesteuert (Standard: alle 5 Minuten).

## Analytics Dashboard

Der Orchestrator enthält ein eingebautes Web-Dashboard zur Visualisierung aller gesammelten Daten.

```bash
# Dashboard starten (öffnet Browser automatisch)
python orchestrator.py --dashboard

# Oder standalone mit Optionen
python dashboard.py
python dashboard.py --port 9000
python dashboard.py --no-open
```

**Dashboard-Sektionen:**

- **Übersichtskarten**: Gesamt-Tasks, Erfolgsrate, Ø Dauer, aktive Provider
- **Tasks/Tag** (30 Tage): Balkenchart der täglichen Task-Abarbeitung
- **Provider-Verteilung**: Donut-Chart der Nutzung pro Provider
- **Provider-Kapazität** (48 h / 7 Tage / 30 Tage): Drei Timeline-Charts für `Claude 5h + Codex (1)`, `Gemini-Modelle`, `Claude 7d + Codex (2)`
- **Letzte Events**: Error-Lines aus Logs + Queue-Events (merge)
- **Session-Stats**: Live-Daten der aktuellen Orchestrator-Sitzung (nur bei `--watch`)

**Datenquellen:**

| Quelle | Dateien | Was wird geparst |
|---|---|---|
| Task Results | `memory/task_results/*.md` + `archive/*.md` | YAML-Frontmatter (Task, Provider, Dauer, Erfolg) |
| Logs | `logs/orchestrator.log*` | Heartbeat check-limits Einträge + ERROR-Zeilen |
| Capacity Log | `logs/capacity-log.md` | Pro-Provider/Window-Snapshots (aktueller Stand) |
| Queue Log | `agent-queue.md` | HTML-Kommentare (`<!-- YYYY-MM-DD HH:MM \| msg -->`) |
| Session | `notifier._stats` (RAM) | Tasks done/failed, Provider-Nutzung, Startzeit |

`_get_current_limits()` liest den neuesten Eintrag pro Provider aus `logs/capacity-log.md` — kein extra `cclimits`-Aufruf im Dashboard-Prozess.

Der API-Endpunkt `GET /api/data` (JSON) wird alle 60s vom Dashboard abgefragt und intern 30s gecacht.

**Standard-Port**: `8411` (konfigurierbar via `DASHBOARD_PORT` in `config.py` oder `--port`).

## Sicherheit / Guardrails

Neben der Policy gibt es zusätzliche Guardrails im Systemprompt und in der Laufzeit:

- harte Verbote für klar destruktive Kommandos (z. B. `rm -rf`, `git reset --hard`, Force-Push)
- Begrenzung von Dateilöschungen
- Schutz vor Änderungen außerhalb des `cwd` (sofern nicht explizit angefordert)
- `cwd:`-Validierung (inkl. `ALLOWED_CWD_ROOTS`, falls gesetzt)
- Dateiänderungs-Snapshot + Änderungszusammenfassung nach Tasks

## Prompt-Aufbau (Token-Budget)

Der Orchestrator baut den Prompt aus mehreren Quellen zusammen, jede mit eigenem Token-Budget:

| Komponente | Budget | Quelle |
|---|---|---|
| Core (Task + Safety) | ~200 Tokens | `config.py` / `SOUL.md` |
| Curated Memory (L1) | ~500 Tokens | `MEMORY.md` — Dauerhafter Kontext |
| Daily Log (L2) | ~1500 Tokens | `daily/` — Verlauf heute + gestern |
| TF-IDF Memory (L3) | ~2000 Tokens | `memory.py` — Relevante Tasks |
| Wikilink-Kontext | ~3000 Tokens | `queue_manager.py` — `[[verlinkte Dateien]]` |
| Skill-Prompt | ~2000 Tokens | `SKILL.md` Body (nur bei `#tool:` Tag) |
| **Gesamt** | **~10000 Tokens** | |

Wikilinks und Memory werden intelligent gekürzt: relevante Abschnitte werden per Keyword-Matching extrahiert, nicht einfach abgeschnitten.

## Logging

- Log-Datei: `logs/orchestrator.log`
- Rotating File Handler: 5 MB pro Datei, 3 Backups
- Console-Output parallel zum File-Logging
- Konfiguration in `logging_setup.py`

## Encoding

- Dateien werden als UTF-8 gelesen
- Fallback auf Windows-1252 (`cp1252`) bei `UnicodeDecodeError`
- Alle `subprocess`-Aufrufe verwenden explizit `encoding="utf-8"`

## Doctor (`--doctor`)

`python orchestrator.py --doctor` führt 15+ Checks durch:

- Provider-CLIs (`claude`, `gemini`, `codex`)
- `git`, `cclimits`
- Vault-Pfad + Queue-Datei
- Telegram Bot-Konfiguration (`getMe` API-Call)
- `.env` (vorhanden + erforderliche Keys)
- Skills Discovery + Requirements-Gating
- Memory-Verzeichnis (`99_System/AI/memory/`)
- Heartbeat-Datei (`99_System/AI/HEARTBEAT.md`)
- Profiles-Verzeichnis + Validierung
- Policy-Datei (`99_System/AI/policy.yaml`)
- Provider-Limits (via `cclimits`)

Mit `--fix` (optional `--yes`) werden einfache Probleme automatisch erstellt/repariert, z. B. Queue-Datei oder Verzeichnisse.

## Architektur (vereinfacht)

```text
orchestrator.py
  -> dispatcher.py          (Provider-Auswahl + Fallback)
  -> queue_manager.py       (Queue lesen/schreiben, Tags, atomare Updates)
  -> parallel_runner.py     (#parallel Subtasks)
  -> tools/registry.py      (#tool Handler)
  -> skills/*               (SKILL.md Discovery / Gating / Loader)
  -> policy.py              (AUTO/APPROVE/DENY + Telegram Freigabe)
  -> profiles.py            (#agent Profile)
  -> memory.py              (Kontextspeicher)
  -> heartbeat.py           (Watch-Modus Checks)
  -> usage_suggester.py     (Proaktive Task-Vorschläge bei freier Kapazität)
  -> analytics.py           (Daten-Parsing + Aggregation für Dashboard)
  -> dashboard.py           (HTTP-Server + Chart.js Web-Dashboard)
  -> telegram_listener.py   (Telegram Commands + Chat)
  -> notifier.py            (Telegram Notifications)
  -> shutdown.py            (Shutdown Countdown / Cancel)
  -> limits.py              (cclimits Wrapper / Provider-Kapazität, Disk-Cache, HTTP 429 Resilience + lokaler JSONL-Fallback)
  -> logging_setup.py       (Rotating File Logger)
  -> doctor.py              (Setup-Validierung / --doctor)
  -> config.py              (Konstanten, .env, SOUL.md Loader)
```

## Troubleshooting

- `--doctor` zuerst ausführen
- `python orchestrator.py --check-limits` prüfen, wenn keine Provider genutzt werden
- Bei `cwd:`-Fehlern Pfad prüfen und ggf. `ALLOWED_CWD_ROOTS` in `config.py` anpassen
- Bei Telegram-Problemen `TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID` prüfen
- Logs: `logs/orchestrator.log`

## Hinweise

- Standardprompts/Guardrails kommen aus `config.py`, können aber durch `SOUL.md` im Vault übersteuert/ergänzt werden.
- Das Projekt ist Windows-freundlich umgesetzt, viele Teile funktionieren aber auch plattformübergreifend.

# AI Orchestrator

Autonomer Task-Executor für `claude`, `gemini` und `codex` CLI mit Queue-Datei, Provider-Fallback, Telegram-Steuerung und Sicherheits-/Freigabe-Layer.

Ziel: Routinearbeit aus einer Markdown-Queue ausführen lassen (Code, Reviews, Tests, Doku, Refactors), ohne API-Keys im Projekt zu verwalten. Es werden die bestehenden CLI-Logins (OAuth/Subscription) genutzt.

## Überblick

- Multi-Provider Routing mit Fallback (`Claude -> Gemini -> Codex`)
- Limit-/Kapazitätsprüfung via `npx cclimits`
- Retry-Handling bei Rate-Limits / Provider-Ausfällen
- Obsidian-Queue mit `cwd:`, `#tool:`, `#agent:`, `#parallel`, `#shutdown`, `#approve:*`
- Tool-Loops (`review-loop`, `test-loop`)
- Skills/`SKILL.md` Discovery + Requirements-Gating
- Memory (TF-IDF + Temporal Decay) für wiederkehrende Tasks
- Execution Profiles (Provider-Reihenfolge, erlaubte Skills, Timeout, Policy-Overrides)
- Execution Policy (`AUTO` / `APPROVE` / `DENY`) mit Telegram-Freigaben
- Telegram Listener (Queue steuern, Status prüfen, Plain-Text Chat)
- Heartbeat + Doctor (Monitoring / Onboarding Checks)
- SOUL.md als zentrale Prompt-/Verhaltens-Konfiguration

## Voraussetzungen

- Python `3.10+`
- Node.js (`npx` für `cclimits`)
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

`requirements.txt` enthält aktuell nur:

- `pyyaml>=6.0`

## Konfiguration (`.env` / Environment)

Die `.env` im Projektroot wird automatisch geladen (ohne externe dotenv-Library).

Wichtige Variablen:

- `ORCH_VAULT_PATH`
  - Pfad zum Obsidian-Vault (Standard-Fallback: `~/obsidian_vault`)
- `ORCH_QUEUE_FILE`
  - Optionaler direkter Pfad zur Queue-Datei (überschreibt Vault-Standardpfad)
- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_ID`

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
| Working Directory | `cwd:<pfad>` | `cwd:D:\projects\repo` |
| Working Directory mit Leerzeichen | `cwd:"<pfad mit spaces>"` | `cwd:"D:\My Projects\App"` |
| Timeout | `#timeout:<n>[s|m|h]` | `#timeout:30s`, `#timeout:15m`, `#timeout:1h` |
| Profil | `#agent:<name>` | `#agent:work` |
| Parallel-Task | `#parallel` | Parent-Task mit eingerückten Subtasks |
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

### Retry-Marker

Der Orchestrator nutzt Retry-Kommentare, um Tasks später erneut auszuführen:

```md
- [ ] Task <!-- retry: 2026-02-26 23:10 -->
```

Unterstützt auch legacy `HH:MM`; Mitternacht wird berücksichtigt.

## Built-in Tools

Aktuell registrierte `#tool:`-Handler:

- `review-loop`
  - Iterativer Review -> Fix -> Re-Review Loop (P1/P2/P3)
- `test-loop`
  - Iterativer Test/Fix Loop (bis Tests grün oder Max-Iterationen)

Tool-Liste anzeigen:

```bash
python orchestrator.py --list-tools
```

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
- `/limits` -> detaillierte Limits
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
  - speichert Task-Ergebnisse als Markdown mit YAML-Frontmatter in `99_System/AI/memory/task_results/`
  - TF-IDF Keyword-Matching + Temporal Decay (Halbwertszeit: 30 Tage)
  - Top-K relevante Erinnerungen (Standard: 5) werden in den Prompt injiziert (max. 2000 Tokens)
  - Automatische Archivierung nach 180 Tagen in `memory/archive/`
- **Heartbeat (`heartbeat.py`)**
  - proaktive Checks im `--watch` Modus, konfiguriert über `99_System/AI/HEARTBEAT.md`
  - 7 Built-in Handler: `queue-idle`, `git-status`, `disk-space`, `check-limits`, `summarize`, `stale-branch`, `usage-suggest`
  - Mtime-cached Config — Änderungen an HEARTBEAT.md werden automatisch übernommen
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
| Queue Log | `agent-queue.md` | HTML-Kommentare (`<!-- YYYY-MM-DD HH:MM \| msg -->`) |
| Session | `notifier._stats` (RAM) | Tasks done/failed, Provider-Nutzung, Startzeit |

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
| Memory-Kontext | ~2000 Tokens | `memory.py` — relevante vergangene Tasks |
| Wikilink-Kontext | ~3000 Tokens | `queue_manager.py` — `[[verlinkte Dateien]]` |
| Skill-Prompt | ~2000 Tokens | `SKILL.md` Body (nur bei `#tool:` Tag) |
| **Gesamt** | **~8000 Tokens** | |

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
- `node`, `git`, `npx cclimits`
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
  -> limits.py              (cclimits Wrapper / Provider-Kapazität, TTL-Cache)
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

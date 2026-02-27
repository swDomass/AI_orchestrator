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
- `/cancel-shutdown`
- `/help`

Plain-Text:

- beliebiger Text -> AI-Chat (Antwort via best available provider)
- `#shutdown` als eigenständiges Tag im Text -> Shutdown planen

## Memory, Heartbeat, SOUL.md

- **Memory (`memory.py`)**
  - speichert Task-Ergebnisse und liefert relevanten Kontext für ähnliche Aufgaben
- **Heartbeat (`heartbeat.py`)**
  - proaktive Checks (z. B. Queue-Idle, Disk-Space, Git-Staleness)
- **SOUL.md**
  - zentrale Prompt-/Persönlichkeitsdefinition im Vault
  - provider-spezifische Abschnitte möglich (`### Claude`, `### Gemini`, `### Codex`)

## Sicherheit / Guardrails

Neben der Policy gibt es zusätzliche Guardrails im Systemprompt und in der Laufzeit:

- harte Verbote für klar destruktive Kommandos (z. B. `rm -rf`, `git reset --hard`, Force-Push)
- Begrenzung von Dateilöschungen
- Schutz vor Änderungen außerhalb des `cwd` (sofern nicht explizit angefordert)
- `cwd:`-Validierung (inkl. `ALLOWED_CWD_ROOTS`, falls gesetzt)
- Dateiänderungs-Snapshot + Änderungszusammenfassung nach Tasks

## Doctor (`--doctor`)

`python orchestrator.py --doctor` prüft u. a.:

- Provider-CLIs (`claude`, `gemini`, `codex`)
- `node`, `git`, `npx cclimits`
- Vault-/Queue-Pfade
- Telegram Bot-Konfiguration
- `.env`
- Skills + Gating
- Memory-/Heartbeat-Dateien
- Profiles / Policy-Dateien

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
  -> telegram_listener.py   (Telegram Commands + Chat)
  -> notifier.py            (Telegram Notifications)
  -> shutdown.py            (Shutdown Countdown / Cancel)
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

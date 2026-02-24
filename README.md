# AI Orchestrator

Autonomer Task-Executor für Claude Code, Gemini CLI und Codex CLI.
Nutzt bestehende Abo-Authentifizierung (OAuth/Subscription) — keine API-Keys nötig.

## Features

- **Multi-Provider Routing**: Claude → Gemini → Codex, automatischer Fallback
- **Usage-Tracking**: Überwacht Limits aller Provider via `npx cclimits`
- **Auto-Retry**: Schläft bei erschöpfter Usage und retried nach Reset
- **Iterative Tools**: Review-Loop und Test-Loop für autonomes Debugging
- **Obsidian Integration**: Liest Tasks aus `agent-queue.md`, injiziert `[[Wikilink]]`-Kontext
- **Telegram Notifications**: Status-Updates bei Task-Erledigung, Fehlern und Queue-Abschluss
- **Autonome Ausführung**: CLIs laufen mit vollen Berechtigungen (Datei-Zugriff, Code-Execution)

## Setup

```bash
# Python 3.10+ (keine externen Dependencies, nur stdlib)
# Benötigt: npx cclimits, claude CLI, gemini CLI, codex CLI

# 1. Repository klonen
git clone <repo-url>
cd AI_orchestrator

# 2. Environment konfigurieren
cp .env.example .env
# .env mit eigenen Werten ausfüllen (Vault-Pfad, Telegram-Token)
```

### Voraussetzungen

| Tool | Installationsbefehl | Auth |
|------|---------------------|------|
| Claude Code | `npm install -g @anthropic-ai/claude-code` | Anthropic Subscription |
| Gemini CLI | `npm install -g @anthropic-ai/gemini-cli` | Google OAuth |
| Codex CLI | `npm install -g @openai/codex` | ChatGPT Subscription |
| cclimits | `npx cclimits` (kein Install nötig) | Liest bestehende Auth |

## Verwendung

```bash
# Queue einmal abarbeiten
python orchestrator.py

# Kontinuierlich laufen (schläft & retried automatisch)
python orchestrator.py --watch

# Nur Limits anzeigen
python orchestrator.py --check-limits

# Tasks validieren ohne auszuführen
python orchestrator.py --dry-run

# Verfügbare Tools anzeigen
python orchestrator.py --list-tools
```

## Queue-Datei

Liegt im Vault unter `99_System/AI/agent-queue.md`:

```markdown
## Queue
- [ ] Schreibe Zusammenfassung von [[Projekt X]]
- [ ] Analysiere Code in [[EEG Programm]] #codex
- [ ] Review und fixe Bugs #tool:review-loop cwd:/d/programmieren/projekt
- [ ] Tests fixen #tool:test-loop cwd:/d/programmieren/projekt
- [ ] Fasse Dokument zusammen #gemini #timeout:10m

## Ergebnisse
<!-- Outputs erscheinen hier automatisch -->

## Log
<!-- Automatische Protokolleinträge -->
```

### Task-Syntax

| Feature | Syntax | Beispiel |
|---------|--------|----------|
| Provider erzwingen | `#claude`, `#gemini`, `#codex` | `- [ ] Task #gemini` |
| Working Directory | `cwd:/pfad` | `- [ ] Fix bug cwd:/d/projekt` |
| Timeout | `#timeout:Xs/m/h` | `- [ ] Langer Task #timeout:15m` |
| Tool verwenden | `#tool:name` | `- [ ] Review #tool:review-loop` |
| Vault-Kontext | `[[Notiz Name]]` | `- [ ] Fasse [[Bericht]] zusammen` |

### Provider-Tags

- `#claude` — bevorzugt Claude Code
- `#gemini` — bevorzugt Gemini CLI
- `#codex` — bevorzugt Codex CLI
- Kein Tag — Dispatcher wählt automatisch (Claude → Gemini → Codex)

### Verfügbare Tools

| Tool | Tag | Beschreibung |
|------|-----|-------------|
| Review-Loop | `#tool:review-loop` | Iteratives Review > Fix > Re-Review bis keine P1/P2/P3 Findings |
| Test-Loop | `#tool:test-loop` | Tests ausführen > Fehler fixen > Re-Run bis grün |

## Routing-Logik

```
Claude verfügbar?  → Claude (--print --dangerously-skip-permissions)
  nein ↓
Gemini verfügbar (irgendein Tier)?  → Gemini CLI (--yolo, wählt Tier intern)
  nein ↓
Codex verfügbar?  → Codex (exec --full-auto)
  nein ↓
Alle voll → Sleep bis frühester Reset → Retry
```

### Fehlerbehandlung

- **Rate Limit** → nächster Provider übernimmt, cclimits trackt Reset-Zeitpunkt
- **Provider unreachable** → 30 Min Cooldown, andere Provider werden versucht
- **Transiente Fehler** → Exponential Backoff (max 2 Retries pro Provider)
- **Tool-Loop stuck** → Infinite-Loop-Detection beendet bei identischen Findings/Fehlern

## Konfiguration

### Environment-Variablen (`.env`)

| Variable | Beschreibung | Pflicht |
|----------|-------------|---------|
| `ORCH_VAULT_PATH` | Pfad zum Obsidian Vault | Ja |
| `ORCH_QUEUE_FILE` | Pfad zur Queue-Datei (default: `VAULT/99_System/AI/agent-queue.md`) | Nein |
| `TELEGRAM_BOT_TOKEN` | Telegram Bot Token für Benachrichtigungen | Nein |
| `TELEGRAM_CHAT_ID` | Telegram Chat ID | Nein |

### Weitere Einstellungen (`config.py`)

| Einstellung | Default | Beschreibung |
|------------|---------|-------------|
| `PROVIDER_COOLDOWN_SEC` | 30 Min | Cooldown nach Erreichbarkeitsfehler |
| `MIN_CAPACITY_PERCENT` | 5% | Mindest-Kapazität für Provider |
| `TASK_TIMEOUT_SEC` | 5 Min | Standard-Timeout pro Task |
| `MAX_RETRIES_PER_PROVIDER` | 2 | Retries bevor Fallback zum nächsten Provider |
| `MAX_CONTEXT_FILE_SIZE` | 1 MB | Max Dateigröße für Kontext-Injection |
| `TOOL_MAX_ITERATIONS` | 10 | Max Iterationen für Review/Test-Loops |
| `TOOL_REVIEW_TIMEOUT_SEC` | 20 Min | Timeout pro Review-Iteration |
| `TOOL_FIX_TIMEOUT_SEC` | 40 Min | Timeout pro Fix-Iteration |

## Projektstruktur

```
AI_orchestrator/
├── orchestrator.py      # Haupteinstiegspunkt (CLI)
├── config.py            # Konfiguration + .env Loader
├── dispatcher.py        # Provider-Auswahl und Routing
├── limits.py            # Usage-Limits via npx cclimits
├── queue_manager.py     # Queue-Datei lesen/schreiben, Kontext-Injection
├── notifier.py          # Telegram-Benachrichtigungen
├── providers/
│   ├── base.py          # BaseProvider ABC mit Cooldown
│   ├── claude.py        # Claude Code CLI (--print --dangerously-skip-permissions)
│   ├── gemini.py        # Gemini CLI (--yolo)
│   └── codex.py         # Codex CLI (exec --full-auto)
├── tools/
│   ├── base_tool.py     # BaseTool ABC
│   ├── registry.py      # Tool-Registry und #tool: Tag-Parser
│   ├── review_loop.py   # Iteratives Code-Review mit P1/P2/P3 Findings
│   └── test_loop.py     # Iteratives Test/Fix bis grün
├── .env                 # Credentials (gitignored)
├── .env.example         # Vorlage für .env
└── .gitignore
```

## Hinweise

- **Windows**: CLI-Befehle verwenden automatisch `.cmd`/`.exe`-Suffixe
- **Gemini Tiers**: Alle drei Modellvarianten (3-Flash, Flash, Pro) werden überwacht — Gemini CLI wählt intern
- **File Locking**: Queue-Datei wird mit plattformspezifischem Locking geschützt (msvcrt/fcntl)
- **Encoding**: UTF-8 mit Fallback auf cp1252 (Windows-Kompatibilität)

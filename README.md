# AI Orchestrator

Autonomer Task-Executor für Claude Code, Gemini CLI und Codex CLI.
Nutzt bestehende Abo-Authentifizierung (OAuth/Subscription) — keine API-Keys nötig.

## Features

- **Multi-Provider Routing**: Claude → Gemini → Codex, automatischer Fallback.
- **Usage-Tracking**: Überwacht Limits aller Provider via `npx cclimits`.
- **Auto-Retry**: Schläft bei erschöpfter Usage und retried nach Reset.
- **Skills System**: Dynamische Tool-Discovery und Gating (YAML-basierte SKILL.md).
- **Memory System**: Langzeitgedächtnis mit TF-IDF Suche und Temporal Decay.
- **Heartbeat System**: Proaktive Hintergrund-Tasks (Git-Status, Disk-Space, Queue-Idle).
- **Execution Profiles**: Named Configs via `#agent:work` (Provider-Order, Roots, Timeouts).
- **Execution Policy**: Sicherheits-Layer mit AUTO/APPROVE/DENY (Telegram Approval-Flow).
- **Parallel Sub-Agents**: Mehrere Tasks gleichzeitig in verschiedenen CWDs abarbeiten (#parallel).
- **SOUL.md**: Zentrale Identität und Regeln als Markdown-Datei im Vault.
- **Obsidian Integration**: Liest Tasks aus `agent-queue.md`, injiziert `[[Wikilink]]`-Kontext.
- **Telegram Control**: Steuerung via `/status`, `/limits`, `/pause`, `/resume`, `/help`, `#shutdown`.
- **Autonome Ausführung**: CLIs laufen mit vollen Berechtigungen (Datei-Zugriff, Code-Execution).

## Setup

```bash
# Python 3.10+ (keine externen Dependencies außer pyyaml)
# Benötigt: npx cclimits, claude CLI, gemini CLI, codex CLI

# 1. Repository klonen
git clone <repo-url>
cd AI_orchestrator

# 2. Dependencies installieren
pip install -r requirements.txt

# 3. Environment konfigurieren
# .env im Projekt anlegen oder ORCH_VAULT_PATH setzen.
```

## Verwendung

```bash
# Queue einmal abarbeiten
python orchestrator.py

# Kontinuierlich laufen (Watch-Modus + Heartbeat + Telegram)
python orchestrator.py --watch

# System-Diagnose (Checks & Auto-Fixes)
python orchestrator.py --doctor [--fix]

# Nur Limits anzeigen
python orchestrator.py --check-limits
```

## Queue-Datei & Syntax

Liegt im Vault unter `99_System/AI/agent-queue.md`:

| Feature | Syntax | Beispiel |
|---------|--------|----------|
| Parallel Tasks | `#parallel` | `- [ ] Task #parallel` (gefolgt von eingerückten Subtasks) |
| Profiles | `#agent:<name>` | `- [ ] Review #agent:work` |
| Shutdown | `#shutdown` | `- [ ] Backup #shutdown` (fährt PC nach Task herunter) |
| Provider erzwingen | `#claude`, `#gemini` | `- [ ] Task #gemini` |
| Working Directory | `cwd:/pfad` | `- [ ] Fix bug cwd:/d/projekt` |
| Tool verwenden | `#tool:name` | `- [ ] Review #tool:review-loop` |

## Architektur

```
orchestrator.py  ──→  policy.py / profiles.py  ──→  Sicherheits- & Profilprüfung
     │                     │
     ├──→  parallel_runner.py  ──→  Parallel-Threads pro CWD
     ├──→  skills/discovery.py ──→  SKILL.md & Tool-Discovery
     ├──→  memory.py           ──→  Gedächtnis-Suche (TF-IDF + Decay)
     ├──→  heartbeat.py        ──→  Geplante Hintergrund-Checks
     ├──→  shutdown.py         ──→  OS-Shutdown Flow (Countdown/Cancel)
     │
     └──→  dispatcher.py  ──→  providers/ (Claude, Gemini, Codex)
```

## Sicherheit (Execution Policy)

- **AUTO**: Routine-Tasks (Read, Write, Commit, Test) laufen ohne Rückfrage.
- **APPROVE**: Kritische Aktionen (Push, Publish, Delete outside CWD) erfordern Bestätigung via Telegram.
- **DENY**: Gefährliche Befehle (`rm -rf /`, force-push to master) werden sofort geblockt.

*Blanket approvals* können pro Session via `/approve-all <category>` erteilt werden.

## Hinweise

- **SOUL.md**: Bearbeite die Datei im Vault, um die Persönlichkeit und Sicherheitsregeln der KI global zu steuern.
- **Doctor**: Nutze `python orchestrator.py --doctor`, um sicherzustellen, dass alle Pfade, Tools und Authentifizierungen korrekt sind.
- **Mitternachts-Support**: Retry-Marker (`<!-- retry: HH:MM -->`) berücksichtigen den Tageswechsel.

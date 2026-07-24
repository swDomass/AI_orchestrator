# ADR-001: Claude-Prompt-Zustellung über Temp-Datei statt stdin-Pipe

- **Status:** accepted
- **Datum:** 2026-07-24
- **Betrifft:** AI_orchestrator / `providers/claude.py` + `providers/process_runner.py`

## Kontext

Der Claude-Provider pipte den voll-assemblierten Prompt (~70 K Tokens, Task-Text am ENDE)
über einen gepufferten stdin-Feeder-Thread an die CLI, auf Windows durch eine Shell. Der
Prompt-Tail ging intermittierend verloren, ohne dass `write/flush/close` eine Exception warf
(≤ 64-KB-Tail landet im Kernel-Puffer, das Kind hört vorher zu lesen auf) → `stdin_error=None`
→ der Lauf wurde als `success=true` verbucht, obwohl nichts geschah. Zwei identische stille
Ausfälle (2026-07-20, 2026-07-24). Die am 2026-07-20 eingebaute write-seitige Detektion kann
diesen Fall strukturell nicht fangen (dort als „R6"-Blind-Spot dokumentiert, aber als
irrelevant fehleingeschätzt).

## Entscheidung

Der Claude-Provider stellt den Prompt über eine **Temp-Datei als stdin** zu
(`run_with_watchdog(..., stdin_via_file=True)`, `_write_temp_prompt`) mit **`shell=False`**:
die gesamte Bytefolge liegt vor dem ersten Lesen vor, das Kind liest bis zu einem
deterministischen Datei-EOF — der Tail kann nicht mehr verloren gehen, und der cmd.exe-Layer
entfällt.

## Alternativen

- **(a) Prompt als positionales argv-Argument** (`claude … -- <task>`) — verworfen: `task` ist
  der ~280-KB-Gesamtprompt und sprengt das Windows-Kommandozeilen-Limit (32.767). Mechanisch
  per Smoke bewiesen, aber nur für kleine Prompts tauglich — die es hier nicht gibt.
- **(b) Reine Detektion über Token-Zahl** (input_tokens≈0 ⇒ Fehler) — verworfen: das
  `result`-Event zählt nur den letzten Turn; ein arbeitender Task mit vielen Tool-Calls zeigt
  ebenfalls winzige `input_tokens` (ha-health 07-24: `input_tokens:18`, voll funktionierend) →
  Erfolg/Fehlschlag überlappen, False-Positives.
- **(c) 2026-07-20-Detektion allein beibehalten** — widerlegt: genau die kehrte den Ausfall
  nicht ab. Bleibt als Netz für den Feeder-Pfad der anderen Provider, ist aber nicht die Lösung.

## Konsequenzen

- **+** Behebt die Ursache (deterministisches EOF, keine Feeder-Close-Race, keine
  cmd.exe-Weiterleitung), injection-frei, kein Prompt-Cache-Impact (Bytes/Reihenfolge
  unverändert), präziserer Tree-Kill direkt auf `claude.exe`. E2E belegt (156-KB-Prompt,
  Tail-Task ausgeführt).
- **−** Ein neuer optionaler Zustellpfad im `process_runner` + Temp-Datei-Lifecycle
  (Schreiben/Schließen/Löschen) zu verwalten. **Host-Annahme:** `claude` ist eine native `.exe`
  (belegt: `C:\Users\domin\.local\bin\claude.exe`). Wäre es je ein `.cmd`/`.ps1`-Shim, schlägt
  der `shell=False`-Spawn **laut** fehl (`FileNotFoundError` → „claude CLI not found"), kein
  stiller Ausfall.

Scope bewusst nur Claude: Codex/Vibe/Gemini nutzen denselben Feeder-Pfad (latentes Restrisiko),
aber ohne beobachteten Ausfall — der neue Parameter ist additiv (Default `False`), sie bleiben
unberührt; eine Umstellung wäre ein eigener Change mit eigenem Blast Radius.

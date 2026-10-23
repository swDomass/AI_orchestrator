# CLAUDE.md

Guidance for Claude Code (claude.ai/code) when working in this repository.

> **Detail-Docs**: Implementierungs-Details pro Modul → [`docs/architecture/components.md`](docs/architecture/components.md). Patterns/Invariants + Phase B Feature Flag → [`docs/architecture/patterns.md`](docs/architecture/patterns.md). Bei Code-Änderungen beide Files (diese Datei + zugehörige Detail-Datei) aktualisieren.

## Obsidian-Projektdoku

`<your-vault>/01_Tasks/01_Projekte/.../AI-System-Intelligence-Orchestrator.md`

## Project

Autonomous task orchestrator routing work across Claude Code and Codex CLI, plus opt-in providers that never enter the fallback chain: OpenRouter (HTTP), Mistral Vibe (reviewer-only) and opencode CLI (tag-activated third external voice, Stufe 2 — a ZDR-guaranteed path for customer code, capped by its own OpenRouter key rather than uncapped like the other two). Gemini (HTTP API or legacy CLI) is still implemented but retired from every active path (2026-08-15). Tasks come from an Obsidian vault Markdown queue (`99_System/AI/agent-queue.md`). Pure Python stdlib + pyyaml, Windows-first.

## Commands

```bash
# Stand 2026-10-23 abends (Windows, Dashboard B1 Queue-Editor nach externem Codex-Review + Panels sichtbar öffnen, +367 Tests): 3827 passed / 0 failed / 2 skipped in 377-600 s; +367 über die 3460 auf 796e141 = test_queue_editor_parse 37, _build 88, _lint 28, _mutations 66, _backups 18, _integration 4, _raw 40, test_dashboard_queue_api 35, test_dashboard_queue_js 50, test_dashboard_static +1.
# Ältere Stände und Messnotizen: docs/test-und-lint-historie.md
# -p no:randomly: Das Flag bleibt als reproduzierbare Standardreihenfolge stehen (auch in ci.yml).
python -m pytest tests/ -q -p no:randomly

# Run a single test file / single test
python -m pytest tests/test_parallel_runner.py -v
python -m pytest tests/test_queue_manager_regressions.py::test_extract_cwd_supports_spaces -v

# Install dependencies
pip install -r requirements.txt

# Validate setup
python orchestrator.py --doctor
python orchestrator.py --doctor --fix --yes

# Modes
python orchestrator.py                # single-shot
python orchestrator.py --watch        # continuous + heartbeat
python orchestrator.py --dry-run      # parse queue, no execute
python orchestrator.py --check-limits # provider capacity
python orchestrator.py --list-tools   # available #tool: handlers
python orchestrator.py --dashboard    # analytics web dashboard (opens browser; --watch serves it anyway, DASHBOARD_AUTOSTART)
python orchestrator.py --lint-queue   # validate agent-queue.md

# Lint / typecheck (config in pyproject.toml; CI runs both, advisory only — continue-on-error)
ruff check .            # ruff 1122 (Stand 2026-10-09; Messnotizen: docs/test-und-lint-historie.md)
python -m mypy .        # mypy 122/38 (Stand 2026-10-09; Messnotizen: docs/test-und-lint-historie.md)
```

> Details zu `pyproject.toml` (Ruleset, `[project]`-Table, lint-baseline, `limits.py:85`-Fix) ausgelagert nach `.claude/rules/scripts-and-setup.md`.

## Architecture

**Execution flow**: Queue read → provider selection (fallback chain) → profile loading → policy check → skill gating → memory context injection → prompt building → provider execution → result persistence → heartbeat.

**Module map** (details in [`docs/architecture/components.md`](docs/architecture/components.md)):

### Core orchestration

> Modul-Details je Themenbereich ausgelagert: `core-dispatch-policy.md`, `queue-and-tasks.md`, `orchestrator-runtime.md`, `quota-and-analytics.md`, `scripts-and-setup.md`, `misc-infra.md`, `git-auto-commit.md` — siehe Index am Dateiende.

### Providers (`providers/`)

> Provider-Implementierungsdetails (Claude/Codex/Gemini/OpenRouter/Vibe/opencode, `process_runner.py`) ausgelagert nach `.claude/rules/providers.md`.

### Tools (`tools/`)

> Tool-Details (dev-loop, review-loop, critical-review, scientific-investigation, brainstorm, security-audit, pr-babysitter, ...) ausgelagert nach `.claude/rules/tools-catalog.md`.

### Scripts

> Details zu `scripts/safety_hook.py` und `scripts/build_audit_pack.py` ausgelagert nach `.claude/rules/scripts-and-setup.md`.

## Phase B Feature Flag — `CLAUDE_SESSION_ENABLED`

Default **OFF**. Opt-in via `.env`. When enabled, Claude tools share CLI session UUIDs across phases (`--session-id`/`--resume`) for ~30-50 % token savings. Rollback by setting `CLAUDE_SESSION_ENABLED=false` and restart. Details + retention policy → [`docs/architecture/patterns.md#phase-b-feature-flag--claude_session_enabled`](docs/architecture/patterns.md#phase-b-feature-flag--claude_session_enabled).

## Key Patterns

Stichworte — Long-form in [`docs/architecture/patterns.md`](docs/architecture/patterns.md):

- **Singletons with threading** — `PolicyEngine`, `UsageSuggester`, providers; own `_lock` + `threading.Event`
- **Provider-bound model tags** — `config.model_id_for_provider(tag, provider)` returns `None` on mismatch; `_forced_model` via `threading.local()`
- **Reasoning effort** — `#effort:<level>` (`low|medium|high|xhigh|max`, `config.CLAUDE_EFFORT_LEVELS`) → `_forced_effort` via `threading.local()`, gesetzt/restauriert an denselben Stellen wie `_forced_model` (`orchestrator.py` ×2, `parallel_runner.py`). War bis 2026-09-04 Claude-exklusiv (nur `providers/claude.py` las die Property). **`providers/opencode.py` liest sie jetzt auch** — kein Mapping, roher Pass-through an `--variant <level>` (opencode toleriert auch unbekannte Werte, gemessen `--variant xhigh` → exit 0). Codex/Gemini/Vibe/OpenRouter ignorieren den Tag weiterhin konstruktionsbedingt. **Bekannte Lücke:** `queue_linter`s `effort_non_claude`-Warnung ("Tag bleibt ohne Wirkung") ist für einen auf opencode gerouteten Task inzwischen sachlich falsch — nicht in diesem Paket gefixt, nur benannt. Ohne Tag **kein** Flag, damit die Session-Einstellung nicht überschrieben wird
- **OpenRouter/Vibe/opencode never in fallback chain** — `dispatcher._PRIORITY` omits alle drei; `.get(name)` not `[name]` for silent fallback. Registrierung ist bedingt (API-Key bzw. Binary auf dem PATH bzw. `OpencodeProvider.is_available()`). opencodes Fehlen in `_PRIORITY` ist zusätzlich Stufe 3 des eigenen Plans, bewusst nicht gebaut. **Ein** Hänger-Mechanismus ist seit 2026-09-04 gemessen: ein Modellbezeichner, den opencode nicht auflösen kann, erzeugt keinen Fehler, sondern einen Stillstand in der Initialisierung (2× reproduziert, keine Ausgabe, kein Exit; Kontrolllauf mit gültigem Alias: 8 s, exit 0). Abgefangen ist er — `OPENCODE_IDLE_TIMEOUT_SEC` (300 s) → Watchdog-Kill → `hang` → Requeue bis `MAX_HANG_RETRIES` — und er kostet kein Geld, weil das Modell nie erreicht wird; seit demselben Tag warnt `doctor.check_opencode_cli()` vorab, wenn `OPENCODE_DEFAULT_MODEL` in der `opencode.json` fehlt. Ob das AUCH die Ursache des am 2026-08-21 beobachteten Hängers war, ist damit **nicht** belegt — Stufe 3 bleibt ungebaut, bis das geklärt ist
- **`_NO_FALLBACK_PROVIDERS` degradiert nicht zu einem anderen Provider** — `dispatcher._NO_FALLBACK_PROVIDERS = {"vibe", "opencode"}` (umbenannt 2026-09-04 von `_REVIEWER_ONLY`, als opencode dazukam — „reviewer-only" beschrieb eine Menge mit einem schreibenden Provider nicht mehr): ein `#vibe`- oder `#opencode`/`#opencode_*`-Tag ohne registrierten Provider liefert **keinen** Provider (Task wird geparkt), statt still auf die Default-Kette durchzufallen (`_PRIORITY` = claude → codex; Gemini ist registriert, steht aber seit 2026-08-15 nicht mehr darin) — mit provider-benannter Log-Zeile, damit „vibe fehlt" und „opencode fehlt" unterscheidbar sind. Zwei verschiedene Gründe für denselben Mechanismus: bei Vibe ist es Blast Radius (nicht-schreibender Reviewer würde durch einen schreibenden Executor ersetzt — erbeten war eine nicht-schreibende Zweitmeinung), bei opencode die Tag-Absicht (kein Claude-Kontingent, bei Kundencode der einzige ZDR-Weg — ein stiller Fallback würde genau das unterlaufen). Bei OpenRouter ist derselbe Fallback harmlos (Executor → Executor)
- **Child-Env statt `os.environ`** — `run_with_watchdog(..., env=…)` bekommt eine fertig gemergte Kopie (Popen ersetzt, merged nicht). Provider sind geteilte Singletons in Parallel-Threads: `os.environ` mutieren würde das Modell eines Runs in einen anderen lecken
- **Mtime-cached config** — policy, profiles, SOUL.md, heartbeat use `(mtime, content)` tuples
- **Token-budget injection** — `_build_prompt()` truncates to `PROMPT_*_TOKENS`
- **Sidecar file locking** — `queue_manager.py` `.lock` file (msvcrt/fcntl)
- **Subtask-aware queue mutations** — `mark_done/mark_retry/finalize` accept `subtasks` kwarg
- **Task dependencies** — `#id:`/`#needs:`, two-pass resolution, blocked-task header
- **Schedule tags** — `#at:`/`#every:` reuse retry primitive; queue file is single source of truth
- **Harness-Index (2026-10-09)** — `harness_index.py`: SQLite über lokale Transkripte/Rollouts/`opencode.db`/Ledger/Marker, eigener Unterprozess alle 30 min (Scheduler-Thread aus dem Autostart), Lock-Datei, natürliche Schlüssel (`message.id` über **alle** Dateien, `max()` je Zähler — bis zu vier Zeilen je API-Antwort mit **nicht** identischer `usage`, dazu Kopien in Subagent-Transkripten derselben Elternsitzung; Ledger je Zeile, Neuaufbau bei Hash-Änderung), kein Text, keine Ordnernamen (cwd nur Hash, Fehler nur Typname), Schema 4 ohne Migration (Windows: Spitze privater Speicher statt des vom Hintergrundmodus auf ~32 MB gedeckelten Arbeitsspeichers; CPU-Klasse nie über BELOW_NORMAL). Reiter „Harness“ liest nur `/api/harness` (`harness_index.dashboard_payload`, read-only, < 1 s auch bei fehlender/gesperrter/kaputter SQLite). Details `.claude/rules/quota-and-analytics.md`
- **Queue-Editor (2026-10-23)** — `queue_editor.py` (HTTP-frei) + dünne Routen in `dashboard.py` (`/api/queue*`). **Identitätsregel:** eine Zeile ist `ref = {lines, occurrence, occurrences}` (Blocktext + n-tes identisches Vorkommen), keine Zeilennummer und kein Datei-SHA; weicht genau dieser Block ab → 409 `changed`, ändert sich die Anzahl identischer Blöcke → 409 `ambiguous` (`_mutate`). **Ein Lock-Durchgang:** jede Änderung läuft in einem `queue_manager.apply_queue_update(transform)` (Wrapper um `_apply_update`, `transform` kann mehrfach laufen, Ergebnis/Fehler über Holder-Closure), zusätzlich `_EDIT_LOCK`; **zweiter Byte-Vergleich** (`_read_raw(qfile) != raw` → `changed`) unmittelbar vor der Rückgabe, weil Obsidian ohne Lock schreibt. **Sicherung vor jeder Änderung** (`logs/queue-backups/<Zeit>-<aktion>.{md,json}`, `.json` zuletzt = Commit-Marker, `config.QUEUE_BACKUP_DIR`/`ORCH_QUEUE_BACKUP_DIR`, `QUEUE_BACKUP_KEEP=20`); scheitert sie → 503, nichts geändert; undo = nur die neueste Sicherung, nur wenn `after_sha` zur Datei passt. **Laufsperre in-process:** `orchestrator._start_dashboard_autostart` übergibt `in_flight=lambda: _in_flight_task` (Getter, kein Wert) → `dashboard._QUEUE_IN_FLIGHT`; **Falle zweite Modulkopie:** `run_orchestrator.ps1` startet `orchestrator.py` als Skript, andere Module importieren eine zweite Kopie — der Getter muss das Global der Kopie lesen, in der `_set_in_flight` läuft (Test `test_the_autostart_getter_reads_the_module_copy_whose_set_in_flight_ran`). **Eigenständig** (`python dashboard.py`, kein Getter): Modus `standalone`, Aktionen an offenen Zeilen + raw/undo fragen nach (428 `confirm_unknown_running`); **Active-Runs-Signal** (`analytics._load_active_runs`, Tool + Task-Text) sperrt in beiden Modi (423). Gefährliche Tags (`#shutdown`, `#approve:`, `#tool_providers:`) → 428 `confirm_dangerous`, auch bei raw für neue/geänderte offene Zeilen. Keine In-flight-Datei (ADR-004). Fehlertexte fest aus `queue_editor.ERROR_TEXTS`, nie `str(exc)`
- **Dashboard-Autostart (2026-10-09)** — `run_watch` → `_start_dashboard_autostart()` → `dashboard.start_autostart()`: direkt nach den Startprüfungen, **vor** `STARTUP_DELAY_SEC`; Daemon-Thread auf 127.0.0.1, Bind **im** Thread (kein Warten), unter Windows exklusiv (`SO_EXCLUSIVEADDRUSE`), wirft nie, jeder Fehler = genau eine Warnzeile (`logger.warning` + `append_log`). `DASHBOARD_AUTOSTART` (an) / `DASHBOARD_OPEN_BROWSER` (aus, nur Autostart). Eine Exception von dort würde `main()`s Absturznetz dem ersten Queue-Task anrechnen — deshalb der Import von `dashboard` im `try`. Server mehrfädig (`_ReuseServer(ThreadingMixIn, TCPServer)`, Daemon-Threads), `_DATA_BUILD_LOCK` = eine Analytics-Berechnung gleichzeitig (`/api/harness` + Active-Runs außerhalb); Seite aus `dashboard_ui/` über die feste Liste `_STATIC_FILES` (exakter Pfad, kein Traversal), Chart.js/Zoom vendort mit SHA-256-Pin im Test (kein CDN), CSP + Sicherheits-Header auf jeder Antwort, nie `Access-Control-*`. Schreibschranke `do_POST`, Prüfreihenfolge: Host (Loopback + gebundener Port) → Origin (Loopback-Menge **und** gleich `http://`+Host) → `Sec-Fetch-Site` → `X-Orch-Token` (Schlüssel je Start, nur bei Loopback-Host in die Seite eingebettet; nach Neustart offene Tabs neu laden) → Route → Content-Type → Content-Length → ≤ 256 KiB; Routen `/api/ping` + 9 Queue-Routen (`/api/queue/{add,edit,delete,retry,duplicate,move,raw,undo,lint}`, B1), Erweiterung über `post_route`/`ApiError`; `DASHBOARD_ALLOWED_HOSTS` gilt nur fürs Lesen. Fremder `Host`, `Sec-Fetch-Site: cross-site|same-site` oder fremdes `Origin` → 403

> Fallen- und Messwissen zu Queue/Tasks, Providern, Tools, Git-Auto-Commit, Orchestrator-Runtime, Quota/Analytics und Policy ausgelagert — siehe Index am Dateiende.

## Testing Conventions

- `unittest.mock.patch` + `pytest` fixtures (`tmp_path`, `monkeypatch`)
- Mock `config._load_dotenv` when importing modules with config side-effects
- All tests synchronous (no async)
- Test files mirror source: `tests/test_<module>.py`

> Test-Fixture-Details (`tests/conftest.py`, provider-bedingte Registrierung) ausgelagert nach `.claude/rules/core-dispatch-policy.md`.

> Details zum Vault-/docs-Schreibschutz der Testsuite ausgelagert nach `.claude/rules/quota-and-analytics.md`.

## Safety Rules (enforced in code)

- **Hard deny**: `scripts/safety_hook.py` (Claude Code `PreToolUse` hook, works even with `--dangerously-skip-permissions`)
- **Soft deny**: `SAFETY_RULES` injected into the non-Claude provider prompts (`config.SYSTEM_PROMPTS`) — they have no hook system. **The asymmetry resolved on 2026-08-17:** `SAFETY_RULES` says *"NEVER push to remote unless the task explicitly says to"* and says **nothing about `git commit`** — and since the hook stopped denying commits, the two layers finally agree. Consequence to be aware of: **nothing in hook or prompt restricts `git commit` any more.** If unattended runs should be kept out of git state, `policy.yaml`'s `approve:` entries are the only remaining lever. **Since 2026-09-10 the orchestrator writes git state itself** (`git_commit.py`, one `orch/*` branch per successful task — see Key Patterns), which is deliberate and is the reason the executor prompt keeps saying `Do NOT commit`: the provider must not commit, because the dev-loop reviewers judge the uncommitted working-tree diff, so the orchestrator does it *after* the loop instead. That also makes the feature work for Codex and opencode, which cannot commit at all. Levers to switch it off: `GIT_AUTO_COMMIT=false` globally, `#no-commit` per queue line.
- **Blocked categories**: `rm -rf`, `git push --force`, `git reset --hard`, `git clean -f`, `DROP/TRUNCATE TABLE`, `DELETE FROM` without WHERE, `format`/`mkfs`/`diskpart`, fork bombs, raw disk writes, credential exfiltration, Windows `del /s`/`rd /s /q`/`Remove-Item -Recurse -Force`
- **Git state (revised 2026-08-17)**: `git push` is hard-denied; **`git commit` is not** — it was, from 2026-08-15 until 2026-08-17. The original rule was meant to keep unattended runs from changing git state, but nothing in the hook ever tested for "unattended": it is registered for *every* Bash call, so it blocked interactive sessions exactly as hard, including ones where the user had just asked for the commit, with no way to override it from inside the session. Dropped for that reason, keeping the irreversible half — a commit is local and revertible (`git reset`), a push leaves the machine and cannot be taken back. Built from `config._git_pattern()` (the hook's import-failure fallback mirrors it as `_fallback_git_pattern()` in `scripts/safety_hook.py`; `_git_subcommand_pattern()` never existed — corrected 2026-09-05) so `git -C <path> push` / `git -c k=v push` / `git --no-pager push` match while read-only commands containing the word (`git log --grep=push`, `git show`, `git rev-parse`) do not. Two known limits: the hook only covers Claude (Codex is fenced by its own sandbox — see below), and the match runs on the raw command line, so a command that merely *quotes* the text `git push` is blocked too.
- **opencode writes with neither a hook nor a sandbox (2026-09-04)**: `providers/opencode.py` runs `--agent extern-dev` whenever `read_only` is not set — and the plain-task path calls `provider.run(prompt, cwd=cwd, timeout=timeout)` without it, so **every ordinary `#opencode` task runs with the writing agent**. The Claude hook does not cover it (different CLI), and there is no Codex-style `--sandbox` flag. The only real fence is the machine-local `~/.config/opencode/opencode.json`: its `extern-dev` block carries `bash: deny`, `webfetch`/`websearch: deny` and `external_directory: deny` (measured 2026-09-04 — `external_directory` is the LAST matching permission rule, which is also why an out-of-`--dir` file can never be re-read and why the prompt cap has to fail loudly instead of truncating). That file is deliberately never written by this repo, and `doctor.check_opencode_cli()` validates only the **ZDR half** of it (aliases, `small_model`, spending cap) — the permission blocks are not checked at all. So the guarantee for the opencode path is exactly as strong as that user-level config, the same way `experimental_windows_sandbox = true` is the whole guarantee for Codex.
- **Codex git writes**: `providers/codex.py` runs `--sandbox workspace-write`, which on Windows denies writes inside `.git/` (`git add`/`commit`/`remote add` fail with "Permission denied" on `.git/index.lock`, verified 2026-08-15) — but only while `experimental_windows_sandbox = true` in `~/.codex/config.toml`. That user-level flag is the whole guarantee for the Codex path.
- **Pre-task git snapshot lives in its own ref namespace (2026-09-03)**: `_git_snapshot()` writes the `git stash create` commit to `refs/orchestrator-backup/<timestamp>` via `git update-ref` (empty `oldvalue` = create-only, `_2` suffix on a same-second collision) — **never to `refs/stash`**, which is the user's own list and where the old `git stash store` piled up 11 unreclaimed entries because nothing in the repo ever ran `stash drop`/`clear`/`pop`. `_prune_snapshot_refs()` caps the namespace on each write: `age >= GIT_SNAPSHOT_PROTECT_DAYS (14) AND (age > GIT_SNAPSHOT_MAX_AGE_DAYS (30) OR outside the newest GIT_SNAPSHOT_MAX_COUNT (50))`. The protect window is a **veto** over both caps, not a third equal test. **Begründung neu gefasst 2026-09-11**, als der Per-Task-Auto-Commit landete: die alte Fassung („night tasks deliberately do not commit, so the snapshot is the only undo") war damit an beiden Hälften falsch. Die Schlussfolgerung überlebt, der Grund ist ein anderer — Snapshot und `orch/*`-Branch decken **Verschiedenes** ab. Der Branch trägt das **Ergebnis** des Laufs für die Pfade, die der Commit genommen hat; der Snapshot trägt den Zustand **davor**, Index eingeschlossen, für alles, was der Commit bewusst liegen ließ (fremd gestaged, beim Start schon schmutzig, Konflikt, Rename) — plus jeden fehlgeschlagenen Lauf und jeden übersprungenen Commit, wo überhaupt kein Branch existiert. Das sind genau die Fälle, in denen fremde Arbeit im Spiel ist, das Undo ist dort also weiterhin einmalig. Umgekehrt deckt der Branch etwas ab, das der Snapshot nie konnte: `git stash create` erfasst **keine** untracked Dateien, der Commit schon. Deshalb wird auf Task-Erfolg weiterhin nichts gelöscht; der count cap kann in einem Repo mit hoher Änderungsrate ausgehungert werden (bewusst). Scoped by a `startswith(GIT_SNAPSHOT_REF_PREFIX)` re-check per ref, so branches, tags, `refs/stash` and the adjacent `refs/orchestrator-backup-sibling/` are out of reach; never raises. Ref name + `git stash apply <ref>` go to **both** `print` and `logger` — `git stash list` no longer shows the snapshot, and `run_orchestrator.ps1` starts `--watch` without stdout redirection. **Alterung laeuft ueber `committerdate`, nicht ueber den Eintritt in den Namensraum** — ein von Hand hereingeschobener Ref (die 11 Maerz-Archive) behaelt das alte Commit-Datum und faellt beim allerersten Prune; die Ref-*Namen* tragen dieselben alten Timestamps, Namens-Alterung hilft also nicht. Deshalb: jede Loeschung mit Name **und** Sha ins Log (bis `git gc` ueber `git stash apply <sha>` erreichbar), und **mehr als ein Ref in einem Durchgang** geht auf `WARNING` statt `INFO` — ein *routinemaessiger* Prune raeumt hoechstens einen Ref ab, mehrere auf einmal sind die Massenverlust-Form und duerfen im unbeaufsichtigten 03:00-Lauf nicht wie Housekeeping aussehen
- CWD validation against `ALLOWED_CWD_ROOTS`
- Skill gating (bins, env vars, OS, provider) before execution
- Policy layer can block tasks pending Telegram approval. **The request shows content since 2026-10-05**: task text up to ~1500 bytes (was 100 characters), `cwd`, the repo state of a git `cwd` (branch, `git status --short` count, commits ahead of `@{u}` — Repo-Zustand mit Gesamtfrist 5 s, sonst ohne Repo-Block — all git calls together in a daemon thread, because a per-call timeout is not hard on Windows) and, per reason, the text that tripped the rule (`PolicyEngine.match_excerpts()`). It is sent BEFORE the provider runs, so there is no diff to show. **Fail-closed since 2026-10-06:** ANY exception in classification or approval (`get_engine`, `check_task`, `request_approval`, the notification, the re-stamp after /deny) holds the task — `Policy-Prüfung gestört — Task bleibt in Queue: …` to log, `append_log` and `notify_error`, then `_mark_retry_checked(+10 min)` and replay `approval_unavailable`; if the requeue fails too, the line simply stays open for the next cycle. Until then it ran the task **unapproved and without the DENY check**. The notifier still never raises (a raising request is one that never arrives, so the short-form fallback stays). **A broken policy.yaml is held too since 2026-10-08:** a file that exists but does not load (parse error, non-mapping root, a section parser raising) makes `check_task` raise `PolicyUnreadableError` on every call — until then it classified with no rules (broken at start: every task AUTO, `git push` included) or the stale last good ones. While broken, the engine re-reads the file on every call, so a repair is picked up even with an unchanged mtime. A **missing** file stays AUTO on purpose (the queue lives in the same vault folder; `policy_missing`/`check_policy_file` report it). The hold's Telegram alert is throttled to one per cause (exception class + text, never the task) and `POLICY_HOLD_NOTIFY_WINDOW_SEC` (6 h, `time.monotonic()`), and only a **delivered** alert counts (`notify_error` returns `bool` since Korrekturrunde 1 — `_send` swallows network errors and returns False; an undelivered alert is retried on the next hold); once every `check_task` of a task passes again, one all-clear (`_notify_policy_recovered`, only for causes raised while classifying; the causes are forgotten only once `send_message` returned True). Log, `append_log` and the requeue still run on every hold. Only the exact answer `approved` runs — `""`, `None` or an unknown word takes the same hold (`_UnusableApprovalAnswerError`; `""` is reachable through the engine's one pending slot). **dev-loop's plan approval** (`tool_phases.dev-loop.plan_approval: approve`) asks through the same engine and is fail-closed too: only `approved` executes, every other outcome ends ❌ with `approval_denied|skipped|timeout|unavailable` (details in `.claude/rules/tools-catalog.md`)

Full pattern catalog → [`docs/architecture/patterns.md#safety-rules-enforced-in-code--details`](docs/architecture/patterns.md#safety-rules-enforced-in-code--details).

## Ausgelagerte Themen

Wer an einem dieser Themen arbeitet, ohne eine passende Datei anzufassen (Antwort aus dem Gedächtnis, Änderung per Skript), liest die Regeldatei vorher von Hand.

| Thema (Stichworte) | Regeldatei | Auslösende Pfade |
|---|---|---|
| Provider-Fallback-Kette, Policy-Gates, uncapped Provider, Modell-/Tag-Regex, Reasoning-Effort, HTTP-429-Resilienz, Token-Schätzung, Test-Fixtures für Provider-Registrierung | `core-dispatch-policy.md` | `dispatcher.py`, `policy.py`, `profiles.py`, `limits.py`, `tests/conftest.py` |
| Queue-Parser, `#needs:`/`#id:`, HTML-Kommentar-Falle, Prompt-Reihenfolge + Abschlussregel, `#verify:` + `verify_absent`, Format-Fehler-Zähler, ❌/✅-Stempel, queue-healing, Queue-Editor (Dashboard) | `queue-and-tasks.md` | `queue_manager.py`, `queue_editor.py`, `queue_linter.py`, `queue_healing.py` |
| Hauptschleife, `main()`-Crash-Breaker, `_snapshot_dir`-`nul`-Bug, Shutdown-State-Machine, Logging | `orchestrator-runtime.md` | `orchestrator.py`, `shutdown.py`, `logging_setup.py` |
| Claude/Codex/Gemini/OpenRouter/Vibe/opencode-Provider, Liveness-Watchdog, stdin-Zustellung, Fehlerklassifikation, `auth_expired`-Saga | `providers.md` | `providers/*.py` |
| dev-loop/review-loop/critical-review/scientific-investigation/brainstorm/security-audit/pr-babysitter, Budget-Landung, Arbeitsbaum-Gate | `tools-catalog.md` | `tools/*.py` |
| Per-Task-Auto-Commit, HEAD-nie-bewegen, Index-Regel, Datei-Deckel, Cleanup-Guard | `git-auto-commit.md` | `git_commit.py` |
| Quota-Kalibrierung, Analytics, Dashboard, Harness-Index (SQLite), Config-Konstanten, Memory/Noop-Filter, Heartbeat, `.env`-Parsing, Vault-Schreibschutz der Tests | `quota-and-analytics.md` | `quota_calibration.py`, `quota_state.py`, `analytics.py`, `dashboard.py`, `harness_index.py`, `dashboard_ui/**`, `config.py`, `usage_suggester.py`, `usage_budget.py`, `memory.py`, `heartbeat.py`, `tests/conftest.py` |
| Doctor-Checks, `safety_hook.py`, `build_audit_pack.py`, Lint-/Typing-Baseline (`pyproject.toml`) | `scripts-and-setup.md` | `doctor.py`, `scripts/*.py`, `pyproject.toml` |
| Telegram, Idempotency, Session-Registry, Replay/Taxonomy, Preflight, Skill-Suggester, PR/CI-Watcher, Parallel-Runner, Watchdog-Skript | `misc-infra.md` | `notifier.py`, `telegram_listener.py`, `idempotency.py`, `session_registry.py`, `replay.py`, `taxonomy.py`, `preflight.py`, `skill_suggester.py`, `skills/index.py`, `gh_helpers.py`, `ci_watcher.py`, `parallel_runner.py`, `run_orchestrator.ps1` |

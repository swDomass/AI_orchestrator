# CLAUDE.md

Guidance for Claude Code (claude.ai/code) when working in this repository.

> **Detail-Docs**: Implementierungs-Details pro Modul → [`docs/architecture/components.md`](docs/architecture/components.md). Patterns/Invariants + Phase B Feature Flag → [`docs/architecture/patterns.md`](docs/architecture/patterns.md). Bei Code-Änderungen beide Files (diese Datei + zugehörige Detail-Datei) aktualisieren.

## Obsidian-Projektdoku

`<your-vault>/01_Tasks/01_Projekte/.../AI-System-Intelligence-Orchestrator.md`

## Project

Autonomous task orchestrator routing work across Claude Code and Codex CLI, plus opt-in providers that never enter the fallback chain: OpenRouter (HTTP), Mistral Vibe (reviewer-only) and opencode CLI (tag-activated third external voice, Stufe 2 — a ZDR-guaranteed path for customer code, capped by its own OpenRouter key rather than uncapped like the other two). Gemini (HTTP API or legacy CLI) is still implemented but retired from every active path (2026-08-15). Tasks come from an Obsidian vault Markdown queue (`99_System/AI/agent-queue.md`). Pure Python stdlib + pyyaml, Windows-first.

## Commands

```bash
# Linux, Cloud (2026-09-24, CPython 3.12.3 und 3.13.12, nach Lint-Paket 3+4):
# 2837 passed / 0 failed / 7 skipped in 62-72 s — 2844 gesammelt wie unten.
# CI (.github/workflows/ci.yml) laeuft auf 3.12 + 3.13. Die 7 Skips: 6 an Windows
# gebunden (4 schon vorher; 2026-09-24 dazu der Backslash-Pfad in test_queue_linter
# und pwsh in test_verify_pin_deps), 1 fehlendes optionales claude_monitor. Die 14
# test_usage_suggester-Faelle, die unten als umgebungsabhaengig rot stehen, patchen
# TELEGRAM_ENABLED seit 2026-09-24 selbst.
# Run all tests (2844 passed / 0 failed in 180 s — measured 2026-09-17 in the main
# checkout on master after merging orch/orchroute-2026-09-17 (+19) and
# orch/orchscicodes-2026-09-17 (+14): 2811 + 19 + 14 = 2844 exactly. The review-loop
# on orchroute had measured 2816 passed / 14 failed in a git worktree, where
# tests/test_usage_suggester.py's 14 environment-dependent cases are red on master
# too, see README Known Limitations. The +14 from orchscicodes are the
# ProviderCallError tests across tests/test_scientific_investigation_*.py.
# orchroute's +19 over 2811: tests/test_dispatcher_routing.py (+17: 6+4 parametrized
# barred-tag cases, 2 capped-unchanged cases, 5 single tests),
# tests/test_orchestrator_tool_tasks.py (+1) and tests/test_queue_linter_policy.py
# (+1: a review-loop-round-2 test pinning that policy_unreadable/policy_empty no
# longer claim every provider bar is gone; two OTHER tests there are renamed,
# not counted). Previously: 2811 passed / 0 failed in 120 s — measured 2026-09-16, +10 aus
# dem externen Review-Nachzug (oc r1) on top of that day's 2801: 2740 at the
# start of that day, +27 from fix/verify-and-test-isolation, +34 from
# fix/error-codes-and-usage-suggest, 2740 + 27 + 34 = 2801, +10 = 2811 exactly.
# Earlier: 2711 passed / 0 failed in 148 s, measured 2026-09-10 as the
# LAST step of the per-task auto-commit package, against 2643 earlier the same
# day, 2571 before that, 2533 in 93 s
# on 2026-09-09, 2447 in
# 123 s / 102 s on 2026-09-05 and ~90 s on 2026-09-04.
# The +68 is fully attributed, counted with --collect-only rather than estimated:
#   tests/test_git_commit.py            47  (NEW — real git repos in tmp_path)
#   tests/test_orchestrator_commit.py   16  (NEW — the wiring, commit_run_result mocked)
#   tests/test_queue_manager_regressions.py  63 -> 68  (+5, the #no-commit tag)
# 2643 + 47 + 16 + 5 = 2711 exactly, so there is no unexplained drift in this
# package. The count grew twice during the package as review rounds landed: the
# last 9 tests exist only because the fourth and fifth reviewers went at the
# ALREADY-FIXED code — see the Review-Bilanz under Key Patterns. The final two
# are the Auftrag DONE criterion itself (two dev-loops in one repo) plus its
# counter-probe with GIT_AUTO_COMMIT=False, which is what proves that test can
# see the defect at all. Treat 90-155 s as the
# normal band (a 146 s outlier was measured under load, and the 141 s above is
# the same shape with 57 more tests, ~35 of which drive real git subprocesses in
# tmp_path — those are seconds, not milliseconds, and that is the deliberate
# trade: a mocked subprocess.run would prove only that the code calls the
# functions it calls); the spread
# is machine load, not a regression signal. The count is a drift baseline, so
# re-measure it as the LAST step before a commit — it was written mid-change
# twice on 2026-09-09 and was stale in three files both times.
# The 2026-09-10 delta is +110 over the 2533 the day started at, all of it in
# four NEW files (counted with --collect-only, not estimated):
#   tests/test_orchestrator_snapshot_dir.py         6   (the `nul` crash)
#   tests/test_orchestrator_crash_breaker.py       29   (traceback + circuit breaker)
#   tests/test_logging_setup.py                     3   (thread excepthook)
#   tests/test_dev_loop_landing_and_resume.py      72   (landing round + capacity resume)
# tests/test_task_dependencies.py was edited the same day but its count is
# unchanged at 39: one test was renamed (sixteen -> seventeen terminal paths)
# and its AST guard rewritten, nothing added or removed. tests/test_dev_loop.py
# is likewise unchanged in count: one test switched its provider error from
# rate_limit to timeout, because a capacity error is now deliberately re-routed
# to capacity_exhausted instead of rotating to the next provider.
# -p no:randomly is load-bearing: tests/test_telegram_listener.py is
# order-dependent, so a random seed can turn the suite red with no code change.
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
python orchestrator.py --dashboard    # analytics web dashboard
python orchestrator.py --lint-queue   # validate agent-queue.md

# Lint / typecheck (config in pyproject.toml; CI runs both, advisory only — continue-on-error)
# Linux, Cloud 2026-09-24 nach Lint-Paket 3+4: ruff 1131 Befunde (vorher 1510),
# mypy 132 Fehler in 39 Dateien (unveraendert) → docs/lint-baseline-2026-09-02.md,
# Status-Nachtrag. Die Zahlen darunter sind aeltere Messungen der Entwicklungsmaschine.
ruff check .            # 1465 findings, re-measured 2026-09-10 as the LAST step, after
                        # the per-task auto-commit package: +13 over the 1452 below.
                        # git_commit.py + its two test files carry 14 of them and every
                        # one is in a class this repo already carries deliberately and
                        # WITHOUT a single noqa: 8x PLW1510 (subprocess.run without
                        # check= — the binding precedent orchestrator._git_snapshot does
                        # the same 8 times), 2x PLC0415 (the lazy orchestrator import
                        # that breaks the module cycle, and one in a test), 1x SIM105,
                        # 1x PLR0911 + 1x PLR0912 + 1x PLR0917 (return/branch/argument
                        # count of commit_run_result, from the many precondition and
                        # skip paths the spec asks for, plus the dirty_before baseline
                        # the adversarial review made necessary). Two findings WERE fixed rather than
                        # accepted: an unused `# noqa: BLE001` (RUF100 — a noqa for a
                        # rule this repo does not enable is actively misleading) and an
                        # unsorted import block. Careful with bulk noqa removal: the
                        # first attempt at the RUF100 fix also matched the pre-existing
                        # em-dash noqa at orchestrator.py:1306 and turned that line into
                        # a syntax error — caught by ruff E701, restored byte-identical.
                        # Earlier note, still true: 1452 findings after
                        # the dev-loop landing/resume package: +6 over the 1446 below,
                        # all six accounted for and all deliberate — 3x PLC0415 (lazy
                        # imports of queue_manager/parallel_runner/subprocess inside
                        # the resume path, matching the 36 the repo already carries
                        # without a single noqa), 1x PLR0911 (return count of
                        # DevLoopTool.run, which gained the landing-round exits), and
                        # 2x from the same class in the touched blocks.
                        # tests/test_dev_loop_landing_and_resume.py: 0 findings.
                        # Earlier note, still true: 1446 findings, measured 2026-09-10 (ruff 0.16.1); +6 over
                        # the 1440 of 2026-09-09, all six accounted for and all in
                        # orchestrator.py: 3x PLW0603 (the `global _in_flight_task`
                        # of the crash register — the repo carries 46 of these in
                        # 10 modules and not one `# noqa`, so suppressing only mine
                        # would BE the deviation), 2x SIM105 (try/except/pass in the
                        # crash handler, where an explicit block reads plainer than
                        # contextlib.suppress) and 1x PLR0912 (branch count of
                        # _charge_process_crash). Both new test files: 0 findings.
                        # Earlier note, still true: +37 over
                        # the 2026-09-05 value of 1403, from queue_linter's policy
                        # checks and the three new test files. The 2026-09-02
                        # baseline of 1351 predates providers/opencode.py,
                        # openrouter_budget.py and their tests. Mostly PLC0415
                        # (deliberate lazy imports) — advisory, no gate
python -m mypy .        # 132 errors in 38 files, re-measured 2026-09-10 as the LAST
                        # step of the per-task auto-commit package — IDENTICAL to the
                        # measurement below, so git_commit.py and the two new test
                        # files contribute zero. Same note, same day (mypy 2.3.1,
                        # lenient config). The +1 over the 131/37 below is NOT
                        # attributed — none of the errors sits in the 2026-09-10
                        # code (checked: no error line falls in orchestrator.py's
                        # new crash-breaker block, and neither new test file nor
                        # logging_setup.py reports one). Was 131 in 37 files:
                        # 132 on 2026-09-04; the one that went away is this day's
                        # work, not a fix aimed at mypy — re-measure, don't assume)
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
- Policy layer can block tasks pending Telegram approval

Full pattern catalog → [`docs/architecture/patterns.md#safety-rules-enforced-in-code--details`](docs/architecture/patterns.md#safety-rules-enforced-in-code--details).

## Ausgelagerte Themen

Wer an einem dieser Themen arbeitet, ohne eine passende Datei anzufassen (Antwort aus dem Gedächtnis, Änderung per Skript), liest die Regeldatei vorher von Hand.

| Thema (Stichworte) | Regeldatei | Auslösende Pfade |
|---|---|---|
| Provider-Fallback-Kette, Policy-Gates, uncapped Provider, Modell-/Tag-Regex, Reasoning-Effort, HTTP-429-Resilienz, Token-Schätzung, Test-Fixtures für Provider-Registrierung | `core-dispatch-policy.md` | `dispatcher.py`, `policy.py`, `profiles.py`, `limits.py`, `tests/conftest.py` |
| Queue-Parser, `#needs:`/`#id:`, HTML-Kommentar-Falle, Prompt-Reihenfolge, `#verify:`, Format-Fehler-Zähler, ❌/✅-Stempel, queue-healing | `queue-and-tasks.md` | `queue_manager.py`, `queue_linter.py`, `queue_healing.py` |
| Hauptschleife, `main()`-Crash-Breaker, `_snapshot_dir`-`nul`-Bug, Shutdown-State-Machine, Logging | `orchestrator-runtime.md` | `orchestrator.py`, `shutdown.py`, `logging_setup.py` |
| Claude/Codex/Gemini/OpenRouter/Vibe/opencode-Provider, Liveness-Watchdog, stdin-Zustellung, Fehlerklassifikation, `auth_expired`-Saga | `providers.md` | `providers/*.py` |
| dev-loop/review-loop/critical-review/scientific-investigation/brainstorm/security-audit/pr-babysitter, Budget-Landung, Arbeitsbaum-Gate | `tools-catalog.md` | `tools/*.py` |
| Per-Task-Auto-Commit, HEAD-nie-bewegen, Index-Regel, Datei-Deckel, Cleanup-Guard | `git-auto-commit.md` | `git_commit.py` |
| Quota-Kalibrierung, Analytics, Dashboard, Config-Konstanten, Memory/Noop-Filter, Heartbeat, `.env`-Parsing, Vault-Schreibschutz der Tests | `quota-and-analytics.md` | `quota_calibration.py`, `quota_state.py`, `analytics.py`, `dashboard.py`, `config.py`, `usage_suggester.py`, `usage_budget.py`, `memory.py`, `heartbeat.py`, `tests/conftest.py` |
| Doctor-Checks, `safety_hook.py`, `build_audit_pack.py`, Lint-/Typing-Baseline (`pyproject.toml`) | `scripts-and-setup.md` | `doctor.py`, `scripts/*.py`, `pyproject.toml` |
| Telegram, Idempotency, Session-Registry, Replay/Taxonomy, Preflight, Skill-Suggester, PR/CI-Watcher, Parallel-Runner, Watchdog-Skript | `misc-infra.md` | `notifier.py`, `telegram_listener.py`, `idempotency.py`, `session_registry.py`, `replay.py`, `taxonomy.py`, `preflight.py`, `skill_suggester.py`, `skills/index.py`, `gh_helpers.py`, `ci_watcher.py`, `parallel_runner.py`, `run_orchestrator.ps1` |

# Test- und Lint-Historie

Ausgelagert aus CLAUDE.md am 2026-10-23 (sync-docs); oberster Eintrag = jüngster Stand

Konvention: Der Befehlsblock in CLAUDE.md trägt nur den aktuellen Stand. Der abgelöste Stand wandert unverändert hierher, oben einfügen.

## Test-Stände

```bash
# Stand 2026-10-27 (Windows, API-Credits ADR-005 nach externem Codex-/opencode-Review, +108 Tests): 3935 passed / 0 failed / 2 skipped in 347-427 s (3932 gemessen vor dem Folge-Commit 0c0d0ea, der 3 Tests ergänzt; --collect-only danach 3937 = 3935 + 2); +108 über die 3827 auf d82d4eb = test_claude_billing 33, _gating 21, _records 18, _refresh 2, test_claude_provider_billing 34. Nicht mit PYTEST_DEBUG_TEMPROOT im Repo laufen lassen (2 git-Tests brauchen ein tmp außerhalb jedes Repos).
# Stand 2026-10-23 abends (Windows, Dashboard B1 Queue-Editor nach externem Codex-Review + Panels sichtbar öffnen, +367 Tests): 3827 passed / 0 failed / 2 skipped in 377-600 s; +367 über die 3460 auf 796e141 = test_queue_editor_parse 37, _build 88, _lint 28, _mutations 66, _backups 18, _integration 4, _raw 40, test_dashboard_queue_api 35, test_dashboard_queue_js 50, test_dashboard_static +1.
# Stand 2026-10-23 (Windows, Dashboard-Fundament B0, +140 Tests): 3460 passed / 0 failed / 2 skipped in 330-609 s (über 400 s unter Last); +140 über die 3320 auf ee75217 = test_dashboard_write_gate 70, test_dashboard_static 56, test_dashboard_threading 6, test_dashboard_page_js 4 -> 12.
# Stand 2026-10-11 abends (Windows, nach conftest-Temp-Aufräumen, +7 Tests): 3320 passed / 0 failed / 2 skipped in 275-289 s; der Lauf hinterlässt keine orch_test_*-Ordner mehr in %TEMP%.
# Stand 2026-10-11 spät (Windows, nach OpenRouter-Drift: −3 Gratis-Aliase, +4 Tests): 3313 passed / 0 failed / 2 skipped in 293-335 s.
# Stand 2026-10-11 (Windows, Monats-Wartung, nach Modell-Umstellung Haiku 5.5/gpt-6-luna): 3309 passed / 0 failed / 2 skipped in 571 s; unter Parallellast bis 20 min, dann reißt der Harness-Index-Subprozess-Test seine 120-s-Grenze.
# Stand 2026-10-09 nach 821574f (Windows, Auftraggeber): 3308 passed / 0 failed / 2 skipped. Gegenprobe im
# Worktree docs-sync: `--collect-only` 3310 (= 3308 + 2), voller Lauf Exit 0.
# Korrekturrunde 2 zu PR #7 (2026-10-09, Linux, Cloud, CPython 3.12.3 und 3.13.16): 3275 passed /
# 0 failed / 9 skipped in 101-104 s — +33 gesammelt über die 3251 darunter, per --collect-only:
# test_harness_index 71 -> 89, test_dashboard_autostart 61 -> 67, test_harness_dashboard 35 -> 44.
# Der 9. Skip: test_windows_background_mode_keeps_below_normal_and_private_peak (nur win32); ohne
# `node` skippen 8 (5 + 3). ruff 1122, mypy 122/38 und win32 124/38 unverändert. Windows-Suite am
# Vorstand 19f55c6 (Auftraggeber): 3250 passed / 0 failed / 1 skipped.
# Korrekturrunde 1 zu PR #7 (2026-10-09, Linux, Cloud, CPython 3.12.3 und 3.13.16): 3243 passed /
# 0 failed / 8 skipped in 94-98 s — +59 gesammelt über die 3185 darunter, per --collect-only:
# test_harness_index 51 -> 71, test_dashboard_autostart 34 -> 61, test_harness_dashboard 23 -> 35.
# Der 8. Skip: test_windows_peak_memory_and_pid_check_work (nur win32). Ohne `node` im PATH
# skippen jetzt 7 Tests (4 in test_harness_dashboard, 3 in test_dashboard_page_js).
# ruff 1122 und mypy 122/38 unverändert; `mypy --platform win32` 127/39 -> 124/38 (die drei
# unused-ignore in harness_index.py sind weg). Die Windows-Suite misst der Auftraggeber.
# Linux, Cloud (2026-10-09, CPython 3.12.3 und 3.13.16, PR #7: Dashboard-Autostart, Quoten inkl.
# opencode, Harness-Index + Reiter "Harness"): 3185 passed / 0 failed / 7 skipped in 93-100 s —
# +131 über die 3054 darunter, per --collect-only (alle Dateien neu): test_harness_index 51,
# test_dashboard_autostart 34, test_harness_dashboard 23, test_dashboard_limits 20,
# test_dashboard_page_js 3. Die 5 node-Tests (3 + 2 in test_harness_dashboard) skippen ohne
# `node` im PATH. ruff 1122 und mypy 122/38 unverändert.
# Linux, Cloud (2026-10-08, CPython 3.12.3 und 3.13.16, PR #6: kaputte policy.yaml hält an,
# Meldungsdrossel, review-loop-Leser, Abschlussregel ohne Kontext, Reste aus PR #5):
# 3040 passed / 0 failed / 7 skipped — +80 über die 2960 darunter, per --collect-only
# zugeordnet: test_policy_unreadable 24 (neu), test_review_loop_policy_reader 24 (neu),
# test_orchestrator_policy_fail_closed +21, test_orchestrator_tool_tasks +4,
# test_telegram_listener +3, test_orchestrator_prompt +2, test_queue_linter_policy +2
# (test_notifier_approval und test_parallel_runner schärfen bestehende Tests).
# ruff 1122 unverändert, mypy 122/38 — −3 gegen master, die toten load_policy-Importe in
# tools/review_loop.py (Punkt 3); die Erstfassung dieser Zeile sagte "125/38 unverändert",
# gemessen war das nur nach Punkt 2.
# Korrekturrunde 1 zu PR #6 (2026-10-08, CPython 3.12.3 und 3.13.16): 3054 passed /
# 0 failed / 7 skipped in 87-97 s — +14 (K1: 4 in test_orchestrator_policy_fail_closed,
# 5 in test_notifier_unit; K2: 5 in test_queue_linter_policy). ruff 1122, mypy 122/38.
# Linux, Cloud (2026-10-06, CPython 3.12.3 und 3.13.16, nach Korrekturrunde 2 zu PR #5):
# 2960 passed / 0 failed / 7 skipped in 81-85 s — +6 über die 2954 darunter (K6: 4 in
# test_dev_loop_plan_approval — Ladefehler im Abschnittsparser, Reparatur, frische Engine;
# K7: 2 in test_parallel_runner — Frist über alle Fragen des Aufrufs, Verdrahtung).
# ruff 1122, mypy 125/38 unverändert.
# Linux, Cloud (2026-10-06, CPython 3.12.3 und 3.13.16, nach Korrekturrunde 1 zu PR #5):
# 2954 passed / 0 failed / 7 skipped in 88-90 s — +23 über die 2931 darunter (K1: 15
# parametrisierte Störfälle in test_orchestrator_policy_fail_closed, K4: 5 Nicht-Freigabe-
# Antworten dort, K5: 3 Join-Frist-Tests in test_parallel_runner; K2/K3 schärfen bestehende
# Tests). ruff 1122, mypy 125/38 unverändert.
# Linux, Cloud (2026-10-06, CPython 3.12.3 und 3.13.16, nach dev-loop-Plan-Freigabe
# fail-closed + run_once-Policy-Block fail-closed): 2931 passed / 0 failed / 7 skipped in
# 92-97 s — +53 über die 2878 darunter (46 in tests/test_dev_loop_plan_approval.py, 7 in
# tests/test_orchestrator_policy_fail_closed.py). ruff 1122 unverändert (nur die Zähler der
# zwei bestehenden run_once-Komplexitätsbefunde PLR0911/PLR0912 steigen), mypy 125 Fehler
# in 38 Dateien (−5: die zwei toten Importe in tools/dev_loop.py und drei Folgefehler des
# alten Freigabe-Codes).
# Linux, Cloud (2026-10-06, CPython 3.12.3 und 3.13.14, nach Codex-Review r1 zu PR #4):
# 2878 passed / 0 failed / 7 skipped in 75-78 s — +4 über die 2874 darunter (K1: 2
# parametrisierte Fälle Diagnose-print, K2: 2 match_excerpts-Schichtung). ruff 1122 und
# mypy 130/38 unverändert.
# Linux, Cloud (2026-10-05, CPython 3.12.3 und 3.13.14, nach den Korrekturen aus dem
# lokalen Review zu PR #4): 2874 passed / 0 failed / 7 skipped in 76-80 s — +5 über die
# 2869 darunter (1 Git-Gesamtfrist — dauert selbst 5 s, 2 Policy-Semantik, 1 fsmonitor/
# Submodule, 1 Hilfezeile). ruff 1122 und mypy 130/38 unverändert.
# Linux, Cloud (2026-10-05, CPython 3.12.3 und 3.13.14, nach Abschlussregel/verify_absent
# + Freigabe mit Inhalt): 2869 passed / 0 failed / 7 skipped in 73-75 s — +30 über die
# 2839 vom 2026-10-02 (Teil A +8: 1 Prompt, 5 Linter, 2 End-to-End-Verify; Teil B +22:
# 21 in tests/test_notifier_approval.py, 1 in test_orchestrator_tool_tasks). ruff 1122
# und mypy 130/38 unverändert.
# Linux, Cloud (2026-10-02, CPython 3.12.3 und 3.13.14, nach Lint-Paket 5+6):
# 2839 passed / 0 failed / 7 skipped in 70-74 s — +2 Regressionstests fuer B023 in
# test_telegram_listener. Davor (2026-09-24, nach Paket 3+4): 2837 / 7, 2844 gesammelt
# wie unten.
# CI (.github/workflows/ci.yml) laeuft auf 3.12 + 3.13. Die 7 Skips: 5 an Windows
# gebunden (4 schon vorher; 2026-09-24 dazu der Backslash-Pfad in test_queue_linter),
# 1 an pwsh (test_verify_pin_deps, skippt nur ohne pwsh im PATH — die Cloud-Umgebung
# hatte keins, die GitHub-ubuntu-Runner haben es), 1 fehlendes optionales
# claude_monitor. Die 14
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
# -p no:randomly ist seit 2026-09-25 nicht mehr tragend. Die Reihenfolgeabhaengigkeit
# von tests/test_telegram_listener.py war ein Leck aus tests/test_shutdown.py:
# test_shutdown_state_management liess shutdown.shutdown_pending GESETZT, und der
# Listener behandelt dann jede Nachricht als "Shutdown abbrechen" (Zusatzantwort,
# Klartext wird verschluckt). Eine autouse-Fixture dort raeumt jetzt beide
# shutdown-Events. Linux, Cloud (CPython 3.12.3, pytest-randomly 4.1.0): Seeds
# 22222, 1, 42, 1234, 31415, 99999, 114, 120 je 2837 passed / 7 skipped; ohne Fix
# waren 22222, 114 und 120 rot. Das Flag bleibt als reproduzierbare Standard-
# reihenfolge stehen (auch in ci.yml).
```

## ruff/mypy-Messnotizen

```bash
# Linux, Cloud 2026-10-02 nach Lint-Paket 5+6: ruff 1122 Befunde, mypy 130 Fehler in
# 38 Dateien (2026-09-24 nach Paket 3+4: 1131 bzw. 132 in 39; davor ruff 1510)
# → docs/lint-baseline-2026-09-02.md, Status-Nachtraege. Die Zahlen darunter sind
# aeltere Messungen der Entwicklungsmaschine.
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

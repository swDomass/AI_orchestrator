---
paths:
  - "doctor.py"
  - "scripts/*.py"
  - "pyproject.toml"
---
# Doctor, Scripts & Lint/Typing-Setup

Ausgelagert aus CLAUDE.md am 2026-09-21; laedt automatisch bei Zugriff auf doctor.py, scripts/*.py, pyproject.toml.


**Linting & Typing** — `pyproject.toml` carries `[project]` (2026-09-04, `requires-python`
only — real key now, was a comment before) + `[tool.ruff]` + `[tool.mypy]`, deliberately
**no** `[build-system]` table: AI_orchestrator is a flat script collection, not an
installable package, and `pip install .` already fails today regardless (setuptools'
auto-discovery refuses the flat multi-module layout — measured, unaffected by the
`[project]` table either way); `pytest.ini` keeps precedence for pytest. Ruleset is
borrowed from the sister repos `eeg_analysis` (layout + lenient mypy) and
`pyrolyse-1d-model` (rule selection), minus `NPY` and minus the `N8xx` block that exists
there only for an SI-suffix convention this repo does not have — deviations are
commented inline. Baseline, defect triage and work order →
[`docs/lint-baseline-2026-09-02.md`](docs/lint-baseline-2026-09-02.md).
**Fixed 2026-09-04:** `limits.py:85` used to annotate `ProviderLimits` in a module-level
annotation before the class definition — a `NameError` on Python ≤3.13 that took down
the whole app on import, masked only by PEP 649 on 3.14. The annotation is now quoted
(same style as three other forward references already in the file); import verified
clean on 3.12/3.13/3.14. The `requires-python = ">=3.12"` key above is therefore
empirically met, not aspirational (documented in `README.md` Requirements).
- **`doctor.py`** — 19 setup validation checks, `--fix`/`--yes` auto-repair, concurrent alias probes (subscription CLIs only — pay-per-token providers are deliberately not probed). `check_opencode_cli()` — the 19th check to be *added*, but 5th in `run_doctor()`'s list and therefore 5th in the `--doctor` output, so do not count down to 19 looking for it — is the exception to "not probed": it doesn't ping a model, it validates the ZDR/budget contract (exe resolution, agents, `data_collection:deny`+`zdr:true` per alias, `small_model`, `OPENCODE_DEFAULT_MODEL`, OpenRouter spending cap) — WARN-only, like `check_vibe_cli()`. Der Default-Modell-Teil kam 2026-09-04 dazu, weil ein von opencode nicht auflösbarer Bezeichner **keinen Fehler, sondern einen Hänger in der Initialisierung** erzeugt (2× gemessen, keine Ausgabe, kein Exit; Kontrolllauf mit gültigem Alias 8 s)
- **`scripts/safety_hook.py`** — Claude Code `PreToolUse` hook, hard-deny via `SAFETY_DENY_PATTERNS`. **The output shape decides whether it blocks at all:** Claude Code honours `decision: "block"` (legacy) and `hookSpecificOutput.permissionDecision: "deny"` (modern). The hook emitted `decision: "deny"` — neither value — so every "block" was silently a no-op until 2026-08-15; it now emits both shapes. Registered globally in `~/.claude/settings.json` (PreToolUse → `Bash`), i.e. it also applies to interactive sessions, not just orchestrator runs. **Command-start boundary (`config._CMD_START`)** — a git write pattern only counts where a command actually *starts*: line start, after `;`/`&`/`|`/newline, after `(` or a backtick, after a shell interpreter's `-c`/`-Command` argument, and (2026-08-15) after the command-taking builtins `eval`/`exec`, which were the same hole as `bash -c` in different syntax. Both prefixes compose, so `bash -c "eval 'git push'"` is covered too. The mandatory `\s+` after the name is what keeps `evaluate`/`execute…`/`shellcheck -c` out; `-` is not a boundary character, so `find . -exec git push` and `docker exec c git push` stay accepted residuals (recognising them needs real argv parsing). Measured: the rewritten boundary is 0.63–1.00× the old runtime on adversarial 10k-char input. **The fallback pattern block inside `safety_hook.py` is a hand-written mirror** of these constants for the case where importing `config.py` fails — edit both; `TestFallbackPatternsStayInSync` exercises that branch for real by making the import fail.
- **`scripts/build_audit_pack.py`** — Scientific-investigation audit pack builder (zip + meta JSON)

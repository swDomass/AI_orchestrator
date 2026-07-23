"""Mistral Vibe CLI provider.

Second non-Claude voice next to Codex (a third training lineage overall). Vibe is
registered ONLY when the `vibe` binary is present and is deliberately kept OUT of
the dispatcher's fallback chain — it activates via an explicit `#vibe` /
`#vibe_*` tag or as the value of `#second_opinion:`. Rationale (and the reason
this provider never writes, see below): in this system Mistral is a *reviewer*,
not an executor. `dispatcher._REVIEWER_ONLY` additionally prevents a tagged-but-
unregistered Vibe from degrading into a file-writing executor.

Tool reach was measured, not assumed (2026-07-23): with `--trust` + `--workdir X`,
`read_file` reads files **inside** X, and refuses both an absolute path outside X
and a `../` traversal. The working directory is therefore the confinement
boundary — but only for reads, since no writing tool is ever enabled here.

CLI surface (verified against vibe 2.22.0 on 2026-07-23):

* ``-p`` **without a value reads the prompt from stdin.** This matters: task
  prompts here reach ~100 KB, well past the Windows command-line ceiling for an
  argv element (32767 chars via CreateProcess; only 8191 when a run goes through
  cmd.exe). Verified with an 808 KB prompt whose marker sat on the very last
  line — delivered intact.
* ``--trust`` is required. Without it the CLI blocks on the trust prompt for an
  untrusted cwd and the run hangs with no output and 0 % CPU.
* ``--max-turns`` / ``--max-price`` bound a run. Both only apply in ``-p`` mode.
* There is **no ``--model`` flag.** The model is a config field, overridable per
  process via ``VIBE_ACTIVE_MODEL`` (pydantic-settings ``VIBE_*`` layer), whose
  value is a vibe *alias* (`mistral-medium-3.5`, `devstral-small`), not a raw
  model name. An unknown alias falls back to the configured default **without
  an error**, so a typo degrades silently — that is why `config.VIBE_MODEL_ALIASES`
  is the single source of these strings.
* ``PYTHONUTF8=1`` in the child env: vibe is a Python CLI and crashes with a
  ``charmap`` codec error when writing non-ASCII output (arrows, umlauts) under
  a Windows cp1252 locale.
* Output stays ``text`` (the default). ``--output json`` echoes the whole
  message list including vibe's multi-KB system prompt and carries **no** usage
  or cost fields, so it buys nothing here — text mode's stdout is exactly the
  assistant's final answer. No token counts are available from vibe at all;
  ``RunResult`` therefore leaves the token fields at 0 and the orchestrator's
  char/duration estimation takes over (same as Codex).
"""

import os
import shutil
import subprocess

import config
from providers.base import BaseProvider, RunResult
from providers.process_runner import run_with_watchdog

_VIBE_CMD = shutil.which("vibe") or "vibe"
# uv installs a real .exe shim, which Popen can start directly. A .cmd/.bat
# wrapper (a different install route) is not executable without a shell, so the
# shell flag follows the resolved suffix instead of an assumption about it.
_VIBE_NEEDS_SHELL = _VIBE_CMD.lower().endswith((".cmd", ".bat"))

# Read-only tool set for non-`read_only` runs. Both are `permission = "always"`
# in vibe's default config, so they need no `--auto-approve` (which would also
# arm bash/write_file — never wanted here).
_READ_TOOLS = ("read_file", "grep")


class VibeProvider(BaseProvider):
    name = "vibe"

    @staticmethod
    def is_available() -> bool:
        """True when the `vibe` binary is on PATH — the registration gate."""
        return shutil.which("vibe") is not None

    @staticmethod
    def _build_command(read_only: bool, cwd: str | None = None) -> list[str]:
        # `-p` with no value → prompt comes from stdin (see module docstring).
        cmd = [_VIBE_CMD, "-p", "--trust", "--output", "text"]
        if cwd:
            cmd.extend(["--workdir", cwd])
        if read_only:
            # Pure reasoning over the piped prompt: no tool call can be attempted,
            # so no approval prompt can stall the run.
            cmd.extend(["--max-turns", str(config.VIBE_READONLY_MAX_TURNS)])
            cmd.extend(["--disabled-tools", "*"])
        else:
            # Still non-writing by design: read + grep only, never bash/edit/
            # write_file. `--enabled-tools` in -p mode disables everything else.
            cmd.extend(["--max-turns", str(config.VIBE_MAX_TURNS)])
            for tool in _READ_TOOLS:
                cmd.extend(["--enabled-tools", tool])
        cmd.extend(["--max-price", str(config.VIBE_MAX_PRICE_USD)])
        return cmd

    @staticmethod
    def _build_env(model: str | None) -> dict[str, str]:
        """Full child environment (Popen replaces, it does not merge).

        Without a forced model the inherited ``VIBE_ACTIVE_MODEL`` is dropped, not
        passed through: otherwise a stray shell export would silently decide the
        model for every untagged run, and `config.VIBE_MODEL_ALIASES` would stop
        being the single source of truth it claims to be.
        """
        env = os.environ.copy()
        env["PYTHONUTF8"] = "1"
        if model:
            env["VIBE_ACTIVE_MODEL"] = model
        else:
            env.pop("VIBE_ACTIVE_MODEL", None)
        return env

    def run(
        self,
        task: str,
        cwd: str | None = None,
        timeout: int = config.TASK_TIMEOUT_SEC,
        read_only: bool = False,
        session_id: str | None = None,  # accepted but ignored (supports_sessions=False)
        resume: bool = False,            # accepted but ignored
    ) -> RunResult:
        model_label = self._forced_model
        if model_label:
            print(f"  [vibe → {model_label}] Führe Task aus...")
        else:
            print("  [vibe] Führe Task aus...")
        try:
            result = run_with_watchdog(
                self._build_command(read_only=read_only, cwd=cwd),
                input_text=task,
                cwd=cwd,
                idle_timeout=config.CLI_IDLE_TIMEOUT_NO_LIVENESS_SEC,
                hard_timeout=timeout,
                shell=_VIBE_NEEDS_SHELL,
                env=self._build_env(model_label),
            )

            output = (result.stdout or "").strip()
            stderr = (result.stderr or "").strip()

            # Same fail-closed stdin contract as claude/codex/gemini: the prompt
            # travels through the pipe, so a lost tail silently truncates the
            # task. getattr() because provider tests fake the watchdog result.
            stdin_error = getattr(result, "stdin_error", None)

            if result.returncode == 0 and output:
                if stdin_error:
                    return RunResult(success=False, error="stdin_incomplete", output=stdin_error)
                return RunResult(success=True, output=output)

            combined = (output + stderr).lower()
            # "too many requests", not a bare "too many" — the latter also matches
            # local CLI usage errors ("too many arguments") and would trip a
            # 30-minute provider cooldown over a typo in the command.
            if any(kw in combined for kw in ("rate limit", "quota", "429", "too many requests")):
                return RunResult(success=False, error="rate_limit")
            if any(kw in combined for kw in ("unauthorized", "401", "api key", "not authenticated")):
                return RunResult(success=False, error="auth_error")
            if any(kw in combined for kw in ("unavailable", "connection", "timeout", "network")):
                return RunResult(success=False, error="unreachable")

            # Only trustworthy at rc == 0 — a CLI dying early also breaks the
            # feeder pipe, and that real cause must win. See providers/codex.py.
            if stdin_error and result.returncode == 0:
                return RunResult(success=False, error="stdin_incomplete", output=stdin_error)

            return RunResult(
                success=False,
                error=stderr or output or "empty output",
                output=(output + "\n[stdin] " + stdin_error) if stdin_error else output,
            )

        except subprocess.TimeoutExpired as exc:
            kind = getattr(exc, "timeout_kind", "hard")
            return RunResult(success=False, error="hang" if kind == "idle" else "timeout")
        except FileNotFoundError:
            return RunResult(success=False, error="vibe CLI not found")
        except (OSError, ValueError) as e:
            return RunResult(success=False, error=str(e))

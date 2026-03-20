"""
Security Audit Tool: Scan → Fix → Verify.

Two-phase workflow:
  1. Audit Agent:  Read-only scan for security vulnerabilities (hardcoded secrets,
                   injection vectors, path traversal, input validation gaps, etc.)
  2. Fix Agent:    Implements all fixes. Runs tests to verify nothing broke.

Output written to {cwd}/docs/security-audit-YYYYMMDD-HHMMSS.md

Usage in queue:
    - [ ] Security audit #tool:security-audit cwd:/d/programmieren/projekt
    - [ ] Check providers/ for injection risks #tool:security-audit cwd:/d/proj
"""

from datetime import datetime
from pathlib import Path

from config import TOOL_DEV_EXEC_TIMEOUT_SEC, TOOL_SA_AUDIT_TIMEOUT_SEC
from limits import is_cached_provider_available
from notifier import notify_tool_done, notify_tool_progress
from providers.base import BaseProvider
from tools.base_tool import (
    BaseTool,
    ToolResult,
    _build_system_prompt,
    _make_capacity_exhausted_result,
    _make_report_header,
    _write_tool_file,
)

_AUDIT_PERSONA = """
## Role: Application Security Engineer

You are a senior application security engineer. Your job is to find every exploitable
vulnerability in the codebase — not theoretical risks, but real attack vectors with
concrete exploitation paths. You do not soften findings. Name files, lines, and code.
""".strip()

_AUDIT_PROMPT = (
    _AUDIT_PERSONA
    + """

---

## Task

Perform a comprehensive security audit. Focus area: {task}

### What to scan (priority order)

**CRITICAL — check every file:**
1. **Hardcoded secrets** in source code (NOT .env): API keys, tokens, passwords,
   connection strings in .py / .yaml / .json / .md. Pattern: `token =`, `password =`,
   `api_key =`, `secret =`, `Bearer `, hard-coded IPs with credentials.
2. **Command injection**: `subprocess` / `Popen` calls with `shell=True` where
   user-controlled input can reach the shell command string.
3. **Path traversal**: user-controlled values joined into file paths without
   `.resolve()` followed by a bounds-check against an allowed root.

**HIGH — thorough check:**
4. **Input validation gaps**: missing null-byte (`\\x00`), newline, or control-char
   filtering in user input handlers (Telegram commands, queue parser, HTTP endpoints).
5. **Log injection**: unsanitized user data written to log/event files — newlines
   allow injecting fake timestamped entries.
6. **Unsafe deserialization**: `yaml.load()` without `Loader=yaml.SafeLoader`,
   `pickle.load()`, `eval()` / `exec()` on external data.

**MEDIUM:**
7. **SSRF**: user-controlled URLs passed directly to `requests` / `urllib`.
8. **TOCTOU**: existence check then use — window for symlink / rename attacks.
9. **Missing timeouts** on subprocess or network calls.

**LOW:**
10. Overly broad `except Exception` swallowing security-relevant errors.
11. Sensitive data (tokens, passwords) printed to logs or console.

### Output format (mandatory)

```
## Audit Summary
Total findings: N (C critical, H high, M medium, L low)

## Findings

### [CRIT-1] <Short title>
- **File:** path/to/file.py:line
- **Severity:** CRITICAL
- **Attack vector:** <concrete exploitation path, one sentence>
- **Current code:** `<relevant snippet>`
- **Fix:** <exact code change required>

### [HIGH-1] <Short title>
...
```

List ONLY real findings with concrete exploitation paths. No false positives.
Do NOT modify any files — this is a read-only audit."""
)

_FIX_PROMPT = """
## Role: Security Engineer — Fix Implementation

You are implementing security fixes identified in a prior audit.
Apply EVERY fix listed in the audit report. Do not skip any finding.

After implementing all fixes:
1. Run `python -m pytest tests/ -q` and ensure all tests pass.
2. If tests fail because of your changes, fix the tests too.
3. Do NOT add new features or refactor unrelated code. Security fixes only.

## Original Task
{task}

## Audit Report
{audit_output}

## Instructions
- Fix each finding in order of severity (CRITICAL → HIGH → MEDIUM → LOW).
- For each fix: state the file, line, and what changed.
- After all fixes: run the test suite and report the result.
- Write a brief "## Fixes Applied" summary at the end.
- If a finding cannot be fixed automatically (e.g. rotate a live API key),
  note it under "## Manual Actions Required".
""".strip()


class SecurityAuditTool(BaseTool):
    name = "security-audit"
    description = (
        "Security audit + fix: scans for hardcoded secrets, injection, path traversal, "
        "input validation gaps. Fixes all findings. Output → docs/security-audit-*.md"
    )
    read_only = False

    def run(
        self,
        task: str,
        provider: BaseProvider,
        cwd: str | None = None,
        timeout: int | None = None,
        memory_context: str = "",
        **kwargs,
    ) -> ToolResult:
        cwd_path = Path(cwd) if cwd else Path(".")
        audit_timeout = TOOL_SA_AUDIT_TIMEOUT_SEC   # 40 min
        fix_timeout = timeout or TOOL_DEV_EXEC_TIMEOUT_SEC  # 2 h

        # ------------------------------------------------------------------ #
        # Phase 1 — Audit (read-only)                                         #
        # ------------------------------------------------------------------ #
        if not is_cached_provider_available(provider.name):
            msg = "Provider nicht verfügbar — Security Audit abgebrochen"
            print(f"  [security-audit] ⏸ {msg}")
            return _make_capacity_exhausted_result(msg, "", 0, 0, 0)

        notify_tool_progress(self.name, 1, 2, "Phase 1/2: Security-Audit läuft (read-only)...")
        print(f"  [security-audit] Phase 1: Audit {cwd_path} ...")

        system_prompt = _build_system_prompt(
            provider.name,
            memory_context=memory_context,
            tool_name=self.name,
        )

        audit_prompt = system_prompt + "\n\n" + _AUDIT_PROMPT.replace("{task}", task)

        audit_result = provider.run(
            audit_prompt,
            cwd=str(cwd_path),
            timeout=audit_timeout,
            read_only=True,
        )

        in_tok = audit_result.input_tokens
        out_tok = audit_result.output_tokens

        if audit_result.error:
            print(f"  [security-audit] ✗ Audit fehlgeschlagen: {audit_result.error}")
            return ToolResult(
                success=False,
                output=audit_result.output,
                iterations=1,
                error=audit_result.error,
                error_code=audit_result.error_code,
                retryable=audit_result.retryable,
                input_tokens=in_tok,
                output_tokens=out_tok,
            )

        audit_output = audit_result.output
        print(f"  [security-audit] ✓ Audit done ({len(audit_output)} chars)")

        # ------------------------------------------------------------------ #
        # Phase 2 — Fix + Verify                                              #
        # ------------------------------------------------------------------ #
        notify_tool_progress(self.name, 2, 2, "Phase 2/2: Fixes werden implementiert...")
        print(f"  [security-audit] Phase 2: Fixes implementieren...")

        if not is_cached_provider_available(provider.name):
            # Save partial result (audit only) and signal retry
            timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            filename = f"security-audit-{timestamp}-audit-only.md"
            _write_tool_file(
                cwd_path / "docs",
                filename,
                f"# Security Audit (Fixes ausstehend — Kapazität erschöpft)\n\n{audit_output}",
            )
            msg = f"Provider erschöpft — Audit gespeichert (docs/{filename}), Fixes noch ausstehend"
            print(f"  [security-audit] ⏸ {msg}")
            return _make_capacity_exhausted_result(msg, audit_output, 1, in_tok, out_tok)

        # Replace {task} before {audit_output}: audit_output (LLM text) may contain the
        # literal substring "{task}" (e.g. from quoting source code with format strings),
        # which a later .replace("{task}", task) call would incorrectly expand.
        fix_prompt = system_prompt + "\n\n" + _FIX_PROMPT.replace("{task}", task).replace("{audit_output}", audit_output)

        fix_result = provider.run(
            fix_prompt,
            cwd=str(cwd_path),
            timeout=fix_timeout,
        )

        in_tok += fix_result.input_tokens
        out_tok += fix_result.output_tokens
        fix_output = fix_result.output
        success = not fix_result.error

        # ------------------------------------------------------------------ #
        # Write combined report                                               #
        # ------------------------------------------------------------------ #
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        filename = f"security-audit-{timestamp}.md"
        header = _make_report_header("Security Audit", timestamp, task, provider.name, cwd_path)
        report = (
            header
            + "## Audit Findings\n\n"
            + f"{audit_output}\n\n"
            + "---\n\n"
            + "## Fixes Applied\n\n"
            + f"{fix_output}\n"
        )
        _write_tool_file(cwd_path / "docs", filename, report)
        print(f"  [security-audit] ✓ Report gespeichert: {cwd_path / 'docs' / filename}")

        if not success:
            error_msg = fix_result.error or "Fix-Phase fehlgeschlagen"
            notify_tool_done(self.name, 2, False, f"Audit OK, Fixes fehlgeschlagen → docs/{filename}")
            return ToolResult(
                success=False,
                output=f"Report: docs/{filename}\n\n{fix_output}",
                iterations=2,
                error=error_msg,
                error_code=fix_result.error_code,
                retryable=fix_result.retryable,
                input_tokens=in_tok,
                output_tokens=out_tok,
            )

        notify_tool_done(self.name, 2, True, f"Audit + Fixes abgeschlossen → docs/{filename}")
        return ToolResult(
            success=True,
            output=f"Report: docs/{filename}\n\n{fix_output}",
            iterations=2,
            input_tokens=in_tok,
            output_tokens=out_tok,
        )

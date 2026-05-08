"""
Deep Security Audit Tool: 6 expert agents + CISO synthesis + fix.

Multi-perspective security analysis with specialized agents:
  1. Penetration Tester — exploit chains, attack paths, privilege escalation
  2. Security Architect — trust boundaries, data flow, threat model
  3. Code Auditor (SAST) — OWASP Top 10, CWE patterns, line-by-line
  4. Supply Chain Analyst — dependencies, imports, third-party risk
  5. Data & Privacy Analyst — secrets management, PII, encryption
  6. Forensics / IR Specialist — logging, audit trails, tamper resistance

Phase 7: CISO synthesis — dedup, cross-validate, prioritized remediation plan
Phase 8: Fix implementation (optional, skip with #no-fix tag)

Output written to {cwd}/docs/deep-security-audit-YYYYMMDD-HHMMSS.md

Usage in queue:
    - [ ] Deep security audit #tool:deep-security-audit cwd:/d/projekt
    - [ ] Audit uncommitted changes #tool:deep-security-audit #no-fix cwd:/d/proj
    - [ ] Full audit with fix #tool:deep-security-audit cwd:/d/proj
"""

import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from config import (
    TOOL_DSA_AGENT_TIMEOUT_SEC,
    TOOL_DSA_FIX_TIMEOUT_SEC,
    TOOL_DSA_MAX_AGENT_OUTPUT_CHARS,
    TOOL_DSA_MAX_TOTAL_INJECT_CHARS,
    TOOL_DSA_SYNTHESIS_TIMEOUT_SEC,
)
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

# ── Tag detection ───────────────────────────────────────────────────

_NO_FIX_RE = re.compile(r"(?i)(?<!\S)#no-fix(?=\s|$)")


def _wants_fix(task: str) -> bool:
    """Return True unless task contains #no-fix tag."""
    return not _NO_FIX_RE.search(task)


def _clean_tags(task: str) -> str:
    """Remove deep-security-audit-specific tags from task text."""
    return " ".join(_NO_FIX_RE.sub("", task).split())


# ── Agent Definitions ───────────────────────────────────────────────

@dataclass(frozen=True)
class _AgentDef:
    key: str
    title: str
    persona: str
    checklist: str


_AGENTS: tuple[_AgentDef, ...] = (
    # ── 1. Penetration Tester ──
    _AgentDef(
        key="pentester",
        title="Offensive Security / Penetration Tester",
        persona=(
            "You are an elite penetration tester with 15+ years of experience breaking "
            "into systems. You think like an attacker. You chain vulnerabilities. You "
            "don't care about theoretical risks — you care about what you can actually "
            "exploit to gain unauthorized access, escalate privileges, or exfiltrate data."
        ),
        checklist="""\
**Your mission: find every exploitable attack chain.**

1. **Injection**: Any path from user-controlled input to dangerous sinks?
   Command Injection (subprocess, os.system, Popen with shell=True), SQL Injection
   (raw queries, ORM bypass), Template Injection (Jinja2, f-strings in HTML),
   NoSQL/LDAP/Header Injection.
2. **XSS**: Reflected, Stored, DOM-based — anywhere user input reaches HTML/JS output.
3. **Path Traversal**: User-controlled values joined into file paths without
   `.resolve()` + bounds-check? Symlinks, null bytes, Zip-Slip?
4. **Authentication Bypass**: Missing auth checks on endpoints/functions? JWT
   weaknesses (none algorithm, weak secret, missing expiry)? Session fixation? CSRF?
5. **Privilege Escalation**: IDOR (direct object references without owner check)?
   Role check gaps? Vertical/horizontal escalation?
6. **SSRF**: User-controlled URLs passed to HTTP clients? DNS rebinding?
   Cloud metadata endpoint access (169.254.169.254)?
7. **Deserialization**: pickle.load, yaml.load without SafeLoader, eval/exec on
   external data, JSON with __class__ hints?
8. **Race Conditions**: TOCTOU in file access, double-spend in business logic,
   missing locks on shared state?
9. **Credential Theft**: Can crafted input exfiltrate secrets via output, logs,
   or error messages?
10. **Denial of Service**: Unbounded resource consumption? Timeout bypass vectors?

For each finding: describe the full attack chain step-by-step. Name files, lines,
and the exact input an attacker would craft.""",
    ),

    # ── 2. Security Architect ──
    _AgentDef(
        key="architect",
        title="Security Architect / Threat Modeler",
        persona=(
            "You are a security architect who designs defense-in-depth systems. You think "
            "in trust boundaries, data flow diagrams, and threat models. You evaluate "
            "whether the security architecture is fundamentally sound — not just whether "
            "individual lines of code are safe."
        ),
        checklist="""\
**Your mission: evaluate the security architecture and identify structural weaknesses.**

1. **Trust Boundaries**: Map EVERY trust boundary in the system. Where does trusted
   code interact with untrusted input? (User Input → Backend, Backend → DB,
   Backend → External APIs, File Upload → Processing, Config → Runtime, CI → Prod)
   Are boundaries explicitly enforced or implicitly assumed?
2. **Data Flow Analysis**: Trace sensitive data (credentials, PII, session tokens,
   API keys) through the entire system. Where is it created, stored, transmitted,
   logged, or deleted? Unintended data leaks between components?
3. **Authentication & Authorization Model**: Is the auth model consistent? Are there
   endpoints/functions without auth? Default credentials? Missing rate limiting?
   Can auth be bypassed or spoofed?
4. **Defense-in-Depth Gaps**: Where does the system rely on a single security control?
   What happens if one layer fails (WAF, input validation, DB constraints)?
5. **Threat Model Completeness**: Which threat actors are NOT considered? Insider
   threats? Supply chain attacks? Compromised dependencies? Cloud provider breach?
6. **Failure Mode Analysis**: What happens when components fail? Fail-open vs.
   fail-closed? Exceptions in security-critical code — caught correctly?
   Silent degradation?
7. **Blast Radius Assessment**: If one component is compromised, how far can the
   attacker spread? Network segmentation? Least privilege? Container isolation?

Output a threat model summary with trust boundary diagram (ASCII), prioritized
architectural risks, and specific remediation recommendations.""",
    ),

    # ── 3. Code Auditor (SAST) ──
    _AgentDef(
        key="code_auditor",
        title="Static Analysis / Code Auditor",
        persona=(
            "You are a code security auditor specializing in static analysis. You read "
            "every line of code systematically. You map inputs to sinks. You know the "
            "OWASP Top 10, CWE Top 25, and SANS Top 25 by heart. You find what automated "
            "SAST tools miss because you understand context."
        ),
        checklist="""\
**Your mission: systematic line-by-line vulnerability scan with CWE mapping.**

1. **Injection Flaws (CWE-78, CWE-79, CWE-89, CWE-94)**:
   - Every `subprocess` / `Popen` / `os.system` call: is `shell=True`? Is input sanitized?
   - Every `eval()` / `exec()` / `compile()`: can external data reach it?
   - Every string formatting into commands, paths, or queries
   - Every `yaml.load()` without `SafeLoader` (CWE-502)

2. **Path Traversal (CWE-22, CWE-73)**:
   - Every `Path()` / `os.path.join()` with external input
   - Missing `.resolve()` + bounds check against allowed root
   - Symlink following without verification
   - Null-byte injection in file paths

3. **Broken Access Control (CWE-284, CWE-862)**:
   - Functions that should check permissions but don't
   - `read_only` flag bypass possibilities
   - CWD validation completeness — every entry point covered?

4. **Security Misconfiguration (CWE-16)**:
   - Default passwords, tokens, or debug flags
   - Overly permissive file permissions on created files
   - Error messages that leak internal paths or stack traces

5. **Cryptographic Failures (CWE-327, CWE-330)**:
   - Weak randomness for security-critical operations
   - Missing integrity checks on config/queue files
   - Plaintext storage of credentials

6. **Error Handling (CWE-209, CWE-755)**:
   - Broad `except Exception` swallowing security errors
   - Error messages exposing sensitive information
   - Missing error handling on security-critical paths

For each finding: specify CWE ID, file:line, vulnerable code snippet, exploitation
scenario, and concrete fix.""",
    ),

    # ── 4. Supply Chain Analyst ──
    _AgentDef(
        key="supply_chain",
        title="Supply Chain / Dependency Analyst",
        persona=(
            "You are a supply chain security specialist. You analyze dependencies, imports, "
            "and third-party integrations for risk. You know that most breaches come through "
            "the supply chain, not direct attack. You check what others overlook: transitive "
            "dependencies, version pinning, import hijacking, and typosquatting."
        ),
        checklist="""\
**Your mission: audit all external dependencies and integration points for supply chain risk.**

1. **Dependency Audit**:
   - Read requirements.txt / package.json / go.mod / Cargo.toml / pom.xml etc.
   - Are versions pinned with exact hashes or just `>=`?
   - Known CVEs in current dependency versions?
   - Unnecessary dependencies that expand attack surface?
   - Unmaintained or abandoned packages?

2. **Lock Files**:
   - Does a lock file exist? Is it committed?
   - Does it match the dependency declarations?

3. **Import / Module Analysis**:
   - Dynamic imports (importlib, __import__, require) — can paths be controlled?
   - Plugin systems without signature validation?
   - Relative vs. absolute imports — namespace confusion risk?

4. **External Binaries & CLI Tools**:
   - Which external programs does the system invoke?
   - PATH hijacking risk? Relative paths instead of absolute?
   - Can a malicious binary in CWD shadow a system binary?

5. **Build & CI/CD Pipeline**:
   - CI/CD config present? Insecure steps (curl|bash, npm install without lockfile,
     Docker without pinned base image)?
   - Secret handling in CI? Exposed in logs?
   - Pre-commit hooks: can they be bypassed?

6. **Third-Party API Integrations**:
   - How are API keys stored and rotated?
   - Are responses validated? TLS certificate pinning? Timeout handling?

7. **Transitive Dependencies**:
   - Indirect dependencies with known weaknesses?
   - Dependency confusion / typosquatting risk?

For each finding: state the risk, the attack scenario, and the mitigation.""",
    ),

    # ── 5. Data & Privacy Analyst ──
    _AgentDef(
        key="data_privacy",
        title="Data Security / Privacy Analyst",
        persona=(
            "You are a data security and privacy specialist. You track every piece of "
            "sensitive data through the system — where it's created, stored, transmitted, "
            "logged, and deleted. You care about secrets management, PII exposure, data "
            "retention, and encryption. You think about GDPR, data minimization, and "
            "the principle of least privilege for data access."
        ),
        checklist="""\
**Your mission: trace all sensitive data flows and find exposure risks.**

1. **Secrets Management**:
   - How are API keys, DB passwords, tokens loaded? Hardcoded in source?
     .env without .gitignore? Vault/KMS or plaintext?
   - Secrets in CLI arguments (visible in `ps`)? In Docker ENV?
   - .env / config file permissions — world-readable?

2. **Credential Exposure**:
   - Are credentials written to logs? Error messages? Stack traces?
   - API responses leaking internal tokens or keys?
   - Git history containing committed secrets?
   - OAuth tokens / refresh flow security

3. **PII Handling**:
   - What personal data (names, email, IP, location) is collected?
   - Where stored? Encrypted? Retention policy? Deletion concept?
   - GDPR/DSGVO compliance if applicable?

4. **Data at Rest**:
   - Database encryption? File encryption? Temp files with sensitive content?
   - Backup encryption? Secure key derivation?

5. **Data in Transit**:
   - TLS everywhere? Certificate validation? No HTTP fallbacks?
   - Sensitive data in URL parameters (GET instead of POST)?

6. **Logging & Retention**:
   - What sensitive data appears in log files?
   - Log rotation: are old logs with credentials properly deleted?
   - Retention policies adequate?

7. **Data Leakage Vectors**:
   - Verbose error pages? Debug endpoints in production?
   - .git directory exposed? Source maps in production?
   - API responses with too many fields (over-fetching)?

For each finding: specify what data is exposed, where, the impact, and the fix.""",
    ),

    # ── 6. Forensics / IR Specialist ──
    _AgentDef(
        key="forensics",
        title="Digital Forensics / Incident Response Specialist",
        persona=(
            "You are a digital forensics and incident response specialist. You evaluate "
            "whether a system can detect, investigate, and recover from security incidents. "
            "You care about audit trails, log integrity, evidence preservation, and the "
            "ability to reconstruct what happened after a breach. A system that can't tell "
            "you it was compromised is already compromised."
        ),
        checklist="""\
**Your mission: evaluate incident detection, investigation, and recovery capabilities.**

1. **Audit Trail Completeness**:
   - Are all security-relevant actions logged? (login/logout, permission changes,
     data access, admin actions, errors, configuration changes)
   - Can you reconstruct WHO did WHAT, WHEN, and from WHERE?
   - Are there gaps where actions happen without any log entry?

2. **Log Integrity & Tamper Resistance**:
   - Are logs append-only or can they be overwritten/deleted?
   - Can an attacker manipulate their own log entries?
   - Timestamps reliable (UTC, monotonic ordering)?
   - Log injection possible (newlines in user input creating fake entries)?
   - Log rotation: could evidence be destroyed during incident?

3. **Anomaly Detection**:
   - Alerting for unusual patterns? (brute force, unusual access times,
     mass downloads, privilege changes, repeated failures)
   - Rate limiting on critical endpoints?
   - Can the system detect compromise indicators?

4. **Incident Response Readiness**:
   - Can the system be safely shut down without state loss?
   - Is there a kill switch? Can compromised sessions/tokens be invalidated?
   - Can changes from a compromised component be rolled back?

5. **Recovery & Continuity**:
   - Backup strategy? Restore tested?
   - Can the point of compromise be determined?
   - Can the system distinguish own state from attacker-modified state?

6. **Evidence Preservation**:
   - Sufficient data retained for forensic analysis?
   - Retention policies adequate?
   - Are artifacts (uploads, temp files) preserved after incidents
     or automatically deleted?

For each finding: rate the detection gap severity and recommend specific
logging/monitoring improvements.""",
    ),
)

# ── CISO Synthesis Prompt ───────────────────────────────────────────

_CISO_SYNTHESIS_PROMPT = """
## Role: Chief Information Security Officer — Synthesis & Prioritization

You are a CISO with 20+ years of experience across enterprise security, incident
response, and risk management. You have received independent security assessments
from 6 specialized experts. Your job is NOT to repeat their findings. Your job is to:

1. **Cross-validate**: Which findings were identified by multiple experts? These are
   high-confidence findings. Which are unique to one expert? These need scrutiny.
2. **Deduplicate**: Merge overlapping findings into single, definitive entries.
3. **Prioritize**: Assign final severity based on exploitability, impact, and effort
   to fix. Use CVSS-like reasoning but express as CRITICAL/HIGH/MEDIUM/LOW.
4. **Identify attack chains**: Connect findings across experts into multi-step attack
   scenarios. The combination of two MEDIUM findings may create a CRITICAL chain.
5. **Assess systemic risk**: Beyond individual findings — is the security posture
   fundamentally sound? Where are the architectural gaps?
6. **Create remediation roadmap**: Ordered by risk reduction per effort invested.

---

## Expert Reports

{agent_reports}

---

## Output Format (mandatory)

### Executive Summary
3-5 sentences. Overall security posture assessment. Headline risk. Confidence level.

### Attack Chain Analysis
Map the most dangerous multi-step attack scenarios by connecting findings across
experts. For each chain: entry point → escalation → impact.

### Consolidated Findings (deduplicated, prioritized)

#### CRITICAL
For each: ID, title, file:line, attack vector, impact, fix, which experts identified it.

#### HIGH
Same format.

#### MEDIUM
Same format.

#### LOW
Same format.

### Cross-Expert Validation Matrix
Table showing which findings were confirmed by multiple experts vs. single-expert.

### Remediation Roadmap
Ordered list of fixes. For each: effort estimate (hours), risk reduction, dependencies.
Group into: Immediate (today), Short-term (this week), Medium-term (this month).

### Systemic Recommendations
Architectural or process changes beyond individual fixes.

### Dissenting Opinions
Where experts disagreed — and your resolution of the disagreement.

## Task context: {task}
""".strip()

# ── Fix Prompt ──────────────────────────────────────────────────────

_FIX_PROMPT = """
## Role: Senior Security Engineer — Remediation Implementation

You are implementing security fixes prioritized by the CISO. Work through the
remediation roadmap in order. Focus on CRITICAL and HIGH findings first.

## Rules
- Fix each finding in severity order (CRITICAL → HIGH → MEDIUM).
- LOW findings: fix only if trivial (< 5 lines changed).
- For each fix: state the finding ID, file, line, and what you changed.
- Run `python -m pytest tests/ -q` after all fixes. Fix broken tests.
- Do NOT add features, refactor, or make changes beyond security fixes.
- If a finding cannot be fixed automatically (rotate API key, update dependency,
  change deployment config): note it under "## Manual Actions Required".

## Original Task
{task}

## CISO Remediation Plan
{synthesis_output}

## Instructions
1. Read each CRITICAL finding. Implement the fix. Move to next.
2. Read each HIGH finding. Implement the fix. Move to next.
3. Read MEDIUM findings. Implement fixes where straightforward.
4. Run the full test suite.
5. Write a "## Fixes Applied" summary listing each finding ID and what changed.
6. Write a "## Manual Actions Required" section for anything you couldn't fix.
""".strip()


# ── Tool Implementation ─────────────────────────────────────────────


class DeepSecurityAuditTool(BaseTool):
    name = "deep-security-audit"
    description = (
        "Multi-agent deep security audit — 6 expert personas (pentester, architect, "
        "code auditor, supply chain, data privacy, forensics) + CISO synthesis + fix. "
        "Output → docs/deep-security-audit-*.md"
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
        # Phase C: capability switch. When the provider supports Claude's
        # internal Task-tool subagents AND the session feature flag is on,
        # delegate the 6 personas + CISO synthesis to a single subprocess
        # that fans out via Task internally — fewer subprocess calls, full
        # cache reuse, parallel persona execution. Otherwise: stay on
        # today's sequential-subprocess path (still works fine).
        from config import CLAUDE_SESSION_ENABLED
        if getattr(provider, "supports_sessions", False) and CLAUDE_SESSION_ENABLED:
            return self._run_subagent_mode(task, provider, cwd, timeout, memory_context)
        return self._run_sequential_mode(task, provider, cwd, timeout, memory_context)

    def _run_subagent_mode(
        self,
        task: str,
        provider: BaseProvider,
        cwd: str | None,
        timeout: int | None,
        memory_context: str,
    ) -> ToolResult:
        """Single-subprocess mode: Claude internally spawns 6 personas as
        Task-tool subagents in parallel, writes per-agent reports + the
        combined CISO synthesis. Phase 8 (fix) runs in a separate fresh
        subprocess as before — fix is a write-phase that benefits from a
        clean read of just-written audit files rather than from session
        sharing.
        """
        cwd_path = Path(cwd) if cwd else Path(".")
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        docs_dir = cwd_path / "docs"
        do_fix = _wants_fix(task)
        clean_task = _clean_tags(task)
        master_timeout = timeout or (TOOL_DSA_SYNTHESIS_TIMEOUT_SEC + TOOL_DSA_AGENT_TIMEOUT_SEC)
        fix_timeout = timeout or TOOL_DSA_FIX_TIMEOUT_SEC

        total_input_tokens = 0
        total_output_tokens = 0
        total_cache_creation = 0
        total_cache_read = 0

        if not is_cached_provider_available(provider.name):
            msg = "Provider nicht verfügbar — Deep Security Audit abgebrochen"
            print(f"  [deep-security-audit] ⏸ {msg}")
            return _make_capacity_exhausted_result(msg, "", 0, 0, 0)

        notify_tool_progress(self.name, 1, 2, "Phase 1/2: 6-Persona Audit + CISO-Synthesis (parallele Subagents)...")
        print(f"  [deep-security-audit] Subagent-Mode: 1 Master-Subprocess mit 6 parallelen Personas")

        system_prompt = _build_system_prompt(
            provider.name,
            memory_context=memory_context,
            tool_name=self.name,
            cwd=cwd,
        )

        # Build agent block listing for the master prompt
        agent_block_lines: list[str] = []
        for idx, agent in enumerate(_AGENTS, 1):
            agent_block_lines.append(
                f"### Persona {idx}: {agent.title}\n"
                f"**Key (for filename):** `{agent.key}`\n\n"
                f"{agent.persona}\n\n"
                f"**Checklist:**\n{agent.checklist}\n"
            )
        agents_block = "\n---\n\n".join(agent_block_lines)

        master_prompt = (
            system_prompt
            + "\n\n## Role: Multi-Agent Security Audit Orchestrator (CISO)\n\n"
            + "You will perform a deep security audit by spawning 6 expert subagents in "
              "PARALLEL via the Task tool, then synthesizing their findings as the CISO.\n\n"
            + f"## Audit Scope\n\n{clean_task}\n\n"
            + "## Step 1: Spawn 6 subagents in PARALLEL\n\n"
            + "Issue a single message containing 6 Task tool calls (one per persona below). "
              "Each subagent gets the full persona prompt + checklist as its task. Use "
              "`subagent_type=\"general-purpose\"` and `description=\"<short persona name>\"`. "
              "Each subagent must produce findings in the structured format described below "
              "and return them as its final message.\n\n"
            + "## Personas\n\n"
            + agents_block
            + "\n\n## Step 2: After all 6 subagents complete\n\n"
            + "Save each subagent's full output as a separate file in this exact location:\n\n"
            + f"  `{docs_dir}/deep-security-audit-{timestamp}-<persona-key>.md`\n\n"
            + "Use the `Write` tool. Persona keys: " + ", ".join(a.key for a in _AGENTS) + "\n\n"
            + "## Step 3: CISO Synthesis\n\n"
            + "Now act as CISO. Read all 6 reports. Produce a combined synthesis file:\n\n"
            + f"  `{docs_dir}/deep-security-audit-{timestamp}.md`\n\n"
            + "Synthesis sections (REQUIRED):\n"
            + "- **Executive Summary** (3-5 sentences for management)\n"
            + "- **Cross-Validated Critical Findings** (where ≥2 personas agreed)\n"
            + "- **Attack Chain Analysis** (multi-step exploitation paths combining findings)\n"
            + "- **Prioritized Remediation Roadmap** (Immediate / Short-term / Medium-term)\n"
            + "- **Systemic Recommendations**\n"
            + "- **Dissenting Opinions**\n\n"
            + "## Output Format for each finding (used by all personas)\n\n"
            + "```\n"
            + "### [SEVERITY-N] Short Title\n"
            + "- **File:** path/to/file.py:line\n"
            + "- **CWE:** CWE-XXX (if applicable)\n"
            + "- **Attack vector:** concrete exploitation, one sentence\n"
            + "- **Evidence:** `relevant code snippet`\n"
            + "- **Fix:** exact change required\n"
            + "- **Confidence:** HIGH/MEDIUM/LOW\n"
            + "```\n\n"
            + "## Output Format for your final response\n\n"
            + "After all files are written, your final assistant message should be a brief "
              "(< 500 words) executive summary referencing the synthesis file path. "
              "The full reports live in the files you wrote.\n\n"
            + "## Hard Rules\n"
            + "- All persona analysis is READ-ONLY. No file modifications during audit.\n"
            + "- Spawn personas in PARALLEL (single message, 6 Task calls).\n"
            + "- One subagent failure must NOT abort the others — note the failure in the synthesis.\n"
            + "- Write output files via the Write tool, not just in your response.\n"
        )

        master_result = provider.run(
            master_prompt,
            cwd=str(cwd_path),
            timeout=master_timeout,
            # Audit phase needs Read+Glob+Grep only; Write tool is enabled for the
            # report files (which are tool-output, not source-code mutation).
            read_only=False,
        )
        total_input_tokens += master_result.input_tokens
        total_output_tokens += master_result.output_tokens
        total_cache_creation += master_result.cache_creation_input_tokens
        total_cache_read += master_result.cache_read_input_tokens

        if not master_result.success:
            msg = f"Audit-Master fehlgeschlagen: {master_result.error}"
            print(f"  [deep-security-audit] {msg}")
            notify_tool_done(self.name, 1, False, msg)
            return ToolResult(
                success=False,
                output=master_result.output,
                iterations=1,
                error=msg,
                error_code=master_result.error,
                retryable=master_result.error in ("rate_limit", "timeout", "session_missing"),
                input_tokens=total_input_tokens,
                output_tokens=total_output_tokens,
                cache_creation_input_tokens=total_cache_creation,
                cache_read_input_tokens=total_cache_read,
            )

        synthesis_path = docs_dir / f"deep-security-audit-{timestamp}.md"
        synthesis_text = master_result.output
        if synthesis_path.exists():
            try:
                synthesis_text = synthesis_path.read_text(encoding="utf-8")
            except OSError:
                pass

        print(f"  [deep-security-audit] Audit + Synthesis abgeschlossen → {synthesis_path}")

        # ── Phase 2: Fix (optional, fresh subprocess) ────────────────
        if not do_fix:
            notify_tool_done(self.name, 1, True, f"Audit gespeichert: {synthesis_path.name}")
            return ToolResult(
                success=True,
                output=master_result.output,
                iterations=1,
                input_tokens=total_input_tokens,
                output_tokens=total_output_tokens,
                cache_creation_input_tokens=total_cache_creation,
                cache_read_input_tokens=total_cache_read,
            )

        if not is_cached_provider_available(provider.name):
            msg = (
                f"Provider erschöpft — Audit gespeichert ({synthesis_path.name}), "
                "Fixes noch ausstehend"
            )
            print(f"  [deep-security-audit] ⏸ {msg}")
            return _make_capacity_exhausted_result(
                msg, master_result.output, 1,
                total_input_tokens, total_output_tokens,
                total_cache_creation, total_cache_read,
            )

        notify_tool_progress(self.name, 2, 2, "Phase 2/2: Fixes werden implementiert...")
        print(f"  [deep-security-audit] Phase 2: Fixes implementieren...")

        fix_prompt = system_prompt + "\n\n" + _FIX_PROMPT.format(
            task=clean_task,
            synthesis_output=synthesis_text,
        )
        fix_result = provider.run(fix_prompt, cwd=str(cwd_path), timeout=fix_timeout)
        total_input_tokens += fix_result.input_tokens
        total_output_tokens += fix_result.output_tokens
        total_cache_creation += fix_result.cache_creation_input_tokens
        total_cache_read += fix_result.cache_read_input_tokens

        if fix_result.error:
            print(f"  [deep-security-audit] ⚠ Fix-Phase Fehler: {fix_result.error}")
            return ToolResult(
                success=False,
                output=master_result.output + "\n\n--- FIX ERROR ---\n" + fix_result.error,
                iterations=2,
                error=fix_result.error,
                error_code=fix_result.error_code,
                retryable=fix_result.retryable,
                input_tokens=total_input_tokens,
                output_tokens=total_output_tokens,
                cache_creation_input_tokens=total_cache_creation,
                cache_read_input_tokens=total_cache_read,
            )

        notify_tool_done(self.name, 2, True, f"Audit + Fixes done: {synthesis_path.name}")
        return ToolResult(
            success=True,
            output=master_result.output + "\n\n--- FIXES ---\n" + fix_result.output,
            iterations=2,
            input_tokens=total_input_tokens,
            output_tokens=total_output_tokens,
            cache_creation_input_tokens=total_cache_creation,
            cache_read_input_tokens=total_cache_read,
        )

    def _run_sequential_mode(
        self,
        task: str,
        provider: BaseProvider,
        cwd: str | None,
        timeout: int | None,
        memory_context: str,
    ) -> ToolResult:
        """Original 6-subprocess sequential path. Preserved for non-Claude
        providers and as the fallback when CLAUDE_SESSION_ENABLED is off."""
        cwd_path = Path(cwd) if cwd else Path(".")
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        docs_dir = cwd_path / "docs"
        do_fix = _wants_fix(task)
        clean_task = _clean_tags(task)
        agent_timeout = timeout or TOOL_DSA_AGENT_TIMEOUT_SEC

        total_input_tokens = 0
        total_output_tokens = 0
        total_agents = len(_AGENTS)
        total_phases = total_agents + 1 + (1 if do_fix else 0)  # agents + synthesis + fix

        agent_outputs: dict[str, str] = {}

        # ── Phase 1-6: Expert Agents (read-only) ─────────────────────

        for idx, agent in enumerate(_AGENTS, 1):
            if not is_cached_provider_available(provider.name):
                msg = (
                    f"Provider nicht verfügbar bei Agent {idx}/{total_agents} "
                    f"({agent.title}) — bisherige Ergebnisse gespeichert"
                )
                print(f"  [deep-security-audit] \u23f8 {msg}")
                if agent_outputs:
                    self._save_partial(docs_dir, timestamp, clean_task, provider, cwd_path, agent_outputs)
                return _make_capacity_exhausted_result(
                    msg,
                    self._format_partial(agent_outputs),
                    idx - 1,
                    total_input_tokens,
                    total_output_tokens,
                )

            notify_tool_progress(
                self.name, idx, total_phases,
                f"Agent {idx}/{total_agents}: {agent.title}...",
            )
            print(f"  [deep-security-audit] Agent {idx}/{total_agents}: {agent.title} ...")

            system_prompt = _build_system_prompt(
                provider.name,
                memory_context=memory_context,
                tool_name=self.name,
                cwd=cwd,
            )

            agent_prompt = (
                system_prompt
                + f"\n\n## Role: {agent.title}\n\n{agent.persona}"
                + f"\n\n---\n\n## Task\n\nPerform a focused security assessment."
                  f" Scope: {clean_task}\n\n### Checklist\n\n{agent.checklist}"
                + "\n\n### Output Format\n\n"
                  "For each finding use this structure:\n"
                  "```\n"
                  "### [SEVERITY-N] Short Title\n"
                  "- **File:** path/to/file.py:line\n"
                  "- **CWE:** CWE-XXX (if applicable)\n"
                  "- **Attack vector:** concrete exploitation, one sentence\n"
                  "- **Evidence:** `relevant code snippet`\n"
                  "- **Fix:** exact change required\n"
                  "- **Confidence:** HIGH/MEDIUM/LOW\n"
                  "```\n\n"
                  "List ONLY real findings with concrete evidence. No false positives.\n"
                  "Do NOT modify any files. This is a read-only analysis."
            )

            result = provider.run(
                agent_prompt,
                cwd=str(cwd_path),
                timeout=agent_timeout,
                read_only=True,
            )

            total_input_tokens += result.input_tokens
            total_output_tokens += result.output_tokens

            if result.error:
                print(f"  [deep-security-audit] \u26a0 Agent {agent.key} Fehler: {result.error}")
                agent_outputs[agent.key] = f"[FEHLER: {result.error}]"
                # Continue with remaining agents — partial results are still valuable
                continue

            agent_outputs[agent.key] = result.output
            print(
                f"  [deep-security-audit] \u2713 {agent.title} fertig "
                f"({len(result.output)} chars)"
            )

            # Save individual agent report
            agent_filename = f"deep-security-audit-{timestamp}-{agent.key}.md"
            agent_header = _make_report_header(
                f"Deep Security Audit \u2014 {agent.title}",
                timestamp, clean_task, provider.name, cwd_path,
            )
            _write_tool_file(docs_dir, agent_filename, agent_header + result.output)

        # ── Phase 7: CISO Synthesis ──────────────────────────────────

        if all(v.startswith("[FEHLER") for v in agent_outputs.values()):
            msg = "Alle Agenten fehlgeschlagen \u2014 keine Synthese m\u00f6glich"
            print(f"  [deep-security-audit] \u2717 {msg}")
            return ToolResult(
                success=False,
                output=self._format_partial(agent_outputs),
                iterations=total_agents,
                error=msg,
                input_tokens=total_input_tokens,
                output_tokens=total_output_tokens,
            )

        if not is_cached_provider_available(provider.name):
            msg = "Provider nicht verfügbar für CISO-Synthese — Agenten-Reports gespeichert"
            print(f"  [deep-security-audit] \u23f8 {msg}")
            self._save_partial(docs_dir, timestamp, clean_task, provider, cwd_path, agent_outputs)
            return _make_capacity_exhausted_result(
                msg,
                self._format_partial(agent_outputs),
                total_agents,
                total_input_tokens,
                total_output_tokens,
            )

        synthesis_phase = total_agents + 1
        notify_tool_progress(
            self.name, synthesis_phase, total_phases,
            "CISO-Synthese: Konsolidierung aller Findings...",
        )
        print(f"  [deep-security-audit] Phase {synthesis_phase}: CISO-Synthese ...")

        system_prompt = _build_system_prompt(
            provider.name,
            memory_context=memory_context,
            tool_name=self.name,
            cwd=cwd,
        )

        # Build agent reports block with per-agent truncation
        report_parts: list[str] = []
        total_chars = 0
        for agent in _AGENTS:
            output = agent_outputs.get(agent.key, "[nicht ausgeführt]")
            if len(output) > TOOL_DSA_MAX_AGENT_OUTPUT_CHARS:
                output = output[:TOOL_DSA_MAX_AGENT_OUTPUT_CHARS] + "\n\n...[truncated]"
            section = f"### {agent.title}\n\n{output}"
            if total_chars + len(section) > TOOL_DSA_MAX_TOTAL_INJECT_CHARS:
                section = section[:max(1000, TOOL_DSA_MAX_TOTAL_INJECT_CHARS - total_chars)]
                section += "\n\n...[total injection limit reached]"
                report_parts.append(section)
                break
            report_parts.append(section)
            total_chars += len(section)

        agent_reports_block = "\n\n---\n\n".join(report_parts)

        synthesis_prompt = (
            system_prompt + "\n\n"
            + _CISO_SYNTHESIS_PROMPT
                .replace("{agent_reports}", agent_reports_block)
                .replace("{task}", clean_task)
        )

        synthesis_result = provider.run(
            synthesis_prompt,
            cwd=str(cwd_path),
            timeout=TOOL_DSA_SYNTHESIS_TIMEOUT_SEC,
            read_only=True,
        )

        total_input_tokens += synthesis_result.input_tokens
        total_output_tokens += synthesis_result.output_tokens

        if synthesis_result.error:
            print(f"  [deep-security-audit] \u2717 Synthese Fehler: {synthesis_result.error}")
            self._save_partial(docs_dir, timestamp, clean_task, provider, cwd_path, agent_outputs)
            return ToolResult(
                success=False,
                output=self._format_partial(agent_outputs),
                iterations=synthesis_phase,
                error=f"CISO-Synthese fehlgeschlagen: {synthesis_result.error}",
                error_code=getattr(synthesis_result, "error_code", ""),
                retryable=getattr(synthesis_result, "retryable", False),
                input_tokens=total_input_tokens,
                output_tokens=total_output_tokens,
            )

        synthesis_output = synthesis_result.output
        print(f"  [deep-security-audit] \u2713 CISO-Synthese fertig ({len(synthesis_output)} chars)")

        # ── Phase 8: Fix Implementation (optional) ───────────────────

        fix_output = ""
        fix_phase_ran = False
        fix_error = ""

        if do_fix:
            fix_phase = total_phases
            if not is_cached_provider_available(provider.name):
                msg = "Provider nicht verfügbar für Fix-Phase — Audit-Report gespeichert"
                print(f"  [deep-security-audit] \u23f8 {msg}")
                # Save audit without fixes and signal retry
                report = self._build_combined_report(
                    timestamp, clean_task, provider, cwd_path,
                    agent_outputs, synthesis_output, "",
                )
                filename = f"deep-security-audit-{timestamp}-audit-only.md"
                _write_tool_file(docs_dir, filename, report)
                return _make_capacity_exhausted_result(
                    msg, synthesis_output, synthesis_phase,
                    total_input_tokens, total_output_tokens,
                )

            notify_tool_progress(self.name, fix_phase, total_phases, "Fix-Implementierung...")
            print(f"  [deep-security-audit] Phase {fix_phase}: Fixes implementieren ...")

            fix_system_prompt = _build_system_prompt(
                provider.name,
                memory_context=memory_context,
                tool_name=self.name,
                cwd=cwd,
            )

            # Truncate synthesis for fix prompt
            synth_for_fix = synthesis_output
            if len(synth_for_fix) > TOOL_DSA_MAX_AGENT_OUTPUT_CHARS:
                synth_for_fix = synth_for_fix[:TOOL_DSA_MAX_AGENT_OUTPUT_CHARS] + "\n...[truncated]"

            fix_prompt_text = (
                fix_system_prompt + "\n\n"
                + _FIX_PROMPT
                    .replace("{task}", clean_task)
                    .replace("{synthesis_output}", synth_for_fix)
            )

            fix_result = provider.run(
                fix_prompt_text,
                cwd=str(cwd_path),
                timeout=TOOL_DSA_FIX_TIMEOUT_SEC,
            )

            total_input_tokens += fix_result.input_tokens
            total_output_tokens += fix_result.output_tokens
            fix_output = fix_result.output
            fix_phase_ran = True

            if fix_result.error:
                fix_error = fix_result.error
                print(f"  [deep-security-audit] \u26a0 Fix-Phase Fehler: {fix_error}")
            else:
                print(f"  [deep-security-audit] \u2713 Fixes implementiert ({len(fix_output)} chars)")

        # ── Combined Report ──────────────────────────────────────────

        report = self._build_combined_report(
            timestamp, clean_task, provider, cwd_path,
            agent_outputs, synthesis_output, fix_output,
        )
        filename = f"deep-security-audit-{timestamp}.md"
        _write_tool_file(docs_dir, filename, report)
        print(f"  [deep-security-audit] \u2713 Report: {docs_dir / filename}")

        iterations = total_agents + 1 + (1 if fix_phase_ran else 0)
        success = not fix_error
        output_summary = f"Report: docs/{filename}"

        if fix_phase_ran and fix_error:
            output_summary += f" (Fix-Fehler: {fix_error[:60]})"
        elif fix_phase_ran and fix_output:
            output_summary += " (inkl. Fixes)"

        notify_tool_done(self.name, iterations, success, output_summary)

        # Propagate error_code/retryable from fix phase (matches security_audit.py)
        fix_error_code = getattr(fix_result, "error_code", "") if fix_phase_ran else ""
        fix_retryable = getattr(fix_result, "retryable", False) if fix_phase_ran else False

        return ToolResult(
            success=success,
            output=f"{output_summary}\n\n{synthesis_output}",
            iterations=iterations,
            error=fix_error,
            error_code=fix_error_code,
            retryable=fix_retryable,
            input_tokens=total_input_tokens,
            output_tokens=total_output_tokens,
        )

    # ── Helpers ──────────────────────────────────────────────────────

    def _build_combined_report(
        self,
        timestamp: str,
        task: str,
        provider: BaseProvider,
        cwd_path: Path,
        agent_outputs: dict[str, str],
        synthesis: str,
        fix_output: str,
    ) -> str:
        header = _make_report_header(
            "Deep Security Audit (Multi-Agent)",
            timestamp, task, provider.name, cwd_path,
        )
        parts = [header]

        # Agent reports
        parts.append("# Expert Agent Reports\n")
        for agent in _AGENTS:
            output = agent_outputs.get(agent.key, "[nicht ausgeführt]")
            parts.append(f"## {agent.title}\n\n{output}\n")

        # Synthesis
        parts.append("\n---\n\n# CISO Synthesis\n\n" + synthesis + "\n")

        # Fixes
        if fix_output:
            parts.append("\n---\n\n# Fixes Applied\n\n" + fix_output + "\n")

        return "\n".join(parts)

    def _save_partial(
        self,
        docs_dir: Path,
        timestamp: str,
        task: str,
        provider: BaseProvider,
        cwd_path: Path,
        agent_outputs: dict[str, str],
    ) -> None:
        """Save partial results when interrupted by capacity exhaustion."""
        filename = f"deep-security-audit-{timestamp}-partial.md"
        header = _make_report_header(
            "Deep Security Audit (PARTIAL — capacity exhausted)",
            timestamp, task, provider.name, cwd_path,
        )
        parts = [header]
        for agent in _AGENTS:
            output = agent_outputs.get(agent.key)
            if output:
                parts.append(f"## {agent.title}\n\n{output}\n")
        _write_tool_file(docs_dir, filename, "\n".join(parts))
        print(f"  [deep-security-audit] Partial report: {docs_dir / filename}")

    @staticmethod
    def _format_partial(agent_outputs: dict[str, str]) -> str:
        """Format partial agent outputs for ToolResult.output."""
        parts = []
        for agent in _AGENTS:
            output = agent_outputs.get(agent.key)
            if output:
                parts.append(f"## {agent.title}\n{output}")
        return "\n\n---\n\n".join(parts) if parts else "[keine Ergebnisse]"

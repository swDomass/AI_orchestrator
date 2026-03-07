"""
Test-Loop Tool: Run tests → Fix failures → Re-run until green.

Usage in queue:
    - [ ] Lasse alle Tests laufen und fixe Fehler #tool:test-loop cwd:/d/programmieren/projekt
    - [ ] pytest tests/ bis alles grün #tool:test-loop cwd:/d/programmieren/projekt
"""

import re
import time

from config import TOOL_MAX_ITERATIONS, TOOL_FIX_TIMEOUT_SEC, TOOL_INTER_STEP_SLEEP_SEC
from notifier import notify_tool_progress, notify_tool_done
from providers.base import BaseProvider
from tools.base_tool import BaseTool, ToolResult, _build_system_prompt

_TEST_PROMPT_BODY = """
Run the test suite in the current working directory.

Instructions:
- Identify the test framework used (pytest, unittest, jest, etc.)
- Run the tests
- Report the results: total, passed, failed, errors
- If ALL tests pass, say exactly: `ALL TESTS PASSED`
- If tests fail, list each failure with file path and error message
"""

_FIX_PROMPT_BODY = """
Fix the failing tests from iteration {iteration}.

Instructions:
- Fix the code (NOT the tests) to make them pass, unless the test itself is clearly wrong.
- Apply changes directly to files.
- Do NOT skip or delete tests.
- Summarize what was fixed.

Test failures:
{failures}
"""


# Patterns that indicate failure when no explicit green summary is detected
_FAIL_PATTERNS = ["failed", "error", "failures"]

# Regex patterns for pass/fail detection to avoid substring false positives
_PYTEST_PASSED_RE = re.compile(r"\d+\s+passed")
_PYTEST_NONZERO_FAILED_RE = re.compile(r"\b[1-9]\d*\s+failed\b", re.IGNORECASE)
_PYTEST_NONZERO_ERRORS_RE = re.compile(r"\b[1-9]\d*\s+errors?\b", re.IGNORECASE)
_UNITTEST_OK_RE = re.compile(r"^OK\b", re.MULTILINE)


def _tests_passed(output: str) -> bool:
    """Heuristic: check if test output indicates all tests passed."""
    lower = output.lower()
    if "all tests passed" in lower:
        return True

    # Do not treat "no tests found/ran" as a success signal.
    if any(p in lower for p in ("no tests ran", "collected 0 items", "ran 0 tests")):
        return False

    if _PYTEST_PASSED_RE.search(output):
        if _PYTEST_NONZERO_FAILED_RE.search(output) or _PYTEST_NONZERO_ERRORS_RE.search(output):
            return False
        return True
    if _UNITTEST_OK_RE.search(output):
        return True

    return False


class TestLoopTool(BaseTool):
    name = "test-loop"
    description = "Run Tests > Fix Failures > Re-run bis gruen"

    def run(
        self,
        task: str,
        provider: BaseProvider,
        cwd: str | None = None,
        timeout: int | None = None,
        memory_context: str = "",
    ) -> ToolResult:
        print(f"  [test-loop] Starte iterativen Test/Fix-Loop (max {TOOL_MAX_ITERATIONS}x)")

        system_prompt = _build_system_prompt(provider.name, memory_context)
        test_prompt = f"{system_prompt}\n\n{task}\n\n{_TEST_PROMPT_BODY}"
        all_outputs: list[str] = []
        last_failures: str = ""

        step_timeout = timeout or TOOL_FIX_TIMEOUT_SEC

        total_input_tokens = 0
        total_output_tokens = 0

        for iteration in range(1, TOOL_MAX_ITERATIONS + 1):
            print(f"\n  [test-loop] === Iteration {iteration}/{TOOL_MAX_ITERATIONS}: TESTS ===")

            test_result = provider.run(
                test_prompt,
                cwd=cwd,
                timeout=step_timeout,
            )
            total_input_tokens += test_result.input_tokens
            total_output_tokens += test_result.output_tokens

            if not test_result.success:
                msg = f"Tests konnten nicht ausgeführt werden: {test_result.error}"
                print(f"  [test-loop] {msg}")
                notify_tool_done(self.name, iteration, False, msg)
                return ToolResult(success=False, output="\n\n".join(all_outputs),
                                  iterations=iteration, error=msg,
                                  error_code=test_result.error, retryable=True,
                                  input_tokens=total_input_tokens,
                                  output_tokens=total_output_tokens)

            all_outputs.append(f"--- Test Run {iteration} ---\n{test_result.output}")

            if _tests_passed(test_result.output):
                msg = f"Alle Tests bestanden nach {iteration} Iteration(en)."
                print(f"  [test-loop] ✅ {msg}")
                notify_tool_done(self.name, iteration, True, msg)
                return ToolResult(success=True, output="\n\n".join(all_outputs),
                                  iterations=iteration,
                                  input_tokens=total_input_tokens,
                                  output_tokens=total_output_tokens)

            # Tests failed - check if same failures as before (loop detection)
            current_failures = test_result.output
            if current_failures == last_failures:
                msg = f"Gleiche Test-Fehler nach {iteration} Iterationen. Loop beendet."
                print(f"  [test-loop] ⚠️ {msg}")
                notify_tool_done(self.name, iteration, False, msg)
                return ToolResult(success=False, output="\n\n".join(all_outputs),
                                  iterations=iteration, error=msg,
                                  input_tokens=total_input_tokens,
                                  output_tokens=total_output_tokens)
            last_failures = current_failures

            # Fix
            print(f"  [test-loop] === Iteration {iteration}/{TOOL_MAX_ITERATIONS}: FIX ===")
            notify_tool_progress(self.name, iteration, TOOL_MAX_ITERATIONS,
                                 "Fixing test failures...")

            fix_prompt = f"{system_prompt}\n\n" + _FIX_PROMPT_BODY.format(iteration=iteration, failures=test_result.output)
            fix_result = provider.run(fix_prompt, cwd=cwd, timeout=step_timeout)
            total_input_tokens += fix_result.input_tokens
            total_output_tokens += fix_result.output_tokens

            if not fix_result.success:
                msg = f"Fix fehlgeschlagen: {fix_result.error}"
                print(f"  [test-loop] {msg}")
                notify_tool_done(self.name, iteration, False, msg)
                return ToolResult(success=False, output="\n\n".join(all_outputs),
                                  iterations=iteration, error=msg,
                                  error_code=fix_result.error, retryable=True,
                                  input_tokens=total_input_tokens,
                                  output_tokens=total_output_tokens)

            all_outputs.append(f"--- Fix {iteration} ---\n{fix_result.output}")
            time.sleep(TOOL_INTER_STEP_SLEEP_SEC)

        msg = f"Max Iterationen ({TOOL_MAX_ITERATIONS}) erreicht."
        notify_tool_done(self.name, TOOL_MAX_ITERATIONS, False, msg)
        return ToolResult(success=False, output="\n\n".join(all_outputs),
                          iterations=TOOL_MAX_ITERATIONS, error=msg,
                          input_tokens=total_input_tokens,
                          output_tokens=total_output_tokens)

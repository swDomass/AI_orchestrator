"""
Test-Loop Tool: Run tests → Fix failures → Re-run until green.

Usage in queue:
    - [ ] Lasse alle Tests laufen und fixe Fehler #tool:test-loop cwd:/d/programmieren/projekt
    - [ ] pytest tests/ bis alles grün #tool:test-loop cwd:/d/programmieren/projekt
"""

import time

from config import SAFETY_RULES, TOOL_MAX_ITERATIONS, TOOL_FIX_TIMEOUT_SEC
from notifier import notify_tool_progress, notify_tool_done
from providers.base import BaseProvider
from tools.base_tool import BaseTool, ToolResult

_TEST_PROMPT = SAFETY_RULES + """

Run the test suite in the current working directory.

Instructions:
- Identify the test framework used (pytest, unittest, jest, etc.)
- Run the tests
- Report the results: total, passed, failed, errors
- If ALL tests pass, say exactly: `ALL TESTS PASSED`
- If tests fail, list each failure with file path and error message
"""

_FIX_PROMPT = SAFETY_RULES + """

Fix the failing tests from iteration {iteration}.

Instructions:
- Fix the code (NOT the tests) to make them pass, unless the test itself is clearly wrong.
- Apply changes directly to files.
- Do NOT skip or delete tests.
- Summarize what was fixed.

Test failures:
{failures}
"""

# Patterns that indicate failure (checked first — takes priority over pass patterns)
FAIL_PATTERNS = [
    "FAILED",
    "failed",
    "ERROR",
    "error",
    "FAILURES",
]

# Regex patterns for pass detection to avoid substring false positives
# e.g. "OK" matching inside "token", "passed" is safe since "failed" is checked first
import re
_PYTEST_PASSED_RE = re.compile(r"\d+\s+passed")
_UNITTEST_OK_RE = re.compile(r"^OK\b", re.MULTILINE)


def _tests_passed(output: str) -> bool:
    """Heuristic: check if test output indicates all tests passed."""
    lower = output.lower()
    if "all tests passed" in lower:
        return True

    # Do not treat "no tests found/ran" as a success signal.
    if any(p in lower for p in ("no tests ran", "collected 0 items", "ran 0 tests")):
        return False

    has_fail = any(p.lower() in lower for p in FAIL_PATTERNS)
    if has_fail:
        return False

    # Use regex to avoid false positives (e.g. "OK" matching inside "token")
    if _PYTEST_PASSED_RE.search(output):
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
    ) -> ToolResult:
        print(f"  [test-loop] Starte iterativen Test/Fix-Loop (max {TOOL_MAX_ITERATIONS}x)")

        test_prompt = f"{task}\n\n{_TEST_PROMPT}"
        all_outputs: list[str] = []
        last_failures: str = ""

        for iteration in range(1, TOOL_MAX_ITERATIONS + 1):
            print(f"\n  [test-loop] === Iteration {iteration}/{TOOL_MAX_ITERATIONS}: TESTS ===")

            test_result = provider.run(
                test_prompt,
                cwd=cwd,
                timeout=TOOL_FIX_TIMEOUT_SEC,
            )

            if not test_result.success:
                msg = f"Tests konnten nicht ausgeführt werden: {test_result.error}"
                print(f"  [test-loop] {msg}")
                notify_tool_done(self.name, iteration, False, msg)
                return ToolResult(success=False, output="\n\n".join(all_outputs),
                                  iterations=iteration, error=msg)

            all_outputs.append(f"--- Test Run {iteration} ---\n{test_result.output}")

            if _tests_passed(test_result.output):
                msg = f"Alle Tests bestanden nach {iteration} Iteration(en)."
                print(f"  [test-loop] ✅ {msg}")
                notify_tool_done(self.name, iteration, True, msg)
                return ToolResult(success=True, output="\n\n".join(all_outputs),
                                  iterations=iteration)

            # Tests failed - check if same failures as before (loop detection)
            current_failures = test_result.output
            if current_failures == last_failures:
                msg = f"Gleiche Test-Fehler nach {iteration} Iterationen. Loop beendet."
                print(f"  [test-loop] ⚠️ {msg}")
                notify_tool_done(self.name, iteration, False, msg)
                return ToolResult(success=False, output="\n\n".join(all_outputs),
                                  iterations=iteration, error=msg)
            last_failures = current_failures

            # Fix
            print(f"  [test-loop] === Iteration {iteration}/{TOOL_MAX_ITERATIONS}: FIX ===")
            notify_tool_progress(self.name, iteration, TOOL_MAX_ITERATIONS,
                                 "Fixing test failures...")

            fix_prompt = _FIX_PROMPT.format(iteration=iteration, failures=test_result.output)
            fix_result = provider.run(fix_prompt, cwd=cwd, timeout=TOOL_FIX_TIMEOUT_SEC)

            if not fix_result.success:
                msg = f"Fix fehlgeschlagen: {fix_result.error}"
                print(f"  [test-loop] {msg}")
                notify_tool_done(self.name, iteration, False, msg)
                return ToolResult(success=False, output="\n\n".join(all_outputs),
                                  iterations=iteration, error=msg)

            all_outputs.append(f"--- Fix {iteration} ---\n{fix_result.output}")
            time.sleep(2)

        msg = f"Max Iterationen ({TOOL_MAX_ITERATIONS}) erreicht."
        notify_tool_done(self.name, TOOL_MAX_ITERATIONS, False, msg)
        return ToolResult(success=False, output="\n\n".join(all_outputs),
                          iterations=TOOL_MAX_ITERATIONS, error=msg)

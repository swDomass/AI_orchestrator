"""Guards for the onboarding docs: README facts that must keep matching the code.

Reads files relative to the repository root, writes nothing.
"""
import json
import re
from pathlib import Path

import config

ROOT = Path(__file__).resolve().parent.parent
README = (ROOT / "README.md").read_text(encoding="utf-8")
_SKIP_DIRS = {".dev-loop", ".git", ".scratch", ".venv", "venv", "node_modules", "__pycache__"}


def _repo_files():
    for path in ROOT.rglob("*"):
        if (path.suffix in {".py", ".md"} and path.is_file()
                and not _SKIP_DIRS.intersection(path.relative_to(ROOT).parts)):
            yield path


def test_old_claude_package_name_is_gone():
    # Assembled so that this file does not match itself.
    old_name = "@anthropic" + "/claude-code"
    hits = [str(p.relative_to(ROOT)) for p in _repo_files()
            if old_name in p.read_text(encoding="utf-8", errors="replace")]
    assert hits == []


def test_readme_states_result_location_and_memory_retention():
    assert "task_results" in README
    for days in (config.MEMORY_MAX_AGE_DAYS, config.MEMORY_ARCHIVE_DELETE_DAYS):
        pattern = rf"archive[^\n]{{0,200}}\b{days} days|\b{days} days[^\n]{{0,200}}archive"
        assert re.search(pattern, README, re.IGNORECASE), f"{days} days near 'archive'"


def test_readme_hook_snippet_is_valid_and_points_at_existing_script():
    blocks = re.findall(r"```json\n(.*?)\n```", README, re.DOTALL)
    snippet = next(b for b in blocks if "PreToolUse" in b)
    entry = json.loads(snippet)["hooks"]["PreToolUse"][0]
    assert entry["matcher"] == "Bash|PowerShell"
    assert entry["hooks"][0]["command"].endswith("scripts/safety_hook.py")
    assert (ROOT / "scripts" / "safety_hook.py").is_file()


def test_readme_head_warns_about_language_and_platform():
    head = "\n".join(README.splitlines()[:40])
    assert "German" in head
    assert "Windows" in head


def test_readme_points_at_the_example_policy_file():
    assert "shipped policy" not in README
    assert "policy.example.yaml" in README
    assert (ROOT / "policy.example.yaml").is_file()


def test_readme_does_not_claim_results_are_appended_to_the_queue():
    assert "appends `## Results`" not in README

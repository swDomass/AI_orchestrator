"""Tests for notifier.notify_approval_required() — the night approval with content.

Until 2026-10-05 the request carried the task cut to 100 characters, the reasons and
the commands, nothing else. It is sent BEFORE the provider runs, so there is no diff
to show; what it can show is the full task, the cwd, the repo state and the text that
tripped each rule. Two properties matter more than any of that: the message must stay
within Telegram's limits and parse as legacy Markdown (otherwise it never arrives and
the request times out), and nothing in the enrichment may raise (orchestrator.py would
book that as "policy check failed" and run the task unapproved).
"""

import shutil
import subprocess

import pytest

import notifier
import policy as policy_module

_LIMIT = 3500
_LAST_LINE = "/skip — skip for now, task retries later"
_has_git = pytest.mark.skipif(shutil.which("git") is None, reason="git not on PATH")


@pytest.fixture
def sent(monkeypatch):
    messages = []

    def record(text):
        messages.append(text)
        return True

    monkeypatch.setattr(notifier, "_send", record)
    return messages


@pytest.fixture
def no_outer_repo(tmp_path, monkeypatch):
    """Keep git from walking up out of tmp_path into whatever repo contains it."""
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    return tmp_path


def _legacy_markdown_problems(text: str) -> list[str]:
    """What would make Telegram's legacy Markdown parser reject *text*.

    Outside a code span: `_` and `[` must be escaped (this message never wants
    italics or links), `*` must pair up (the bold title). Inside one nothing is
    special until the closing backtick, which therefore has to exist.
    """
    problems: list[str] = []
    in_code = False
    stars = 0
    i = 0
    while i < len(text):
        ch = text[i]
        if in_code:
            in_code = ch != "`"
        elif ch == "\\" and i + 1 < len(text):
            i += 2
            continue
        elif ch == "`":
            in_code = True
        elif ch in "_[":
            problems.append(f"unescaped {ch!r} at {i}: {text[max(0, i - 20):i + 20]!r}")
        elif ch == "*":
            stars += 1
        i += 1
    if in_code:
        problems.append("unclosed code span")
    if stars % 2:
        problems.append("unbalanced '*'")
    return problems


def _git(cwd, *args):
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid",
         "-c", "commit.gpgsign=false", *args],
        cwd=cwd, check=True, capture_output=True,
    )


# ---------------------------------------------------------------------------
# Length and Markdown
# ---------------------------------------------------------------------------

def test_long_task_text_stays_under_the_limit(sent):
    task = "Rendere die Reel-Assets für Kleid ÄÖÜ_ß " * 400          # ~16 kB, multibyte
    reasons = [f"reason_{i}_" + "x_" * 80 for i in range(7)]          # escaping doubles these
    triggers = dict.fromkeys(reasons, "…" + "y_" * 100 + "…")
    cwd = "/very/long/path_" + "segment_" * 60

    notifier.notify_approval_required(task, reasons, 1800, cwd=cwd, triggers=triggers)

    (msg,) = sent
    assert len(msg.encode("utf-8")) <= _LIMIT
    assert msg.endswith(_LAST_LINE)              # the commands survive, the task gave way
    assert "Rendere die Reel-Assets" in msg
    assert _legacy_markdown_problems(msg) == []


def test_task_text_is_kept_up_to_about_1500_characters(sent):
    """The old cut was 100 characters. A 1400-character task now arrives in full."""
    task = "a" * 1300 + " ENDE-DES-AUFTRAGS"

    notifier.notify_approval_required(task, ["git push to remote"], 1800)

    (msg,) = sent
    assert "ENDE-DES-AUFTRAGS" in msg


def test_task_text_beyond_1500_bytes_is_cut(sent):
    notifier.notify_approval_required("b" * 1490 + "c" * 100, ["git push to remote"], 1800)

    (msg,) = sent
    assert "b" * 1490 in msg
    assert "c" * 11 not in msg
    assert "bcccccccccc..." in msg


def test_underscore_path_yields_valid_markdown(sent, no_outer_repo):
    cwd = no_outer_repo / "my_project_dir" / "sub_dir"
    cwd.mkdir(parents=True)
    reasons = ["rm_rf on build_output", "git push to remote"]
    triggers = {"rm_rf on build_output": "…lösche build_output mit rm -rf und…"}

    notifier.notify_approval_required(
        "Räume build_output auf und push_e #tag", reasons, 1800,
        cwd=str(cwd), triggers=triggers,
    )

    (msg,) = sent
    assert _legacy_markdown_problems(msg) == []
    assert f"cwd: `{cwd}`" in msg                       # verbatim inside the code span
    assert "rm\\_rf on build\\_output" in msg            # reason escaped outside code


def test_markdown_checker_catches_what_it_should():
    """Gegenprobe for the helper above — otherwise its silence would prove nothing."""
    assert _legacy_markdown_problems("cwd: /a/my_dir")
    assert _legacy_markdown_problems("Task: `unclosed")
    assert _legacy_markdown_problems("*bold")
    assert _legacy_markdown_problems("cwd: `/a/my_dir`") == []
    assert _legacy_markdown_problems("a\\_b *x*") == []


# ---------------------------------------------------------------------------
# Repo block
# ---------------------------------------------------------------------------

@_has_git
def test_cwd_without_git_has_no_repo_block(sent, no_outer_repo):
    plain = no_outer_repo / "kein_repo"
    plain.mkdir()

    notifier.notify_approval_required("Task", ["git push to remote"], 1800, cwd=str(plain))

    (msg,) = sent
    assert f"cwd: `{plain}`" in msg
    assert "Repo:" not in msg


def test_no_cwd_means_neither_cwd_nor_repo_line(sent):
    notifier.notify_approval_required("Task", ["git push to remote"], 1800)

    (msg,) = sent
    assert "cwd:" not in msg
    assert "Repo:" not in msg


@_has_git
def test_git_repo_reports_branch_uncommitted_and_ahead(sent, no_outer_repo):
    repo = no_outer_repo / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "checkout", "-q", "-b", "feature_x")
    (repo / "a.txt").write_text("v1\n", encoding="utf-8")
    _git(repo, "add", "a.txt")
    _git(repo, "commit", "-q", "-m", "c1")
    _git(repo, "branch", "base")
    _git(repo, "branch", "-q", "--set-upstream-to=base")
    (repo / "a.txt").write_text("v2\n", encoding="utf-8")
    _git(repo, "commit", "-q", "-am", "c2")                 # one commit ahead of base
    (repo / "a.txt").write_text("v3\n", encoding="utf-8")   # one uncommitted file

    notifier.notify_approval_required("Task", ["git push to remote"], 1800, cwd=str(repo))

    (msg,) = sent
    assert "Repo: `feature_x`, 1 uncommitted, 1 ahead of upstream" in msg
    assert _legacy_markdown_problems(msg) == []


@_has_git
def test_git_repo_without_upstream_omits_the_ahead_count(sent, no_outer_repo):
    repo = no_outer_repo / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "checkout", "-q", "-b", "main")
    (repo / "a.txt").write_text("v1\n", encoding="utf-8")
    _git(repo, "add", "a.txt")
    _git(repo, "commit", "-q", "-m", "c1")
    (repo / "new.txt").write_text("x\n", encoding="utf-8")

    notifier.notify_approval_required("Task", ["git push to remote"], 1800, cwd=str(repo))

    (msg,) = sent
    assert "Repo: `main`, 1 uncommitted\n" in msg
    assert "ahead" not in msg


def test_git_timeout_drops_the_repo_block_silently(sent, monkeypatch, tmp_path):
    timeouts = []

    def hanging_git(cmd, **kwargs):
        timeouts.append(kwargs.get("timeout"))
        raise subprocess.TimeoutExpired(cmd, kwargs.get("timeout"))

    monkeypatch.setattr(notifier.subprocess, "run", hanging_git)

    notifier.notify_approval_required("Task", ["git push to remote"], 1800, cwd=str(tmp_path))

    (msg,) = sent
    assert "Repo:" not in msg
    assert f"cwd: `{tmp_path}`" in msg
    assert msg.endswith(_LAST_LINE)
    assert timeouts and all(t is not None and t <= 5 for t in timeouts)


def test_every_git_call_is_capped_at_five_seconds(sent, monkeypatch, tmp_path):
    seen = []

    def fake_git(cmd, **kwargs):
        seen.append((cmd[1], kwargs.get("timeout")))
        out = {"branch": "main\n", "status": " M a.txt\n", "rev-list": "2\n"}[cmd[1]]
        return subprocess.CompletedProcess(cmd, 0, stdout=out, stderr="")

    monkeypatch.setattr(notifier.subprocess, "run", fake_git)

    notifier.notify_approval_required("Task", ["git push to remote"], 1800, cwd=str(tmp_path))

    assert [c for c, _ in seen] == ["branch", "status", "rev-list"]
    assert all(t is not None and t <= 5 for _, t in seen), seen
    assert "Repo: `main`, 1 uncommitted, 2 ahead of upstream" in sent[0]


def test_any_git_exception_never_blocks_the_request(sent, monkeypatch, tmp_path):
    def broken_git(cmd, **kwargs):
        raise RuntimeError("git exploded")

    monkeypatch.setattr(notifier.subprocess, "run", broken_git)

    notifier.notify_approval_required("Task", ["git push to remote"], 1800, cwd=str(tmp_path))

    (msg,) = sent
    assert "Repo:" not in msg
    assert msg.endswith(_LAST_LINE)


# ---------------------------------------------------------------------------
# Triggers per reason, and the never-raise guarantee
# ---------------------------------------------------------------------------

def test_each_reason_shows_its_trigger_when_known(sent):
    reasons = ["git push to remote", "Datei löschen"]
    triggers = {"git push to remote": "…committe und mache git push origin main danach…"}

    notifier.notify_approval_required("Task", reasons, 1800, triggers=triggers)

    (msg,) = sent
    assert "• git push to remote\n  ↳ `…committe und mache git push origin main danach…`" in msg
    assert "• Datei löschen\n\n" in msg                 # no trigger → reason only


def test_formatting_failure_falls_back_to_the_short_request(sent):
    """A broken enrichment must still send the request — never raise."""
    notifier.notify_approval_required(
        "Task", ["git push to remote"], 1800, cwd=None, triggers=object(),  # no .get
    )

    (msg,) = sent
    assert "• git push to remote" in msg
    assert msg.endswith(_LAST_LINE)


def test_reasons_as_a_set_are_accepted(sent):
    """reasons[:5] on a set raised TypeError — in the approval path that means an
    unapproved run, so any iterable has to do."""
    notifier.notify_approval_required("Task", {"git push to remote"}, 1800)

    (msg,) = sent
    assert "• git push to remote" in msg


def test_even_the_short_form_failing_still_sends_a_request(sent):
    notifier.notify_approval_required("Task", ["git push to remote"], None)  # no // 60

    (msg,) = sent
    assert msg == notifier._APPROVAL_BARE_TEXT
    assert _legacy_markdown_problems(msg) == []


# ---------------------------------------------------------------------------
# policy side: where the triggers come from
# ---------------------------------------------------------------------------

def _engine(tmp_path, policy_yaml: str) -> policy_module.PolicyEngine:
    ai = tmp_path / "vault" / "99_System" / "AI"
    ai.mkdir(parents=True)
    (ai / "policy.yaml").write_text(policy_yaml, encoding="utf-8")
    return policy_module.PolicyEngine(vault_path=tmp_path / "vault")


_POLICY = """
approve:
  - pattern: "git\\\\s+push"
    message: "git push to remote"
  - pattern: "rm\\\\s+-rf"
    message: "recursive delete"
"""


def test_match_excerpts_quotes_the_trigger_with_context(tmp_path):
    engine = _engine(tmp_path, _POLICY)
    text = (
        "Baue das Release, committe alles und mache dann git push origin main, "
        "danach schreibe die Release-Mail an das ganze Team"
    )

    excerpts = engine.match_excerpts([text], ["git push to remote", "frei erfunden"])

    assert set(excerpts) == {"git push to remote"}
    assert "git push" in excerpts["git push to remote"]
    assert excerpts["git push to remote"].startswith("…")
    assert excerpts["git push to remote"].endswith("…")


def test_match_excerpts_searches_subtask_texts_too(tmp_path):
    engine = _engine(tmp_path, _POLICY)

    excerpts = engine.match_excerpts(["Parent ohne Treffer", "rm -rf build"], ["recursive delete"])

    assert excerpts == {"recursive delete": "rm -rf build"}


def test_match_excerpts_never_raises(tmp_path):
    engine = _engine(tmp_path, _POLICY)
    assert engine.match_excerpts(None, ["git push to remote"]) == {}


def test_request_approval_hands_cwd_and_triggers_to_the_notifier(tmp_path, monkeypatch):
    engine = _engine(tmp_path, _POLICY)
    calls = []
    monkeypatch.setattr(
        notifier, "notify_approval_required",
        lambda *args, **kwargs: calls.append((args, kwargs)),
    )

    result = engine.request_approval(
        "Release bauen #claude", ["git push to remote"], 0,
        cwd="/srv/proj_x", checked_texts=["Release bauen und git push"],
    )

    assert result == "timeout"
    ((args, kwargs),) = calls
    assert args == ("Release bauen #claude", ["git push to remote"], 0)
    assert kwargs["cwd"] == "/srv/proj_x"
    assert "git push" in kwargs["triggers"]["git push to remote"]


def test_request_approval_without_context_keeps_working(tmp_path, monkeypatch):
    """The scientific-investigation caller passes neither cwd nor texts."""
    engine = _engine(tmp_path, _POLICY)
    calls = []
    monkeypatch.setattr(
        notifier, "notify_approval_required",
        lambda *args, **kwargs: calls.append((args, kwargs)),
    )

    assert engine.request_approval("bypass for run 7", ["Run-ID: 7"], 0) == "timeout"
    ((_args, kwargs),) = calls
    assert kwargs == {"cwd": None, "triggers": {}}

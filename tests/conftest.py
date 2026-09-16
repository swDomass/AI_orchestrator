"""Pytest fixtures shared across the suite.

- ``sys.path``: makes project packages importable.
- **Root vault redirect** (module-level, below): Isolation for `memory.py`'s path
  constants, preventing test writes to the real vault. Root-cause fix for a
  2026-09-16 finding (Runde 2, code review): `_refuse_real_vault_writes_under_pytest()`
  in `memory.py` was called only from `_ensure_dirs()`, missing `_cleanup_lessons()`
  and other writers that never touch that function — measured on 2026-09-16 14:19:33,
  `archive_old_memories()` moved a real file during a test run. Mechanics:
  (1) `ORCH_VAULT_PATH` is set in `os.environ` to a scratch directory BEFORE
  `config` is imported for the first time — `config._load_dotenv()` respects
  pre-existing env vars, so the redirected value propagates to every `from config
  import VAULT_PATH`. (2) The real vault path is obtained independently via
  `_find_real_vault_path()` and exported as `_ORCH_TEST_REAL_VAULT_PATH` — this
  reference never touches `config`, so it cannot be computed against the wrong
  vault. (3) `memory._refuse_real_vault_writes_under_pytest()` is called at every
  write/move/delete operation (9 locations), comparing the target path against the
  real root — a distributed check that cannot miss a writer. (4) Session-level
  verification in `_guard_real_vault_and_docs_untouched`: file counts and content
  hashes of `lessons.md` and `MEMORY.md` at session start and end.
- ``_isolate_replay_store``: autouse — prevents tests from polluting the
  production ``logs/runs.jsonl`` when they exercise code paths that emit
  replay records (e.g. orchestrator ``_RunSpan.emit``). Individual test
  files can still override the path with their own fixture.
- ``_isolate_memory_and_docs_output``: autouse — the process cwd half of the leak
  above (tools defaulting to ``Path(".")``/``Path.cwd()`` when no ``cwd=`` is
  given); the memory-path half is now handled entirely by the root redirect, so
  this fixture no longer lists `memory.py` constants by hand. See the fixture's
  own docstring for the measured `docs/` leak this closes.
- ``_guard_real_vault_and_docs_untouched``: autouse, SESSION-scoped — the
  belt-and-suspenders check for the redirect above. Compares file counts AND
  content hashes (`lessons.md`, the curated `MEMORY.md`) in the real vault, and
  file counts in this repo's own `docs/`, once at session start and once at
  session end; a mismatch fails the run even if something bypasses the redirect.
  Lives here (not in a standalone test module) so a `-k`/single-file selection
  still gets it — an autouse fixture in an ordinary test file only applies to
  tests collected from THAT module.
"""
import os
import re
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))


def _normalize_dotenv_value(value: str) -> str:
    """Mirror of ``config._normalize_dotenv_value`` (config.py:7) — duplicated
    byte-for-byte, not imported, because this file must read ORCH_VAULT_PATH
    from ``.env`` BEFORE the first ``import config`` anywhere in the process
    (see ``_find_real_vault_path``'s own docstring below for why importing
    config here would be wrong). P3-2 (oc r1): an earlier version of
    ``_find_real_vault_path`` stripped only surrounding quotes and did not
    handle an inline ``#`` comment the way ``config._normalize_dotenv_value``
    does — a future ``ORCH_VAULT_PATH=D:\\… # comment`` line in ``.env`` would
    make `config` and this file disagree on the real vault path, and the
    guard above would then compare against a directory that does not exist
    (silent failure, exactly the class this guard exists to close).
    ``tests/test_dotenv_normalization_mirror.py`` holds this copy equal to
    ``config._normalize_dotenv_value`` across a battery of example values so
    the two cannot silently drift apart again.
    """
    m = re.match(r'^(["\'])(.*)\1(?:\s*#.*)?$', value)
    if m:
        return m.group(2)
    return re.split(r'\s+#', value)[0].strip()


def _find_real_vault_path() -> Path:
    """Standalone `.env`/environment read for `ORCH_VAULT_PATH`, deliberately NOT
    done by importing `config` — importing it would freeze `config.py`'s OWN
    internally-derived constants (`POLICY_FILE`, `QUEUE_FILE`, …) against
    whatever VAULT_PATH is current at THAT moment. Those are computed once, in
    config.py's own module body, from a LOCAL variable — overwriting the
    `config.VAULT_PATH` ATTRIBUTE afterward (what an earlier version of this file
    did) does not retroactively recompute them, and broke
    `tests/test_queue_linter_policy.py::test_policy_file_path_has_no_layout_literal_of_its_own`
    (measured 2026-09-16, Runde 2). Mirrors config._load_dotenv()'s own precedence
    (a real env var wins over `.env`) and its fallback default, on purpose, so this
    reads the exact same value config.py would have — just without the side effect
    of importing it before the redirect below is in place. Value normalization
    (quotes, inline comments) is mirrored via `_normalize_dotenv_value` above,
    same reason (P3-2, oc r1).
    """
    override = os.environ.get("ORCH_VAULT_PATH")
    if override:
        return Path(override)
    env_file = Path(__file__).resolve().parent.parent / ".env"
    if env_file.is_file():
        for raw_line in env_file.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            # Key selection mirrors config._load_dotenv() too (partition, then
            # strip the key), so `ORCH_VAULT_PATH = D:\…` is found here exactly
            # when config.py finds it (oc r2 P3-1).
            key, _, value = line.partition("=")
            if key.strip() == "ORCH_VAULT_PATH":
                return Path(_normalize_dotenv_value(value.strip()))
    return Path.home() / "obsidian_vault"  # config.py's own fallback, mirrored


# The value the guard below verifies stays untouched.
_REAL_VAULT_PATH = _find_real_vault_path()

# Exported so memory._refuse_real_vault_writes_under_pytest() has an independent
# reference to compare against — memory.py's OWN constants are derived from
# config.VAULT_PATH, which IS the redirected value by the time memory.py runs, so
# memory.py comparing against its own derived state would be a tautology. This is
# the pre-redirect value, read nowhere else.
os.environ["_ORCH_TEST_REAL_VAULT_PATH"] = str(_REAL_VAULT_PATH)

# Root redirect — set BEFORE the first `import config` anywhere in this process
# (this file included), so config.py's OWN module-level derived constants
# (`POLICY_FILE`, `QUEUE_FILE`, …) compute correctly from the redirected value
# from the start, rather than needing to be patched retroactively one by one —
# which is the exact enumeration problem this fix replaces, just one layer up.
#
# Kill switch: `_ORCH_TEST_DISABLE_MEMORY_DOCS_ISOLATION=1` skips this, leaving
# `ORCH_VAULT_PATH` (and therefore `config.VAULT_PATH`) exactly as the real
# environment/`.env` resolves it — or as a test-provided stand-in, for `tests/
# test_no_real_writes_guard.py`'s subprocess mutation proof, which needs the
# redirect OFF to simulate "isolation bypassed" for the memory-path half, not
# just the chdir half.
if not os.environ.get("_ORCH_TEST_DISABLE_MEMORY_DOCS_ISOLATION"):
    _TEST_VAULT_ROOT = Path(tempfile.mkdtemp(prefix="orch_test_vault_"))
    os.environ["ORCH_VAULT_PATH"] = str(_TEST_VAULT_ROOT)

import replay  # noqa: E402 — must follow sys.path tweak


@pytest.fixture(scope="session")
def real_vault_path() -> Path:
    """The REAL vault path (or the mutation-proof's stand-in under the kill
    switch) — for tests that need to simulate "isolation bypassed" and must
    therefore reference the pre-redirect value, not `config.VAULT_PATH` (which
    is the redirected one from the moment this file finishes loading)."""
    return _REAL_VAULT_PATH


def _snapshot_dir_files(d: Path) -> frozenset[str] | None:
    """Relative file paths under ``d``, or ``None`` if ``d`` doesn't exist.

    A SET of names rather than a count (P2-2, oc r1): a bare integer tells you
    something drifted but not what, so every failure message used to read the
    same regardless of cause. A set lets the caller name exactly which files
    are new or missing.
    """
    if not d.is_dir():
        return None
    return frozenset(str(p.relative_to(d)) for p in d.rglob("*") if p.is_file())


def _hash_file(p: Path) -> str | None:
    if not p.is_file():
        return None
    import hashlib
    return hashlib.sha256(p.read_bytes()).hexdigest()


def _mtime_of(path: Path) -> str:
    try:
        return f"{path.stat().st_mtime:.3f}"
    except OSError:
        return "?"


# Shared by both drift-message builders below — the P2-2 finding is that the
# ORIGINAL message asserted a single cause ("a test wrote/moved/deleted
# outside the redirected scratch path") when a second, entirely innocent cause
# produces the identical symptom on this machine: the live orchestrator
# (`run_orchestrator.ps1 --watch`) runs continuously and finishes tasks on its
# own schedule — a completion landing inside the ~2-minute test window moves or
# creates a real file the SAME WAY a leaking test would (see this file's module
# docstring, measured 2026-09-16 14:19:33). The guard stays hard (it still
# fails the run either way — "Fehlalarm billiger als Blindheit"); only the
# diagnosis in the message is corrected to name both candidates and tell them
# apart by re-running with the live orchestrator idle.
_DUAL_CAUSE_HINT = (
    "Two known causes produce this: (1) a test that wrote past the "
    "redirected scratch path, or (2) a parallel live orchestrator "
    "(`run_orchestrator.ps1 --watch`) that completed a task during this "
    "test session's ~2-minute window — its writes land in the SAME real "
    "vault this guard watches (see this file's module docstring for a "
    "measured 2026-09-16 14:19:33 collision). Re-run the suite with the "
    "live orchestrator idle to tell the two apart."
)


def _describe_dir_drift(
    path: Path, before: frozenset[str] | None, after: frozenset[str] | None
) -> str | None:
    """``None`` if unchanged, else a message naming every new/missing file
    under ``path`` (each with its current mtime) plus the dual-cause hint."""
    if before == after:
        return None
    added = sorted((after or frozenset()) - (before or frozenset()))
    removed = sorted((before or frozenset()) - (after or frozenset()))

    def _list(names: list[str]) -> str:
        if not names:
            return "    (none)"
        lines = []
        for name in names:
            p = path / name
            lines.append(f"    {name} (mtime={_mtime_of(p)})" if p.is_file() else f"    {name} (gone)")
        return "\n".join(lines)

    return (
        f"REAL {path} changed during the test session "
        f"({len(before or ())} -> {len(after or ())} files).\n"
        f"  new:\n{_list(added)}\n"
        f"  missing:\n{_list(removed)}\n"
        f"{_DUAL_CAUSE_HINT}"
    )


def _describe_hash_drift(
    path: Path, before: str | None, after: str | None
) -> str | None:
    """``None`` if unchanged, else a message naming the hash change and the
    file's current mtime, plus the dual-cause hint. Catches content pruned
    in place (``_cleanup_lessons()`` etc.) that a file-count check cannot see."""
    if before == after:
        return None
    return (
        f"REAL {path} content changed during the test session "
        f"(hash {before} -> {after}, mtime={_mtime_of(path)}) — Hash geändert.\n"
        f"{_DUAL_CAUSE_HINT}"
    )


@pytest.fixture(scope="session", autouse=True)
def _guard_real_vault_and_docs_untouched():
    """Fail the whole test session if the REAL vault memory or this repo's own
    ``docs/`` changed between session start and session end.

    Session-scoped so its teardown runs once, after every test — a mismatch
    surfaces as a session teardown error regardless of which test caused it,
    which is the point: this is a safety net for writes the root redirect
    (module-level, top of this file) and `memory._refuse_real_vault_writes_
    under_pytest()` were supposed to prevent but did not, not a duplicate of
    either mechanism's own job.

    Checks TWO different things for a reason. File SETS (not mere counts —
    see `_snapshot_dir_files`) for `task_results/`, `archive/` and `daily/`
    catch a create/move/delete and name exactly which file. But
    `archive_old_memories()` (`orchestrator.py:2025`) also PRUNES CONTENT from
    two files it never replaces wholesale (`lessons.md` via
    `_cleanup_lessons()`, `memory.py:1258`; the curated `MEMORY.md`) — a
    count/set-only guard cannot see a file that keeps existing but loses
    lines. Hashing catches that: measured 2026-09-16, this is exactly the gap
    the Runde-1 guard had, found by code review, not by a red test
    (`_LESSONS_FILE` was never redirected by the Runde-1 fixture at all).
    ``tests/test_no_real_writes_guard.py`` proves this guard trips by disabling
    the root redirect and pointing the constants at the real vault directly.

    This check stays HARD on purpose (P2-2, oc r1): it is a rare but real
    false positive when a live orchestrator (`run_orchestrator.ps1 --watch`)
    finishes a task during the ~2-minute test window — see `_DUAL_CAUSE_HINT`
    above for how the message now names that second cause instead of only
    blaming "a test". Softening the check (ignoring drift instead of naming
    its likely sources) was rejected: a false alarm here is cheaper than
    silently missing a real leak.
    """
    real_root = _REAL_VAULT_PATH / "99_System" / "AI" / "memory"
    real_task_results = real_root / "task_results"
    real_archive = real_root / "archive"
    real_daily = real_root / "daily"
    real_lessons = real_root / "lessons.md"
    real_curated = real_root / "MEMORY.md"
    repo_docs = Path(__file__).resolve().parent.parent / "docs"

    before = {
        "task_results": _snapshot_dir_files(real_task_results),
        "archive": _snapshot_dir_files(real_archive),
        "daily": _snapshot_dir_files(real_daily),
        "docs": _snapshot_dir_files(repo_docs),
        "lessons_hash": _hash_file(real_lessons),
        "curated_hash": _hash_file(real_curated),
    }
    yield
    after = {
        "task_results": _snapshot_dir_files(real_task_results),
        "archive": _snapshot_dir_files(real_archive),
        "daily": _snapshot_dir_files(real_daily),
        "docs": _snapshot_dir_files(repo_docs),
        "lessons_hash": _hash_file(real_lessons),
        "curated_hash": _hash_file(real_curated),
    }

    for key, path in (
        ("task_results", real_task_results),
        ("archive", real_archive),
        ("daily", real_daily),
        ("docs", repo_docs),
    ):
        msg = _describe_dir_drift(path, before[key], after[key])
        assert msg is None, msg
    for key, path in (("lessons_hash", real_lessons), ("curated_hash", real_curated)):
        msg = _describe_hash_drift(path, before[key], after[key])
        assert msg is None, msg


@pytest.fixture(autouse=True)
def _isolate_replay_store(tmp_path: Path):
    """Redirect the replay JSONL store + archive into pytest's tmp_path.

    Restores the previous paths after each test so production defaults are
    not mutated globally. Falls back to a no-op when ``replay`` lacks the
    expected hooks (e.g. partial import during collection-time errors).
    """
    saved_store = getattr(replay, "_store_path", None)
    saved_archive = getattr(replay, "_archive_dir", None)
    setter = getattr(replay, "set_store_path", None)
    resetter = getattr(replay, "reset_for_tests", None)
    if setter is None:
        yield
        return
    setter(tmp_path / "runs.jsonl", tmp_path / "runs-archive")
    try:
        yield
    finally:
        if resetter is not None:
            try:
                resetter()
            except Exception:  # noqa: BLE001 — teardown must never fail tests
                pass
        if saved_store is not None and setter is not None:
            setter(saved_store, saved_archive)


@pytest.fixture(autouse=True)
def _isolate_gemini_api_key(monkeypatch):
    """Default GEMINI_API_KEY to empty so the suite is hermetic against a real
    key in the developer's .env.

    With a key present, the Gemini provider switches to HTTP-API mode
    (always-available, cclimits refresh skipped) — which would otherwise flip
    the CLI-mode / limits-governed assumptions baked into unrelated dispatcher,
    limits and provider-permission tests. Tests that exercise HTTP mode set the
    key explicitly (see test_providers_gemini.py)."""
    monkeypatch.setattr("config.GEMINI_API_KEY", "", raising=False)
    yield


@pytest.fixture(autouse=True)
def _isolate_openrouter_api_key(monkeypatch):
    """Default OPENROUTER_API_KEY to empty so the suite is hermetic against a
    real key in the developer's .env.

    Added alongside limits.py's opencode budget override (2026-09-04):
    limits._get_limits_fresh() now calls openrouter_budget.fetch_budget()
    unconditionally on every refresh, which makes a real GET /api/v1/key HTTP
    call whenever config.OPENROUTER_API_KEY is truthy. Dozens of existing
    tests in tests/test_limits.py call limits._get_limits_fresh()/get_limits()
    directly without mocking that call — without this fixture they would fire
    real network requests using the developer's real key on every run (this
    repo's .env has one configured, confirmed 2026-09-04). Same problem, same
    fix shape as _isolate_gemini_api_key above; test_heartbeat_model_check.py
    already carries a narrower, file-local version of this exact guard for the
    same underlying reason (there: dispatcher._llm_check_for_newer_models
    trying OpenRouter first when configured).

    Tests that need a real-shaped key set it explicitly (see
    tests/test_providers_openrouter.py's `provider` fixture, tests/
    test_openrouter_budget.py's `_configured_key` fixture) — those run after
    this one and simply override the value for their own test.
    """
    monkeypatch.setattr("config.OPENROUTER_API_KEY", "", raising=False)
    yield


@pytest.fixture(autouse=True)
def _isolate_active_runs_dir(tmp_path: Path, monkeypatch):
    """Redirect ActiveRunRegistry writes into pytest's tmp_path.

    Tool-tests construct ToolTracer instances which mirror lifecycle events
    into a central ``logs/active_runs/`` directory. Without isolation those
    files leak between tests and pollute the production repo.
    """
    try:
        from tools import base_tool
    except ImportError:
        yield
        return
    monkeypatch.setattr(base_tool, "ACTIVE_RUNS_DIR", tmp_path / "active_runs")
    yield


@pytest.fixture(scope="session", autouse=True)
def _isolate_memory_and_docs_output(tmp_path_factory):
    """Redirect the process cwd for the ENTIRE session, so tools that default an
    unset ``cwd`` to ``Path(".")``/``Path.cwd()`` never land in this repo's own
    ``docs/``.

    Measured 2026-09-16 (Betriebsprüfung finding #2): `tools/critical_review.py`,
    `tools/deep_security_audit.py`, `tools/brainstorm.py` and `tools/security_audit.py`
    each compute `cwd_path = Path(cwd) if cwd else Path(".")` (`Path.cwd()` for
    brainstorm) and write into `{cwd_path}/docs`. There is no module constant to
    patch — the path is a per-call default. Exactly two tests currently omit `cwd=`
    (`test_critical_review.py::test_no_cwd_defaults_to_dot`, `test_deep_security_audit.
    py::test_no_cwd_defaults_to_dot`), and every pytest invocation that reaches them
    adds new timestamped files nothing ever deletes — 4491 accumulated in the live
    repo's `docs/` as of 2026-09-16, reproduced live in THIS worktree's own `docs/`
    (5 → 14 files) by one `-p no:randomly` full run. Redirecting the process cwd
    fixes this generically for those two tests AND any future tool/test with the
    same `Path(".")` fallback, rather than special-casing two test names.

    **The `memory.py` half of this fixture (patching its five path constants by
    hand) was removed 2026-09-16 (Runde 2, code review)** in favour of the module-
    level root redirect above (`config.VAULT_PATH` overwritten before `memory` is
    ever imported): the hand-enumerated list had already missed `_LESSONS_FILE`
    (`memory.py:906`), whose own writer, `_cleanup_lessons()` (`memory.py:1258`),
    never calls `_ensure_dirs()` and therefore bypassed every guard silently.
    Measured proof this was live: 2026-09-16 14:19:33, live orchestrator idle,
    `archive_old_memories()` (`orchestrator.py:2025`, runs from every real
    `run_once()`) moved a real file from the vault's `task_results/` to `archive/`
    during a test run. Redirecting `VAULT_PATH` itself closes the class of bug
    (an unlisted derived constant) rather than one instance of it, and needs no
    per-test teardown — it is set once, for the whole process, before `memory.py`
    is ever imported.

    `memory._refuse_real_vault_writes_under_pytest()` remains as a second,
    independent layer, now called at every write/move/delete operation `memory.py`
    performs (not only inside `_ensure_dirs()`) — see that function's own
    docstring. It is the belt to this fixture's suspenders, not the other way
    round: the root redirect above is what actually prevents the write in the
    overwhelming majority of cases.

    Kill switch: ``_ORCH_TEST_DISABLE_MEMORY_DOCS_ISOLATION=1`` skips the chdir below
    and is checked nowhere else in this codebase — its only purpose is to let
    ``tests/test_no_real_writes_guard.py`` spawn a pytest SUBPROCESS with this fixture
    deliberately inert and assert that `_guard_real_vault_and_docs_untouched` then fails
    the run, proving the guard is load-bearing rather than merely present (see that
    file). Never set in normal use.
    """
    if os.environ.get("_ORCH_TEST_DISABLE_MEMORY_DOCS_ISOLATION"):
        yield
        return

    scratch = tmp_path_factory.mktemp("memory_docs_isolation")
    original_cwd = os.getcwd()
    os.chdir(scratch)
    try:
        yield
    finally:
        os.chdir(original_cwd)


@pytest.fixture(autouse=True)
def _isolate_policy_engine(tmp_path: Path, monkeypatch):
    """Point the PolicyEngine singleton at an empty vault so the suite is hermetic
    against the developer's real ``99_System/AI/policy.yaml``.

    Without this the live file decides test outcomes: since provider lookups are
    filtered through ``tool_providers`` (dispatcher.policy_allows_provider and the
    forced-tag gate), a machine whose policy.yaml bars gemini/openrouter/vibe gets
    different routing results than a machine without a policy file at all — and the
    failure looks like a routing bug, not a fixture leak.

    Tests that need a policy build their own engine and monkeypatch
    ``policy._engine`` themselves; that assignment simply wins over this one.
    """
    try:
        import policy as policy_module
    except ImportError:
        yield
        return
    # A real but empty policy.yaml is written rather than left absent. The engine
    # itself treats the two identically (no rules, no tool_providers either way),
    # but queue_linter._policy_status() does not, and must not: "policy.yaml is
    # missing" is a finding it exists to report. An empty mapping is the hermetic
    # stand-in for "a policy file is present and imposes nothing" — the state the
    # rest of the suite assumes. Tests that exercise the missing/corrupt cases
    # point the linter at their own path.
    vault = tmp_path / "_empty_vault"
    (vault / "99_System" / "AI").mkdir(parents=True, exist_ok=True)
    (vault / "99_System" / "AI" / "policy.yaml").write_text("{}\n", encoding="utf-8")
    monkeypatch.setattr(
        policy_module,
        "_engine",
        policy_module.PolicyEngine(vault_path=vault),
    )
    yield


@pytest.fixture
def with_opencode():
    """Register opencode in ``dispatcher._providers`` regardless of local CLI/config
    presence, and hand the instance back.

    NOT autouse: it exists so a test that reasons about opencode says so out loud.
    Registration is a real precondition, not a detail — ``resolve_forced_provider()``
    returns None for an unregistered name, so ``forced_provider_policy_violation()``
    reports nothing and a policy test asserting a finding turns into a test that
    passes only on machines with the opencode CLI installed (measured 2026-09-09:
    both queue-linter policy tests went green here and red with the provider popped).
    Shared by tests/test_dispatcher_routing.py and tests/test_queue_linter_policy.py.
    """
    import dispatcher
    from providers.opencode import OpencodeProvider

    had_it = "opencode" in dispatcher._providers
    if not had_it:
        dispatcher._providers["opencode"] = OpencodeProvider()
    yield dispatcher._providers["opencode"]
    if not had_it:
        dispatcher._providers.pop("opencode", None)


@pytest.fixture
def without_opencode():
    """The mirror image: guarantee opencode is absent from the registry."""
    import dispatcher

    saved = dispatcher._providers.pop("opencode", None)
    yield
    if saved is not None:
        dispatcher._providers["opencode"] = saved


@pytest.fixture
def with_vibe():
    """Register vibe in ``dispatcher._providers`` regardless of local CLI presence,
    and hand the instance back.

    Same contract and same reason as ``with_opencode`` above: vibe is registered
    conditionally (``dispatcher.py``: ``if VibeProvider.is_available()``), so any
    test that reasons about vibe routing is machine-dependent without this. It
    lives here rather than in one test file because three files need it —
    test_dispatcher_routing.py, test_provider_list_derivation.py and
    test_queue_linter_policy.py each grew their own copy or, worse, went without
    (measured 2026-09-09: ``_selection_order("do X #vibe", ...)`` yields
    ``['vibe', 'claude', 'codex']`` here and ``['claude', 'codex']`` on a box
    without the Mistral binary, so the assertion pinning the forced-tag gap was
    green only on this machine).

    The constructor runs without the CLI present — nothing is invoked on it by
    the routing tests.
    """
    import dispatcher
    from providers.vibe import VibeProvider

    had_it = "vibe" in dispatcher._providers
    if not had_it:
        dispatcher._providers["vibe"] = VibeProvider()
    yield dispatcher._providers["vibe"]
    if not had_it:
        dispatcher._providers.pop("vibe", None)


@pytest.fixture
def without_vibe():
    """The mirror image: guarantee vibe is absent from the registry."""
    import dispatcher

    saved = dispatcher._providers.pop("vibe", None)
    yield
    if saved is not None:
        dispatcher._providers["vibe"] = saved

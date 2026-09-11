"""Commit a successful run's changes onto their own branch, HEAD never moves.

Fixes the gap named in ``.dev-loop/auftrag.md``: an unattended night run leaves its
work uncommitted in the tree, so the SECOND ``#tool:dev-loop`` run in the same repo
dies terminal on ``worktree_dirty`` (measured 2026-09-03/04, nightstash -> nightfloor,
see CLAUDE.md). ``commit_run_result()`` is called after a successful task run and
leaves exactly the paths that run touched -- per the before/after ``_snapshot_dir``
comparison -- committed on ``refs/heads/<GIT_COMMIT_BRANCH_PREFIX><slug>-<date>``,
then restores those same paths in the working tree to HEAD's content. Foreign changes
elsewhere in the tree are never touched (see the hard rule below).

Architecture decision (settled, see spec-git-commit.md): plumbing, not a checkout
dance. The commit is built via a temporary index (``GIT_INDEX_FILE``) +
``commit-tree`` + ``update-ref`` -- HEAD is never moved and no branch is ever checked
out. A ``git checkout -b`` / ``git checkout <orig>`` pair would be shorter but leaves
HEAD on a foreign branch if the process dies between the two commands -- exactly the
measured ``nightstash`` failure this feature exists to fix. On the plumbing path the
worst crash state is "branch exists, tree still dirty", i.e. today's status quo, never
worse. Mirrors ``orchestrator._git_snapshot`` / ``_prune_snapshot_refs``
(``orchestrator.py:627-830``), the binding precedent for this module: git via bare
``subprocess``, no library, an own ref/branch namespace, "never raises", a collision
suffix, logging to both ``print`` and ``logger`` (``run_orchestrator.ps1`` starts
``--watch`` without stdout redirection, so ``print`` alone is lost unattended).

Hard rule added after an empirical correction mid-build (``git checkout HEAD --
<path>`` on a path whose INDEX already differs from HEAD discards that staged content
from both the index and the working tree -- verified: a file with a user's staged
edit, then modified again on disk, ends up back at the staged version with the disk
edit gone and NOT in our commit either): a path is only ever added to the commit
(and only ever touched during working-tree cleanup) when its status-porcelain index
column (X, the first of the two status characters) is ``' '`` (clean vs HEAD) or
``'?'`` (untracked). Any other X (``M``/``A``/``D``/``R``/``C``/``U``) means something
already staged a change to that path independently of this run's own edits -- that
path is skipped entirely, logged at WARNING, and left exactly as it was.

That rule covers the STAGED half and only that half -- it asks whether something is
staged, never who wrote it, and nobody stages mid-typing. The unstaged half is closed
by a second, independent rule: ``dirty_before`` (see ``dirty_paths_snapshot``), a
baseline of what was already dirty when the run STARTED. Without it, the module's
whole attribution is a ``(mtime, size)`` diff across the entire task duration, so any
file a human edits during a ``--watch`` run reads as the run's own work and is both
committed and reset. Measured in the adversarial review, not reasoned about. The
earlier version of this docstring claimed the index rule ruled the destructive case
out "structurally"; it ruled out one of its two halves.

Benannte Grenzen (aus der Spec, nicht Zufall -- absichtlich so gelassen):
- Tool-Artefaktverzeichnisse (``.dev-loop/``, ``.research-qa/``, ...) werden
  mitcommittet, wenn das Zielrepo sie nicht selbst gitignoriert. Der Auftrag verlangt
  "genau die Pfade aus dem Snapshot-Vergleich"; ein Extra-Filter dafuer waere eine
  zweite Liste, die von der ersten abdriften kann.
- Leere Verzeichnisse bleiben nach dem Entfernen untracked erzeugter Dateien stehen
  (Schritt 8 raeumt Dateien, keine Verzeichnisse).
- Zeilenenden: das Cleanup laeuft ueber ``git checkout``, also durch git's eigene
  Filter. In einem Repo OHNE ``eol=``-Attribut und mit ``core.autocrlf=true`` (auf
  dieser Maschine system-weit gesetzt) kommt eine Datei, die vor dem Lauf LF-Enden
  hatte, mit CRLF zurueck -- byte-verschieden vom Ausgangszustand, gemessen. Kein
  Defekt und bewusst nicht umgangen: es ist exakt das, was jedes ``git checkout``
  in diesem Repo tut, ``git status`` bleibt danach sauber, und der committete Blob
  traegt ohnehin den normalisierten Inhalt. An den Filtern vorbeizuschreiben waere
  schlimmer als der Effekt. Wer es nicht will, setzt ``* text=auto eol=lf`` in die
  ``.gitattributes`` des Zielrepos -- AI_orchestrator selbst hat das und ist
  deshalb nicht betroffen.
- ``#parallel``/``#worktree`` ist KUER und wird von diesem Modul nicht bedient -- der
  Aufrufer entscheidet, wann ``commit_run_result`` ueberhaupt aufgerufen wird.
- Ein Pfad mit fremd gestagtem Index-Stand (X nicht ``' '``/``'?'``) wird komplett
  uebersprungen -- inklusive des Falls, dass der PROVIDER selbst waehrend des Laufs
  ``git add`` ausgefuehrt hat (erlaubt; nur ``commit``/``push`` sind verboten). Ein
  selbst gestagter Pfad sieht fuer diese Pruefung identisch aus wie ein fremd
  gestagter -- die beiden sind aus dem Index heraus nicht unterscheidbar, und im
  Zweifel gilt "liegen lassen" statt "raten und moeglicherweise loeschen".
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import datetime

import config
import queue_manager

logger = logging.getLogger(__name__)

# Die einzigen Index-Statuszeichen (X, erstes der beiden porcelain-Zeichen), bei denen
# ein Pfad angefasst werden darf: ' ' (Index stimmt mit HEAD ueberein) und '?'
# (untracked). Bewusst eine ERLAUBNIS-, keine Verbotsliste: kaeme in git je ein neues
# Statuszeichen dazu, ueberspringt die Erlaubnisliste es (sicher), waehrend eine Liste
# der verbotenen Zeichen es durchliesse und der Pfad angefasst wuerde. Siehe
# Modul-Docstring fuer den gemessenen Fall, der diese Regel erzwungen hat.
_TOUCHABLE_INDEX_X = frozenset(" ?")

# Praefix jeder Commit-Betreffzeile. Macht orchestrator-erzeugte Commits in
# `git log --oneline` auf einen Blick erkennbar und wird bei der Laengenkappung
# mitgezaehlt (siehe _build_message).
_SUBJECT_PREFIX = "orch: "

_SLUG_INVALID_RE = re.compile(r"[^A-Za-z0-9._-]+")
_SLUG_DASH_COLLAPSE_RE = re.compile(r"-{2,}")


@dataclass(frozen=True)
class CommitOutcome:
    """Result of one ``commit_run_result`` call. ``skipped`` and ``error`` are
    mutually exclusive; a clean success has both at None."""

    branch: str | None = None      # angelegter Branch, sonst None
    sha: str | None = None         # Commit-Sha, sonst None
    files: int = 0                 # Zahl der committeten Pfade
    skipped: str | None = None     # Grund fuer den stillen No-Op
    error: str | None = None       # Grund fuer den lauten Fehlschlag
    # Pfade, die der Lauf geaendert hat, die aber wegen fremden Index-Stands
    # bewusst liegen blieben. Ein Commit kann erfolgreich sein UND unvollstaendig;
    # ohne dieses Feld sieht der Aufrufer nur den gruenen Branch und meldet
    # morgens Vollstaendigkeit, die es nicht gibt. Aus dem externen Review.
    skipped_paths: int = 0
    # Pfade, die im Commit stecken, im Arbeitsbaum aber NICHT zurueckgesetzt wurden,
    # weil sie sich waehrend des Commits veraendert haben. Zweiter Weg zu genau der
    # Konsequenz, fuer die `skipped_paths` existiert: der Baum bleibt schmutzig und
    # der naechste dev-loop stirbt daran, waehrend die Meldung Vollstaendigkeit
    # behauptet. Aus dem adversarialen Review.
    unrestored_paths: int = 0


def _skip(reason: str, cwd: str | None = None) -> CommitOutcome:
    """Silent no-op per Ablauf Schritt 1/3/4: DEBUG only, no WARNING, no Telegram."""
    logger.debug("git_commit: uebersprungen (%s), cwd=%s", reason, cwd)
    return CommitOutcome(skipped=reason)


def _is_git_repo(cwd: str) -> bool:
    """Mirrors orchestrator._is_git_repo. Kept local rather than imported: this is
    a single mechanical check (identical shape to the one below), not the named
    _snapshot_dir comparison baseline the spec requires be imported, not copied --
    duplicating six lines here avoids any lazy-import ceremony for something this
    small. Never raises; a missing git binary or a slow/broken repo reads as "not
    a repo", the conservative direction (silent no-op, not a loud error)."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--is-inside-work-tree"],
            cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=5,
        )
        return result.returncode == 0 and result.stdout.strip() == "true"
    except (OSError, subprocess.TimeoutExpired) as exc:
        # Ein fehlendes git-Binary oder ein Timeout ist KEIN "hier ist kein Repo",
        # sondern ein systemischer Ausfall -- er wuerde das Auto-Committen still
        # fuer jeden Task abschalten. Der Rueckgabewert bleibt False (die
        # konservative Richtung: nichts anfassen), aber er wird nicht mehr
        # verschwiegen. Aus dem externen Review.
        logger.warning(
            "git_commit: git nicht ausfuehrbar in %s (%s) -- Auto-Commit "
            "uebersprungen, das ist kein 'kein Repo'.", cwd, exc,
        )
        return False


def _git_head_sha(cwd: str) -> str | None:
    """HEAD's sha, or None for an empty repo / unborn HEAD (or any subprocess
    failure -- never raises, same reasoning as _is_git_repo)."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--verify", "HEAD"],
            cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    sha = result.stdout.strip()
    return sha or None


def _orchestrator_state_paths() -> set[str]:
    """Absolute paths of the orchestrator's OWN bookkeeping files, lowercased.

    These are never task work, even when they sit inside the task's ``cwd``: the
    queue file, its lock sidecar and the completed-task archive. The queue file is
    the dangerous one. ``finalize_task_with_result`` stamps the line ✅ BEFORE this
    module runs (that ordering is a GUARDRAIL of the Auftrag), so the stamp is part
    of ``snap_after``. Committing it and then resetting the path to HEAD would undo
    the finalization IN THE WORKING TREE while leaving the task reported as done --
    the line reappears as open and the task runs again on the next poll, forever.

    Not reachable today: the vault holding the queue is not a git repository
    (measured 2026-09-10), so ``not_a_repo`` already stops it -- although five
    queue tasks do carry a ``cwd:`` pointing at the vault. A single ``git init``
    there would arm it. Raised by the external review (Codex) as a P1; kept as a
    real guard rather than a comment, because the failure is silent, unattended,
    and self-perpetuating.

    Derived from ``queue_manager`` at call time rather than hand-copied, so a
    renamed queue file cannot leave a stale literal behind. Lowercased because
    Windows paths compare case-insensitively.
    """
    paths: set[str] = set()
    try:
        queue_file = queue_manager.QUEUE_FILE
        # Archivname ABGELEITET, nicht als Literal: ein umbenanntes Erledigt-File
        # wuerde sonst still aus dem Schutz fallen.
        erledigt = getattr(
            queue_manager, "_ERLEDIGT_FILE",
            queue_file.with_name("agent-queue-erledigt.md"),
        )
        for candidate in (
            queue_file,
            queue_file.with_name(f"{queue_file.name}.lock"),
            erledigt,
        ):
            # realpath, nicht nur abspath: ein `subst`-Laufwerk, eine Junction oder
            # ein 8.3-Kurzname zeigen auf dieselbe Datei unter anderem Namen, und
            # ein reiner Textvergleich haette den Schutz genau dort verfehlt, wo er
            # gebraucht wird. Aus dem externen Review (Grok).
            paths.add(os.path.normcase(os.path.realpath(str(candidate))))
    except Exception:  # never let bookkeeping introspection break a commit
        return set()
    return paths


def _repo_prefix(cwd: str) -> str:
    """`cwd` relative to the worktree root, with a trailing '/' (empty at the root).

    Bridges the two path spaces this module joins: `git status --porcelain` reports
    root-relative paths, `_snapshot_dir` reports cwd-relative ones. Never raises; on
    any failure it returns "", which restores the pre-fix behaviour (a silent skip
    for a subdirectory cwd) rather than inventing a prefix.
    """
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-prefix"],
            cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    if result.returncode != 0:
        return ""
    return result.stdout.strip()


def dirty_paths_snapshot(cwd: str) -> frozenset[str] | None:
    """Root-relative paths that are ALREADY dirty, as a baseline taken BEFORE a run.

    The one thing this module cannot work out on its own. Its whole attribution
    ("which paths did the run change?") is a ``(mtime, size)`` comparison across
    the run, and that window is the entire task duration -- minutes to hours for a
    dev-loop, and ``--watch`` runs while the user is at the same machine. Anything
    a human edits in that window looks exactly like the run's own work: it gets
    committed under our message AND reset in the working tree. Measured in the
    adversarial review on a throwaway repo: a file only ever touched by a human,
    never staged, ended up in the branch and back at its HEAD content on disk,
    with ``git status`` reporting a clean tree afterwards.

    The index rule (``_TOUCHABLE_INDEX_X``) does not help here: it asks whether
    something is STAGED, not who wrote it, and nobody stages mid-typing.

    So the caller takes this snapshot before the run starts, and anything already
    dirty then is off limits -- neither committed nor cleaned up. Returns None if
    git could not be asked, which the caller must treat as "no baseline", not as
    "nothing was dirty".

    Costs nothing where it matters: ``requires_clean_worktree`` already guarantees
    an empty result for every ``#tool:dev-loop`` run, so the rule only ever bites
    on a single-shot task in a tree that was already dirty -- which is exactly the
    case it exists for.
    """
    try:
        return frozenset(_git_status_map(cwd))
    except Exception:  # a missing baseline is reported, never raised
        logger.warning("git_commit: Dirty-Baseline fuer %s nicht ermittelbar", cwd)
        return None


def _stat_key(cwd: str, rel: str) -> tuple[float, int] | None:
    """`(mtime, size)` of one cwd-relative `/`-path, or None if it is not there.

    Same shape as an ``orchestrator._snapshot_dir`` value, so a recorded entry and
    a fresh reading compare directly. Used to answer "has this path changed since
    we measured it", which the porcelain status code cannot answer: a second
    session that keeps editing an already-modified file leaves XY at `` M`` while
    the bytes change underneath.
    """
    try:
        st = os.stat(os.path.join(cwd, *rel.split("/")))
    except OSError:
        return None
    return (st.st_mtime, st.st_size)


def _diff_candidates(
    before: dict[str, tuple[float, int]],
    after: dict[str, tuple[float, int]],
) -> list[str]:
    """created | deleted | modified, `/`-normalized, `.git/` entries dropped.

    Same set logic as orchestrator._diff_snapshot (read there for the comparison
    itself); this returns a sorted path list with the two extra filters Ablauf
    Schritt 3 asks for, instead of a formatted summary string.
    """
    created = set(after) - set(before)
    deleted = set(before) - set(after)
    modified = {name for name in set(before) & set(after) if before[name] != after[name]}

    result: set[str] = set()
    for raw in created | deleted | modified:
        norm = raw.replace(os.sep, "/")
        if norm == ".git" or norm.startswith(".git/"):
            continue
        result.add(norm)
    return sorted(result)


def _parse_status_porcelain_z(raw: str) -> dict[str, str]:
    """Parse ``git status --porcelain -z -uall`` output into ``{path: XY}``.

    A rename/copy entry (R/C in X or Y) carries a SECOND NUL-terminated field (the
    origin path) that must be consumed, or every following entry parses one field
    short and the whole result is garbage from that point on. The origin path is
    discarded, not stored: a rename by definition has a non-blank staged X, so it
    is excluded downstream by the foreign-staged-index rule regardless of which of
    its two paths would otherwise have matched a candidate (see module docstring).
    """
    fields = raw.split("\0")
    result: dict[str, str] = {}
    i = 0
    while i < len(fields):
        entry = fields[i]
        i += 1
        if not entry or len(entry) < 3:
            continue
        xy = entry[:2]
        path = entry[3:]
        result[path] = xy
        if "R" in xy or "C" in xy:
            i += 1  # discard the paired origin-path field, see docstring
    return result


def _git_status_map(cwd: str) -> dict[str, str]:
    """`git status --porcelain -z -uall` for the whole repo as {path: XY}.

    No pathspecs on the command line (Windows argv length limit) -- callers
    intersect against their own candidate list instead. Raises RuntimeError on a
    non-zero exit; caught by commit_run_result's outer handler like every other
    plumbing step in Ablauf Schritt 4-8.
    """
    result = subprocess.run(
        ["git", "status", "--porcelain", "-z", "-uall"],
        cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()[:300]
        raise RuntimeError(f"git status failed (rc={result.returncode}): {detail}")
    return _parse_status_porcelain_z(result.stdout)


def _slugify(raw: str) -> str:
    """Sanitize into a valid git ref-name component: replace everything outside
    [A-Za-z0-9._-] with '-', collapse repeats, strip leading/trailing '-'/'.',
    cap at 40 chars. Truncation can re-expose a trailing '-'/'.' (cut mid-run of
    dashes), so the edge strip runs a second time after the cut."""
    slug = _SLUG_INVALID_RE.sub("-", raw)
    slug = _SLUG_DASH_COLLAPSE_RE.sub("-", slug)
    slug = slug.strip("-.")
    slug = slug[:40]
    return slug.strip("-.")


def _task_slug(task: str) -> str:
    """#id:-Wert falls vorhanden, sonst die ersten 8 Hex-Zeichen von
    sha256(strip_metadata_tags(task)). Faellt auf den Hash zurueck, wenn die
    Sanitisierung des Ergebnisses (z.B. eines rein aus Sonderzeichen bestehenden
    #id:) leer wird -- der Hash selbst ist immer schon slug-sauber."""
    clean_task = queue_manager.strip_metadata_tags(task)
    task_hash = hashlib.sha256(clean_task.encode("utf-8")).hexdigest()[:8]
    id_tag = queue_manager.extract_id_tag(task)
    raw = id_tag if id_tag else task_hash
    slug = _slugify(raw)
    return slug if slug else task_hash


def _truncate(text: str, limit: int) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "…"


def _build_message(task: str, provider: str, slug: str) -> str:
    """Deterministic commit message, no model call (KUER per Auftrag)."""
    clean = queue_manager.strip_metadata_tags(task)
    first_line = next((line.strip() for line in clean.splitlines() if line.strip()), "")
    # Das "orch: "-Praefix zaehlt MIT: sonst ist die fertige Betreffzeile um genau
    # dessen Laenge laenger als die Konstante erlaubt, und der Kommentar an der
    # Konstante ("a longer subject just wraps ugly") beschreibt dann den eigenen
    # Code. Aus dem externen Review (opencode).
    subject = _truncate(
        first_line, max(1, config.GIT_COMMIT_SUBJECT_MAX_LEN - len(_SUBJECT_PREFIX))
    )
    body_task = _truncate(clean, 500)
    return (
        f"{_SUBJECT_PREFIX}{subject}\n"
        f"\n"
        f"Task: {body_task}\n"
        f"Provider: {provider}\n"
        f"Task-ID: {slug}\n"
    )


def _run_git(
    args: list[str],
    *,
    cwd: str,
    env: dict[str, str] | None = None,
    input_text: str | None = None,
    timeout: int = 60,
) -> subprocess.CompletedProcess[str]:
    """Fixed kwargs per Ablauf Schritt 6: cwd=cwd, capture_output=True, text=True,
    encoding='utf-8', errors='replace'."""
    return subprocess.run(
        ["git", *args],
        cwd=cwd, env=env, input=input_text,
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=timeout,
    )


def _literal_pathspecs(paths: list[str]) -> list[str]:
    """Prefix each path with git's ``:(literal)`` magic so it is a NAME, not a glob.

    ``--`` only ends option parsing; it does NOT literalise pathspecs -- git still
    reads them as glob patterns. Measured: with a file literally named
    ``config[.]py`` in the tree, ``git add -A -- 'config[.]py'`` also staged the
    FOREIGN ``config.py`` (``M  config.py`` in the index column), whose X column
    this module never checked. The cleanup step would then have reset that foreign
    file to HEAD as well -- uncommitted user work destroyed, and its content pulled
    into our branch. Exactly the data-loss class the index rule exists to exclude,
    reached around the side. With ``:(literal)`` the same call leaves it alone.
    Found by the external review (Codex), then reproduced before fixing.
    """
    return [f":(literal){path}" for path in paths]


def _pathspec_chunks(paths: list[str]) -> list[list[str]]:
    """Split a path list into argv-safe chunks, budgeted by BYTES, not by count.

    A fixed count is the wrong unit: Windows caps a CreateProcess command line at
    32767 characters, and 100 paths near the 260-character MAX_PATH limit come to
    ~26000 before the git executable, the subcommand and the separators are added
    -- close enough that a slightly deeper tree crosses it, and the failure mode is
    an opaque git error, not a clear one. Budgeting bytes makes the bound hold for
    ANY path length; the count cap stays as a second, cheap ceiling. Reported by
    the external review.
    """
    chunks: list[list[str]] = []
    current: list[str] = []
    used = 0
    for path in paths:
        cost = len(path.encode("utf-8")) + 1  # +1 for the argument separator
        if current and (
            used + cost > config.GIT_COMMIT_ARGV_BUDGET_BYTES
            or len(current) >= config.GIT_COMMIT_PATHS_PER_CALL
        ):
            chunks.append(current)
            current, used = [], 0
        current.append(path)
        used += cost
    if current:
        chunks.append(current)
    return chunks


def _require_ok(result: subprocess.CompletedProcess[str], step: str) -> str:
    """Raise a labeled RuntimeError on a non-zero exit, else return stripped stdout.
    Caught by commit_run_result's outer except -- the raise is internal control
    flow, not a promise that this module can throw."""
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()[:500]
        raise RuntimeError(f"{step} failed (rc={result.returncode}): {detail}")
    return result.stdout.strip()


def _child_env(cwd: str, index_path: str) -> dict[str, str]:
    """Copy of os.environ (never mutate the real one -- providers are shared
    singletons across parallel threads, same reasoning as process_runner's
    run_with_watchdog) plus GIT_INDEX_FILE, plus a fallback identity ONLY when
    the target repo has none configured (commit-tree refuses an empty author/
    committer identity, and a fresh clone/CI checkout often has none). An
    already-configured identity is respected, never overridden."""
    env = dict(os.environ)
    env["GIT_INDEX_FILE"] = index_path
    # BEIDE Felder, nicht nur user.email: commit-tree braucht Name UND Mail, und
    # ein Repo mit gesetzter Mail aber fehlendem Namen haette den Fallback nicht
    # ausgeloest und waere an git's eigener Identity-Fehlermeldung gestorben --
    # Task erfolgreich, Commit unterblieben, also genau der Zustand, den dieses
    # Feature beseitigen soll. Aus dem externen Review (opencode).
    def _configured(key: str) -> bool:
        probe = subprocess.run(
            ["git", "config", key],
            cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=10,
        )
        return probe.returncode == 0 and bool(probe.stdout.strip())

    if not (_configured("user.email") and _configured("user.name")):
        env["GIT_AUTHOR_NAME"] = config.GIT_COMMIT_FALLBACK_AUTHOR_NAME
        env["GIT_AUTHOR_EMAIL"] = config.GIT_COMMIT_FALLBACK_AUTHOR_EMAIL
        env["GIT_COMMITTER_NAME"] = config.GIT_COMMIT_FALLBACK_AUTHOR_NAME
        env["GIT_COMMITTER_EMAIL"] = config.GIT_COMMIT_FALLBACK_AUTHOR_EMAIL
    return env


def commit_run_result(
    cwd: str | None,
    task: str,
    provider: str,
    snap_before: dict[str, tuple[float, int]] | None,
    snap_after: dict[str, tuple[float, int]] | None = None,
    dirty_before: frozenset[str] | None = frozenset(),
) -> CommitOutcome:
    """Commit the paths a run touched onto their own branch; HEAD never moves.

    Args:
        cwd: Arbeitsverzeichnis des gelaufenen Tasks.
        task: Roher Task-Text (inkl. Metadata-Tags) -- fuer Slug und Commit-Message.
        provider: Provider-Label des Laufs (z. B. "claude+dev-loop"), unveraendert
            in die Commit-Message uebernommen.
        snap_before: ``_snapshot_dir(cwd)`` von VOR dem Lauf.
        snap_after: ``_snapshot_dir(cwd)`` von NACH dem Lauf. Optional -- wenn nicht
            uebergeben, importiert diese Funktion ``orchestrator._snapshot_dir``
            LAZY und berechnet es selbst (siehe Kommentar an der Importstelle fuer
            die Begruendung). Der bevorzugte Weg ist, dass der Aufrufer in
            orchestrator.py den Wert hereinreicht, den er ohnehin schon hat.

    Returns:
        CommitOutcome. ``skipped`` fuer jede verletzte Vorbedingung (stiller
        No-Op, DEBUG-Log). ``error`` fuer einen Fehlschlag WAEHREND des Commit-
        Vorgangs (WARNING-Log) -- die Funktion wirft dabei nie nach aussen; jede
        Entscheidung, ob ein Commit-Fehlschlag den Task selbst rot macht, trifft
        der Aufrufer in orchestrator.py, nicht dieses Modul.
    """
    # Ablauf Schritt 1 -- Vorbedingungen, jede ein stiller No-Op.
    if not config.GIT_AUTO_COMMIT:
        return _skip("disabled")
    if not cwd or snap_before is None:
        return _skip("no_snapshot")
    # Diese beiden Aufrufe standen frueher VOR dem try und fingen nur OSError/
    # TimeoutExpired ab. Ein eingebettetes NUL-Zeichen im cwd laesst subprocess
    # `ValueError: embedded null character` werfen -- kein OSError, also verliess es
    # das Modul und brach den "never raises"-Vertrag. Das ist woertlich dieselbe
    # Fehlerklasse, die am 2026-09-09/10 96 Prozessabstuerze verursacht hat und
    # derentwegen _snapshot_dir seither ValueError zusaetzlich faengt. Aus dem
    # adversarialen Review, dort gemessen.
    try:
        if not _is_git_repo(cwd):
            return _skip("not_a_repo", cwd)
        head_sha = _git_head_sha(cwd)
        if not head_sha:
            return _skip("no_head", cwd)
    except (KeyboardInterrupt, SystemExit):
        raise
    except BaseException as exc:
        logger.warning("git_commit: Vorbedingungspruefung fehlgeschlagen (%s): %s", cwd, exc)
        return CommitOutcome(error=str(exc)[:500])

    # Ausserhalb des try: eine Exception NACH dem update-ref darf den bereits
    # angelegten Branch nicht aus dem Rueckgabewert verlieren -- sonst meldet der
    # Lauf einen Fehlschlag, waehrend der Branch existiert, und der Morgen sucht
    # etwas, das da ist. Aus dem externen Review.
    made_branch: str | None = None
    made_sha: str | None = None
    made_files = 0

    try:
        # Ablauf Schritt 2.
        if snap_after is None:
            # Lazy import: orchestrator.py imports this module at module level, so
            # a top-level `from orchestrator import _snapshot_dir` here would be a
            # cycle (limits.py's lazy import of dispatcher is the repo's existing
            # pattern for exactly this shape). This branch only runs when the
            # caller did NOT already have snap_after on hand.
            from orchestrator import _snapshot_dir
            snap_after = _snapshot_dir(cwd)

        # Ablauf Schritt 3 -- Kandidatenpfade.
        candidates = _diff_candidates(snap_before, snap_after)
        # `/`-normalisierte Sicht auf snap_after, damit der Cleanup-Guard unten eine
        # frische Messung direkt gegen den aufgezeichneten Stand halten kann.
        after_state = {k.replace(os.sep, "/"): v for k, v in snap_after.items()}
        if not candidates:
            return _skip("no_changes", cwd)
        # Ablauf Schritt 4 -- Git-Sicht abgleichen, EIN Aufruf ohne Pathspecs.
        status = _git_status_map(cwd)
        # `git status --porcelain` liefert Pfade relativ zum WORKTREE-ROOT, waehrend
        # _snapshot_dir sie relativ zum `cwd` des Tasks bildet. Bei einem `cwd:`
        # unterhalb des Repo-Roots war die Schnittmenge deshalb immer leer und das
        # Feature still tot (gemessen: status 'M sub/a.txt' gegen Snapshot-Key
        # 'a.txt'). --show-prefix liefert genau die Differenz, am Repo-Root ist er
        # leer und alles verhaelt sich wie zuvor. Aus dem externen Review.
        prefix = _repo_prefix(cwd)
        visible = {p: status[prefix + p] for p in candidates if prefix + p in status}

        # Die eigenen Buchhaltungsdateien des Orchestrators sind nie Task-Arbeit --
        # siehe _orchestrator_state_paths() fuer den Grund (der ✅-Stempel wird VOR
        # diesem Modul geschrieben und wuerde beim Cleanup zurueckgesetzt).
        state_paths = _orchestrator_state_paths()
        if state_paths:
            dropped = [
                p for p in visible
                if os.path.normcase(os.path.realpath(os.path.join(cwd, *p.split("/"))))
                in state_paths
            ]
            for p in dropped:
                del visible[p]
                logger.warning(
                    "git_commit: Orchestrator-Zustandsdatei nicht committet: %s "
                    "(Queue-/Lock-/Archivdatei -- ein Commit wuerde die "
                    "Finalisierung im Arbeitsbaum zurueckdrehen)", p,
                )
        if not visible:
            return _skip("nothing_git_visible", cwd)

        # Harte Regel (Korrektur): nur Pfade, deren Index-Spalte X mit HEAD
        # uebereinstimmt (' ') oder untracked ist ('?'). Alles andere hat einen
        # fremden (oder vom Provider selbst per `git add` gemachten) Staging-
        # Stand -- anfassen wuerde ihn zerstoeren. Siehe Modul-Docstring.
        # Zusaetzlich zur X-Spalte: ein Rename/Copy in IRGENDEINER der beiden Spalten
        # macht den Pfad unanfassbar. Der Parser verwirft den Ursprungspfad (er
        # braucht ihn nur, um das Feld zu konsumieren), committen wir also nur das
        # Ziel, bliebe im Branch der alte UND der neue Name stehen. Bei X=R faengt
        # die Erlaubnisliste das schon; die zweite Spalte war die Luecke. Aus dem
        # externen Review.
        def _touchable(xy: str) -> bool:
            return xy[0] in _TOUCHABLE_INDEX_X and "R" not in xy and "C" not in xy

        foreign_staged = sorted(p for p, xy in visible.items() if not _touchable(xy))
        committed = sorted(p for p, xy in visible.items() if _touchable(xy))

        if foreign_staged:
            detail = ", ".join(foreign_staged)
            logger.warning(
                "git_commit: %d Pfad(e) mit fremd gestagtem Index uebersprungen "
                "(Index weicht von HEAD ab, bleiben uncommittet im Baum liegen): %s",
                len(foreign_staged), detail,
            )
            print(
                f"  [commit] WARNUNG: {len(foreign_staged)} Pfad(e) mit fremd "
                f"gestagtem Index uebersprungen (bleiben liegen): {detail}"
            )

        # Pfade, die schon VOR dem Lauf schmutzig waren, sind nicht seine Arbeit --
        # weder committen noch aufraeumen. `dirty_before is None` heisst "Baseline
        # nicht ermittelbar" und ist NICHT dasselbe wie "nichts war schmutzig":
        # dann wird gar nichts angefasst (fail-closed), weil die Zuordnung dann
        # unbelegbar ist. Siehe dirty_paths_snapshot() fuer die gemessene Begruendung.
        if dirty_before is None:
            logger.warning(
                "git_commit: keine Dirty-Baseline fuer %s -- kein Commit, weil "
                "eigene und fremde Aenderungen nicht unterscheidbar waeren.", cwd,
            )
            print("  [commit] WARNUNG: keine Dirty-Baseline -- kein Commit")
            return CommitOutcome(skipped="no_dirty_baseline")
        pre_dirty = sorted(p for p in committed if prefix + p in dirty_before)
        if pre_dirty:
            logger.warning(
                "git_commit: %d Pfad(e) waren schon vor dem Lauf geaendert und "
                "bleiben unangetastet (nicht committet, nicht aufgeraeumt): %s",
                len(pre_dirty), ", ".join(pre_dirty),
            )
            print(
                f"  [commit] WARNUNG: {len(pre_dirty)} Pfad(e) waren vor dem Lauf "
                f"schon geaendert, bleiben liegen: {', '.join(pre_dirty)}"
            )
            pre_dirty_set = set(pre_dirty)
            committed = [p for p in committed if p not in pre_dirty_set]
            foreign_staged = foreign_staged + pre_dirty

        if not committed:
            # Hier ist der Grund NICHT harmlos: es gab Kandidaten, sie tragen nur
            # alle fremden Index-Stand. Die Zahl wandert mit, damit der Aufrufer
            # das von einem leeren Diff unterscheiden und melden kann.
            outcome = _skip("nothing_git_visible", cwd)
            return CommitOutcome(
                skipped=outcome.skipped, skipped_paths=len(foreign_staged),
            )

        # Ablauf Schritt 3b (verschoben) -- der Deckel zaehlt jetzt die Pfade, die
        # WIRKLICH committet wuerden, nicht die rohe Snapshot-Differenz.
        #
        # Vorher stand er vor dem git-Sichtbarkeitsfilter und zaehlte damit auch
        # jede gitignorierte Datei, die der Lauf nebenbei angefasst hat. Gemessen in
        # genau dem Repo, fuer das dieses Feature gebaut wurde: 5011 gitignorierte
        # Dateien, davon 496 in 24 h veraendert und 61 in der letzten Stunde
        # (__pycache__, .ruff_cache, .mypy_cache, logs/, .dev-loop/). Ein 3-h-dev-loop,
        # der wie vorgeschrieben pytest + ruff + mypy als letzten Schritt faehrt, reisst
        # 200 damit regelmaessig -- und der Ausfall waere STILL gewesen: Task gruen,
        # Baum schmutzig, und die Ursache erst sichtbar, wenn der naechste dev-loop an
        # worktree_dirty stirbt, also am urspruenglichen Bug. Aus dem adversarialen
        # Review, dort mit einer eigenen Messung belegt (206 Kandidaten, davon 1
        # git-sichtbar). Der Deckel behaelt seinen Zweck -- ein Lauf, der 200 EchtE
        # Dateien anfasst, ist ein entgleistes Build-Artefakt-Commit -- er misst ihn
        # jetzt nur an der richtigen Menge.
        if len(committed) > config.GIT_COMMIT_MAX_FILES:
            logger.warning(
                "git_commit: %d committbare Pfade > GIT_COMMIT_MAX_FILES (%d) -- "
                "uebersprungen (cwd=%s)",
                len(committed), config.GIT_COMMIT_MAX_FILES, cwd,
            )
            print(
                f"  [commit] WARNUNG: {len(committed)} committbare Pfade > "
                f"GIT_COMMIT_MAX_FILES ({config.GIT_COMMIT_MAX_FILES}) -- kein "
                f"Commit (cwd={cwd})"
            )
            return CommitOutcome(skipped="too_many_files")

        # Ablauf Schritt 5 -- Branch-Name.
        slug = _task_slug(task)
        branch_base = (
            f"{config.GIT_COMMIT_BRANCH_PREFIX}{slug}-{datetime.now().strftime('%Y-%m-%d')}"
        )
        message = _build_message(task, provider, slug)

        # Ablauf Schritt 6 -- Commit bauen, ueber einen temporaeren Index.
        fd, index_path = tempfile.mkstemp(prefix="orch-commit-index-")
        os.close(fd)
        try:
            env = _child_env(cwd, index_path)
            # head_sha, NICHT das symbolische "HEAD": read-tree wuerde sonst zur
            # Aufrufzeit aufloesen, waehrend commit-tree unten den weiter oben
            # erfassten head_sha als Elternteil setzt. Bewegt sich HEAD zwischen
            # den beiden Zeilen (zweite Session, der User committet selbst), baute
            # der Commit einen Baum vom NEUEN HEAD mit dem ALTEN als Elternteil --
            # die fremden Aenderungen saehen im Branch aus wie unsere. Ein Sha ist
            # unveraenderlich, damit beschreiben Baum und Elternteil zwingend
            # denselben Ausgangspunkt. Gefunden im externen Review.
            _require_ok(_run_git(["read-tree", head_sha], cwd=cwd, env=env), "read-tree")
            for chunk in _pathspec_chunks(_literal_pathspecs(committed)):
                _require_ok(_run_git(["add", "-A", "--", *chunk], cwd=cwd, env=env), "add")
            tree_sha = _require_ok(_run_git(["write-tree"], cwd=cwd, env=env), "write-tree")
            commit_sha = _require_ok(
                _run_git(
                    ["commit-tree", tree_sha, "-p", head_sha, "-F", "-"],
                    cwd=cwd, env=env, input_text=message,
                ),
                "commit-tree",
            )
        finally:
            try:
                os.remove(index_path)
            except OSError:
                pass

        # Ablauf Schritt 7 -- Branch anlegen, Kollisions-Suffix wie bei den
        # Snapshot-Refs (GIT_SNAPSHOT_REF_MAX_ATTEMPTS-Vorbild).
        branch: str | None = None
        for attempt in range(config.GIT_COMMIT_BRANCH_MAX_ATTEMPTS):
            suffix = "" if attempt == 0 else f"_{attempt + 1}"
            candidate_branch = f"{branch_base}{suffix}"
            # Leerer oldvalue = "darf noch nicht existieren" -- selbe Idee wie bei
            # _git_snapshot's update-ref, hier auf refs/heads/ statt dem eigenen
            # Snapshot-Namensraum.
            result = _run_git(
                ["update-ref", f"refs/heads/{candidate_branch}", commit_sha, ""],
                cwd=cwd, timeout=30,
            )
            if result.returncode == 0:
                branch = candidate_branch
                break
        if branch is None:
            logger.warning(
                "git_commit: Branch-Name %s auch nach %d Versuchen belegt -- kein "
                "Commit, der Arbeitsbaum bleibt unangetastet.",
                branch_base, config.GIT_COMMIT_BRANCH_MAX_ATTEMPTS,
            )
            print(f"  [commit] WARNUNG: Branch {branch_base} belegt, kein Commit angelegt")
            return CommitOutcome(error="branch_collision")
        made_branch, made_sha = branch, commit_sha
        # Vor dem Cleanup-Guard festhalten: `committed` kann unten schrumpfen (Pfade,
        # die jemand waehrend des Commits angefasst hat, werden nicht mehr
        # zurueckgesetzt) -- der COMMIT enthaelt sie trotzdem. `files` beschreibt den
        # Commit, sonst meldet die Morgenmeldung 3 Dateien und `git show` zeigt 5.
        committed_count = len(committed)
        made_files = committed_count

        # Ablauf Schritt 8 -- Arbeitsbaum saeubern, NUR die committeten Pfade,
        # NUR nachdem der Commit sicher im Ref steht.
        #
        # Vorher der Status ein ZWEITES Mal: zwischen dem ersten `git status` und
        # hier liegen mehrere Git-Aufrufe, und in diesem Fenster kann eine zweite
        # Session (oder der User selbst) einen unserer Pfade anfassen. Das Fenster
        # hat zwei Haelften mit sehr unterschiedlichem Gewicht: beim `add` wuerde
        # eine fremde Aenderung nur MITCOMMITTET (falsch zugeordnet, aber im Branch
        # erhalten), beim Cleanup wuerde sie GELOESCHT. Die zweite Haelfte laesst
        # sich billig schliessen -- ein Pfad, dessen Status sich seit der ersten
        # Aufnahme veraendert hat, wird nicht mehr angefasst. Aus dem externen
        # Review (opencode). Das Fenster bleibt fuer die Commit-Haelfte offen; das
        # ist als Grenze benannt, nicht behoben.
        # ZWEI Pruefungen, weil eine allein nicht reicht -- beide aus dem externen
        # Review (opencode fand das Fenster, Grok fand die Luecke in der ersten
        # Fassung dieses Guards):
        #   (a) Status-Code: faengt "jemand hat den Pfad inzwischen GESTAGED".
        #       Ein `checkout` wuerde dessen Index-Stand mitreissen.
        #   (b) (mtime, size): faengt "jemand hat den Pfad WEITER GESCHRIEBEN".
        #       Das ist der Fall, den (a) NICHT sieht -- eine zweite Session, die
        #       eine ohnehin schon geaenderte Datei weiter editiert, laesst XY auf
        #       `` M`` stehen, waehrend sich die Bytes aendern. Die erste Fassung
        #       verglich nur XY und haette genau diesen Inhalt geloescht; der Test
        #       dazu prüfte den Fall, den der Code abdeckt, statt den, den die
        #       Behauptung verspricht.
        # Grenze, geerbt von _snapshot_dir: eine Inhaltsaenderung mit identischer
        # mtime UND identischer Groesse bleibt unsichtbar.
        cleanup_errors: list[str] = []
        unrestored = 0
        try:
            status_now = _git_status_map(cwd)
        except Exception as exc:
            # FAIL-CLOSED: ohne frischen Status ist unbekannt, ob die Pfade noch
            # die sind, die wir gemessen haben -- dann wird gar nichts angefasst.
            # Der Baum bleibt schmutzig (Status quo), der Commit steht trotzdem.
            logger.warning(
                "git_commit: zweite Statusaufnahme fehlgeschlagen (%s) -- Cleanup "
                "wird uebersprungen, der Arbeitsbaum bleibt unveraendert.", exc,
            )
            cleanup_errors.append(f"status_recheck_failed: {exc}")
            status_now = None
            committed = []

        if status_now is not None:
            moved = [
                p for p in committed
                if status_now.get(prefix + p) != visible[p]
                or _stat_key(cwd, p) != after_state.get(p)
            ]
            if moved:
                logger.warning(
                    "git_commit: %d Pfad(e) haben sich waehrend des Commits "
                    "veraendert und werden im Arbeitsbaum NICHT zurueckgesetzt "
                    "(fremder Zugriff im selben Repo): %s",
                    len(moved), ", ".join(moved),
                )
                print(
                    f"  [commit] WARNUNG: {len(moved)} Pfad(e) waehrend des Commits "
                    f"veraendert, bleiben im Baum: {', '.join(moved)}"
                )
                committed = [p for p in committed if p not in set(moved)]
                unrestored = len(moved)
        # visible, NICHT status: `visible` ist bereits auf die cwd-relativen Pfade
        # umgeschluesselt, `status` traegt Root-relative Schluessel. Mit `status[p]`
        # war das bei einem `cwd:` unterhalb des Repo-Roots ein KeyError -- vom
        # "never raises"-Vertrag aufgefangen, aber der Commit blieb aus.
        tracked = sorted(p for p in committed if visible[p] != "??")
        untracked = sorted(p for p in committed if visible[p] == "??")

        for chunk in _pathspec_chunks(_literal_pathspecs(tracked)):
            # head_sha statt "HEAD", aus demselben Grund wie bei read-tree oben:
            # der Baum wird auf genau den Stand zurueckgesetzt, gegen den die
            # Statusklassifikation getroffen wurde.
            try:
                result = _run_git(
                    ["checkout", head_sha, "--", *chunk],
                    cwd=cwd, timeout=config.GIT_COMMIT_CLEANUP_TIMEOUT_SEC,
                )
            except subprocess.TimeoutExpired:
                # Eigener Zweig, weil die Folge eine ANDERE ist als bei einem
                # Fehlercode: ein gekilltes `git checkout` kann `.git/index.lock`
                # zuruecklassen, und dann scheitert JEDER spaetere git-Aufruf in
                # diesem Repo -- unbeaufsichtigt, und der Watchdog hilft nicht,
                # weil die Sperre eine Datei ist und kein Prozess. Die Sperre wird
                # bewusst NICHT automatisch geloescht: sie kann einem fremden,
                # lebenden git gehoeren, und blind loeschen waere genau die Sorte
                # Rateschritt, die dieses Repo sonst vermeidet. Stattdessen laut
                # melden, mit dem Handgriff im Text. Aus dem externen Review (Grok).
                msg = (
                    f"checkout timeout nach {config.GIT_COMMIT_CLEANUP_TIMEOUT_SEC}s "
                    f"-- pruefe .git/index.lock in {cwd} (haengendes git, z. B. "
                    f"OneDrive-Rehydrierung oder Virenscanner)"
                )
                logger.warning("git_commit: %s", msg)
                print(f"  [commit] WARNUNG: {msg}")
                cleanup_errors.append(msg)
                continue
            if result.returncode != 0:
                detail = (result.stderr or result.stdout or "").strip()[:300]
                cleanup_errors.append(detail)
        for rel in untracked:
            abs_path = os.path.join(cwd, *rel.split("/"))
            try:
                os.remove(abs_path)
            except OSError as exc:
                cleanup_errors.append(f"{rel}: {exc}")

        error = None
        if cleanup_errors:
            # Commit + Branch sind trotzdem gueltig -- nur der Baum ist noch
            # schmutzig (Ablauf Schritt 8, letzter Punkt). Branch/Sha werden
            # unten trotzdem zurueckgegeben.
            error = "worktree_cleanup_failed: " + "; ".join(cleanup_errors)[:800]

        # Ablauf Schritt 9 -- Erfolg protokollieren, auf BEIDE Senken (print UND
        # logger -- run_orchestrator.ps1 startet --watch ohne stdout-Umleitung).
        # committed_count, NICHT len(committed): die Liste kann der Cleanup-Guard
        # oben verkleinert haben, der COMMIT enthaelt die Pfade trotzdem.
        print(
            f"  [commit] {branch} ({commit_sha[:8]}, {committed_count} Datei(en))"
            f"  |  Ansehen: git show {branch}"
        )
        logger.info(
            "Commit: %s (%s, %d Datei(en)) -- Ansehen: git show %s",
            branch, commit_sha[:8], committed_count, branch,
        )

        return CommitOutcome(
            branch=branch, sha=commit_sha, files=committed_count,
            skipped_paths=len(foreign_staged), unrestored_paths=unrestored,
            error=error,
        )

    except (KeyboardInterrupt, SystemExit):
        # MUSS vor dem BaseException-Handler stehen -- diese Reihenfolge ist die ganze
        # Sicherung, genau wie in orchestrator.main(). Dahinter wuerde ein Ctrl+C
        # geschluckt und, schwerer, das SystemExit der Shutdown-State-Machine
        # (shutdown.py, ausgeloest durch einen #shutdown-Task): ein geordnetes
        # Herunterfahren waere dann davon abhaengig, ob es zufaellig waehrend eines
        # Commit-Versuchs ausgeloest wurde. Der "never raises"-Vertrag existiert
        # dafuer, dass ein KAPUTTER Commit einen erfolgreichen Task nicht mitreisst --
        # ein absichtlicher Abbruch ist kein kaputter Commit.
        raise
    except BaseException as exc:  # "never raises" contract, see Fehlerregel in Modul-Docstring
        # Wie _prune_snapshot_refs / _charge_process_crash: ein kaputter Commit-
        # Versuch darf einen sonst erfolgreichen Task-Lauf nicht mitreissen. Der
        # Aufrufer in orchestrator.py entscheidet, ob dieses `error` den Task rot
        # macht (Fehlerregel der Spec) -- hier wird nur berichtet, nie geworfen.
        logger.warning("git_commit: Commit fehlgeschlagen (cwd=%s): %s", cwd, exc)
        return CommitOutcome(
            branch=made_branch, sha=made_sha, files=made_files,
            error=str(exc)[:500],
        )

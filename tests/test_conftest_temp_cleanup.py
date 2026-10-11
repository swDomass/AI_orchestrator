"""The conftest removes the scratch dirs it creates in %TEMP% (2026-10-11)."""
import os
import subprocess
import sys
from pathlib import Path

from conftest import _remove_created_temp_roots

_REPO = Path(__file__).resolve().parents[1]
_V = "orch_test_vault_x"
_H = "orch_test_harness_y"


def test_removes_both_prefixed_dirs_with_content(tmp_path):
    v, h = tmp_path / _V, tmp_path / _H
    (v / "sub").mkdir(parents=True)
    (v / "sub" / "f.txt").write_text("x")
    h.mkdir()
    done = _remove_created_temp_roots([v, h], tmp_path, None)
    assert sorted(done) == sorted([v, h])
    assert not v.exists()
    assert not h.exists()


def test_refuses_wrong_prefix(tmp_path):
    d = tmp_path / "other_dir"
    d.mkdir()
    assert _remove_created_temp_roots([d], tmp_path, None) == []
    assert d.exists()


def test_refuses_dir_one_level_deeper(tmp_path):
    d = tmp_path / "nest" / _V
    d.mkdir(parents=True)
    assert _remove_created_temp_roots([d], tmp_path, None) == []
    assert d.exists()


def test_refuses_root_equal_to_real_vault(tmp_path):
    d = tmp_path / _V
    d.mkdir()
    assert _remove_created_temp_roots([d], tmp_path, d) == []
    assert d.exists()


def test_refuses_real_vault_inside_root(tmp_path):
    d = tmp_path / _V
    rv = d / "vault"
    rv.mkdir(parents=True)
    assert _remove_created_temp_roots([d], tmp_path, rv) == []
    assert rv.exists()


def test_refuses_root_inside_real_vault(tmp_path):
    rv = tmp_path / _V
    rv.mkdir()
    assert _remove_created_temp_roots([rv], tmp_path, tmp_path) == []
    assert rv.exists()


def test_child_pytest_leaves_no_scratch_dirs(tmp_path):
    t = tmp_path / "t"
    t.mkdir()
    env = {**os.environ, "TEMP": str(t), "TMP": str(t), "TMPDIR": str(t)}
    target = f"{_REPO / 'tests' / 'test_conftest_temp_cleanup.py'}::test_removes_both_prefixed_dirs_with_content"
    r = subprocess.run(
        [sys.executable, "-m", "pytest", target, "-q", "-p", "no:randomly", "-p", "no:cacheprovider"],
        cwd=_REPO, env=env, timeout=120, capture_output=True, check=False,
    )
    out = r.stdout.decode(errors="replace") + r.stderr.decode(errors="replace")
    assert r.returncode == 0, out
    assert list(t.glob("orch_test_*")) == []

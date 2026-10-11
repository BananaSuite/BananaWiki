"""A release from 1.7 on that lacks its hash-pinned dependency locks is refused, unless the operator allows it (OPS2-09).

Without the locks the installer resolves every dependency anew from PyPI,
newest in range, with no hash check. That is how releases from before 1.7
(a rollback) were always installed, never how a later release missing a lock
by mistake should be: it is refused before anything is installed, and only
``--allow-unpinned`` on ``update`` (never an automatic update), ``restore``,
``rollback`` or ``backups restore`` deploys it.
"""

from __future__ import annotations

import json
import shutil

import pytest
from ops_fakes import FakeSystem, Upstream, git, legacy_install  # type: ignore[import-not-found]

from bananawiki.ops.backups.store import Store
from bananawiki.ops.cli import dispatch, parser
from bananawiki.ops.files import write_json
from bananawiki.ops.manager import Manager, check_dependency_locks, declared_version

LOCKS = {"requirements.lock": "# locked\n", "requirements-build.lock": "# locked\n"}


def version(value: str) -> dict[str, str]:
    return {"bananawiki/__init__.py": f'__version__ = "{value}"\n'}


def without(upstream: Upstream, *names: str) -> None:
    git("rm", "--quiet", "--ignore-unmatch", *names, cwd=upstream.work)


@pytest.fixture
def platform(tmp_path):
    upstream = Upstream(tmp_path)
    system = FakeSystem(tmp_path / "host")
    root = tmp_path / "opt/bananawiki"
    legacy_install(root, system, upstream, upstream.commit("1.7.0", extra={**version("1.7.0"), **LOCKS}))
    return Manager(root, system=system), upstream, system


def test_a_release_from_1_7_on_without_its_locks_is_refused(platform, capsys):
    manager, upstream, system = platform
    installed = manager.settings()["revision"]
    without(upstream, "requirements-build.lock")
    broken = upstream.commit("1.7.1, its build lock forgotten", extra=version("1.7.1"))
    with pytest.raises(ValueError, match="requirements-build.lock.*--allow-unpinned"):
        manager.update()
    assert broken not in system.prepared and manager.settings()["revision"] == installed
    # An automatic update never deploys it, whatever it is asked.
    write_json(manager.config_dir / "updates.json", {"enabled": True, "interval_minutes": 60, "keep_backups": 3,
                                                     "unsigned": True})
    with pytest.raises(ValueError, match="--allow-unpinned"):
        manager.update(automatic=True, retry_failed=True, allow_unpinned=True)
    assert broken not in system.prepared
    result = dispatch(manager, parser().parse_args(["--root", str(manager.root), "update", "--allow-unpinned"]))
    assert result["outcome"] == "complete" and result["revision"] == broken
    assert result["dependencies_unpinned"] is True
    assert "requirements.txt" in capsys.readouterr().err


def test_a_release_with_one_lock_only_is_refused_whatever_its_version(platform):
    manager, upstream, system = platform
    without(upstream, "requirements.lock")
    broken = upstream.commit("a release with half its locks", extra=version("1.6.9"))
    with pytest.raises(ValueError, match="requirements.lock"):
        manager.update()
    assert broken not in system.prepared


def test_a_release_from_before_1_7_installs_from_its_ranges(platform):
    """A rollback to 1.6 (or 1.4, which declared no version) installs the way it was tested."""
    manager, upstream, _system = platform
    without(upstream, *LOCKS)
    older = upstream.commit("1.6.0 again", extra=version("1.6.0"))
    result = manager.update()
    assert result["outcome"] == "complete" and result["revision"] == older and result["dependencies_unpinned"]
    without(upstream, "bananawiki/__init__.py")
    unversioned = upstream.commit("1.4 layout")
    assert manager.update()["revision"] == unversioned


def test_a_remote_snapshot_of_such_a_release_is_restored_only_with_allow_unpinned(platform, tmp_path, monkeypatch,
                                                                                    capsys):
    """``backups restore`` takes the flag too, so a snapshot of a 1.7 release deployed with --allow-unpinned can be
    restored on a new server by the same explicit choice, instead of only after downloading it by hand."""
    manager, upstream, _system = platform
    without(upstream, "requirements-build.lock")
    broken = upstream.commit("1.7.1, its build lock forgotten", extra=version("1.7.1"))
    assert manager.update(allow_unpinned=True)["revision"] == broken
    snapshot = manager.backup()
    # The remote series hands back that package (encryption and Git: test_ops_backups.py).
    monkeypatch.setattr(Store, "download", lambda _store, _identifier, output, **_options: (
        shutil.copyfile(snapshot, output), {"verified": True, "authenticated": True})[1])
    monkeypatch.setattr(Store, "set_schedule", lambda _store, enabled, interval=None: {"enabled": bool(enabled),
                                                                                       "interval_minutes": 1440})
    system = FakeSystem(tmp_path / "new-host")
    new_server = Manager(tmp_path / "new-server", system=system)
    command = ["--root", str(new_server.root), "backups", "restore", "20261010T000000.000000Z-fixture",
               "--trust-source", broken]
    capsys.readouterr()
    with pytest.raises(ValueError, match="requirements-build.lock.*--allow-unpinned"):
        dispatch(new_server, parser().parse_args(command))
    assert broken not in system.prepared and not (new_server.config_dir / "installation.json").exists()
    result = dispatch(new_server, parser().parse_args([*command, "--allow-unpinned"]))
    assert result["outcome"] == "complete" and result["revision"] == broken
    assert new_server.settings()["revision"] == broken and broken in system.prepared
    assert json.loads((new_server.root / "releases" / broken / ".release.json").read_text())["unpinned"] is True
    assert "requirements.txt" in capsys.readouterr().err


@pytest.mark.parametrize("line", ['__version__ = "1.7.1"\n', '__version__: str = "1.7.1"\n',
                                  'from typing import Final\n__version__: Final = "1.7.1"\n'])
def test_an_annotated_version_counts_like_a_plain_one(tmp_path, line):
    """``__version__: str = "1.7.1"`` is a 1.7 release too: read as no version, a release missing its locks
    would install from its ranges like one from before 1.7 (the verifier's OPS2-09 note)."""
    (tmp_path / "bananawiki").mkdir()
    (tmp_path / "bananawiki" / "__init__.py").write_text(line, encoding="utf-8")
    assert declared_version(tmp_path) == (1, 7, 1)
    with pytest.raises(ValueError, match="--allow-unpinned"):
        check_dependency_locks(tmp_path)
    (tmp_path / "bananawiki" / "__init__.py").write_text("__version__: str\n", encoding="utf-8")
    assert declared_version(tmp_path) is None

"""The hash-pinned locks production installs use stay complete and in step with requirements.txt.

A requirement added to requirements.txt without regenerating the locks
(python scripts/lock_requirements.py) would be missing from every managed
server and Docker image: this fails first.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FILES = ("requirements.txt", "requirements.lock", "requirements-build.lock")


def lock_tool():
    spec = importlib.util.spec_from_file_location("lock_requirements", ROOT / "scripts/lock_requirements.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def copy(tmp_path: Path) -> Path:
    for name in FILES:
        (tmp_path / name).write_bytes((ROOT / name).read_bytes())
    return tmp_path


def test_the_locks_pin_every_requirement_by_hash():
    assert lock_tool().check(ROOT) == []


def test_the_locks_are_universal_and_hashed():
    entries = lock_tool().pins(ROOT / "requirements.lock")
    names = {name for name, *_ in entries}
    assert {"flask", "werkzeug", "cryptography", "gunicorn", "jinja2", "markupsafe"} <= names
    assert all(version and hashes for _name, version, _marker, hashes in entries)
    assert {name for name, *_ in lock_tool().pins(ROOT / "requirements-build.lock")} == {"pip", "setuptools"}


def test_a_requirement_added_without_relocking_is_reported(tmp_path):
    tool, root = lock_tool(), copy(tmp_path)
    with (root / "requirements.txt").open("a", encoding="utf-8") as requirements:
        requirements.write("totally-new-package>=1.0\n")
    assert any("totally-new-package" in problem for problem in tool.check(root))


def test_a_pin_outside_its_range_or_without_hashes_is_reported(tmp_path):
    tool, root = lock_tool(), copy(tmp_path)
    lock = root / "requirements.lock"
    text = lock.read_text(encoding="utf-8")
    flask = next(version for name, version, *_ in tool.pins(lock) if name == "flask")
    lock.write_text(text.replace(f"flask=={flask}", "flask==9.0.0"), encoding="utf-8")
    assert any("flask" in problem.lower() and "outside" in problem for problem in tool.check(root))
    (root / "requirements-build.lock").write_text("pip==26.2.1\nsetuptools==84.0.0\n", encoding="utf-8")
    assert any("not pinned to one version with SHA-256 hashes" in problem for problem in tool.check(root))

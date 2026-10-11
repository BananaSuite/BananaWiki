"""Remote backup retention and restores trust only what the series' recovery key authenticated.

Anyone holding the upload token (the threat an append-only series exists
for) can create branches in the backup repository. Snapshot IDs are branch
names, so they say nothing by themselves:

* ``prune`` (and the pruning after an upload) counts only snapshots whose
  index this recovery key authenticated, newest first, and never deletes
  while fewer than ``keep`` of them would remain. IDs dated in the future, or
  that are no dates, are neither counted nor deleted, only reported (OPS2-02).
* No ID, however crafted, keeps uploads from working (OPS2-02).
* ``--allow-unauthenticated`` restores only what can be a snapshot made
  before backups were authenticated: not one dated at or after the series'
  first authenticated snapshot, which every authenticated index records, so
  retention deleting that snapshot changes nothing (OPS2-06).
"""

from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from test_ops_backups import series as series  # noqa: F401 - the local-repository fixture

from bananawiki.ops.backups import crypto
from bananawiki.ops.backups.files import digest, write_json
from bananawiki.ops.backups.git import Git

pytestmark = pytest.mark.skipif(not __import__("shutil").which("age"), reason="needs age")


def stamp(delta: timedelta = timedelta(0)) -> str:
    return (datetime.now(UTC) + delta).strftime("%Y%m%dT%H%M%S.%fZ")


def push_branch(backups, tmp_path: Path, identifier: str, *, garbage: bool = True) -> str:
    """A branch named like a snapshot: garbage (no key needed), or an unauthenticated but well-formed snapshot."""
    config = backups.settings()
    work = tmp_path / ("push-" + identifier.replace(".", "-"))
    work.mkdir(mode=0o700)
    paths = []
    index: dict = {"garbage": True}
    if not garbage:
        context = backups.context(config, identifier)
        package = b"not from this server"
        header = {**context, "schema": 1, "bytes": len(package), "sha256": __import__("hashlib").sha256(package).hexdigest()}
        part = work / "part-00000.age"
        subprocess.run(["age", "--encrypt", "--recipient", config["recipient"], "--output", str(part)],
                       input=crypto.MAGIC + json.dumps(header, sort_keys=True).encode() + b"\n" + package, check=True)
        paths.append(part)
        index = {**context, "schema": 1, "parts": [{"name": part.name, "bytes": part.stat().st_size,
                                                    "sha256": digest(part)}]}
    write_json(work / "index.json", index)
    pusher = Git(work, config, backups.token, allow_local=True)
    pusher.push(pusher.commit([*paths, work / "index.json"]), backups.prefix(config) + identifier)
    return identifier


def append_only(backups, keep: int) -> None:
    path = backups.root / "repository.json"
    write_json(path, {**json.loads(path.read_text()), "append_only": True, "keep": keep})


def upload(backups, tmp_path: Path, count: int) -> list[str]:
    package = tmp_path / "package.tar.gz"
    package.write_bytes(b"genuine package")
    return [backups.upload(package)["snapshot"] for _ in range(count)]


def listed(backups) -> list[str]:
    return [item["snapshot"] for item in backups.list()]


def test_branches_dated_in_the_future_never_make_prune_delete_genuine_snapshots(series):
    backups, tmp_path = series
    append_only(backups, 3)
    genuine = upload(backups, tmp_path, 3)
    bogus = [push_branch(backups, tmp_path, f"99991231T23595{number}.000000Z-0000000{number}") for number in range(3)]
    result = backups.prune()
    assert result["removed"] == []
    assert set(listed(backups)) == {*genuine, *bogus}
    assert any(all(name in warning for name in bogus) for warning in result["warnings"])


def test_unauthenticated_snapshots_do_not_count_toward_keep(series):
    backups, tmp_path = series
    append_only(backups, 2)
    genuine = upload(backups, tmp_path, 3)
    older = push_branch(backups, tmp_path, stamp(-timedelta(days=30)) + "-0dd0dd00", garbage=False)
    newer = [push_branch(backups, tmp_path, stamp(timedelta(minutes=number)) + f"-f0f0f0f{number}")
             for number in (1, 2, 3)]
    result = backups.prune()
    # The two newest authenticated snapshots stay; everything older than them goes; the newer forgeries stay.
    assert sorted(result["removed"]) == sorted([older, genuine[0]])
    assert set(listed(backups)) == {*genuine[1:], *newer}
    assert any(all(name in warning for name in newer) for warning in result["warnings"])


def test_pruning_after_an_upload_counts_only_authenticated_snapshots_too(series):
    backups, tmp_path = series  # keep=2, not append-only: each upload prunes
    genuine = upload(backups, tmp_path, 2)
    forged = [push_branch(backups, tmp_path, stamp(timedelta(minutes=number)) + f"-0000000{number}")
              for number in (1, 2)]
    latest = upload(backups, tmp_path, 1)
    assert set(listed(backups)) == {genuine[1], *latest, *forged}


@pytest.mark.parametrize("identifier", ["99991231T235959.999999Z-ffffffff", "99999999T999999Z-00000000",
                                        "20261301T000000.000000Z-abcdef01"])
def test_no_crafted_snapshot_id_breaks_uploads(series, identifier):
    backups, tmp_path = series
    upload(backups, tmp_path, 1)  # the series exists
    push_branch(backups, tmp_path, identifier)
    uploaded = upload(backups, tmp_path, 1)[0]
    assert uploaded in listed(backups) and uploaded < "9"
    assert backups.download(uploaded, tmp_path / "restored.tar.gz")["authenticated"] is True


def test_an_unauthenticated_snapshot_newer_than_an_authenticated_one_is_never_restored(series):
    backups, tmp_path = series
    upload(backups, tmp_path, 1)
    pre_authentication = push_branch(backups, tmp_path, stamp(-timedelta(days=30)) + "-0dd0dd00", garbage=False)
    forged = push_branch(backups, tmp_path, stamp(timedelta(minutes=1)) + "-feedf00d", garbage=False)
    with pytest.raises(ValueError, match="authenticated since"):
        backups.download(forged, tmp_path / "forged.tar.gz", allow_unauthenticated=True)
    assert not (tmp_path / "forged.tar.gz").exists()
    future = push_branch(backups, tmp_path, "99991231T235959.000000Z-feedf00d", garbage=False)
    with pytest.raises(ValueError, match="dated in the future"):
        backups.download(future, tmp_path / "future.tar.gz", allow_unauthenticated=True)
    # A snapshot older than every authenticated one can be from before authentication: the operator decides.
    result = backups.download(pre_authentication, tmp_path / "old.tar.gz", allow_unauthenticated=True)
    assert result["authenticated"] is False
    # Without the opt-in, the refusal does not claim to know how old the snapshot is.
    with pytest.raises(ValueError, match="--allow-unauthenticated") as error:
        backups.download(pre_authentication, tmp_path / "again.tar.gz")
    assert "it was made before" not in str(error.value)


def _between(first: str, second: str) -> str:
    """A snapshot ID dated between two others."""
    times = [datetime.strptime(name.split("Z-")[0], "%Y%m%dT%H%M%S.%f").replace(tzinfo=UTC) for name in (first, second)]
    middle = times[0] + (times[1] - times[0]) / 2
    assert times[0] < middle < times[1]
    return middle.strftime("%Y%m%dT%H%M%S.%fZ") + "-feedf00d"


def test_an_unauthenticated_snapshot_dated_like_the_first_authenticated_one_is_never_restored(series):
    # Second 1.7 audit (OPS2-06 follow-up): only snapshots dated strictly later than an
    # authenticated one were refused, and a series' IDs only ever grow.
    backups, tmp_path = series
    (first,) = upload(backups, tmp_path, 1)
    same = push_branch(backups, tmp_path, first.split("Z-")[0] + "Z-00000000", garbage=False)
    with pytest.raises(ValueError, match="authenticated since"):
        backups.download(same, tmp_path / "same.tar.gz", allow_unauthenticated=True)
    assert not (tmp_path / "same.tar.gz").exists()


def test_retention_does_not_forget_when_authentication_began(series):
    # The boundary was the oldest authenticated snapshot still in the repository; retention
    # deletes old ones, so a forgery dated after authentication began passed once they were gone.
    backups, tmp_path = series  # keep=2, not append-only: each upload prunes
    genuine = upload(backups, tmp_path, 3)
    assert genuine[0] not in listed(backups)
    forged = push_branch(backups, tmp_path, _between(genuine[0], genuine[1]), garbage=False)
    with pytest.raises(ValueError, match=f"authenticated since {genuine[0]}"):
        backups.download(forged, tmp_path / "forged.tar.gz", allow_unauthenticated=True)
    # Older than the series' first authenticated snapshot, it can be a genuine old one: the operator decides.
    older = push_branch(backups, tmp_path, stamp(-timedelta(days=1)) + "-0dd0dd00", garbage=False)
    assert backups.download(older, tmp_path / "old.tar.gz", allow_unauthenticated=True)["authenticated"] is False

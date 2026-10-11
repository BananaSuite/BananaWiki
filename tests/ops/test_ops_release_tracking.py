"""What automatic updates deploy: release tags by default, and only signed revisions unless the operator opts out.

Branch HEAD is whatever was pushed last, work in progress included; a
release tag (``vX.Y.Z``) is what the maintainers released. Automatic updates
run the fetched code as root, so turning them on requires signature checks
(``--require-signatures``) unless the operator explicitly accepts unsigned
revisions (``--allow-unsigned``).
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
from ops_fakes import FakeSystem, Upstream, git, legacy_install  # type: ignore[import-not-found]

from bananawiki.ops.cli import dispatch, parser
from bananawiki.ops.files import read_json, write_json
from bananawiki.ops.manager import Manager
from bananawiki.ops.source import GitSource

needs_ssh_keygen = pytest.mark.skipif(shutil.which("ssh-keygen") is None, reason="needs ssh-keygen")


@pytest.fixture
def upstream(tmp_path) -> Upstream:
    return Upstream(tmp_path)


def tag(upstream: Upstream, name: str, *options: str) -> None:
    git("tag", *options, name, cwd=upstream.work)
    git("push", "--quiet", str(upstream.bare), f"refs/tags/{name}", cwd=upstream.work)


def tags_source(tmp_path: Path, upstream: Upstream, **extra) -> GitSource:
    root = tmp_path / "root"
    (root / "config").mkdir(parents=True, exist_ok=True)
    return GitSource(root, {"url": str(upstream.bare), "branch": "main", "track": "tags", **extra})


def run(manager: Manager, *argv: str):
    return dispatch(manager, parser().parse_args(["--root", str(manager.root), *argv]))


def test_new_sources_track_release_tags_and_existing_ones_keep_their_branch(tmp_path):
    manager = Manager(tmp_path / "opt/bananawiki", system=FakeSystem(tmp_path / "host"))
    manager.layout()
    assert manager.configure_source()["track"] == "tags"
    assert manager.configure_source(track="branch")["track"] == "branch"
    with pytest.raises(ValueError, match="track"):
        manager.configure_source(track="nightly")
    # A source configured before 1.7 says nothing: it keeps following its branch.
    write_json(manager.config_dir / "source.json", {"url": "https://example.org/r.git", "branch": "main",
                                                    "fallback_branch": None, "auth": "none", "signing": "none"})
    assert manager.source()["track"] == "branch"
    assert "track" not in manager.configure_source(fallback="stable")


def test_tags_follow_the_highest_release_not_the_branch_head(tmp_path, upstream):
    first = upstream.commit("1.6.0")
    tag(upstream, "v1.6.0")
    upstream.commit("1.9.0")
    tag(upstream, "v1.9.0")
    release = upstream.commit("1.10.0")
    tag(upstream, "v1.10.0", "-a", "-m", "BananaWiki 1.10.0")  # annotated: resolved to its commit
    head = upstream.commit("work in progress")
    tag(upstream, "v2.0.0-rc1")
    tag(upstream, "latest")
    tag(upstream, "v02.0.0")  # not a release name either
    source = tags_source(tmp_path, upstream)
    assert source.resolve(first) == (release, "v1.10.0", True)
    # A server installed from a newer checkout (its cache holds that revision) already has the release:
    # nothing to deploy, nothing diverges.
    GitSource(source.root, {**source.source, "track": "branch"}).resolve(first)
    assert source.resolve(head) == (head, "v1.10.0", True)


def test_without_any_release_tag_updates_wait_and_say_how_to_follow_a_branch(tmp_path, upstream):
    first = upstream.commit("untagged")
    with pytest.raises(RuntimeError, match="--track branch"):
        tags_source(tmp_path, upstream).resolve(first)


@needs_ssh_keygen
def test_a_release_tag_signed_for_its_name_is_required_when_signatures_are(tmp_path, upstream):
    key, stranger = tmp_path / "maintainer", tmp_path / "stranger"
    for path in (key, stranger):
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(path)], check=True)
    first = upstream.commit("unsigned 1.0.0")
    tag(upstream, "v1.0.0")
    source = tags_source(tmp_path, upstream, signing="ssh")
    signers = source.config_dir / "repo.allowed_signers"
    signers.write_text("maintainer@example.org " + (tmp_path / "maintainer.pub").read_text())
    signers.chmod(0o600)
    with pytest.raises(RuntimeError, match="not a signed tag"):
        source.resolve(first)
    # A tag signed by an allowed key releases the commit it names, signed or not.
    release = upstream.commit("unsigned commit, signed release")
    git("-c", "gpg.format=ssh", "-c", f"user.signingkey={key}", "tag", "-s", "-m", "1.1.0", "v1.1.0",
        cwd=upstream.work)
    git("push", "--quiet", str(upstream.bare), "refs/tags/v1.1.0", cwd=upstream.work)
    assert source.resolve(first) == (release, "v1.1.0", True) and source.skipped_releases == []
    # The signed tag object only vouches for its own name: pushed as v9.0.0 it releases nothing, and the
    # highest release that verifies is deployed instead.
    git("push", "--quiet", str(upstream.bare), "refs/tags/v1.1.0:refs/tags/v9.0.0", cwd=upstream.work)
    assert source.resolve(first) == (release, "v1.1.0", True) and source.skipped_releases == ["v9.0.0"]
    git("push", "--quiet", str(upstream.bare), ":refs/tags/v9.0.0", cwd=upstream.work)
    # Signed by a key that is not an allowed signer.
    upstream.commit("1.2.0")
    git("-c", "gpg.format=ssh", "-c", f"user.signingkey={stranger}", "tag", "-s", "-m", "1.2.0", "v1.2.0",
        cwd=upstream.work)
    git("push", "--quiet", str(upstream.bare), "refs/tags/v1.2.0", cwd=upstream.work)
    assert source.resolve(first) == (release, "v1.1.0", True) and source.skipped_releases == ["v1.2.0"]
    # A commit signed by an allowed key under a tag anyone could push releases nothing either.
    git("-c", "gpg.format=ssh", "-c", f"user.signingkey={key}", "commit", "-S", "--allow-empty", "--quiet",
        "-m", "signed work in progress", cwd=upstream.work)
    tag(upstream, "v1.3.0")
    assert source.resolve(first) == (release, "v1.1.0", True)
    assert source.skipped_releases == ["v1.3.0", "v1.2.0"]


def test_an_update_deploys_the_latest_release(tmp_path, upstream):
    system = FakeSystem(tmp_path / "host")
    root = tmp_path / "opt/bananawiki"
    installed = upstream.commit("1.6.0")
    legacy_install(root, system, upstream, installed)
    manager = Manager(root, system=system)
    manager.configure_source(track="tags")
    release = upstream.commit("1.7.0")
    tag(upstream, "v1.7.0")
    upstream.commit("unreleased work")
    result = manager.update()
    assert result["outcome"] == "complete" and result["revision"] == release and result["release"] == "v1.7.0"
    assert "used_fallback" not in result
    assert manager.update()["outcome"] == "current"


def test_enabling_automatic_updates_requires_signatures_unless_explicitly_waived(tmp_path, upstream, capsys):
    system = FakeSystem(tmp_path / "host")
    root = tmp_path / "opt/bananawiki"
    legacy_install(root, system, upstream, upstream.commit("1.6.0"))
    manager = Manager(root, system=system)
    with pytest.raises(ValueError, match="--require-signatures"):
        run(manager, "updates", "enable")
    assert manager.policy()["enabled"] is False
    assert run(manager, "updates", "enable", "--allow-unsigned")["outcome"] == "enabled"
    assert manager.policy()["enabled"] is True and manager.policy()["unsigned"] is True
    assert "unsigned" in capsys.readouterr().err.lower()
    run(manager, "updates", "disable")
    signers = tmp_path / "allowed_signers"
    signers.write_text("maintainer@example.org ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIExample\n")
    assert run(manager, "updates", "enable", "--require-signatures", str(signers))["outcome"] == "enabled"
    assert manager.source()["signing"] == "ssh" and manager.policy()["unsigned"] is False
    # Turning the checks off later needs the same explicit waiver while automatic updates are on.
    with pytest.raises(ValueError, match="--allow-unsigned"):
        run(manager, "source", "set", "--clear-signatures")
    assert manager.source()["signing"] == "ssh"
    assert run(manager, "source", "set", "--clear-signatures", "--allow-unsigned")["signing"] == "none"
    assert read_json(manager.config_dir / "updates.json")["unsigned"] is True
    with pytest.raises(ValueError, match="--require-signatures"):
        manager.set_updates(True)

"""Signed releases on servers that require signatures: what updates deploy and what restores accept.

* A release is an annotated tag signed for its own name (``git tag -s``). A
  signed commit under a tag anyone could push says nothing about which
  version it is, so it releases nothing.
* A higher tag that does not verify is skipped, never a reason to stop
  updating: anyone who can push tags could otherwise hold back every server's
  security fixes.
* A restore deploys a revision the update source released under such a tag,
  as the update did, also once its release directory was pruned.
"""

from __future__ import annotations

import os
import shutil
import subprocess

import pytest
from ops_fakes import FakeSystem, Upstream, git, legacy_install  # type: ignore[import-not-found]

from bananawiki.ops.files import write_json
from bananawiki.ops.manager import Manager
from bananawiki.ops.source import GitSource

pytestmark = pytest.mark.skipif(shutil.which("ssh-keygen") is None, reason="needs ssh-keygen")


@pytest.fixture
def keys(tmp_path):
    key = tmp_path / "maintainer"
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key)], check=True)
    signers = tmp_path / "allowed_signers"
    signers.write_text("maintainer@example.org " + key.with_suffix(".pub").read_text())
    signers.chmod(0o600)
    return key, signers


@pytest.fixture
def keyring(tmp_path, monkeypatch):
    """A GnuPG keyring the updater consults, holding a key that is not an allowed signer."""
    if shutil.which("gpg") is None:
        pytest.skip("needs gpg")
    home = tmp_path / "gnupg"
    home.mkdir(mode=0o700)
    subprocess.run(["gpg", "--homedir", str(home), "--batch", "--pinentry-mode", "loopback", "--passphrase", "",
                    "--quick-gen-key", "Other <other@example.org>", "ed25519", "sign", "never"],
                   check=True, capture_output=True)
    environment = GitSource.environment
    monkeypatch.setattr(GitSource, "environment", lambda self: {**environment(self), "GNUPGHOME": str(home)})
    yield home
    subprocess.run(["gpgconf", "--homedir", str(home), "--kill", "all"], capture_output=True)


def openpgp(keyring, *args: str, cwd, input: str | None = None) -> str:
    environment = {**os.environ, "GNUPGHOME": str(keyring), "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1"}
    return subprocess.run(args, cwd=cwd, env=environment, input=input, capture_output=True, text=True,
                          check=True).stdout.strip()


def push_tag(upstream: Upstream, name: str) -> None:
    git("push", "--quiet", "--force", str(upstream.bare), f"refs/tags/{name}", cwd=upstream.work)


def lightweight(upstream: Upstream, name: str) -> None:
    git("tag", name, cwd=upstream.work)
    push_tag(upstream, name)


def signed_tag(upstream: Upstream, key, name: str) -> None:
    git("-c", "gpg.format=ssh", "-c", f"user.signingkey={key}", "tag", "-s", "-m", name, name, cwd=upstream.work)
    push_tag(upstream, name)


def server(tmp_path, upstream, signers, revision) -> Manager:
    system = FakeSystem(tmp_path / "host")
    root = tmp_path / "opt/bananawiki"
    legacy_install(root, system, upstream, revision)
    manager = Manager(root, system=system)
    manager.configure_source(track="tags", signers_file=signers)
    return manager


def test_a_signed_commit_under_a_tag_anyone_could_push_releases_nothing(tmp_path, keys):
    key, signers = keys
    upstream = Upstream(tmp_path)
    installed = upstream.commit("1.7.0")
    signed_tag(upstream, key, "v1.7.0")
    manager = server(tmp_path, upstream, signers, installed)
    git("-c", "gpg.format=ssh", "-c", f"user.signingkey={key}", "commit", "-S", "--allow-empty", "--quiet",
        "-m", "signed work in progress, not a release", cwd=upstream.work)
    git("push", "--quiet", "--force", str(upstream.bare), "HEAD:refs/heads/main", cwd=upstream.work)
    lightweight(upstream, "v99.0.0")  # pushed by someone who can push tags but holds no signing key
    result = manager.update()
    assert result["outcome"] == "current" and result["release"] == "v1.7.0"
    assert result["skipped_releases"] == ["v99.0.0"]
    assert manager.settings()["revision"] == installed
    # An annotated but unsigned tag, or a signed tag object pushed under another name, does not either.
    git("tag", "-a", "-f", "-m", "v99.0.0", "v99.0.0", cwd=upstream.work)
    push_tag(upstream, "v99.0.0")
    git("push", "--quiet", str(upstream.bare), "refs/tags/v1.7.0:refs/tags/v98.0.0", cwd=upstream.work)
    result = manager.update()
    assert result["outcome"] == "current" and result["skipped_releases"] == ["v99.0.0", "v98.0.0"]


def test_an_unverifiable_higher_tag_does_not_hold_back_a_signed_release(tmp_path, keys, capsys):
    key, signers = keys
    upstream = Upstream(tmp_path)
    first = upstream.commit("1.7.0")
    manager = server(tmp_path, upstream, signers, first)
    upstream.commit("junk")
    lightweight(upstream, "v999.0.0")
    git("checkout", "--quiet", "-b", "fix", first, cwd=upstream.work)
    fix = upstream.commit("1.7.1 security fix")
    signed_tag(upstream, key, "v1.7.1")
    result = manager.update()
    assert result["outcome"] == "complete" and result["revision"] == fix and result["release"] == "v1.7.1"
    assert result["skipped_releases"] == ["v999.0.0"]
    assert "v999.0.0" in capsys.readouterr().err
    # With nothing that verifies, nothing is deployed and the error says why.
    git("push", "--quiet", str(upstream.bare), ":refs/tags/v1.7.1", cwd=upstream.work)
    with pytest.raises(RuntimeError, match="None of the 1 release tags"):
        manager.update()


def test_a_tag_signed_release_restores_once_its_release_directory_is_gone(tmp_path, keys):
    key, signers = keys
    upstream = Upstream(tmp_path)
    manager = server(tmp_path, upstream, signers, upstream.commit("1.6.0"))
    r170 = upstream.commit("1.7.0")  # unsigned commits: the documented procedure signs the tag
    signed_tag(upstream, key, "v1.7.0")
    assert manager.update()["revision"] == r170
    package = manager.backup()
    r171 = upstream.commit("1.7.1")
    signed_tag(upstream, key, "v1.7.1")
    assert manager.update()["revision"] == r171
    shutil.rmtree(manager.root / "releases" / r170)  # pruned (keep_backups), or a reinstalled server
    shutil.rmtree(manager.root / "repository.git")  # nothing fetched yet either
    result = manager.restore(package)
    assert result["outcome"] == "complete" and manager.settings()["revision"] == r170


def test_a_restore_of_an_unsigned_revision_says_how_to_proceed(tmp_path, keys):
    key, signers = keys
    upstream = Upstream(tmp_path)
    unsigned = upstream.commit("1.6.0")
    manager = server(tmp_path, upstream, signers, unsigned)
    package = manager.backup()
    newer = upstream.commit("1.7.0")
    signed_tag(upstream, key, "v1.7.0")
    assert manager.update()["revision"] == newer
    shutil.rmtree(manager.root / "releases" / unsigned)
    for trust in (None, unsigned):
        with pytest.raises(ValueError) as error:
            manager.restore(package, trust_source=trust)
        message = str(error.value)
        assert unsigned in message and "--clear-signatures" in message and "--trust-source" in message
        assert "--require-signatures" in message and "Nothing was changed" in message
    assert manager.settings()["revision"] == newer


def test_a_restore_keeps_the_servers_update_source_and_signature_trust(tmp_path, keys):
    """A package's own source settings never replace the ones the trust decision was made with."""
    key, signers = keys
    upstream = Upstream(tmp_path)
    installed = upstream.commit("1.7.0")
    signed_tag(upstream, key, "v1.7.0")
    manager = server(tmp_path, upstream, signers, installed)
    trusted = (manager.config_dir / "source.json").read_bytes()
    allowed = (manager.config_dir / "repo.allowed_signers").read_bytes()
    # A package (made elsewhere, or tampered with) whose revision this server trusts, carrying other settings.
    write_json(manager.config_dir / "source.json", {"url": "https://attacker.example/wiki.git", "branch": "main",
                                                    "fallback_branch": None, "auth": "none", "signing": "none",
                                                    "track": "branch"})
    (manager.config_dir / "repo.allowed_signers").unlink()
    package = manager.backup()
    (manager.config_dir / "source.json").write_bytes(trusted)
    (manager.config_dir / "repo.allowed_signers").write_bytes(allowed)
    result = manager.restore(package)
    assert result["outcome"] == "complete"
    assert (manager.config_dir / "source.json").read_bytes() == trusted
    assert (manager.config_dir / "repo.allowed_signers").read_bytes() == allowed
    assert "attacker.example" in result["source_warning"] and "kept" in result["source_warning"]


def test_an_openpgp_signed_tag_quoting_an_ssh_signature_releases_nothing(tmp_path, keys, keyring):
    """Git verifies the last signature block of a tag, whatever text comes before it."""
    key, signers = keys
    upstream = Upstream(tmp_path)
    installed = upstream.commit("1.7.0")
    signed_tag(upstream, key, "v1.7.0")
    manager = server(tmp_path, upstream, signers, installed)
    upstream.commit("pushed by the holder of a key this machine's GnuPG trusts")
    openpgp(keyring, "git", "-c", "gpg.format=openpgp", "-c", "user.signingkey=other@example.org", "-c",
            "user.name=Other", "-c", "user.email=other@example.org", "tag", "-s", "-m",
            "v9.9.9\n\n-----BEGIN SSH SIGNATURE-----\n(quoted from the release notes)", "v9.9.9", cwd=upstream.work)
    push_tag(upstream, "v9.9.9")
    result = manager.update()
    assert result["outcome"] == "current" and result["release"] == "v1.7.0"
    assert result["skipped_releases"] == ["v9.9.9"]
    assert manager.settings()["revision"] == installed


def test_an_openpgp_signed_commit_with_an_ssh_signature_header_is_refused(tmp_path, keys, keyring):
    _key, signers = keys
    upstream = Upstream(tmp_path)
    installed = upstream.commit("1.7.0")
    manager = server(tmp_path, upstream, signers, installed)
    manager.configure_source(track="branch")
    header = (f"tree {git('rev-parse', 'HEAD^{tree}', cwd=upstream.work)}\nparent {installed}\n"
              "author Other <other@example.org> 1767225600 +0000\ncommitter Other <other@example.org> 1767225600 +0000\n"
              "note -----BEGIN SSH SIGNATURE-----")
    message = "Pushed by the holder of a key this machine's GnuPG trusts\n"
    signature = openpgp(keyring, "gpg", "--batch", "--armor", "--detach-sign", "--local-user", "other@example.org",
                        cwd=upstream.work, input=header + "\n\n" + message)
    signed = header + "\ngpgsig " + signature.replace("\n", "\n ") + "\n\n" + message
    commit = openpgp(keyring, "git", "hash-object", "-t", "commit", "-w", "--literally", "--stdin",
                     cwd=upstream.work, input=signed)
    # Without the format check, this machine's GnuPG would accept it.
    openpgp(keyring, "git", "verify-commit", commit, cwd=upstream.work)
    git("push", "--quiet", "--force", str(upstream.bare), f"{commit}:refs/heads/main", cwd=upstream.work)
    with pytest.raises(RuntimeError, match="OpenPGP"):
        manager.update()
    assert manager.settings()["revision"] == installed

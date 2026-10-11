"""Fetch the configured Git source (public, private, fork) with a hardened Git.

Git never runs hooks, credential helpers, ``ext::`` transports or follows
redirects; credentials never appear in URLs, argv or error messages. The
cache is a bare repository (``repository.git``) that is never checked out:
releases are produced with ``git archive``.

A source follows release tags (``"track": "tags"``, the default for sources
configured by 1.7 and later): the highest ``vX.Y.Z`` tag is deployed, never
work pushed after it. ``"track": "branch"`` (what a source configured
earlier says by saying nothing) follows a branch's HEAD.

When the operator requires signatures, a release is an annotated tag signed
(SSH) by an allowed signer for exactly its own name: a tag name alone is
whatever whoever can push tags says it is, so a signed commit under an
unsigned or lightweight tag releases nothing. The highest release that
verifies is deployed; higher tags that do not are skipped and reported.
A branch-tracking source needs the commit itself signed.
"""

from __future__ import annotations

import os
import re
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .files import atomic_write

_SHA = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?")
RELEASE_TAG = re.compile(r"v(0|[1-9][0-9]{0,8})\.(0|[1-9][0-9]{0,8})\.(0|[1-9][0-9]{0,8})")
TRACKS = ("tags", "branch")
# Release tags tried, highest first, before an update gives up on finding one that verifies.
MAX_RELEASE_CANDIDATES = 20


def valid_track(value: Any) -> str:
    if value not in TRACKS:
        raise ValueError("Choose what updates track: tags (releases) or branch.")
    return value


def release_version(tag: str) -> tuple[int, int, int] | None:
    """``(major, minor, patch)`` of a release tag ``vX.Y.Z``; None for any other tag (pre-releases included)."""
    match = RELEASE_TAG.fullmatch(tag)
    return (int(match[1]), int(match[2]), int(match[3])) if match else None


def valid_branch(value: Any) -> str:
    if (not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_./-]*", value)
            or ".." in value or any(part in {"", ".", ".."} or part.startswith(".") or part.endswith((".lock", "."))
                                    for part in value.split("/"))):
        raise ValueError("Invalid Git branch name.")
    return value


def valid_url(value: Any, *, allow_local: bool = False) -> str:
    if not isinstance(value, str) or not value or any(char.isspace() or ord(char) < 32 for char in value):
        raise ValueError("Invalid Git URL.")
    if allow_local and Path(value).is_absolute():
        return value
    if re.fullmatch(r"[A-Za-z0-9_.-]+@[A-Za-z0-9_.-]+:[A-Za-z0-9_./-]+", value) and not value.startswith("-"):
        return value
    parsed = urlsplit(value)
    if parsed.scheme not in {"https", "ssh"} or not parsed.hostname or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("Use an HTTPS or SSH Git URL. Store credentials separately.")
    if parsed.scheme == "https" and parsed.username:
        raise ValueError("Do not put credentials in the Git URL. Use --token-file instead.")
    return value


def credential_host(url: str) -> str | None:
    if "@" in url and "://" not in url:
        return url.split("@", 1)[-1].split(":", 1)[0]
    return urlsplit(url).hostname


def valid_revision(value: str) -> str:
    if not isinstance(value, str) or not _SHA.fullmatch(value):
        raise ValueError("Invalid deployment revision.")
    return value


def private_file(path: Path) -> None:
    """Credentials must be regular files owned by the operator with mode 0600."""
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"Missing repository credential file {path.name}.")
    info = path.stat()
    if info.st_mode & 0o077 or info.st_uid != os.geteuid():
        raise ValueError("Repository credentials must be owned by the operator and have mode 0600.")


class GitSource:
    """One configured source (``config/source.json``) and the bare cache it fetches into."""

    def __init__(self, root: Path, source: dict[str, Any]):
        self.root, self.source = Path(root), source
        self.git_dir = self.root / "repository.git"
        self.config_dir = self.root / "config"
        # Release tags higher than the one :meth:`resolve` chose that did not verify (the newest first).
        self.skipped_releases: list[str] = []

    def environment(self) -> dict[str, str]:
        values = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"), "LANG": "C.UTF-8", "GIT_TERMINAL_PROMPT": "0",
            "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_LFS_SKIP_SMUDGE": "1",
            "HOME": str(self.config_dir),
        }
        auth = self.source.get("auth", "none")
        if auth == "token":
            credential = self.config_dir / "repo.token"
            private_file(credential)
            helper = self.config_dir / "git-askpass"
            atomic_write(helper, (
                "#!" + sys.executable + "\nimport os, sys\nfrom pathlib import Path\n"
                "prompt = sys.argv[1].lower() if len(sys.argv) > 1 else ''\n"
                "print(os.environ['BANANA_REPO_USER'] if 'username' in prompt "
                "else Path(os.environ['BANANA_REPO_TOKEN_FILE']).read_text().strip())\n"
            ), 0o700)
            values.update(GIT_ASKPASS=str(helper), BANANA_REPO_TOKEN_FILE=str(credential),
                          BANANA_REPO_USER=self.source.get("username", "git"))
        elif auth == "ssh":
            key, known = self.config_dir / "repo.key", self.config_dir / "repo.known_hosts"
            private_file(key)
            private_file(known)
            values["GIT_SSH_COMMAND"] = shlex.join([
                "ssh", "-i", str(key), "-F", "/dev/null", "-o", "BatchMode=yes", "-o", "IdentitiesOnly=yes",
                "-o", "StrictHostKeyChecking=yes", "-o", "UserKnownHostsFile=" + str(known),
                "-o", "ConnectTimeout=20",
            ])
        elif auth != "none":
            raise ValueError("Unknown repository authentication method.")
        return values

    def git(self, *arguments: str, output: Any = None, check: bool = True, timeout: int = 300,
            git_dir: bool = True) -> subprocess.CompletedProcess:
        command = [
            "git", "-c", "core.hooksPath=/dev/null", "-c", "credential.helper=", "-c", "protocol.ext.allow=never",
            "-c", "core.fsmonitor=false", "-c", "http.followRedirects=false", "-c", "http.lowSpeedLimit=1024",
            "-c", "http.lowSpeedTime=30", "-c", "transfer.fsckObjects=true",
        ]
        if git_dir:
            command += ["--git-dir", str(self.git_dir)]
        command += [str(argument) for argument in arguments]
        result = subprocess.run(command, env=self.environment(), stdout=output or subprocess.PIPE,
                                stderr=subprocess.PIPE, text=output is None, timeout=timeout, check=False)
        if check and result.returncode:
            # Git error text can include authentication details: never print or store it.
            raise RuntimeError(f"Git {arguments[0]} failed. Check repository access, credentials and the branch.")
        return result

    def initialize(self) -> None:
        if self.git_dir.is_symlink():
            raise ValueError("The repository cache must not be a symlink.")
        if not self.git_dir.exists():
            self.git_dir.mkdir(mode=0o700, parents=True)
            self.git("init", "--bare", "--quiet", str(self.git_dir), git_dir=False)
        self.git_dir.chmod(0o700)

    def seed(self, checkout: Path) -> str:
        """Import the reviewed, clean checkout ``install`` runs from; return its commit."""
        checkout = Path(checkout).resolve()
        local = ["git", "-c", "safe.directory=" + str(checkout), "-c", "core.hooksPath=/dev/null",
                 "-c", "core.fsmonitor=false", "-C", str(checkout)]
        environment = {**self.environment(), "HOME": str(self.config_dir)}
        status = subprocess.run([*local, "status", "--porcelain"], env=environment, capture_output=True,
                                text=True, check=True, timeout=60)
        if status.stdout:
            raise ValueError("Commit the reviewed source before installing; the checkout contains changes.")
        self.initialize()
        staging = self.root / "staging"
        staging.mkdir(mode=0o700, parents=True, exist_ok=True)
        # A bundle imports the checkout even under sudo without relaxing Git's
        # ownership checks globally (a local fetch would run upload-pack there).
        with tempfile.TemporaryDirectory(prefix="source-", dir=staging) as directory:
            bundle = Path(directory) / "source.bundle"
            subprocess.run([*local, "bundle", "create", str(bundle), "HEAD"], env=environment,
                           capture_output=True, check=True, timeout=300)
            self.git("fetch", "--quiet", "--no-tags", str(bundle), "HEAD")
        return self.head()

    def head(self, peel: str = "") -> str:
        sha = self.git("rev-parse", "FETCH_HEAD" + peel).stdout.strip()
        if not _SHA.fullmatch(sha):
            raise RuntimeError("Git did not return a valid source revision.")
        return sha

    def verify(self, revision: str) -> str | None:
        """Refuse revisions not SSH-signed by an allowed signer (when the operator requires signatures).

        Only SSH signatures are accepted: Git picks its verifier from the
        signature itself, so an OpenPGP signature would be checked against the
        machine's keyring instead of the allowed-signers file.
        """
        if self.source.get("signing", "none") != "ssh":
            return None
        signers = self.config_dir / "repo.allowed_signers"
        private_file(signers)
        header = self.git("cat-file", "commit", revision).stdout.split("\n\n", 1)[0]
        if "BEGIN SSH SIGNATURE" not in header:
            detail = "carries an OpenPGP signature, which is not accepted" if "gpgsig" in header else "is not signed"
            raise RuntimeError(f"Revision {revision[:12]} {detail}. Updates require an SSH signature from a key "
                               "in the allowed signers file. Nothing was deployed.")
        result = self.git("-c", "gpg.format=ssh", "-c", "gpg.ssh.allowedSignersFile=" + str(signers),
                          "verify-commit", revision, check=False)
        if result.returncode:
            raise RuntimeError(f"Revision {revision[:12]} is not signed by a key in the allowed signers file. "
                               "Nothing was deployed. Review the commit, then sign it or add its key.")
        return revision

    def verify_release(self, tag: str, revision: str) -> str | None:
        """Refuse a release unless it is an annotated tag SSH-signed for its own name (when signatures are required).

        The tag object fetched last (``FETCH_HEAD``) must be a tag named
        *tag* for commit *revision*, signed by a key in the allowed signers
        file. A signed commit is not enough: it says nothing about which
        version it is, so anyone who can push tags could release any signed
        work in progress under a higher version number. Likewise a signed tag
        object pushed under another name releases nothing.
        """
        if self.source.get("signing", "none") != "ssh":
            return None
        signers = self.config_dir / "repo.allowed_signers"
        private_file(signers)
        tag_object = self.head()
        if self.git("cat-file", "-t", tag_object).stdout.strip() != "tag":
            raise RuntimeError(f"Release {tag} is not a signed tag: with signatures required, a release is an "
                               "annotated tag signed with an allowed SSH key (git tag -s). Nothing was deployed.")
        body = self.git("cat-file", "tag", tag_object).stdout
        header = body.split("\n\n", 1)[0].splitlines()
        if "tag " + tag not in header or "object " + revision not in header or "type commit" not in header:
            raise RuntimeError(f"Release {tag} is a tag object made for another name or commit, so it releases "
                               "nothing. Nothing was deployed.")
        if "BEGIN SSH SIGNATURE" not in body:
            detail = ("carries an OpenPGP signature, which is not accepted" if "BEGIN PGP SIGNATURE" in body
                      else "is not signed")
            raise RuntimeError(f"Release {tag} {detail}: with signatures required, a release is an annotated tag "
                               "signed with an allowed SSH key (git tag -s). Nothing was deployed.")
        result = self.git("-c", "gpg.format=ssh", "-c", "gpg.ssh.allowedSignersFile=" + str(signers),
                          "verify-tag", tag_object, check=False)
        if result.returncode:
            raise RuntimeError(f"Release {tag} is not signed by a key in the allowed signers file. Nothing was "
                               "deployed.")
        return revision

    def release_tags(self, url: str) -> list[tuple[str, str]]:
        """``(release tag, commit)`` of every release tag (``vX.Y.Z``) of the source at *url*, highest first."""
        listing = self.git("ls-remote", "--tags", url, "refs/tags/v*").stdout
        objects: dict[str, str] = {}
        commits: dict[str, str] = {}
        for line in listing.splitlines():
            sha, _, ref = line.partition("\t")
            if not ref.startswith("refs/tags/") or not _SHA.fullmatch(sha):
                continue
            name = ref.removeprefix("refs/tags/")
            if name.endswith("^{}"):
                commits[name[:-3]] = sha
            else:
                objects[name] = sha
        releases = sorted(((version, name) for name in objects if (version := release_version(name)) is not None),
                          reverse=True)
        return [(name, commits.get(name, objects[name])) for _version, name in releases]

    def latest_release(self, url: str) -> str:
        """The highest release tag (``vX.Y.Z``) of the source at *url*."""
        releases = self.release_tags(url)
        if not releases:
            raise RuntimeError("The update source has no release tag (vX.Y.Z), so there is nothing to update to. "
                               "Updates wait for the next release; 'source set --track branch' follows a branch's "
                               "latest commit instead.")
        return releases[0][0]

    def verify_restorable(self, revision: str) -> None:
        """Refuse to restore *revision* unless it is signed (when signatures are required); it is fetched already.

        Either the commit itself is SSH-signed by an allowed signer, or a
        release tag of the update source names it and verifies as
        :meth:`verify_release` requires: the signatures updates accept.
        """
        if self.source.get("signing", "none") != "ssh":
            return
        try:
            self.verify(revision)
            return
        except RuntimeError:
            pass
        url = valid_url(self.source["url"], allow_local=Path(self.source["url"]).is_absolute())
        for name, commit in self.release_tags(url):
            if commit != revision:
                continue
            self.git("fetch", "--quiet", "--no-tags", url, "refs/tags/" + name)
            if self.head("^{commit}") != revision:
                continue
            try:
                self.verify_release(name, revision)
                return
            except RuntimeError:
                continue
        raise RuntimeError(f"Revision {revision[:12]} is neither signed itself nor released under a tag signed by "
                           "a key in the allowed signers file.")

    def _ancestor(self, older: str, newer: str) -> bool:
        return bool(older) and self.git("merge-base", "--is-ancestor", older, newer, check=False).returncode == 0

    def resolve(self, current: str) -> tuple[str, str, bool]:
        """Fetch what the source tracks; return ``(revision, release tag or branch used, fast-forward from current)``.

        Tracking tags, a server whose revision already contains the latest
        release (installed from a newer checkout) gets *current* back: there
        is nothing to deploy and nothing diverges.
        """
        self.initialize()
        url = valid_url(self.source["url"], allow_local=Path(self.source["url"]).is_absolute())
        if valid_track(self.source.get("track", "branch")) == "tags":
            release, sha = self._verified_release(url)
            if current and sha != current and self._ancestor(sha, current):
                return current, release, True
            return sha, release, sha == current or self._ancestor(current, sha)
        chosen = valid_branch(self.source["branch"])
        if not self.git("ls-remote", "--heads", url, "refs/heads/" + chosen).stdout.strip():
            fallback = self.source.get("fallback_branch")
            if not fallback:
                raise RuntimeError("The selected branch was deleted. Updates are paused until a branch "
                                   "or an explicit fallback is configured.")
            chosen = valid_branch(fallback)
            if not self.git("ls-remote", "--heads", url, "refs/heads/" + chosen).stdout.strip():
                raise RuntimeError("Both the selected branch and its fallback are missing. Nothing was updated.")
        self.git("fetch", "--quiet", "--no-tags", url, "refs/heads/" + chosen)
        sha = self.head()
        self.verify(sha)
        return sha, chosen, sha == current or self._ancestor(current, sha)

    def _verified_release(self, url: str) -> tuple[str, str]:
        """Fetch the highest release that verifies; return ``(tag, commit)``.

        Higher tags that do not verify (anyone who can push tags can push
        one) are skipped, so they cannot hold back a signed release such as a
        security fix; :attr:`skipped_releases` names them.
        """
        releases = [name for name, _commit in self.release_tags(url)]
        if not releases:
            self.latest_release(url)  # raises: no release at all
        self.skipped_releases = []
        reasons = []
        for release in releases[:MAX_RELEASE_CANDIDATES]:
            self.git("fetch", "--quiet", "--no-tags", url, "refs/tags/" + release)
            sha = self.head("^{commit}")
            try:
                self.verify_release(release, sha)
            except RuntimeError as error:
                self.skipped_releases.append(release)
                reasons.append(str(error))
                continue
            return release, sha
        more = len(releases) > MAX_RELEASE_CANDIDATES
        raise RuntimeError(f"None of the {'highest ' if more else ''}{min(len(releases), MAX_RELEASE_CANDIDATES)} "
                           f"release tags of the update source verifies: {reasons[0]}"
                           + (" Remove the tags that are not releases from the update source." if more else ""))

    def archive(self, revision: str, destination: Path) -> None:
        valid_revision(revision)
        with Path(destination).open("wb") as target:
            self.git("archive", "--format=tar.gz", revision, output=target)

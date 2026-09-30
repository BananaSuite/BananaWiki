"""Fetch the configured Git source (public, private, fork) with a hardened Git.

Git never runs hooks, credential helpers, ``ext::`` transports or follows
redirects; credentials never appear in URLs, argv or error messages. The
cache is a bare repository (``repository.git``) that is never checked out:
releases are produced with ``git archive``.
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

    def head(self) -> str:
        sha = self.git("rev-parse", "FETCH_HEAD").stdout.strip()
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

    def resolve(self, current: str) -> tuple[str, str, bool]:
        """Fetch the configured branch; return ``(revision, branch used, fast-forward from current)``."""
        self.initialize()
        url = valid_url(self.source["url"], allow_local=Path(self.source["url"]).is_absolute())
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
        forward = sha == current or (
            bool(current) and self.git("merge-base", "--is-ancestor", current, sha, check=False).returncode == 0
        )
        return sha, chosen, forward

    def archive(self, revision: str, destination: Path) -> None:
        valid_revision(revision)
        with Path(destination).open("wb") as target:
            self.git("archive", "--format=tar.gz", revision, output=target)

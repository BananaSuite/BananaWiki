# Security policy

## Reporting a vulnerability

Please report vulnerabilities privately through GitHub's private
vulnerability reporting:
<https://github.com/BananaSuite/BananaWiki/security/advisories/new>

Include the affected version or commit, the steps to reproduce, the impact,
and a suggested fix if you have one. Don't include real credentials or other
people's data.

If private reporting isn't available, contact
[Luca Zani (OverloadedTech)](https://github.com/OverloadedTech) using the
contact details on their GitHub profile. Only use a public issue to ask for a
private channel, and never post exploit details there.

We'll acknowledge your report as soon as we can, keep you updated while we
work on a fix, and credit you in the release notes unless you'd rather not be
credited.

## Supported versions

| Version | Supported |
|---|---|
| 1.8.x | Yes |
| 1.7 and earlier | No. See [UPGRADING.md](UPGRADING.md) to upgrade to 1.8. |

Security fixes are released as new 1.8 versions with signed tags (`vX.Y.Z`).
Managed servers follow release tags by default. We also recommend keeping the
operating system, Python dependencies and Caddy or Docker up to date, and
testing your backups before you upgrade.

## Scope

These components are in scope:

* the wiki (`bananawiki/wiki`)
* the hosting portal (`bananawiki/hosting`)
* the lifecycle controller and runtime agent (`bananawiki/ops`)
* the desktop launcher
* the GPU speech server (`contrib/tts-gpu-server`)
* the container images and the configurations in `deploy/`

The owner and superusers are fully trusted. Administrators are trusted with
the wiki's content and accounts, but not with the server.

Third-party plugins run with the same rights as the wiki. They are disabled
unless the server operator sets `BW_ALLOW_EXTERNAL_PLUGINS=1`, and only the
owner or a superuser can install or enable a plugin. The same goes for
exporting or importing a whole-site archive, which contains every password
hash and replaces every account. Anything an owner or superuser does through
a plugin they enabled or an archive they imported is expected behaviour, and
so is an administrator reading the data their role gives them access to.

We consider these to be vulnerabilities:

* anyone without the administrator role gaining administrator powers
* an administrator who isn't the owner or a superuser, or an API token,
  getting plugin code installed, enabled or run, running a whole-site export
  or import, or gaining owner or superuser rights
* reading or changing pages, categories, files, chats, boards or canvases
  that you aren't allowed to see or change
* a hosted wiki (or its administrator) affecting the hosting platform, the
  host or other wikis
* bypassing CSRF protection, sessions, rate limits, the Content Security
  Policy or the upload checks
* anything that exposes secrets such as keys, tokens or password hashes

These are out of scope: denial of service through sheer volume, missing
hardening headers that don't lead to an exploit, self-XSS, social
engineering, and issues that require administrators to act against their own
wiki.

For a description of the security model, see
[docs/security.md](docs/security.md).

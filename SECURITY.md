# Security policy

## Reporting a vulnerability

Report vulnerabilities privately through GitHub's private vulnerability
reporting:
<https://github.com/BananaSuite/BananaWiki/security/advisories/new>

Include the affected version or commit, the steps to reproduce, the impact,
and a suggested fix if you have one. Do not include real credentials or other
people's data. If private reporting is unavailable, contact
[Luca Zani (OverloadedTech)](https://github.com/OverloadedTech) through the
contact details on that profile. Use a public issue only to ask for a private
channel; never publish exploit details there.

We acknowledge reports as soon as we can, keep you informed while we work on
a fix, and credit you in the release notes unless you prefer otherwise.

## Supported versions

| Version | Supported |
|---|---|
| 1.6.x | Yes |
| 1.4 and earlier | No: upgrade to 1.6 ([UPGRADING.md](UPGRADING.md)) |

Security fixes are released for the latest 1.6 release. Keep the operating
system, Python dependencies and Caddy/Docker up to date, and test your backups
before upgrades.

## Scope

In scope: the wiki (`bananawiki/wiki`), the hosting portal
(`bananawiki/hosting`), the lifecycle controller and runtime agent
(`bananawiki/ops`), the desktop launcher, the GPU speech server
(`contrib/tts-gpu-server`), the container images and the configurations
shipped in `deploy/`.

**Administrators are fully trusted.** An administrator can install a plugin
(code that runs with the wiki's rights) or import a whole-site archive (which
replaces every account). An administrator gaining owner or superuser rights,
or reading data, through a plugin or an import is expected behaviour, not a
vulnerability. These are vulnerabilities:

* anyone without the administrator role reaching administrator powers, or
  running plugin code no administrator enabled;
* reading or changing pages, categories, files, chats, boards or canvases you
  may not see or change;
* a hosted wiki (or its administrator) affecting the hosting platform, the host
  or other wikis;
* bypassing CSRF, sessions, rate limits, the Content Security Policy or the
  upload checks;
* anything that exposes secrets (keys, tokens, password hashes).

Out of scope: denial of service by volume, missing hardening headers that do
not lead to an exploit, self-XSS, social engineering, and findings that
require an administrator to act against their own wiki.

The security model is described in [docs/security.md](docs/security.md).

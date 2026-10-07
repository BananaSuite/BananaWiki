BananaWiki container ACL library update

The runtime installs Debian's libacl1 2.4.0-1 and GNU tar 1.35+dfsg-6, downloaded
through Debian's authenticated sid package/source indexes. Only these two binary
packages are updated; the image keeps trixie repositories, libc and coreutils.
The Debian package hashes, signed InRelease and archive keyring are preserved.
source-SHA256SUMS pins all corresponding ACL/tar source archives and Debian
packaging. Matching coreutils source is included for consumer review/rebuilding.
Debian's source archives include the complete packaging/build recipes and tar's
namespace-collision compatibility patch, 0001-Avoid-acl_-prefix-for-functions.patch.

ACL's unmodified upstream 2.4.0 source also passes its release signature check.
Source SHA256:
  e661131456d2708a01c614a0f400e11d7d1bfaeb6f3e74b75bb980b72f0161a3
Maintainer fingerprint:
  259B3792B3D6D319212CC4DCD5BF9FEB0313653A
The vendored key was authenticated via Debian's matching source package
debian/upstream/signing-key.asc, not accepted solely from a keyserver search.
Sources, signatures, licenses, recipe and inventories ship in
/usr/local/share/bananawiki/acl. Package notices remain under /usr/share/doc.
libacl is LGPL-2.1-or-later. GNU tar and coreutils are GPL-3.0-or-later.
Their dynamically linked libraries remain replaceable in derived images.

Debian marks ACL 2.4.0-1 fixed for CVE-2026-54369. The upstream fix adds safe
acl_*_file_at APIs with directory descriptors and AT_SYMLINK_NOFOLLOW/AT_EMPTY_PATH.
It deliberately preserves the old acl_*_file APIs' symlink-following contract;
calling those legacy APIs on attacker-controlled paths with elevated privileges
is still unsafe. A new version number does not make those invocations safe.
New directory-relative calls must also use a trusted directory descriptor;
AT_SYMLINK_NOFOLLOW only protects the final pathname component.

The image's advertised Wiki HTTP/CLI/tenant paths do not invoke libacl, cp or tar.
GNU timeout (required by tenant task deadlines) links only to libc. All coreutils
programs and tar remain available for compatibility. Manual privileged copying
or archiving of attacker-controlled paths in a derived image needs separate
consumer-level review and must not be inferred safe from this package update.
Keep the documented non-root, read-only, capability-free container deployment.

Primary references:
  https://www.openwall.com/lists/oss-security/2026/06/29/1
  https://security-tracker.debian.org/tracker/CVE-2026-54369
  https://download.savannah.nongnu.org/releases/acl/
  https://lists.nongnu.org/archive/html/bug-tar/2026-06/msg00013.html

To reproduce, build either Dockerfile. fetch-acl-packages.sh shows the complete
signed-index download and verification recipe. It downloads pinned package
versions for the build architecture and validates the signed-index hashes;
the current verification covers Linux/amd64. The source archives and Debian
packaging can be extracted with dpkg-source -x and rebuilt on a compatible
Debian toolchain. Rebuilding is separate from the official publisher binaries
installed by this image recipe; no locally rebuilt binary is misidentified as
a Debian publisher package.

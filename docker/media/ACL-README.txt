BananaWiki container ACL library update

The runtime installs Debian's libacl1 2.4.0-1 and GNU tar 1.35+dfsg-6, downloaded
through Debian's authenticated sid package/source indexes at a fixed
snapshot.debian.org timestamp (see fetch-acl-packages.sh). Only these two binary
packages are updated; the image keeps trixie repositories, libc and coreutils.
The Debian package hashes, signed InRelease and archive keyring are preserved.
source-SHA256SUMS pins all corresponding ACL/tar source archives and Debian
packaging. Matching coreutils source is included for review and rebuilding.
Debian's source archives include the complete packaging/build recipes and tar's
namespace-collision compatibility patch, 0001-Avoid-acl_-prefix-for-functions.patch.

ACL's unmodified upstream 2.4.0 source also passes its release signature check.
Source SHA256:
  e661131456d2708a01c614a0f400e11d7d1bfaeb6f3e74b75bb980b72f0161a3
Maintainer fingerprint:
  259B3792B3D6D319212CC4DCD5BF9FEB0313653A
The vendored key comes from Debian's matching source package
(debian/upstream/signing-key.asc), not from a keyserver.
Sources, signatures, licenses, recipe and inventories ship in
/usr/local/share/bananawiki/acl. Package notices remain under /usr/share/doc.
libacl is LGPL-2.1-or-later. GNU tar and coreutils are GPL-3.0-or-later.
Their dynamically linked libraries remain replaceable in derived images.

Debian marks ACL 2.4.0-1 fixed for CVE-2026-54369. The fix adds acl_*_file_at
calls that take a directory descriptor and AT_SYMLINK_NOFOLLOW/AT_EMPTY_PATH;
the legacy acl_*_file calls still follow symlinks, and AT_SYMLINK_NOFOLLOW
covers only the final path component.

BananaWiki's HTTP, command-line and tenant code does not call libacl, cp or tar.
GNU timeout (used for tenant task deadlines) links only to libc. coreutils and
tar stay installed for compatibility. Review any privileged copying or archiving
of untrusted paths you add in a derived image, and keep the containers
non-root, read-only and without capabilities.

Primary references:
  https://www.openwall.com/lists/oss-security/2026/06/29/1
  https://security-tracker.debian.org/tracker/CVE-2026-54369
  https://download.savannah.nongnu.org/releases/acl/
  https://lists.nongnu.org/archive/html/bug-tar/2026-06/msg00013.html

To reproduce, build either Dockerfile. fetch-acl-packages.sh is the complete
download and verification recipe: it fetches the pinned package versions for the
build architecture and checks them against the signed indexes (verified on
Linux/amd64). The image installs Debian's own binaries. To rebuild from source,
extract the source archives and Debian packaging with dpkg-source -x and build
them on a compatible Debian toolchain.

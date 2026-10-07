#!/bin/sh
# Fetch only the compatible, fixed ACL library and tar from signed Debian indexes.
# The runtime keeps its trixie repositories and all existing coreutils programs.
set -eu
destination=/usr/local/share/bananawiki/acl
packages=/build/acl-packages
acl_version=2.4.0-1
tar_version=1.35+dfsg-6
fingerprint=259B3792B3D6D319212CC4DCD5BF9FEB0313653A
mkdir -p "$destination/sources" "$packages" /build/acl-unpacked

cat > /etc/apt/sources.list.d/acl-update.sources <<'SOURCES'
Types: deb deb-src
URIs: https://deb.debian.org/debian
Suites: sid
Components: main
Signed-By: /usr/share/keyrings/debian-archive-keyring.gpg
SOURCES
apt-get -o APT::Get::AllowUnauthenticated=false \
    -o Acquire::AllowInsecureRepositories=false update
cp /var/lib/apt/lists/*_sid_InRelease "$destination/"
cp /usr/share/keyrings/debian-archive-keyring.gpg "$destination/"
cd "$packages"
apt-cache show "libacl1=$acl_version" > "$destination/libacl-package-index.txt"
apt-cache show "tar=$tar_version" > "$destination/tar-package-index.txt"
apt-get -o APT::Get::AllowUnauthenticated=false download \
    "libacl1=$acl_version" "tar=$tar_version"
architecture=$(dpkg --print-architecture)
for name in libacl1 tar; do
    case "$name" in
        libacl1) version=$acl_version; index=libacl-package-index.txt ;;
        tar) version=$tar_version; index=tar-package-index.txt ;;
    esac
    archive=${name}_${version}_${architecture}.deb
    expected=$(awk '/^SHA256:/ {print $2; exit}' "$destination/$index")
    test -n "$expected"
    printf '%s  %s\n' "$expected" "$archive" | sha256sum --check --strict
    test "$(dpkg-deb --field "$archive" Version)" = "$version"
    test "$(dpkg-deb --field "$archive" Architecture)" = "$architecture"
    dpkg-deb --extract "$archive" "/build/acl-unpacked/$name"
done
sha256sum ./*.deb > "$destination/package-SHA256SUMS"

cd "$destination/sources"
apt-get -o APT::Get::AllowUnauthenticated=false source --download-only \
    "acl=$acl_version" "tar=$tar_version"
# Preserve the matching source of the unchanged, ACL-dependent coreutils too.
coreutils_source=$(dpkg-query -W -f='${source:Version}' coreutils)
apt-get -o APT::Get::AllowUnauthenticated=false source --download-only \
    "coreutils=$coreutils_source"
cp /build/acl/source-SHA256SUMS .
sha256sum --check --strict source-SHA256SUMS
cp /build/acl/acl-maintainer.asc /build/acl/acl-2.4.0.tar.xz.sig .
export GNUPGHOME=/build/acl-gnupg
mkdir -m 0700 "$GNUPGHOME"
actual=$(gpg --batch --show-keys --with-colons acl-maintainer.asc \
    | awk -F: '$1 == "fpr" {print $10; exit}')
test "$actual" = "$fingerprint"
gpg --batch --import acl-maintainer.asc
gpg --batch --status-fd 1 --verify acl-2.4.0.tar.xz.sig acl_2.4.0.orig.tar.xz \
    > "$destination/signature-verification.txt" 2>&1
grep -F "[GNUPG:] VALIDSIG $fingerprint " "$destination/signature-verification.txt"
sha256sum ./* > "$destination/source-SHA256SUMS"

tar -xJOf acl_2.4.0.orig.tar.xz acl-2.4.0/doc/COPYING.LGPL \
    > "$destination/COPYING.LGPL"
tar -xJOf tar_1.35+dfsg.orig.tar.xz tar-1.35+dfsg/COPYING \
    > "$destination/tar-COPYING"
cp /build/acl/fetch-acl-packages.sh /build/acl/ACL-README.txt \
    /build/acl/acl.cdx.json "$destination/"
find /build/acl-unpacked -type f \( -name 'libacl.so.*' -o -path '*/bin/tar' \) \
    -exec sha256sum {} + > "$destination/binary-SHA256SUMS"
dpkg-query -W coreutils libc6 > "$destination/build-base-packages.txt"
# Record the actual retained coreutils consumers. GNU timeout is intentionally
# kept; unlike cp/mv/install, its ELF imports do not include libacl.
dpkg-query -L coreutils | while IFS= read -r program; do
    if [ -f "$program" ] && [ -x "$program" ] \
        && readelf --dynamic "$program" 2>/dev/null | grep -q '\[libacl\.so\.1\]'; then
        printf '\n%s\n' "$program"
        readelf --dyn-syms --wide "$program" | grep 'acl_' || true
    fi
done > "$destination/coreutils-acl-consumer-imports.txt"
find /build/acl-unpacked/tar -type f -path '*/bin/tar' \
    -exec readelf --dyn-syms --wide {} \; \
    | grep 'acl_' > "$destination/tar-acl-consumer-imports.txt"
readelf --dynamic /usr/bin/timeout > "$destination/timeout-dynamic-imports.txt"

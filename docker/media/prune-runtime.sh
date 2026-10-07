#!/bin/sh
# Remove unused administration/installation utilities from the final runtime.
# Keep package records and the libraries used by Python/Piper and GNU timeout.
set -eu
destination=/usr/local/share/bananawiki/media
dpkg-query -W > "$destination/runtime-packages-before-prune.txt"

# Debian cannot infer dependencies of pip's binary wheels. ONNX Runtime/Piper
# need libstdc++6, and Python's uuid/curses/readline modules need these libraries.
apt-mark manual libstdc++6 libgomp1 libmp3lame0 libuuid1 libncursesw6 libtinfo6 \
    libreadline8t64 fonts-dejavu-core
# These Debian-essential utilities are for host management, login, mounts and
# package installation, none of which runs in these unprivileged containers.
# They are removed through the package manager, including their actual files.
apt-get purge -y --auto-remove --allow-remove-essential \
    bsdutils util-linux login mount liblastlog2-2 ncurses-bin \
    libblkid1 libmount1 libsmartcols1 passwd adduser \
    libpam-modules libpam-modules-bin libpam-runtime libpam0g
# Package purge scripts use Perl/debconf and systemd helpers. Finish them before
# removing the installers and finally Perl; otherwise dpkg's purge can fail.
apt-get purge -y --auto-remove --allow-remove-essential apt libapt-pkg7.0
dpkg --purge --force-remove-essential perl-base
dpkg-query -W > "$destination/runtime-packages-after-prune.txt"

test -x /usr/bin/timeout
test ! -e /usr/bin/mount
test ! -e /usr/bin/nsenter
test ! -e /usr/bin/perl
test ! -e /usr/bin/apt-get
test ! -e /usr/bin/infocmp
python -c 'import bz2, cryptography, curses, lzma, numpy, onnxruntime, piper, readline, sqlite3, ssl, uuid; assert uuid.uuid4().version == 4; curses.setupterm(term="xterm")'
ffmpeg -hide_banner -version

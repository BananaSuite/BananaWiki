#!/bin/sh
# Run inside the media-builder Docker stage; this recipe travels with the binary.
set -eu

version=9.0.2
archive="ffmpeg-$version.tar.xz"
digest=8c3850283eb25fa026482078a04051e0be17347b09ef81a0849bec15a96e002e
fingerprint=FCF986EA15E6E293A5644F10B4322F04D67658D8
recipe_dir=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
destination=/usr/local/share/bananawiki/media
mkdir -p "$destination/sources" /build/gnupg
chmod 0700 /build/gnupg

# The vendored key fingerprint is independently pinned to ffmpeg.org/download.html.
gpg --homedir /build/gnupg --batch --import "$recipe_dir/ffmpeg-devel.asc"
actual_fingerprint=$(gpg --homedir /build/gnupg --batch --with-colons --fingerprint \
    | awk -F: '$1 == "fpr" {print $10; exit}')
test "$actual_fingerprint" = "$fingerprint"
cd /build
if [ -s /run/secrets/proxy_ca ]; then
    export CURL_CA_BUNDLE=/run/secrets/proxy_ca
fi
curl --fail --show-error --location --retry 3 --proto '=https' --proto-redir '=https' \
    --tlsv1.2 "https://ffmpeg.org/releases/$archive" -o "$archive"
printf '%s  %s\n' "$digest" "$archive" | sha256sum -c -
gpg --homedir /build/gnupg --batch --status-fd 1 \
    --verify "$recipe_dir/$archive.asc" "$archive" > "$destination/signature-verification.txt" 2>&1
grep -F "[GNUPG:] VALIDSIG $fingerprint " "$destination/signature-verification.txt"
tar -xf "$archive"
cd "ffmpeg-$version"

# All Wiki and standalone GPU-service TTS inputs and playback speeds are retained.
# No network protocols, video/image/subtitle codecs or hardware decoders. The
# upstream CLI also selects its built-in transform/buffer filters and atrim.
./configure \
    --prefix=/usr/local \
    --disable-everything \
    --disable-autodetect \
    --disable-doc \
    --disable-debug \
    --disable-network \
    --disable-ffplay \
    --disable-ffprobe \
    --disable-avdevice \
    --disable-swscale \
    --disable-shared \
    --enable-static \
    --enable-ffmpeg \
    --enable-libmp3lame \
    --enable-decoder=pcm_s16le,pcm_s24le,pcm_s32le,pcm_f32le,pcm_f64le,pcm_u8,mp3,mp3float \
    --enable-demuxer=wav,mp3 \
    --enable-encoder=libmp3lame \
    --enable-muxer=mp3 \
    --enable-protocol=file,pipe \
    --enable-filter=abuffer,abuffersink,aformat,anull,aresample,atempo \
    --enable-swresample \
    --extra-cflags='-O2 -fstack-protector-strong -D_FORTIFY_SOURCE=2' \
    --extra-ldflags='-Wl,-z,relro,-z,now'
make -j "${MEDIA_BUILD_JOBS:-2}"
install -m 0755 ffmpeg /usr/local/bin/ffmpeg
cp config.h config_components.h ffbuild/config.mak "$destination/"
cp COPYING.LGPLv2.1 LICENSE.md "$destination/"
cp "$recipe_dir/build-ffmpeg.sh" "$recipe_dir/README.txt" "$destination/"
cp "$recipe_dir/ffmpeg.cdx.json" "$destination/"
cp "$recipe_dir/ffmpeg-devel.asc" "$recipe_dir/$archive.asc" "$destination/sources/"
cp "/build/$archive" "$destination/sources/"
cp /build/lame_*.dsc /build/lame_*.orig.tar.* /build/lame_*.debian.tar.* "$destination/sources/"
sha256sum "$destination"/sources/* > "$destination/SHA256SUMS"
sha256sum /usr/local/bin/ffmpeg > "$destination/binary-SHA256SUMS"
dpkg-query -W build-essential gcc libc6-dev libmp3lame0 libmp3lame-dev nasm pkg-config \
    > "$destination/build-packages.txt"
ffmpeg -version > "$destination/ffmpeg-version.txt"
ffmpeg -buildconf > "$destination/build-configuration.txt" 2>&1

BananaWiki container media component

FFmpeg 9.0.2, Copyright (c) FFmpeg contributors, https://ffmpeg.org/
License: LGPL 2.1 or later; see COPYING.LGPLv2.1 and LICENSE.md.
This build enables no GPL or nonfree components. It links dynamically to Debian's
libmp3lame0; its LGPL 2.0 or later license and notices are installed at
/usr/share/doc/libmp3lame0/copyright. BananaWiki invokes FFmpeg as a separate
process. Debian library notices remain in /usr/share/doc/.

The exact, unmodified FFmpeg source archive, upstream signature and signing key
are in sources/. The archive is pinned by SHA256 and the release key fingerprint:
  8c3850283eb25fa026482078a04051e0be17347b09ef81a0849bec15a96e002e
  FCF986EA15E6E293A5644F10B4322F04D67658D8
Key identity and release: https://ffmpeg.org/download.html
Source: https://ffmpeg.org/releases/ffmpeg-9.0.2.tar.xz

Matching Debian LAME source files (.dsc, original tarball and Debian changes) are
also included in sources/. They were fetched with apt-get source using Debian's
signed package index and match the source version reported by libmp3lame0. The
Debian binary package may have a +bN suffix for its architecture rebuild.

build-ffmpeg.sh is the complete FFmpeg build recipe, also present in the source
repository at docker/media/build-ffmpeg.sh. build-packages.txt records the exact
compiler and build dependencies used. build-configuration.txt and config.mak
record the effective configuration. Rebuild in the same Python/Debian base with
those tools and libmp3lame-dev installed; the Dockerfiles show the complete build
stages. To use a modified FFmpeg or LAME, rebuild those stages or replace the
FFmpeg executable / dynamically linked LAME library in your own derived image.
ffmpeg.cdx.json explicitly inventories the source-built FFmpeg component with
its version, source hash, license, CPE and package URL. binary-SHA256SUMS records
the built executable hash. Generic C binaries may not be covered by automatic
OS/Python package scanners; review upstream FFmpeg security advisories as well.
Sources can be retrieved without running the image:
  docker create --name media-source <image>
  docker cp media-source:/usr/local/share/bananawiki/media ./media-source
  docker cp media-source:/usr/share/doc/libmp3lame0 ./libmp3lame0-notices
  docker rm media-source

Only speech capabilities used by BananaWiki and its GPU speech service are
enabled: PCM WAV and MP3 input, all supported PCM sample formats, MP3 output via
LAME, sample conversion, pitch-preserving tempo changes and file/pipe protocols.
No FFmpeg network protocols, video/image codecs or unrelated parsers are built.
FFmpeg's upstream CLI also requires atrim and built-in video transform/buffer
filters; no enabled input decoder or output encoder handles video frames.
This reduces exposed code; it is not a claim that source-built software has no
vulnerabilities. Update the pinned release after verifying newer signed releases
and rerun the media container verifier plus the application tests.

# syntax=docker/dockerfile:1
# BananaWiki: a single wiki as a container.
#
#   docker build -t bananawiki .
#   docker run -d -p 127.0.0.1:5001:5001 -v bananawiki-data:/data \
#     --read-only --tmpfs /tmp --cap-drop ALL --security-opt no-new-privileges bananawiki
#
# Everything the wiki writes lives in /data (database, uploads, secret key,
# logs); the image itself can be mounted read-only. Behind a TLS proxy set
# BW_PROXY_MODE=1 (see compose.yaml). The setup token for the first visit:
#   docker exec <container> python -m bananawiki.cli setup-token
FROM python:3.12-slim-trixie AS media-base

RUN apt-get update \
    && apt-get upgrade -y \
    && apt-get install -y --no-install-recommends libmp3lame0 libgomp1 fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

FROM media-base AS media-builder
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        build-essential pkg-config nasm curl gpg gpg-agent xz-utils ca-certificates \
        "libmp3lame-dev=$(dpkg-query -W -f='${Version}' libmp3lame0)" \
    && sed -i 's/^Types: deb$/Types: deb deb-src/' /etc/apt/sources.list.d/debian.sources \
    && apt-get update \
    && mkdir -p /build && cd /build \
    && apt-get source --download-only "lame=$(dpkg-query -W -f='${source:Version}' libmp3lame0)"
COPY docker/media/build-ffmpeg.sh docker/media/README.txt docker/media/ffmpeg.cdx.json \
    docker/media/ffmpeg-devel.asc docker/media/ffmpeg-9.0.2.tar.xz.asc /build/media/
RUN --mount=type=secret,id=proxy_ca sh /build/media/build-ffmpeg.sh

FROM media-builder AS acl-package-builder
COPY docker/media/fetch-acl-packages.sh docker/media/source-SHA256SUMS \
    docker/media/ACL-README.txt docker/media/acl.cdx.json \
    docker/media/acl-maintainer.asc docker/media/acl-2.4.0.tar.xz.sig /build/acl/
RUN sh /build/acl/fetch-acl-packages.sh

FROM media-base
COPY --from=media-builder /usr/local/bin/ffmpeg /usr/local/bin/ffmpeg
COPY --from=media-builder /usr/local/share/bananawiki/media /usr/local/share/bananawiki/media
COPY --from=acl-package-builder /usr/local/share/bananawiki/acl /usr/local/share/bananawiki/acl
COPY --from=acl-package-builder /build/acl-packages /tmp/acl-update
RUN dpkg -i /tmp/acl-update/libacl1_*.deb /tmp/acl-update/tar_*.deb \
    && rm -rf /tmp/acl-update

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    ORT_DISABLE_TELEMETRY=1 \
    HOME=/tmp \
    BW_ENV=production \
    BW_HOST=0.0.0.0 \
    BW_PORT=5001 \
    BW_INSTANCE_DIR=/data

RUN groupadd --system --gid 10001 bananawiki \
    && useradd --system --uid 10001 --gid 10001 --home-dir /data --no-create-home \
        --shell /usr/sbin/nologin bananawiki \
    && install -d -o 10001 -g 10001 -m 0700 /data

WORKDIR /app
COPY requirements.txt ./
RUN --mount=type=secret,id=proxy_ca \
    if [ -s /run/secrets/proxy_ca ]; then export PIP_CERT=/run/secrets/proxy_ca; fi \
    && python -m pip install --no-cache-dir --only-binary=:all: --no-deps --upgrade 'pip>=26.2.1' \
    && python -m pip install --no-cache-dir --only-binary=:all: --requirement requirements.txt \
    && python -m pip uninstall -y pip setuptools wheel \
    && rm -rf /usr/local/lib/python3.12/ensurepip

COPY LICENSE NOTICE wsgi.py gunicorn.conf.py ./
COPY bananawiki ./bananawiki
RUN find /app -type d -exec chmod 0755 {} + \
    && find /app -type f -exec chmod 0644 {} + \
    && python -m compileall -q bananawiki

COPY docker/media/prune-runtime.sh /usr/local/share/bananawiki/media/prune-runtime.sh
RUN sh /usr/local/share/bananawiki/media/prune-runtime.sh

USER 10001:10001
VOLUME ["/data"]
EXPOSE 5001
HEALTHCHECK --interval=30s --timeout=5s --start-period=90s --retries=3 \
    CMD ["python", "-c", "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/health' % os.environ.get('BW_PORT', '5001'), timeout=4)"]
CMD ["gunicorn", "-c", "gunicorn.conf.py", "wsgi:app"]

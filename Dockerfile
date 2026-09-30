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
FROM python:3.14-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    HOME=/tmp \
    BW_ENV=production \
    BW_HOST=0.0.0.0 \
    BW_PORT=5001 \
    BW_INSTANCE_DIR=/data

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg libgomp1 fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --system --gid 10001 bananawiki \
    && useradd --system --uid 10001 --gid 10001 --home-dir /data --no-create-home \
        --shell /usr/sbin/nologin bananawiki \
    && install -d -o 10001 -g 10001 -m 0700 /data

WORKDIR /app
COPY requirements.txt ./
RUN python -m pip install --no-cache-dir --only-binary=:all: --no-deps --upgrade 'pip>=26.2.1' \
    && python -m pip install --no-cache-dir --only-binary=:all: --requirement requirements.txt

COPY LICENSE NOTICE wsgi.py gunicorn.conf.py ./
COPY bananawiki ./bananawiki
RUN python -m compileall -q bananawiki

USER 10001:10001
VOLUME ["/data"]
EXPOSE 5001
HEALTHCHECK --interval=30s --timeout=5s --start-period=90s --retries=3 \
    CMD ["python", "-c", "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/health' % os.environ.get('BW_PORT', '5001'), timeout=4)"]
CMD ["gunicorn", "-c", "gunicorn.conf.py", "wsgi:app"]

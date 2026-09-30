"""Read aloud: audio versions of wiki pages (the 1.4 ``tts`` plugin).

* Readers press "Generate" in the player under a page (or the admin turns on
  automatic generation); that only queues a row in ``tts_generations``.
* A worker (``scripts/tts_worker.py``, or threads in the web process with
  ``BW_TTS_INLINE_WORKER=1``) synthesises queued pages with local Piper
  voices or a remote GPU server (``contrib/tts-gpu-server``).
* Editing a page invalidates its audio and queues fresh audio when the page
  had some; deleting it removes its files. A periodic sweep deletes files no
  row references and refreshes audio that fell out of date.

``piper-tts``, ``langdetect`` and ffmpeg are optional: without them the
wiki works and the admin page says what is missing.
"""

from typing import Any

from flask import Flask

from ... import auth
from ...registry import Feature, Job, NavItem, is_enabled
from . import options, service
from .routes import bp, render_panel


def _init_app(app: Flask) -> None:
    cfg = options.load(app.config["BW"])
    app.extensions["bananawiki.tts.config"] = cfg
    if not cfg.inline_worker or app.config["BW"].testing:
        return

    @app.before_request
    def _start_inline_worker() -> None:
        from .worker import start_inline

        start_inline(app)


def _visible(user: Any) -> bool:
    return auth.is_admin(user) and is_enabled("tts")


FEATURE = Feature(
    id="tts",
    name="feature.tts.name",
    description="feature.tts.description",
    toggle="plugin",
    default_enabled=True,
    easy_wiki=False,
    blueprints=[bp],
    nav=[NavItem("tts.admin.nav", "tts.admin", icon="volume", area="admin", order=70, visible=_visible)],
    jobs=[Job("tts.sweep", 6 * 3600, service.sweep, initial_delay=300)],
    events={
        "page.created": [service.on_page_created],
        "page.updated": [service.on_page_updated],
        "page.deleted": [service.on_page_deleted],
    },
    slots={"page.below_content": render_panel},
    init_app=_init_app,
)

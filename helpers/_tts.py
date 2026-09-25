"""Text-to-speech engine + background worker for the built-in ``tts`` plugin.

Two layers:

1.  ``tts_content_hash`` / ``tts_normalize_text``: pure helpers that prepare
    a wiki page for synthesis (strip Markdown / HTML, collapse whitespace,
    truncate to a sane upper bound).
2.  ``synthesize_to_mp3``: the engine.  It writes a local audio file with
    the configured backend (``BW_TTS_BACKEND`` env var):

    - ``piper`` (default): local neural TTS using Piper voice models.  Text is
      never sent to an upstream provider; missing voice files can be downloaded
      once into the local voice directory.
    - ``stub``: writes a tiny placeholder MP3 immediately.  Used by the test
      suite so unit tests stay offline and deterministic.
    - ``remote-gpu``: POSTs the text to a ``banana-tts-gpu`` server on a
      private network and falls back to local Piper when that fails.  On a
      hosted wiki its URL and token come only from the host's environment.

3.  ``run_tts_worker_loop``: standalone worker loop that claims durable DB
    rows and drives them through ``pending`` -> ``processing`` ->
    ``completed`` / ``failed``.

``run_generation_async`` remains as a small compatibility shim.  In production
it only returns a completed handle because web requests should enqueue durable
DB rows and let the separate worker process synthesize them.  Set
``BW_TTS_INLINE_WORKER=1`` for tests or tiny single-process dev runs.

The worker re-checks the DB row's status before writing a file.  If the row
has been deleted (page edited / deleted, plugin disabled, admin cancel) the
worker discards its output and exits cleanly.

When the host switches TTS off for a wiki (``BW_MANAGED_TTS_DISABLED`` or
EasyWiki mode, see :func:`tts_generation_disabled_by_host`), nothing in this
module creates or synthesizes a generation, whichever path asks for it.
"""


from __future__ import annotations

from http_transport import open_http

from contextlib import contextmanager
from functools import lru_cache
import hashlib
import json
import logging
import os
from pathlib import Path
import queue
import re
import shutil
import subprocess
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import wave

import config
import db
from helpers._server_restart import is_easy_wiki, is_hosted_instance


_logger = logging.getLogger("bananawiki.tts")
_auto_generation_suppression = threading.local()


def tts_generation_disabled_by_host():
    """Return True when the host has switched TTS generation off for this wiki.

    Two host-level switches apply, and a wiki admin can change neither:
    ``BW_MANAGED_TTS_DISABLED`` (the hosting portal's global off switch or its
    whitelist / blacklist policy) and EasyWiki mode, which removes the TTS
    plugin. The manual Generate route is not the only way in: a tenant admin
    can turn on ``tts_auto_generate_enabled`` through the API, and page saves
    then queue work from the plugin hooks. So every path that queues, recovers
    or synthesizes a generation checks this, including the standalone worker.
    Audio that already exists stays playable.
    """
    if getattr(config, "MANAGED_TTS_DISABLED", False):
        return True
    return is_easy_wiki()


@contextmanager
def suppress_tts_auto_generation():
    """Temporarily skip hook-driven TTS auto-generation in this thread."""
    depth = getattr(_auto_generation_suppression, "depth", 0)
    _auto_generation_suppression.depth = depth + 1
    try:
        yield
    finally:
        if depth:
            _auto_generation_suppression.depth = depth
        else:
            try:
                delattr(_auto_generation_suppression, "depth")
            except AttributeError:
                pass


def tts_auto_generation_suppressed():
    """Return True when bulk/import code has disabled hook fanout."""
    return getattr(_auto_generation_suppression, "depth", 0) > 0



# Hard upper bound on the number of characters synthesised per generation.
# Keep generation latency predictable and bound how large the cached audio can
# grow, regardless of the selected backend.
TTS_MAX_INPUT_CHARS = 20_000

# Languages exposed to the user.  This preserves the historical catalogue so
# existing settings/content hashes remain stable.  The DB-side
# check in ``db/_tts.py`` imports this tuple, and the per-site "enabled
# languages" admin setting is restricted to entries that appear here.
#
# The order of the tuple drives the default rendering order of the admin
# checkbox grid; keep it alphabetical by code so the UI is predictable.
#
# We hard-code the catalogue because provider language lists can change, while
# the DB-side validation needs a stable schema.
TTS_SUPPORTED_LANGUAGES = (
    "af", "am", "ar", "bg", "bn", "bs", "ca", "cs", "cy", "da",
    "de", "el", "en", "es", "et", "eu", "fa", "fi", "fr", "fr-CA", "gl",
    "gu", "ha", "hi", "hr", "hu", "id", "is", "it", "iw", "ja",
    "jw", "ka", "kk", "km", "kn", "ko", "ku", "la", "lb", "lt", "lv", "ml", "mr", "ms",
    "my", "ne", "nl", "no", "pa", "pl", "pt", "pt-PT", "ro", "ru",
    "si", "sk", "sl", "sq", "sr", "su", "sv", "sw", "ta", "te", "th",
    "tl", "tr", "uk", "ur", "vi", "yue", "zh", "zh-CN", "zh-TW",
)

# Convenience set for O(1) ``in`` checks.  Tuple stays as the canonical
# ordering source.
TTS_SUPPORTED_LANGUAGE_SET = frozenset(TTS_SUPPORTED_LANGUAGES)

# Friendly display labels for the language picker.  Names are sourced from
# ISO 639 / provider listings and rendered as-is in the admin grid
# and the per-page picker.  Keep keys in sync with TTS_SUPPORTED_LANGUAGES.
TTS_LANGUAGE_LABELS = {
    "af": "Afrikaans",
    "am": "\u12a0\u121b\u122d\u129b (Amharic)",
    "ar": "\u0627\u0644\u0639\u0631\u0628\u064a\u0629 (Arabic)",
    "bg": "\u0411\u044a\u043b\u0433\u0430\u0440\u0441\u043a\u0438 (Bulgarian)",
    "bn": "\u09ac\u09be\u0982\u09b2\u09be (Bengali)",
    "bs": "Bosanski (Bosnian)",
    "ca": "Catal\u00e0 (Catalan)",
    "cs": "\u010ce\u0161tina (Czech)",
    "cy": "Cymraeg (Welsh)",
    "da": "Dansk (Danish)",
    "de": "Deutsch (German)",
    "el": "\u0395\u03bb\u03bb\u03b7\u03bd\u03b9\u03ba\u03ac (Greek)",
    "en": "English",
    "es": "Espa\u00f1ol (Spanish)",
    "et": "Eesti (Estonian)",
    "eu": "Euskara (Basque)",
    "fa": "\u0641\u0627\u0631\u0633\u06cc (Persian)",
    "fi": "Suomi (Finnish)",
    "fr": "Fran\u00e7ais (French)",
    "fr-CA": "Fran\u00e7ais canadien (French - Canada)",
    "gl": "Galego (Galician)",
    "gu": "\u0a97\u0ac1\u0a9c\u0ab0\u0abe\u0aa4\u0ac0 (Gujarati)",
    "ha": "Hausa",
    "hi": "\u0939\u093f\u0928\u094d\u0926\u0940 (Hindi)",
    "hr": "Hrvatski (Croatian)",
    "hu": "Magyar (Hungarian)",
    "id": "Bahasa Indonesia (Indonesian)",
    "is": "\u00cdslenska (Icelandic)",
    "it": "Italiano (Italian)",
    "iw": "\u05e2\u05d1\u05e8\u05d9\u05ea (Hebrew)",
    "ja": "\u65e5\u672c\u8a9e (Japanese)",
    "jw": "Basa Jawa (Javanese)",
    "ka": "\u10e5\u10d0\u10e0\u10d7\u10e3\u10da\u10d8 (Georgian)",
    "kk": "\u049a\u0430\u0437\u0430\u049b \u0442\u0456\u043b\u0456 (Kazakh)",
    "km": "\u1781\u17d2\u1798\u17c2\u179a (Khmer)",
    "kn": "\u0c95\u0ca8\u0ccd\u0ca8\u0ca1 (Kannada)",
    "ko": "\ud55c\uad6d\uc5b4 (Korean)",
    "ku": "Kurd\u00ee (Kurdish)",
    "la": "Latina (Latin)",
    "lb": "L\u00ebtzebuergesch (Luxembourgish)",
    "lt": "Lietuvi\u0173 (Lithuanian)",
    "lv": "Latvie\u0161u (Latvian)",
    "ml": "\u0d2e\u0d32\u0d2f\u0d3e\u0d33\u0d02 (Malayalam)",
    "mr": "\u092e\u0930\u093e\u0920\u0940 (Marathi)",
    "ms": "Bahasa Melayu (Malay)",
    "my": "\u1019\u103c\u1014\u103a\u1019\u102c (Myanmar / Burmese)",
    "ne": "\u0928\u0947\u092a\u093e\u0932\u0940 (Nepali)",
    "nl": "Nederlands (Dutch)",
    "no": "Norsk (Norwegian)",
    "pa": "\u0a2a\u0a70\u0a1c\u0a3e\u0a2c\u0a40 (Punjabi)",
    "pl": "Polski (Polish)",
    "pt": "Portugu\u00eas (Portuguese - Brazil)",
    "pt-PT": "Portugu\u00eas europeu (Portuguese - Portugal)",
    "ro": "Rom\u00e2n\u0103 (Romanian)",
    "ru": "\u0420\u0443\u0441\u0441\u043a\u0438\u0439 (Russian)",
    "si": "\u0dc3\u0dd2\u0d82\u0dc4\u0dbd (Sinhala)",
    "sk": "Sloven\u010dina (Slovak)",
    "sl": "Sloven\u0161\u010dina (Slovenian)",
    "sq": "Shqip (Albanian)",
    "sr": "\u0421\u0440\u043f\u0441\u043a\u0438 (Serbian)",
    "su": "Basa Sunda (Sundanese)",
    "sv": "Svenska (Swedish)",
    "sw": "Kiswahili (Swahili)",
    "ta": "\u0ba4\u0bae\u0bbf\u0bb4\u0bcd (Tamil)",
    "te": "\u0c24\u0c46\u0c32\u0c41\u0c17\u0c41 (Telugu)",
    "th": "\u0e44\u0e17\u0e22 (Thai)",
    "tl": "Tagalog (Filipino)",
    "tr": "T\u00fcrk\u00e7e (Turkish)",
    "uk": "\u0423\u043a\u0440\u0430\u0457\u043d\u0441\u044c\u043a\u0430 (Ukrainian)",
    "ur": "\u0627\u0631\u062f\u0648 (Urdu)",
    "vi": "Ti\u1ebfng Vi\u1ec7t (Vietnamese)",
    "yue": "\u7cb5\u8a9e (Cantonese)",
    "zh": "\u4e2d\u6587 (Chinese)",
    "zh-CN": "\u4e2d\u6587\uff08\u7b80\u4f53\uff09 (Chinese - Simplified)",
    "zh-TW": "\u4e2d\u6587\uff08\u7e41\u9ad4\uff09 (Chinese - Traditional)",
}

# Default subset of languages that are enabled on a fresh install / when
# the admin clears the selector to an empty string.  We keep English and
# Italian as the defaults because that is what the product historically
# supported, but admins can opt in to any subset of
# :data:`TTS_SUPPORTED_LANGUAGES`.
TTS_DEFAULT_ENABLED_LANGUAGES = ("en", "it")

# Fallback language used when both detection fails and the admin's enabled
# subset is empty.  English is the wiki's lingua franca.
TTS_FALLBACK_LANGUAGE = "en"

# Discrete playback / download speed presets surfaced to the user via the
# in-page speed selector.  Values are simple floats so they map cleanly onto
# ``HTMLMediaElement.playbackRate`` on the client and onto ffmpeg's
# ``atempo`` filter on the server.  Anything outside this set is rejected
# (or coerced to ``TTS_DEFAULT_PLAYBACK_SPEED``) so a malicious client can
# never make the server burn time generating an absurdly slow / fast file.
TTS_SPEED_PRESETS = (0.75, 1.0, 1.25, 1.5, 1.75, 2.0)

# Default playback speed used by the in-page player.  We pick something a
# little above 1.0 because generated narration is often more pleasant for
# listening: a gentle 1.25x makes the panel feel snappier without
# distorting the voice.  Users can still pick 1.0x (or slower / faster) via
# the selector.
TTS_DEFAULT_PLAYBACK_SPEED = 1.0

# Automatic resume budget for transient backend failures.  The first failed
# attempt is not counted here; this is the number of background resumes the
# worker may schedule before leaving the row as ``failed`` for admin review.
TTS_MAX_AUTO_RESUME_ATTEMPTS = 3
TTS_AUTO_RESUME_BASE_DELAY_SECONDS = 2.0
TTS_AUTO_RESUME_MAX_DELAY_SECONDS = 30.0

# Process-local throttling for background generation.  Automatic work is
# funneled through a tiny queue instead of spawning one synthesiser per page.
# Operators can raise the worker count for a faster machine/private backend,
# but the default is deliberately gentle.
TTS_DEFAULT_WORKER_COUNT = 1
TTS_MAX_WORKER_COUNT = 8
TTS_DEFAULT_INLINE_WORKER = False
TTS_DEFAULT_REAL_BACKEND_START_INTERVAL_SECONDS = 0.25
TTS_PENDING_RECOVERY_INTERVAL_SECONDS = 60.0
TTS_WORKER_DEFAULT_POLL_INTERVAL_SECONDS = 0.5
# How long an idle worker sleeps while the host has TTS switched off.
TTS_WORKER_DISABLED_POLL_INTERVAL_SECONDS = 60.0
TTS_RATE_LIMIT_COOLDOWN_SECONDS = 15 * 60.0
TTS_LANGDETECT_MIN_LATIN_WORDS = 12

# Piper is the local/offline default.  Voice models live outside the package
# so operators can swap in better voices without changing code.
TTS_DEFAULT_BACKEND = "piper"
TTS_DEFAULT_PIPER_VOICE_DIR = os.environ.get(
    "BW_TTS_PIPER_VOICE_DIR",
    os.path.join(
        os.environ.get("BW_INSTANCE_DIR", os.path.join(os.getcwd(), "instance")),
        "piper-voices",
    ),
)
TTS_DEFAULT_PIPER_AUTO_DOWNLOAD = True
TTS_DEFAULT_PIPER_OUTPUT_FORMAT = "auto"
TTS_DEFAULT_PIPER_LENGTH_SCALE = 1.0
TTS_DEFAULT_PIPER_NOISE_SCALE = 0.667
TTS_DEFAULT_PIPER_NOISE_W_SCALE = 0.8

# Good-enough default local neural voices.  These are all Piper voices that
# are local neural models; page text is never sent out for synthesis.  Piper
# does not ship a model for every historical BananaWiki language code, but
# admins can add any missing language via BW_TTS_PIPER_VOICE_MAP once they
# place a matching .onnx voice in the voice directory.  Regional codes are
# mapped to the closest local Piper model.
TTS_DEFAULT_PIPER_VOICE_MAP = {
    "ar": "ar_JO-kareem-medium",
    "bg": "bg_BG-dimitar-medium",
    "ca": "ca_ES-upc_ona-medium",
    "cs": "cs_CZ-jirka-medium",
    "cy": "cy_GB-bu_tts-medium",
    "da": "da_DK-talesyntese-medium",
    "de": "de_DE-thorsten-medium",
    "el": "el_GR-rapunzelina-medium",
    "en": "en_US-lessac-medium",
    "es": "es_ES-sharvard-medium",
    "eu": "eu_ES-maider-medium",
    "fa": "fa_IR-amir-medium",
    "fi": "fi_FI-harri-medium",
    "fr": "fr_FR-tom-medium",
    "fr-CA": "fr_FR-tom-medium",
    "hi": "hi_IN-pratham-medium",
    "hu": "hu_HU-anna-medium",
    "id": "id_ID-news_tts-medium",
    "is": "is_IS-salka-medium",
    "it": "it_IT-paola-medium",
    "ka": "ka_GE-natia-medium",
    "kk": "kk_KZ-issai-high",
    "ku": "ku_TR-berfin_renas-medium",
    "lb": "lb_LU-marylux-medium",
    "lv": "lv_LV-aivars-medium",
    "ml": "ml_IN-meera-medium",
    "ne": "ne_NP-google-medium",
    "nl": "nl_NL-mls-medium",
    "no": "no_NO-talesyntese-medium",
    "pl": "pl_PL-gosia-medium",
    "pt": "pt_BR-cadu-medium",
    "pt-PT": "pt_PT-tug\u00e3o-medium",
    "ro": "ro_RO-mihai-medium",
    "ru": "ru_RU-irina-medium",
    "sk": "sk_SK-lili-medium",
    "sl": "sl_SI-artur-medium",
    "sq": "sq_AL-edon-medium",
    "sr": "sr_RS-serbski_institut-medium",
    "sv": "sv_SE-lisa-medium",
    "sw": "sw_CD-lanfrica-medium",
    "te": "te_IN-maya-medium",
    "tr": "tr_TR-dfki-medium",
    "uk": "uk_UA-mykyta-high",
    "ur": "ur_PK-fasih-medium",
    "vi": "vi_VN-vais1000-medium",
    "zh": "zh_CN-huayan-medium",
    "zh-CN": "zh_CN-huayan-medium",
    "zh-TW": "zh_CN-huayan-medium",
    "yue": "zh_CN-huayan-medium",
}



# Drop fenced code blocks so generated narration stays focused on prose.
_FENCED_CODE_RE = re.compile(r"```(?:\w*\n?)?(.*?)```", re.DOTALL)
# Inline-code spans get replaced with their inner text without the backticks.
_INLINE_CODE_RE = re.compile(r"`([^`]*)`")
# Markdown image: ![alt](url) -- keep the alt text, drop the URL.
_MD_IMAGE_RE = re.compile(r"!\[([^\]]*)\]\([^)]*\)")
# Markdown link: [text](url) -- keep the text, drop the URL.
_MD_LINK_RE = re.compile(r"\[([^\]]+)\]\([^)]*\)")
# Reference-style link definitions: drop them entirely (they're metadata).
_MD_REF_DEF_RE = re.compile(r"^\s*\[[^\]]+\]:\s*\S+.*$", re.MULTILINE)
# Strip HTML tags (anything wiki Markdown couldn't catch).
_HTML_TAG_RE = re.compile(r"<[^>]+>")
# Heading / blockquote / list markers at the start of a line.  Handles both
# the common indented-with-spaces and tab-indented variants, plus optional
# task-list markers `[ ]` / `[x]`.
_LINE_PREFIX_RE = re.compile(
    r"^[ \t]*(?:#+|[>\-*+]|\d+[.)])\s+(?:\[[ xX]\]\s+)?",
    re.MULTILINE,
)
# Setext headings (`===` / `---` underlines) and horizontal rules.
_HR_RE = re.compile(
    r"^[ \t]*(?:[-*_]\s*){3,}\s*$",
    re.MULTILINE,
)
# Markdown table separators (e.g. `|---|---|`).
_TABLE_SEP_RE = re.compile(r"^[ \t]*\|?[ \t]*:?-{2,}:?(?:[ \t]*\|[ \t]*:?-{2,}:?)+[ \t]*\|?[ \t]*$", re.MULTILINE)
# Bold / italic / strikethrough markers: keep the inner text.
_EMPHASIS_RE = re.compile(r"(\*{1,3}|_{1,3}|~~)(.+?)\1")
# Stray punctuation runs that nothing should ever try to speak.  We replace
# them with a space so word boundaries don't collide.
_STRAY_PUNCT_RE = re.compile(r"[*_~`|]+")
# Repeated whitespace -> single space.
_WS_RE = re.compile(r"\s+")


def tts_normalize_text(title, content):
    """Return a plain-text rendering of *title* + *content* suitable for TTS.

    Strips Markdown formatting and HTML, normalises whitespace, and truncates
    to ``TTS_MAX_INPUT_CHARS``.  The output is what the engine actually
    synthesises (and what feeds into ``tts_content_hash``).
    """
    title = (title or "").strip()
    body = content or ""

    # Drop fenced code blocks; reading implementation details aloud makes
    # page narration noisy and can expose snippets that were meant as context.
    body = _FENCED_CODE_RE.sub(" ", body)
    # Drop reference-style link definitions (just URLs, no speakable text).
    body = _MD_REF_DEF_RE.sub(" ", body)
    # Replace images / links with their visible text.
    body = _MD_IMAGE_RE.sub(r"\1", body)
    body = _MD_LINK_RE.sub(r"\1", body)
    # Inline-code -> plain text.
    body = _INLINE_CODE_RE.sub(r"\1", body)
    # Strip Markdown emphasis markers.
    body = _EMPHASIS_RE.sub(r"\2", body)
    # Strip Markdown table separator rows so TTS doesn't read "dash dash".
    body = _TABLE_SEP_RE.sub(" ", body)
    # Strip horizontal rules / setext underlines.
    body = _HR_RE.sub(" ", body)
    # Strip leading heading / list / task markers.
    body = _LINE_PREFIX_RE.sub("", body)
    # Strip any HTML tags that survived.
    body = _HTML_TAG_RE.sub(" ", body)
    # Convert table pipes into spaces; whatever bold/italic markers leaked
    # through (e.g. unbalanced ``*``) get replaced with a space too so the
    # synthesiser never tries to vocalise punctuation.
    body = _STRAY_PUNCT_RE.sub(" ", body)
    # Normalise unicode (so e.g. "café" stays as one token, not c+a+f+e+combining
    # acute) and collapse whitespace.
    body = unicodedata.normalize("NFC", body)
    body = _WS_RE.sub(" ", body).strip()

    if title:
        spoken = f"{title}. {body}" if body else title
    else:
        spoken = body

    if len(spoken) > TTS_MAX_INPUT_CHARS:
        spoken = spoken[:TTS_MAX_INPUT_CHARS]
    return spoken


def tts_content_hash_for_text(spoken_text, language):
    """Return the synthesis cache hash for already-normalised page text."""
    payload = (spoken_text or "") + "\x00" + language
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def tts_content_hash(title, content, language):
    """Return a stable SHA-256 hex digest of the synthesis input.

    Combines the normalised title/body with the target *language* so that
    re-requesting the same page in a different language does not silently
    serve stale audio.
    """
    return tts_content_hash_for_text(
        tts_normalize_text(title, content), language,
    )


# Language detection runs in two tiers, because the good detector is optional:
#
#  1. ``langdetect`` (third-party, pure-Python) is the primary detector.
#     It supports ~55 languages with good precision on prose of >= ~10 words.
#     Imported lazily so the module loads cleanly when langdetect is not
#     installed (e.g. minimal CI envs).  Its codes are mapped onto
#     BananaWiki's stable TTS language code space.
#
#  2. A tiny hand-rolled English/Italian stop-word heuristic is used as a
#     fallback when langdetect is missing, raises (e.g. on inputs with no
#     detectable features), or returns a code that is not enabled by the
#     admin and we still need to pick *something* sensible.  This keeps the
#     historical EN/IT behaviour intact when the optional dep is absent.

# Small disjoint stop-word sets used to pick between EN and IT in the
# fallback path.  We deliberately avoid words that occur in both languages
# (e.g. "no", "non", "in") to keep the score signal-rich.
_IT_STOPWORDS = frozenset({
    "il", "lo", "la", "i", "gli", "le",
    "un", "uno", "una",
    "di", "del", "dello", "della", "dei", "degli", "delle",
    "al", "allo", "alla", "ai", "agli", "alle",
    "dal", "dallo", "dalla", "dai", "dagli", "dalle",
    "sul", "sullo", "sulla", "sui", "sugli", "sulle",
    "nel", "nello", "nella", "nei", "negli", "nelle",
    "col", "coi",
    "che", "chi", "cui", "questa", "questo", "questi", "queste",
    "quella", "quello", "quelli", "quelle",
    "sono", "siamo", "siete", "essere", "stato", "stata",
    "ho", "hai", "abbiamo", "avete", "hanno", "avere",
    "per", "con", "senza", "tra", "fra", "anche", "ma", "perch\u00e9",
    "come", "quando", "dove", "quindi", "allora", "sempre", "mai",
    "ogni", "oppure", "mentre", "per\u00f2", "gi\u00e0", "pi\u00f9", "cos\u00ec",
    "molto", "poco", "tutto", "tutti", "tutte", "niente", "qualcosa",
    "su", "gi\u00f9", "qua", "qui", "l\u00ec", "l\u00e0",
    "anno", "anni", "giorno", "giorni",
})

_EN_STOPWORDS = frozenset({
    "the", "a", "an",
    "and", "or", "but", "nor", "yet", "so",
    "of", "to", "in", "on", "at", "by", "for", "with", "from", "into",
    "is", "are", "was", "were", "be", "been", "being", "am",
    "has", "have", "had", "having",
    "do", "does", "did", "doing",
    "will", "would", "shall", "should", "can", "could", "may", "might",
    "this", "that", "these", "those",
    "it", "its", "itself", "they", "them", "their", "theirs", "themselves",
    "you", "your", "yours", "yourself", "yourselves",
    "we", "our", "ours", "ourselves",
    "he", "she", "her", "him", "his", "hers", "himself", "herself",
    "what", "which", "who", "whom", "whose", "where", "when", "why", "how",
    "because", "while", "however", "therefore", "although", "though",
    "also", "than", "then", "thus", "here", "there", "today",
    "already", "always", "never", "nothing", "something", "everything",
})

# Italian-only letters and digraphs we can weight when stop-words tie.
_IT_CHAR_RE = re.compile(r"[\u00e0\u00e8\u00e9\u00ec\u00f2\u00f9\u00ee\u00ea\u00eb]", re.IGNORECASE)
_IT_DIGRAPH_RE = re.compile(r"\b(?:gli|gn|sci|sce|\u017a)", re.IGNORECASE)
# Words must be 2+ letters; numbers and punctuation are ignored.
_WORD_RE = re.compile(r"[A-Za-z\u00c0-\u017f]{2,}")

# Mapping from langdetect's ISO 639-1 codes to BananaWiki's stable TTS
# language codes.  Codes that already match (the vast majority) are omitted;
# the helper below falls through to the original value.
_LANGDETECT_TO_TTS = {
    "he": "iw",       # langdetect uses the modern ISO 639-1 code
    "jv": "jw",       # BananaWiki preserves the historical Javanese code
    "zh-cn": "zh-CN",
    "zh-tw": "zh-TW",
    "pt-br": "pt",    # langdetect doesn't actually emit this, but we accept it
}


def _heuristic_en_it(text, default):
    """Fallback EN/IT stop-word + diacritic heuristic.

    Used when langdetect is unavailable / raises, and when the langdetect
    result is not in the admin's enabled subset.  Returns one of
    ``"en"`` / ``"it"`` / *default*.
    """
    if not text:
        return default
    sample = text[:20_000].lower()
    words = _WORD_RE.findall(sample)
    if len(words) < 5:
        return default

    it_score = 0
    en_score = 0
    for w in words:
        if w in _IT_STOPWORDS:
            it_score += 1
        if w in _EN_STOPWORDS:
            en_score += 1

    it_score += len(_IT_CHAR_RE.findall(sample))
    it_score += min(len(_IT_DIGRAPH_RE.findall(sample)), 20)

    if it_score == 0 and en_score == 0:
        return default
    if it_score == en_score:
        return default
    return "it" if it_score > en_score else "en"


def _langdetect_to_tts(code):
    """Translate a langdetect code into a supported TTS language code.

    Codes that are not in the supported catalogue (after explicit mapping)
    return ``None`` so callers know to fall back.
    """
    if not code:
        return None
    lower = str(code).strip().lower()
    mapped = _LANGDETECT_TO_TTS.get(lower, lower)
    if mapped in TTS_SUPPORTED_LANGUAGE_SET:
        return mapped
    # Try the language family alone (e.g. ``pt-PT`` -> ``pt``) for codes
    # that langdetect doesn't emit but a future caller might pass through.
    if "-" in mapped:
        family = mapped.split("-", 1)[0]
        if family in TTS_SUPPORTED_LANGUAGE_SET:
            return family
    return None


def _has_non_latin_letter(text):
    """Return True when *text* contains letters outside Latin scripts."""
    for ch in text or "":
        if not unicodedata.category(ch).startswith("L"):
            continue
        if "A" <= ch <= "Z" or "a" <= ch <= "z":
            continue
        if "\u00c0" <= ch <= "\u017f":
            continue
        return True
    return False


def _langdetect_detect(sample):
    """Run langdetect on *sample* and return its raw code, or ``None``.

    The import is lazy so :mod:`helpers._tts` keeps working when langdetect
    is not installed.  ``DetectorFactory.seed`` is set once per process so
    detections become deterministic, which matters for tests and for the
    cache hash.
    """
    try:
        from langdetect import detect, DetectorFactory  # type: ignore[import-not-found]
        from langdetect.lang_detect_exception import LangDetectException  # type: ignore[import-not-found]
    except ImportError:
        return None
    DetectorFactory.seed = 0  # deterministic across processes
    try:
        return detect(sample)
    except LangDetectException:
        return None
    except Exception:  # noqa: BLE001 (never let detection break a page save)
        _logger.warning("langdetect raised unexpectedly", exc_info=True)
        return None


def detect_tts_language(text, default="en", *, allowed=None):
    """Return the best-guess TTS language code for *text*.

    Uses ``langdetect`` when available, falling back to a small EN/IT
    stop-word heuristic.  Very short Latin-script snippets skip langdetect
    because imported asset pages often contain only filenames / titles, which
    otherwise produce confident-looking random language guesses.

    When *allowed* is provided (an iterable of TTS language codes), the returned
    value is guaranteed to be inside that set.  The selection order is:

    1. langdetect result, if enabled;
    2. heuristic EN/IT result, if enabled;
    3. *default*, if enabled;
    4. ``"en"``, if enabled;
    5. the first element of *allowed* (sorted).

    This means "enabling Spanish" and saving a Spanish page does the right
    thing (langdetect picks ``es``, ``es`` is enabled, we're done), but if
    only Italian + French are enabled and a reader writes Korean, we fall
    back to Italian (the admin's first preference) rather than synthesizing
    Korean text with an Italian voice silently breaking detection later.
    """
    allowed_set = (
        frozenset(allowed) if allowed is not None
        else TTS_SUPPORTED_LANGUAGE_SET
    )
    if not text:
        return _pick_allowed_fallback(default, allowed_set)

    sample = text[:20_000]
    words = _WORD_RE.findall(sample.lower())

    if (
        len(words) < TTS_LANGDETECT_MIN_LATIN_WORDS
        and not _has_non_latin_letter(sample)
    ):
        heuristic = _heuristic_en_it(sample, default=default)
        if heuristic in allowed_set:
            return heuristic
        return _pick_allowed_fallback(default, allowed_set)

    # Try langdetect first: it handles long Latin prose and non-Latin
    # scripts (Cyrillic, CJK, Arabic, Devanagari, Greek, Thai, etc.).
    raw = _langdetect_detect(sample)
    mapped = _langdetect_to_tts(raw)
    if mapped and mapped in allowed_set:
        return mapped

    # langdetect failed or returned a disabled language.  For short
    # Latin-script text try the EN/IT stop-word heuristic.
    if len(words) < 5:
        return _pick_allowed_fallback(default, allowed_set)

    heuristic = _heuristic_en_it(sample, default=default)
    if heuristic in allowed_set:
        return heuristic

    if mapped and mapped not in allowed_set:
        _logger.debug(
            "TTS: detected language %r is not in enabled set, falling back",
            mapped,
        )
    return _pick_allowed_fallback(default, allowed_set)


def _pick_allowed_fallback(default, allowed_set):
    """Return the best fallback TTS language code given an *allowed_set*.

    Preference order: *default* if allowed, then ``"en"``, then the first
    code in ``allowed_set`` sorted lexicographically.  Returns ``"en"``
    when *allowed_set* is empty so the caller always gets something usable.
    """
    if default in allowed_set:
        return default
    if TTS_FALLBACK_LANGUAGE in allowed_set:
        return TTS_FALLBACK_LANGUAGE
    if allowed_set:
        return sorted(allowed_set)[0]
    return TTS_FALLBACK_LANGUAGE


def parse_enabled_languages(value):
    """Return the tuple of enabled TTS language codes parsed from *value*.

    *value* is the on-disk representation of the
    ``tts_enabled_languages`` site setting: a comma-separated string of
    TTS language codes.  Unknown codes are silently dropped (defence-in-depth
    against a hand-edited DB row).  When *value* is empty or no valid
    codes remain, the historical default (``en, it``) is used so the TTS
    plugin keeps working on upgrades / mis-configured sites.

    The result preserves the *catalogue order* (i.e. matches
    :data:`TTS_SUPPORTED_LANGUAGES`) rather than the order in *value*, so
    rendering is stable regardless of how the admin saved their picks.
    """
    if not value:
        return tuple(TTS_DEFAULT_ENABLED_LANGUAGES)
    if isinstance(value, (list, tuple, set, frozenset)):
        raw_codes = [str(c).strip() for c in value]
    else:
        raw_codes = [c.strip() for c in str(value).split(",")]
    seen = set()
    for c in raw_codes:
        if c and c in TTS_SUPPORTED_LANGUAGE_SET:
            seen.add(c)
    if not seen:
        return tuple(TTS_DEFAULT_ENABLED_LANGUAGES)
    return tuple(code for code in TTS_SUPPORTED_LANGUAGES if code in seen)


def serialize_enabled_languages(codes):
    """Return the on-disk CSV representation for an iterable of *codes*.

    Unknown codes are filtered out and the result is sorted by catalogue
    order so two admins enabling the same subset end up with byte-identical
    rows in the DB (handy for diffs / change auditing).  When *codes* is
    empty or all-unknown, the function returns the empty string, which the
    parser later interprets as "use the default".
    """
    valid = {c for c in codes if c in TTS_SUPPORTED_LANGUAGE_SET}
    return ",".join(code for code in TTS_SUPPORTED_LANGUAGES if code in valid)


def get_enabled_tts_languages(settings=None):
    """Return the tuple of all supported TTS language codes.

    All languages in :data:`TTS_SUPPORTED_LANGUAGES` are always available.
    There is no per-language enable/disable mechanism.  The *settings*
    parameter is kept for backward compatibility but is ignored.
    """
    return TTS_SUPPORTED_LANGUAGES


def normalize_tts_speed(value, default=None):
    """Coerce *value* to one of :data:`TTS_SPEED_PRESETS`.

    The selector lives on the client so users could in theory request an
    arbitrary value, but we only honour the preset speeds: anything else
    snaps to *default* (which defaults to ``TTS_DEFAULT_PLAYBACK_SPEED``).
    The return value is always a ``float`` rounded to two decimals.
    """
    if default is None:
        default = TTS_DEFAULT_PLAYBACK_SPEED
    if value is None:
        return float(default)
    try:
        # Tolerate both numeric and string inputs, including locale-style
        # commas (e.g. "1,25") because the selector emits a string and the
        # download URL embeds it as a query param.
        val = float(str(value).strip().replace(",", "."))
    except (TypeError, ValueError):
        return float(default)
    # Snap to the nearest preset to forgive minor floating-point drift
    # (e.g. ``1.2500001``) while still rejecting clearly out-of-range
    # inputs.  We accept anything within 0.05 of a preset.
    for preset in TTS_SPEED_PRESETS:
        if abs(val - preset) < 0.05:
            return float(preset)
    return float(default)


def _ffmpeg_binary():
    """Return the absolute path to ``ffmpeg`` or ``None`` when missing.

    Allows ``BW_TTS_FFMPEG`` to override the lookup so admins can point at
    a custom build (e.g. a static binary in ``/opt/bin``) without polluting
    ``PATH``.
    """
    override = os.environ.get("BW_TTS_FFMPEG", "").strip()
    if override:
        if os.path.isabs(override) and os.path.isfile(override) and os.access(override, os.X_OK):
            return override
        resolved = shutil.which(override)
        if resolved:
            return resolved
        # Override was set but unusable: fall through to PATH lookup so we
        # still get a working binary when one is available.
    return shutil.which("ffmpeg")


def ffmpeg_speed_available():
    """Return ``True`` when the server has a usable ffmpeg for atempo."""
    return _ffmpeg_binary() is not None


def write_speed_adjusted_mp3(src_path, dest_path, speed):
    """Write a tempo-shifted copy of *src_path* to *dest_path* at *speed*.

    Uses ffmpeg's ``atempo`` filter, which preserves pitch (so the voice
    sounds natural, just faster / slower).  ``atempo`` itself only accepts
    factors in ``[0.5, 100.0]``; the preset range comfortably sits inside
    that so a single filter invocation always works.

    Raises ``RuntimeError`` when ffmpeg is missing or the subprocess
    fails.  Callers should treat this as a recoverable error and fall back
    to serving the original file at 1.0x.
    """
    binary = _ffmpeg_binary()
    if not binary:
        raise RuntimeError("ffmpeg is not available; cannot adjust TTS speed.")
    factor = float(speed)
    if not (0.5 <= factor <= 2.0):
        raise RuntimeError(f"Unsupported TTS speed factor: {factor!r}")
    cmd = [
        binary, "-y",
        "-loglevel", "error",
        "-i", src_path,
        "-filter:a", f"atempo={factor:.4f}",
        "-vn",
        "-acodec", "libmp3lame",
        "-q:a", "4",
        dest_path,
    ]
    try:
        result = subprocess.run(
            cmd,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=60,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError(f"ffmpeg invocation failed: {exc}") from exc
    if result.returncode != 0 or not os.path.isfile(dest_path) or os.path.getsize(dest_path) == 0:
        stderr = (result.stderr or b"").decode("utf-8", errors="replace").strip()
        # Don't leak the full transcript into the user-facing error; keep a
        # one-line summary for logs.
        _logger.warning("ffmpeg atempo failed (rc=%s): %s", result.returncode, stderr[:400])
        raise RuntimeError("ffmpeg failed to write the speed-adjusted MP3.")


def normalize_tts_language(value, default="en", *, allow_auto=True,
                           allowed=None):
    """Coerce a raw language input to a supported TTS language code.

    ``"auto"`` (case-insensitive) is preserved when *allow_auto* is true so
    the caller can decide whether to run the detector.  Any other value is
    matched against :data:`TTS_SUPPORTED_LANGUAGES` (case-insensitive for
    the language portion; the regional suffix is preserved in canonical form,
    e.g. ``zh-CN``).  When the value is
    missing from the catalogue or is excluded by *allowed*, we fall back to
    *default*.

    *allowed* (when provided) is an iterable of TTS language codes the admin has
    enabled.  This lets routes reject "please synthesise this in Korean"
    when Korean is currently disabled, even though Korean *is* in the
    underlying language catalogue.

    Pre-existing callers that pass ``default="en"`` keep their original
    behaviour as long as English is enabled (which is the install default).
    """
    allowed_set = (
        frozenset(allowed) if allowed is not None
        else TTS_SUPPORTED_LANGUAGE_SET
    )
    if value is None:
        return _pick_allowed_fallback(default, allowed_set)
    val = str(value).strip()
    if not val:
        return _pick_allowed_fallback(default, allowed_set)
    if allow_auto and val.lower() == "auto":
        return "auto"

    # Try the canonical form first.  We normalise the language part to lower
    # and the regional suffix to upper before lookup.
    if "-" in val:
        lang, region = val.split("-", 1)
        canonical = f"{lang.lower()}-{region.upper()}"
    else:
        canonical = val.lower()
    if canonical in TTS_SUPPORTED_LANGUAGE_SET and canonical in allowed_set:
        return canonical

    # Family fallback: ``zh-CN`` -> ``zh`` if the regional variant is not
    # enabled but the base language is.
    if "-" in canonical:
        family = canonical.split("-", 1)[0]
        if family in TTS_SUPPORTED_LANGUAGE_SET and family in allowed_set:
            return family

    # English-name synonyms (``"italian"`` -> ``"it"``, etc.) for legacy
    # callers that pass human-readable names rather than ISO codes.  We
    # consult :data:`TTS_LANGUAGE_NAME_SYNONYMS` first because it is a
    # small explicit table, then fall back to the trailing-parenthetical
    # English gloss inside :data:`TTS_LANGUAGE_LABELS` (e.g. ``"Italian
    # (it)"`` style entries).
    lowered = canonical.replace("-", " ").replace("_", " ")
    code = TTS_LANGUAGE_NAME_SYNONYMS.get(lowered)
    if code and code in TTS_SUPPORTED_LANGUAGE_SET and code in allowed_set:
        return code

    return _pick_allowed_fallback(default, allowed_set)


# Lower-cased English name -> TTS language code.  Covers the common spellings
# editors are most likely to type by hand; the canonical ISO code path
# above handles everything else.  Order matters only for documentation;
# the lookup itself is O(1).
TTS_LANGUAGE_NAME_SYNONYMS = {
    "afrikaans": "af",
    "amharic": "am",
    "arabic": "ar",
    "bulgarian": "bg",
    "bengali": "bn",
    "bosnian": "bs",
    "catalan": "ca",
    "czech": "cs",
    "welsh": "cy",
    "danish": "da",
    "german": "de",
    "greek": "el",
    "english": "en",
    "spanish": "es",
    "estonian": "et",
    "basque": "eu",
    "persian": "fa",
    "farsi": "fa",
    "finnish": "fi",
    "french": "fr",
    "french canadian": "fr-CA",
    "canadian french": "fr-CA",
    "galician": "gl",
    "gujarati": "gu",
    "hausa": "ha",
    "hindi": "hi",
    "croatian": "hr",
    "hungarian": "hu",
    "indonesian": "id",
    "icelandic": "is",
    "italian": "it",
    "hebrew": "iw",
    "japanese": "ja",
    "javanese": "jw",
    "georgian": "ka",
    "kazakh": "kk",
    "khmer": "km",
    "kannada": "kn",
    "korean": "ko",
    "kurdish": "ku",
    "latin": "la",
    "luxembourgish": "lb",
    "lithuanian": "lt",
    "latvian": "lv",
    "malayalam": "ml",
    "marathi": "mr",
    "malay": "ms",
    "burmese": "my",
    "nepali": "ne",
    "dutch": "nl",
    "norwegian": "no",
    "punjabi": "pa",
    "polish": "pl",
    "portuguese": "pt",
    "portuguese brazilian": "pt",
    "brazilian portuguese": "pt",
    "portuguese european": "pt-PT",
    "european portuguese": "pt-PT",
    "romanian": "ro",
    "russian": "ru",
    "sinhala": "si",
    "slovak": "sk",
    "slovenian": "sl",
    "albanian": "sq",
    "serbian": "sr",
    "sundanese": "su",
    "swedish": "sv",
    "swahili": "sw",
    "tamil": "ta",
    "telugu": "te",
    "thai": "th",
    "filipino": "tl",
    "tagalog": "tl",
    "turkish": "tr",
    "ukrainian": "uk",
    "urdu": "ur",
    "vietnamese": "vi",
    "cantonese": "yue",
    "chinese": "zh",
    "mandarin": "zh",
    "chinese simplified": "zh-CN",
    "simplified chinese": "zh-CN",
    "chinese traditional": "zh-TW",
    "traditional chinese": "zh-TW",
}


# Everything below picks and drives a backend.  Each engine helper must
# tolerate being called from a background thread with no request context.

def _safe_get_site_settings():
    """Return site_settings dict, or {} if unavailable (e.g. background threads)."""
    try:
        settings = db.get_site_settings()
        return settings if isinstance(settings, dict) else {}
    except Exception:
        return {}


def _selected_backend():
    """Return the configured backend identifier (``"piper"`` / ``"stub"`` / ``"remote-gpu"``).

    Resolution order:
    1. ``BW_TTS_BACKEND`` env var (legacy, always wins if set).
    2. On a hosted wiki: ``"remote-gpu"`` when the host injected both
       ``BW_TTS_REMOTE_GPU_URL`` and ``BW_TTS_REMOTE_GPU_AUTH_TOKEN``,
       otherwise ``"piper"``.  The tenant's ``tts_gpu_*`` columns are never
       read there (see :func:`remote_gpu_settings_managed_by_host`).
    3. ``tts_gpu_enabled`` in site_settings → ``"remote-gpu"``.
    4. Default: ``"piper"``.
    """
    raw = os.environ.get("BW_TTS_BACKEND", "").strip().lower()
    if raw:
        if raw in {"local", "offline"}:
            raw = "piper"
        if raw in {"piper", "stub", "remote-gpu"}:
            return raw
        return TTS_DEFAULT_BACKEND

    if remote_gpu_settings_managed_by_host():
        url, token, _ = _remote_gpu_config()
        return "remote-gpu" if url and token else TTS_DEFAULT_BACKEND

    settings = _safe_get_site_settings()
    if settings.get("tts_gpu_enabled"):
        url = (settings.get("tts_gpu_url") or "").strip()
        if url:
            return "remote-gpu"

    return TTS_DEFAULT_BACKEND


def _truthy_env(name, default=False):
    """Return a boolean from a common env var spelling."""
    raw = os.environ.get(name)
    if raw is None:
        return bool(default)
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def _inline_tts_worker_enabled():
    """Return True when this process should synthesize queued rows itself."""
    return _truthy_env("BW_TTS_INLINE_WORKER", TTS_DEFAULT_INLINE_WORKER)


def inline_tts_worker_enabled():
    """Public wrapper for the inline-worker runtime flag."""
    return _inline_tts_worker_enabled()


def _configured_piper_voice_dir():
    """Return the directory containing local Piper .onnx voice files."""
    return os.environ.get(
        "BW_TTS_PIPER_VOICE_DIR",
        TTS_DEFAULT_PIPER_VOICE_DIR,
    )


def _configured_piper_output_format():
    """Return the desired Piper cache format: ``wav``, ``mp3``, or ``auto``."""
    raw = os.environ.get(
        "BW_TTS_PIPER_OUTPUT_FORMAT",
        TTS_DEFAULT_PIPER_OUTPUT_FORMAT,
    ).strip().lower()
    if raw not in {"auto", "wav", "mp3"}:
        return TTS_DEFAULT_PIPER_OUTPUT_FORMAT
    return raw


def _configured_piper_float(name, default):
    """Parse one Piper synthesis float option from the environment."""
    raw = os.environ.get(name)
    try:
        value = float(raw) if raw is not None else float(default)
    except (TypeError, ValueError):
        value = float(default)
    return value


def _piper_auto_download_enabled():
    """Return True when missing Piper voices may be downloaded during setup/use."""
    return _truthy_env(
        "BW_TTS_PIPER_AUTO_DOWNLOAD",
        TTS_DEFAULT_PIPER_AUTO_DOWNLOAD,
    )


def _parse_piper_voice_map_override(value):
    """Parse BW_TTS_PIPER_VOICE_MAP from JSON or ``code=voice,code=voice``."""
    if not value:
        return {}
    raw = str(value).strip()
    if not raw:
        return {}
    parsed = None
    if raw.startswith("{"):
        try:
            parsed = json.loads(raw)
        except (TypeError, ValueError):
            parsed = None
    if parsed is None:
        parsed = {}
        for part in raw.split(","):
            if "=" not in part:
                continue
            key, voice = part.split("=", 1)
            key = key.strip()
            voice = voice.strip()
            if key and voice:
                parsed[key] = voice
    result = {}
    for key, voice in getattr(parsed, "items", lambda: [])():
        code = normalize_tts_language(
            key, default=TTS_FALLBACK_LANGUAGE, allow_auto=False,
        )
        voice = str(voice).strip()
        if code in TTS_SUPPORTED_LANGUAGE_SET and voice:
            result[code] = voice
    return result


_PIPER_VOICE_MAP_CACHE_LOCK = threading.Lock()
_PIPER_VOICE_MAP_CACHE_KEY = None
_PIPER_VOICE_MAP_CACHE = None


def _configured_piper_voice_map():
    """Return the effective language-code -> local Piper voice mapping."""
    global _PIPER_VOICE_MAP_CACHE_KEY, _PIPER_VOICE_MAP_CACHE
    override = os.environ.get("BW_TTS_PIPER_VOICE_MAP", "")
    with _PIPER_VOICE_MAP_CACHE_LOCK:
        if (
            _PIPER_VOICE_MAP_CACHE is None
            or _PIPER_VOICE_MAP_CACHE_KEY != override
        ):
            mapping = dict(TTS_DEFAULT_PIPER_VOICE_MAP)
            mapping.update(_parse_piper_voice_map_override(override))
            _PIPER_VOICE_MAP_CACHE_KEY = override
            _PIPER_VOICE_MAP_CACHE = mapping
        return dict(_PIPER_VOICE_MAP_CACHE)


def _piper_voice_name_from_map(language, mapping):
    """Return the matching Piper voice name from *mapping*, or ``None``."""
    candidates = [language]
    if "-" in language:
        candidates.append(language.split("-", 1)[0])
    for candidate in candidates:
        voice = mapping.get(candidate)
        if voice:
            return voice
    return None


def _resolve_piper_voice_name(language):
    """Return the configured Piper voice/model identifier for *language*."""
    voice = _piper_voice_name_from_map(language, _configured_piper_voice_map())
    if voice:
        return voice
    raise RuntimeError(
        "No local Piper voice configured for TTS language "
        f"{language!r}. Add BW_TTS_PIPER_VOICE_MAP or choose a language "
        "with a local Piper voice model."
    )


def piper_voice_configured_for_language(language):
    """Return True when the local Piper backend has a voice mapping for *language*."""
    return bool(
        _piper_voice_name_from_map(language, _configured_piper_voice_map())
    )


def piper_voice_availability_map(languages=None):
    """Return ``{language_code: has_local_voice_mapping}`` efficiently."""
    mapping = _configured_piper_voice_map()
    codes = languages or TTS_SUPPORTED_LANGUAGES
    return {
        code: bool(_piper_voice_name_from_map(code, mapping))
        for code in codes
    }


def _piper_voice_paths(language):
    """Resolve local model/config paths for *language*."""
    voice = _resolve_piper_voice_name(language)
    if voice.endswith(".onnx") or os.path.isabs(voice):
        model_path = voice
    else:
        model_path = os.path.join(_configured_piper_voice_dir(), f"{voice}.onnx")
    config_path = f"{model_path}.json"
    return voice, model_path, config_path


_PIPER_DOWNLOAD_LOCK = threading.Lock()
_PIPER_VOICE_CACHE_LOCK = threading.Lock()
_PIPER_VOICE_CACHE = {}


def _ensure_piper_voice_files(language):
    """Ensure the Piper model/config files exist locally for *language*."""
    voice, model_path, config_path = _piper_voice_paths(language)
    if os.path.isfile(model_path) and os.path.isfile(config_path):
        return voice, model_path, config_path

    if voice.endswith(".onnx") or os.path.isabs(voice):
        raise RuntimeError(
            "Local Piper voice files are missing for TTS language "
            f"{language!r}: expected {model_path!r} and {config_path!r}."
        )

    if not _piper_auto_download_enabled():
        raise RuntimeError(
            "Local Piper voice files are missing for TTS language "
            f"{language!r}. Run `python -m piper.download_voices "
            f"--download-dir {_configured_piper_voice_dir()} {voice}` or "
            "enable BW_TTS_PIPER_AUTO_DOWNLOAD=1."
        )

    os.makedirs(_configured_piper_voice_dir(), exist_ok=True)
    with _PIPER_DOWNLOAD_LOCK:
        if os.path.isfile(model_path) and os.path.isfile(config_path):
            return voice, model_path, config_path
        try:
            from piper.download_voices import download_voice
        except ImportError as exc:  # pragma: no cover - package missing in production
            raise RuntimeError(
                "Piper TTS is not installed. Install `piper-tts` or set "
                "BW_TTS_BACKEND=stub for tests."
            ) from exc
        try:
            download_voice(voice, Path(_configured_piper_voice_dir()).resolve())
        except OSError as exc:
            # Catch DNS / network failures from urllib inside download_voice
            # (e.g. "[Errno -3] Temporary failure in name resolution") and
            # surface a clear, actionable error rather than a raw socket trace.
            # URLError stores the real reason in .reason; fall back to str(exc)
            # only when the attribute is absent (plain OSError).
            reason = getattr(exc, "reason", None) or exc
            raise RuntimeError(
                f"Cannot download Piper voice model {voice!r}: {reason}. "
                "Pre-install the voice files locally or set "
                "BW_TTS_PIPER_AUTO_DOWNLOAD=0 and add the .onnx model "
                "files to the configured voice directory."
            ) from exc

    if not os.path.isfile(model_path) or not os.path.isfile(config_path):
        raise RuntimeError(
            "Piper voice download did not create the expected files for "
            f"{voice!r}."
        )
    return voice, model_path, config_path


def _load_piper_voice(language):
    """Load/cache a PiperVoice for *language*."""
    voice_name, model_path, config_path = _ensure_piper_voice_files(language)
    key = (os.path.realpath(model_path), os.path.realpath(config_path))
    with _PIPER_VOICE_CACHE_LOCK:
        cached = _PIPER_VOICE_CACHE.get(key)
        if cached is not None:
            return cached
        try:
            from piper import PiperVoice
        except ImportError as exc:  # pragma: no cover - package missing in production
            raise RuntimeError(
                "Piper TTS is not installed. Install `piper-tts` or set "
                "BW_TTS_BACKEND=stub for tests."
            ) from exc
        voice = PiperVoice.load(
            model_path,
            config_path=config_path,
            download_dir=_configured_piper_voice_dir(),
        )
        _PIPER_VOICE_CACHE[key] = voice
        _logger.info("Loaded local Piper voice %s for %s", voice_name, language)
        return voice


@lru_cache(maxsize=16)
def _cached_piper_synthesis_config(length_scale, noise_scale, noise_w_scale):
    """Return a cached Piper synthesis config for the given voice settings."""
    try:
        from piper import SynthesisConfig
    except ImportError as exc:  # pragma: no cover - package missing in production
        raise RuntimeError(
            "Piper TTS is not installed. Install `piper-tts` or set "
            "BW_TTS_BACKEND=stub for tests."
        ) from exc
    return SynthesisConfig(
        length_scale=length_scale,
        noise_scale=noise_scale,
        noise_w_scale=noise_w_scale,
    )


def _piper_synthesis_config():
    """Return Piper's optional synthesis config object when available.

    Resolution order:
    1. ``BW_TTS_PERFORMANCE_MODE`` env var (lets easy deployment and
       containers set a policy without touching site settings).
    2. ``tts_performance_mode`` site setting (admin UI).
    3. ``"auto"`` (hardware-detected).

    In ``auto`` mode the system's CPU cores and available memory are
    checked once at startup and the fastest viable quality preset is
    selected.  ``fast`` and ``balanced`` can still be forced explicitly.
    """
    settings = _safe_get_site_settings()
    perf_mode = os.environ.get("BW_TTS_PERFORMANCE_MODE") or ""
    if not perf_mode:
        perf_mode = (settings.get("tts_performance_mode") or "auto").strip().lower()
    if perf_mode == "fast":
        return _cached_piper_synthesis_config(
            _configured_piper_float("BW_TTS_PIPER_LENGTH_SCALE", 1.0),
            _configured_piper_float("BW_TTS_PIPER_NOISE_SCALE", 0.333),
            _configured_piper_float("BW_TTS_PIPER_NOISE_W_SCALE", 0.55),
        )
    if perf_mode == "balanced":
        return _cached_piper_synthesis_config(
            _configured_piper_float("BW_TTS_PIPER_LENGTH_SCALE", TTS_DEFAULT_PIPER_LENGTH_SCALE),
            _configured_piper_float("BW_TTS_PIPER_NOISE_SCALE", TTS_DEFAULT_PIPER_NOISE_SCALE),
            _configured_piper_float("BW_TTS_PIPER_NOISE_W_SCALE", TTS_DEFAULT_PIPER_NOISE_W_SCALE),
        )
    # auto: detect hardware and pick the fastest viable preset
    return _cached_piper_synthesis_config(
        _configured_piper_float("BW_TTS_PIPER_LENGTH_SCALE", 1.0),
        _configured_piper_float("BW_TTS_PIPER_NOISE_SCALE", _auto_noise_scale()),
        _configured_piper_float("BW_TTS_PIPER_NOISE_W_SCALE", _auto_noise_w_scale()),
    )


@lru_cache(maxsize=1)
def _detect_system_specs():
    """Return (cpu_count, memory_mb) once per process."""
    import multiprocessing
    cpus = multiprocessing.cpu_count() or 1
    mem_mb = 0
    try:
        import psutil
        mem_mb = psutil.virtual_memory().total / (1024 * 1024)
    except Exception:
        try:
            with open("/proc/meminfo") as f:
                for line in f:
                    if line.startswith("MemTotal:"):
                        mem_mb = int(line.split()[1]) / 1024
                        break
        except Exception:
            pass
    return cpus, int(mem_mb)


def _auto_noise_scale():
    """Pick noise_scale based on hardware: lower = faster.

    Thresholds are deliberately conservative so that weaker machines
    (common in classrooms using the Easy Deployment App) default to
    the fast/efficient preset.  The admin can switch to ``balanced``
    or set ``BW_TTS_PERFORMANCE_MODE=balanced`` for higher quality.
    """
    cpus, mem_mb = _detect_system_specs()
    if cpus <= 4 or (mem_mb and mem_mb < 4000):
        return 0.333
    if cpus <= 8 or (mem_mb and mem_mb < 8000):
        return 0.5
    return TTS_DEFAULT_PIPER_NOISE_SCALE


def _auto_noise_w_scale():
    """Pick noise_w_scale based on hardware: lower = faster.

    Thresholds mirror :func:`_auto_noise_scale` above.
    """
    cpus, mem_mb = _detect_system_specs()
    if cpus <= 4 or (mem_mb and mem_mb < 4000):
        return 0.55
    if cpus <= 8 or (mem_mb and mem_mb < 8000):
        return 0.65
    return TTS_DEFAULT_PIPER_NOISE_W_SCALE


def _convert_wav_to_mp3(src_path, dest_path):
    """Convert a local Piper WAV file to MP3 when ffmpeg is available."""
    binary = _ffmpeg_binary()
    if not binary:
        raise RuntimeError(
            "ffmpeg is not available; cannot encode Piper output as MP3. "
            "Install ffmpeg or set BW_TTS_PIPER_OUTPUT_FORMAT=wav."
        )
    cmd = [
        binary, "-y",
        "-loglevel", "error",
        "-i", src_path,
        "-vn",
        "-acodec", "libmp3lame",
        "-q:a", "4",
        dest_path,
    ]
    try:
        result = subprocess.run(
            cmd,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=120,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError(f"ffmpeg invocation failed: {exc}") from exc
    if result.returncode != 0 or not os.path.isfile(dest_path) or os.path.getsize(dest_path) == 0:
        stderr = (result.stderr or b"").decode("utf-8", errors="replace").strip()
        _logger.warning("ffmpeg Piper MP3 encode failed (rc=%s): %s", result.returncode, stderr[:400])
        raise RuntimeError("ffmpeg failed to encode Piper audio as MP3.")


def _synthesize_with_piper(text, language, dest_path):
    """Write local Piper speech audio to *dest_path* as WAV or MP3."""
    voice = _load_piper_voice(language)
    dest_ext = os.path.splitext(dest_path)[1].lower()
    wav_path = dest_path if dest_ext == ".wav" else f"{dest_path}.wav"
    try:
        with wave.open(wav_path, "wb") as wav_file:
            voice.synthesize_wav(
                text,
                wav_file,
                syn_config=_piper_synthesis_config(),
            )
        if not os.path.isfile(wav_path) or os.path.getsize(wav_path) == 0:
            raise RuntimeError("Piper wrote an empty audio file.")
        if dest_ext == ".mp3":
            _convert_wav_to_mp3(wav_path, dest_path)
    finally:
        if dest_ext == ".mp3":
            _delete_file_quietly(wav_path)


# Smallest-possible silent MP3 frame.  This is a single 1-frame MPEG-1 Layer
# III silent payload: enough that browsers' <audio> tags load it without
# erroring, but doesn't actually have to play anything.  Used by the stub
# backend.
_STUB_MP3_BYTES = (
    b"\xff\xfb\x90\x44\x00" + b"\x00" * 417
)


def _synthesize_with_stub(text, language, dest_path):
    """Write a deterministic tiny MP3 to *dest_path* without touching the network."""
    del text, language  # unused: the stub never actually synthesises anything
    with open(dest_path, "wb") as fh:
        fh.write(_STUB_MP3_BYTES)


# open_http refuses a timeout above one hour, so a larger configured value is
# clamped instead of failing every request.
TTS_REMOTE_GPU_DEFAULT_TIMEOUT_SECONDS = 120
TTS_REMOTE_GPU_MAX_TIMEOUT_SECONDS = 3600


def remote_gpu_settings_managed_by_host():
    """Return True when only the host may configure the remote GPU backend.

    On managed hosting (``BW_MANAGED_HOSTING=1``, or ``BW_INSTANCE_DIR`` set,
    which is the test the admin Settings form uses to hide the GPU fields) the
    GPU server and its token belong to the platform. The URL, token and timeout
    then come only from ``BW_TTS_REMOTE_GPU_URL``,
    ``BW_TTS_REMOTE_GPU_AUTH_TOKEN`` and ``BW_TTS_REMOTE_GPU_TIMEOUT``. The
    ``tts_gpu_*`` columns in site_settings are ignored there: a tenant admin
    can write them (the settings API accepts any allowed column, and a
    full-site import restores them), so reading them would let the tenant
    send the platform's token, or the wiki's own requests, to a server of
    their choosing.
    """
    return bool(getattr(config, "MANAGED_HOSTING", False)) or is_hosted_instance()


def _remote_gpu_timeout(value):
    """Return *value* as a timeout in seconds, or ``None`` when it is unusable."""
    try:
        seconds = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    if seconds <= 0:
        return None
    return min(seconds, TTS_REMOTE_GPU_MAX_TIMEOUT_SECONDS)


def _remote_gpu_config():
    """Return ``(url, token, timeout)`` for the remote GPU backend.

    Self-hosted resolution order:
    1. ``BW_TTS_REMOTE_GPU_*`` env vars (always win if set).
    2. ``tts_gpu_*`` columns in site_settings.
    3. Empty defaults.

    On a hosted wiki only step 1 applies; see
    :func:`remote_gpu_settings_managed_by_host`.
    """
    url = os.environ.get("BW_TTS_REMOTE_GPU_URL", "").strip().rstrip("/")
    token = os.environ.get("BW_TTS_REMOTE_GPU_AUTH_TOKEN", "").strip()
    timeout = _remote_gpu_timeout(os.environ.get("BW_TTS_REMOTE_GPU_TIMEOUT", ""))
    timeout_from_env = timeout is not None
    if timeout is None:
        timeout = TTS_REMOTE_GPU_DEFAULT_TIMEOUT_SECONDS

    if remote_gpu_settings_managed_by_host() or (url and token):
        return url, token, timeout

    settings = _safe_get_site_settings()
    if not url:
        url = (settings.get("tts_gpu_url") or "").strip().rstrip("/")
    if not token:
        token = (settings.get("tts_gpu_auth_token") or "").strip()
    if not timeout_from_env:
        stored = _remote_gpu_timeout(settings.get("tts_gpu_timeout"))
        if stored is not None:
            timeout = stored

    return url, token, timeout


def _remote_gpu_endpoint(base_url):
    """Return the ``/synthesize`` URL on the configured GPU server.

    The configured value must be a plain ``http(s)://host[:port][/path]``
    base. Embedded credentials, a query string or a fragment are refused
    instead of carried along: appending ``/synthesize`` to
    ``http://host/x?q=`` used to request ``/x?q=/synthesize``, which let
    whoever set the URL pick any path on that host.
    """
    try:
        parsed = urllib.parse.urlsplit(base_url)
        port = parsed.port  # raises ValueError for a malformed or out-of-range port
    except ValueError as exc:
        raise RuntimeError("The remote GPU TTS server URL is not valid.") from exc
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or port == 0
        or parsed.username is not None
        or parsed.password is not None
        or "?" in base_url
        or "#" in base_url
    ):
        raise RuntimeError(
            "The remote GPU TTS server URL must be a plain http(s)://host:port "
            "address without credentials, a query string or a fragment."
        )
    path = parsed.path.rstrip("/") + "/synthesize"
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))


def _remote_gpu_address_allowed(address):
    """Address policy for requests to the remote GPU server.

    ``open_http`` resolves the configured host once, checks every address with
    this policy and then connects only to those checked addresses, with
    redirects never followed, so the request (and its bearer token) stays
    pinned to the configured host. The GPU server normally sits on loopback, a
    LAN or a tailnet, so those ranges stay allowed. Link-local addresses
    (including the 169.254.169.254 cloud metadata service), multicast,
    unspecified and reserved ranges never host it and are refused before
    anything is sent.
    """
    return not (
        address.is_link_local
        or address.is_multicast
        or address.is_unspecified
        or address.is_reserved
    )


def _looks_like_audio(data):
    """Return True for the MP3 or WAV bytes the GPU TTS server sends back.

    Anything else is refused rather than cached and served as page audio, so a
    wrong or hijacked URL cannot turn some other service's reply into a file
    readers can download.
    """
    if data[:3] == b"ID3":
        return True
    if data[:4] == b"RIFF" and data[8:12] == b"WAVE":
        return True
    # A bare MPEG Layer III frame header, which is what libmp3lame writes when
    # no ID3 tag comes first. The 11-bit sync alone also matches text that
    # starts with 0xFF 0xFE (a UTF-16 byte order mark), so the version, layer,
    # bitrate and sample rate fields must hold valid values too.
    if len(data) < 3 or data[0] != 0xFF or (data[1] & 0xE0) != 0xE0:
        return False
    version = (data[1] >> 3) & 0x3
    layer = (data[1] >> 1) & 0x3
    bitrate_index = data[2] >> 4
    sample_rate_index = (data[2] >> 2) & 0x3
    return (
        version != 0x1
        and layer == 0x1
        and bitrate_index not in (0x0, 0xF)
        and sample_rate_index != 0x3
    )


def _synthesize_with_remote_gpu(text, language, dest_path):
    """POST *text* to a remote GPU TTS server and write the returned audio.

    The server is expected to be a BananaWiki GPU TTS server (``banana-tts-gpu``)
    running Piper with CUDA acceleration on a separate machine.  Communication
    happens over a private network (e.g. Tailscale) with a shared Bearer token.

    Raises ``RuntimeError`` on any failure (network, HTTP, timeout, a reply
    that is not audio) so the caller can fall back to local Piper.
    """
    url, token, timeout = _remote_gpu_config()
    if not url:
        raise RuntimeError(
            "BW_TTS_REMOTE_GPU_URL is not set.  Configure the remote GPU "
            "TTS server URL (e.g. http://100.x.x.x:8787)."
        )
    if not token:
        raise RuntimeError(
            "BW_TTS_REMOTE_GPU_AUTH_TOKEN is not set.  Configure the shared "
            "auth token for the remote GPU TTS server."
        )
    if any(ord(char) < 32 or ord(char) > 126 for char in token):
        # http.client refuses a header with control characters and quotes the
        # whole value, token included, in its error, which would then reach
        # the logs. The GPU server compares UTF-8 bytes, so a token outside
        # printable ASCII could not match there anyway.
        raise RuntimeError(
            "The remote GPU TTS auth token may only contain printable ASCII "
            "characters."
        )

    endpoint = _remote_gpu_endpoint(url)
    payload = json.dumps({
        "text": text,
        "language": language,
    }).encode("utf-8")

    req = urllib.request.Request(
        endpoint,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
        },
        method="POST",
    )

    try:
        with open_http(
            req,
            timeout=timeout,
            max_bytes=64 * 1024 * 1024,
            public_only=False,
            address_policy=_remote_gpu_address_allowed,
        ) as resp:
            if resp.status != 200:
                body = resp.read(500).decode("utf-8", errors="replace")
                raise RuntimeError(
                    f"Remote GPU TTS server returned HTTP {resp.status}: {body}"
                )
            audio_bytes = resp.read()
    except urllib.error.HTTPError as exc:
        body = exc.read(500).decode("utf-8", errors="replace") if exc.fp else ""
        raise RuntimeError(
            f"Remote GPU TTS server error HTTP {exc.code}: {body}"
        ) from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(
            f"Cannot reach remote GPU TTS server at {url}: {exc.reason}"
        ) from exc
    except TimeoutError as exc:
        raise RuntimeError(
            f"Remote GPU TTS server timed out after {timeout}s"
        ) from exc
    except ValueError as exc:
        # Raised before connecting when the host resolves to an address the
        # policy above refuses.
        raise RuntimeError(
            f"Refusing to contact remote GPU TTS server at {url}: {exc}"
        ) from exc
    except OSError as exc:
        # DNS failures surface here: name resolution runs before open_http's
        # own connection error handling.
        raise RuntimeError(
            f"Cannot reach remote GPU TTS server at {url}: {exc}"
        ) from exc

    if not audio_bytes or len(audio_bytes) < 100:
        raise RuntimeError(
            f"Remote GPU TTS server returned suspiciously small audio "
            f"({len(audio_bytes)} bytes)"
        )
    if not _looks_like_audio(audio_bytes):
        raise RuntimeError(
            "Remote GPU TTS server returned a response that is not MP3 or WAV audio."
        )

    with open(dest_path, "wb") as fh:
        fh.write(audio_bytes)


def synthesize_to_mp3(text, language, dest_path):
    """Render *text* as a *language*-locale audio file saved to *dest_path*.

    *language* must be one of :data:`TTS_SUPPORTED_LANGUAGES`.  The DB-side
    validation and the route-level normalisation guarantee that, but we
    re-check here so a programming error is caught before synthesis.

    Raises ``RuntimeError`` (or a backend-specific exception) on failure;
    callers translate that into a ``failed`` row on the generation.
    """
    if not text or not text.strip():
        raise RuntimeError("Page is empty, so there is nothing to synthesise.")
    if language not in TTS_SUPPORTED_LANGUAGE_SET:
        raise RuntimeError(f"Unsupported TTS language: {language!r}")

    backend = _selected_backend()
    if backend == "stub":
        _synthesize_with_stub(text, language, dest_path)
    elif backend == "remote-gpu":
        try:
            _synthesize_with_remote_gpu(text, language, dest_path)
        except Exception as gpu_exc:
            _logger.warning(
                "Remote GPU TTS failed (%s), falling back to local piper",
                gpu_exc,
            )
            _synthesize_with_piper(text, language, dest_path)
    else:
        _synthesize_with_piper(text, language, dest_path)


def preferred_tts_audio_extension():
    """Return the file extension used for newly cached TTS generations."""
    backend = _selected_backend()
    if backend == "piper":
        fmt = _configured_piper_output_format()
        if fmt == "mp3":
            return "mp3"
        if fmt == "wav":
            return "wav"
        return "mp3" if _ffmpeg_binary() else "wav"
    return "mp3"


def tts_mimetype_for_filename(filename):
    """Return the best response MIME type for a cached TTS filename."""
    ext = os.path.splitext(filename or "")[1].lower()
    if ext == ".wav":
        return "audio/wav"
    return "audio/mpeg"


# The queue and worker machinery below is what keeps synthesis off the
# request path; generations are owned by one process at a time.

def _ensure_tts_folder(tts_folder):
    """Create *tts_folder* if it does not already exist."""
    os.makedirs(tts_folder, exist_ok=True)


def _final_filename(generation_id, page_id, language, content_hash, extension="mp3"):
    """Build the on-disk filename for a completed generation.

    Format: ``tts_<page_id>_<lang>_<hash8>_<gen_id>.<ext>``: the trailing
    ``generation_id`` makes it impossible for stale workers (whose row was
    deleted and replaced) to overwrite a fresh worker's output.
    """
    digest = (content_hash or "")[:8] or "x" * 8
    ext = str(extension or "mp3").lower().lstrip(".")
    if ext not in {"mp3", "wav"}:
        ext = "mp3"
    return f"tts_{int(page_id)}_{language}_{digest}_{int(generation_id)}.{ext}"


def _delete_file_quietly(path):
    """Delete *path* if it exists, swallowing any OSError."""
    if not path:
        return
    try:
        if os.path.isfile(path):
            os.remove(path)
    except OSError:
        _logger.warning("Failed to remove TTS file: %s", path, exc_info=True)


def _row_get(row, key, default=None):
    """Return *key* from sqlite rows, dicts, or lightweight row-like objects."""
    if row is None:
        return default
    try:
        return row[key]
    except (KeyError, IndexError, TypeError):
        pass
    getter = getattr(row, "get", None)
    if callable(getter):
        return getter(key, default)
    return getattr(row, key, default)


class _TtsGenerationHandle:
    """Small joinable handle returned for queued background generations."""

    def __init__(self, generation_id):
        """Create a handle for one queued generation id."""
        self.generation_id = generation_id
        self.name = f"tts-job-{generation_id}"
        self._done = threading.Event()

    def _mark_done(self):
        """Mark the queued generation as completed."""
        self._done.set()

    def join(self, timeout=None):
        """Wait for completion, mirroring ``threading.Thread.join``."""
        self._done.wait(timeout)

    def is_alive(self):
        """Return whether the queued generation is still pending."""
        return not self._done.is_set()


class _TtsGenerationJob:
    """Queued generation payload consumed by the process-local worker pool."""

    __slots__ = (
        "handle",
        "generation_id",
        "page_id",
        "language",
        "content_hash",
        "text",
        "tts_folder",
    )

    def __init__(self, handle, generation_id, page_id, language,
                 content_hash, text, tts_folder):
        """Store the immutable payload needed by the TTS worker."""
        self.handle = handle
        self.generation_id = generation_id
        self.page_id = page_id
        self.language = language
        self.content_hash = content_hash
        self.text = text
        self.tts_folder = tts_folder


_TTS_QUEUE = queue.Queue()
_TTS_WORKER_LOCK = threading.Lock()
_TTS_WORKERS_STARTED = 0
_TTS_START_RATE_LOCK = threading.Lock()
_TTS_LAST_START_MONOTONIC = 0.0
_TTS_BACKEND_COOLDOWN_UNTIL = 0.0
_TTS_ENQUEUED_LOCK = threading.Lock()
_TTS_ENQUEUED_HANDLES = {}
_TTS_RECOVERY_WATCHDOG_LOCK = threading.Lock()
_TTS_RECOVERY_WATCHDOG_STARTED = False


def _configured_worker_count():
    """Return the number of process-local TTS workers to keep running."""
    raw = os.environ.get("BW_TTS_WORKER_COUNT")
    try:
        count = int(raw) if raw is not None else TTS_DEFAULT_WORKER_COUNT
    except (TypeError, ValueError):
        count = TTS_DEFAULT_WORKER_COUNT
    return max(1, min(count, TTS_MAX_WORKER_COUNT))


def _configured_start_interval_seconds():
    """Return the minimum delay between real backend synthesis starts."""
    if _selected_backend() == "stub":
        return 0.0
    raw = os.environ.get("BW_TTS_MIN_START_INTERVAL_SECONDS")
    try:
        interval = (
            float(raw)
            if raw is not None
            else TTS_DEFAULT_REAL_BACKEND_START_INTERVAL_SECONDS
        )
    except (TypeError, ValueError):
        interval = TTS_DEFAULT_REAL_BACKEND_START_INTERVAL_SECONDS
    return max(0.0, interval)


def _configured_pending_recovery_interval_seconds():
    """Return how often to scan for orphaned pending TTS rows."""
    raw = os.environ.get("BW_TTS_PENDING_RECOVERY_INTERVAL_SECONDS")
    try:
        interval = (
            float(raw)
            if raw is not None
            else TTS_PENDING_RECOVERY_INTERVAL_SECONDS
        )
    except (TypeError, ValueError):
        interval = TTS_PENDING_RECOVERY_INTERVAL_SECONDS
    return max(0.0, interval)


def _configured_rate_limit_cooldown_seconds():
    """Return how long to pause backend starts after a legacy 429-style error."""
    raw = os.environ.get("BW_TTS_RATE_LIMIT_COOLDOWN_SECONDS")
    try:
        cooldown = (
            float(raw)
            if raw is not None
            else TTS_RATE_LIMIT_COOLDOWN_SECONDS
        )
    except (TypeError, ValueError):
        cooldown = TTS_RATE_LIMIT_COOLDOWN_SECONDS
    return max(0.0, cooldown)


def _rate_limit_cooldown_remaining():
    """Return seconds left in the process-local backend cooldown."""
    with _TTS_START_RATE_LOCK:
        remaining = _TTS_BACKEND_COOLDOWN_UNTIL - time.monotonic()
    return max(0.0, remaining)


def _note_rate_limited_backend():
    """Pause future backend starts after a legacy rate-limit style response."""
    cooldown = _configured_rate_limit_cooldown_seconds()
    if cooldown <= 0:
        return 0.0
    global _TTS_BACKEND_COOLDOWN_UNTIL
    with _TTS_START_RATE_LOCK:
        until = time.monotonic() + cooldown
        if until > _TTS_BACKEND_COOLDOWN_UNTIL:
            _TTS_BACKEND_COOLDOWN_UNTIL = until
        return max(0.0, _TTS_BACKEND_COOLDOWN_UNTIL - time.monotonic())


def _reset_tts_runtime_state_for_tests():
    """Reset process-local TTS queue bookkeeping for isolated tests."""
    global _TTS_BACKEND_COOLDOWN_UNTIL, _TTS_LAST_START_MONOTONIC
    global _PIPER_VOICE_MAP_CACHE_KEY, _PIPER_VOICE_MAP_CACHE
    with _TTS_START_RATE_LOCK:
        _TTS_BACKEND_COOLDOWN_UNTIL = 0.0
        _TTS_LAST_START_MONOTONIC = 0.0
    with _TTS_ENQUEUED_LOCK:
        _TTS_ENQUEUED_HANDLES.clear()
    with _PIPER_VOICE_CACHE_LOCK:
        _PIPER_VOICE_CACHE.clear()
    with _PIPER_VOICE_MAP_CACHE_LOCK:
        _PIPER_VOICE_MAP_CACHE_KEY = None
        _PIPER_VOICE_MAP_CACHE = None
    _cached_piper_synthesis_config.cache_clear()


def _wait_for_generation_start_slot():
    """Rate-limit generation starts across all process-local TTS workers."""
    interval = _configured_start_interval_seconds()
    global _TTS_LAST_START_MONOTONIC
    while True:
        with _TTS_START_RATE_LOCK:
            now = time.monotonic()
            cooldown_wait = max(0.0, _TTS_BACKEND_COOLDOWN_UNTIL - now)
            interval_wait = (
                (_TTS_LAST_START_MONOTONIC + interval) - now
                if interval > 0
                else 0.0
            )
            wait_for = max(cooldown_wait, interval_wait)
            if wait_for <= 0:
                _TTS_LAST_START_MONOTONIC = now
                return
        time.sleep(wait_for)


def _tts_queue_worker():
    """Consume queued TTS jobs forever."""
    while True:
        job = _TTS_QUEUE.get()
        try:
            _wait_for_generation_start_slot()
            _run_generation(
                job.generation_id,
                job.page_id,
                job.language,
                job.content_hash,
                job.text,
                job.tts_folder,
            )
        except Exception:  # noqa: BLE001 - daemon workers must not die
            _logger.warning(
                "TTS queued worker failed for generation %s",
                getattr(job, "generation_id", None),
                exc_info=True,
            )
        finally:
            try:
                job.handle._mark_done()
            finally:
                with _TTS_ENQUEUED_LOCK:
                    current = _TTS_ENQUEUED_HANDLES.get(job.generation_id)
                    if current is job.handle:
                        _TTS_ENQUEUED_HANDLES.pop(job.generation_id, None)
                _TTS_QUEUE.task_done()


def _ensure_tts_workers_started():
    """Start enough daemon queue workers for the current configuration."""
    global _TTS_WORKERS_STARTED
    target = _configured_worker_count()
    if _TTS_WORKERS_STARTED >= target:
        return
    with _TTS_WORKER_LOCK:
        while _TTS_WORKERS_STARTED < target:
            index = _TTS_WORKERS_STARTED + 1
            thread = threading.Thread(
                target=_tts_queue_worker,
                name=f"tts-queue-worker-{index}",
                daemon=True,
            )
            thread.start()
            _TTS_WORKERS_STARTED += 1


def _configured_auto_resume_attempts():
    """Return the configured automatic retry budget."""
    raw = os.environ.get("BW_TTS_MAX_AUTO_RESUME_ATTEMPTS")
    if raw is None:
        return TTS_MAX_AUTO_RESUME_ATTEMPTS
    try:
        return max(0, int(raw))
    except (TypeError, ValueError):
        return TTS_MAX_AUTO_RESUME_ATTEMPTS


def _configured_resume_delay_seconds(retry_count):
    """Return the backoff delay for a queued resume attempt."""
    try:
        base = float(os.environ.get(
            "BW_TTS_AUTO_RESUME_BASE_DELAY_SECONDS",
            TTS_AUTO_RESUME_BASE_DELAY_SECONDS,
        ))
    except (TypeError, ValueError):
        base = TTS_AUTO_RESUME_BASE_DELAY_SECONDS
    try:
        max_delay = float(os.environ.get(
            "BW_TTS_AUTO_RESUME_MAX_DELAY_SECONDS",
            TTS_AUTO_RESUME_MAX_DELAY_SECONDS,
        ))
    except (TypeError, ValueError):
        max_delay = TTS_AUTO_RESUME_MAX_DELAY_SECONDS

    if base <= 0:
        return 0.0
    retry_index = max(0, int(retry_count or 1) - 1)
    delay = base * (2 ** retry_index)
    if max_delay <= 0:
        return delay
    return min(delay, max_delay)


def _is_retryable_generation_error(error_message):
    """Return True when a generation error could plausibly recover later."""
    msg = (error_message or "").lower()
    permanent_markers = (
        "page is empty",
        "nothing to synthesise",
        "nothing to synthesize",
        "unsupported tts language",
        "unsupported language",
        "piper tts is not installed",
        "no local piper voice configured",
        "local piper voice files are missing",
        "ffmpeg is not available; cannot encode piper output",
    )
    return not any(marker in msg for marker in permanent_markers)


def _is_rate_limited_generation_error(error_message):
    """Return True when the backend says to slow down rather than fail."""
    msg = (error_message or "").lower()
    return (
        "429" in msg
        or "too many requests" in msg
        or "rate limit" in msg
        or "ratelimit" in msg
    )


def _mark_tts_failed_quietly(generation_id, error_message):
    """Best-effort terminal failure marker for daemon worker threads."""
    try:
        db.mark_tts_failed(generation_id, error_message)
    except Exception:  # noqa: BLE001 (shutdown/test teardown may remove DB)
        _logger.warning(
            "Could not mark TTS generation %s failed", generation_id,
            exc_info=True,
        )


def _schedule_generation_resume(generation_id, page_id, language, content_hash,
                                text, tts_folder, retry_count):
    """Queue a resumed worker after the configured backoff delay."""
    delay = _configured_resume_delay_seconds(retry_count)
    if delay <= 0:
        return run_generation_async(
            generation_id=generation_id,
            page_id=page_id,
            language=language,
            content_hash=content_hash,
            text=text,
            tts_folder=tts_folder,
            replace_existing=True,
        )

    timer = threading.Timer(
        delay,
        run_generation_async,
        kwargs={
            "generation_id": generation_id,
            "page_id": page_id,
            "language": language,
            "content_hash": content_hash,
            "text": text,
            "tts_folder": tts_folder,
            "replace_existing": True,
        },
    )
    timer.name = f"tts-resume-{generation_id}-{retry_count}"
    timer.daemon = True
    timer.start()
    return timer


def _handle_generation_failure(generation_id, page_id, language, content_hash,
                               text, tts_folder, error_message, dest_path=None):
    """Mark a generation failure and auto-resume it when retryable."""
    if dest_path:
        _delete_file_quietly(dest_path)

    if _is_rate_limited_generation_error(error_message):
        cooldown = _note_rate_limited_backend()
        try:
            pending_row = db.mark_tts_rate_limited_pending(
                generation_id, error_message,
            )
        except Exception:  # noqa: BLE001 - worker teardown must stay best-effort
            _logger.warning(
                "Could not keep rate-limited TTS generation %s pending",
                generation_id,
                exc_info=True,
            )
            pending_row = None
        if not pending_row:
            _mark_tts_failed_quietly(generation_id, error_message)
            return False
        delay = max(cooldown, _configured_start_interval_seconds())
        _logger.warning(
            "TTS backend rate-limited generation %s for page %s; "
            "cooling down for %.1fs",
            generation_id, page_id, delay,
        )
        if not _inline_tts_worker_enabled():
            return True
        timer = threading.Timer(
            delay,
            run_generation_async,
            kwargs={
                "generation_id": generation_id,
                "page_id": page_id,
                "language": language,
                "content_hash": content_hash,
                "text": text,
                "tts_folder": tts_folder,
                "replace_existing": True,
            },
        )
        timer.name = f"tts-rate-limit-resume-{generation_id}"
        timer.daemon = True
        timer.start()
        return True

    if not _is_retryable_generation_error(error_message):
        _mark_tts_failed_quietly(generation_id, error_message)
        return False

    max_retries = _configured_auto_resume_attempts()
    try:
        retry_row = db.mark_tts_retry_pending(
            generation_id, error_message, max_retries,
        )
    except Exception:  # noqa: BLE001 (worker teardown must stay best-effort)
        _logger.warning(
            "Could not queue TTS generation %s for retry", generation_id,
            exc_info=True,
        )
        return False
    if not retry_row:
        _mark_tts_failed_quietly(generation_id, error_message)
        return False

    retry_count = _row_get(retry_row, "retry_count", 0)
    delay = _configured_resume_delay_seconds(retry_count)
    _logger.warning(
        "TTS generation %s for page %s failed; auto-resuming "
        "attempt %s/%s in %.1fs: %s",
        generation_id, page_id, retry_count, max_retries, delay,
        error_message,
    )
    if _inline_tts_worker_enabled():
        _schedule_generation_resume(
            generation_id=generation_id,
            page_id=page_id,
            language=language,
            content_hash=content_hash,
            text=text,
            tts_folder=tts_folder,
            retry_count=retry_count,
        )
    return True


def _run_generation(generation_id, page_id, language, content_hash, text, tts_folder):
    """Worker body: drive a single ``tts_generations`` row to a terminal state."""
    if tts_generation_disabled_by_host():
        # Leave the row pending instead of failing it. The host policy comes
        # from the environment, so it holds for this whole process; if the
        # host lifts it later, the next worker picks the row up as usual.
        return

    try:
        _ensure_tts_folder(tts_folder)
    except OSError as exc:
        _handle_generation_failure(
            generation_id, page_id, language, content_hash, text, tts_folder,
            f"Could not create TTS folder: {exc}",
        )
        return

    if not db.mark_tts_processing(generation_id):
        # Row was deleted (cancelled / page updated) before we even started.
        return

    # Re-check that the row is still ours after marking processing.  If the
    # page has been edited and a new row inserted, the new row will have a
    # different id; we should bail out and not write the file.
    current = db.get_tts_generation(page_id)
    if not current or current["id"] != generation_id:
        _mark_tts_failed_quietly(generation_id, "Cancelled (superseded).")
        return

    filename = _final_filename(
        generation_id,
        page_id,
        language,
        content_hash,
        preferred_tts_audio_extension(),
    )
    dest_path = os.path.join(tts_folder, filename)

    try:
        synthesize_to_mp3(text, language, dest_path)
    except Exception as exc:  # noqa: BLE001 (surface any synthesis error)
        _logger.warning("TTS synthesis failed for page %s: %s", page_id, exc)
        _handle_generation_failure(
            generation_id, page_id, language, content_hash, text, tts_folder,
            str(exc), dest_path=dest_path,
        )
        return

    try:
        size = os.path.getsize(dest_path) if os.path.isfile(dest_path) else 0
    except OSError:
        size = 0

    # Final supersede check: if while we were synthesising, the row was
    # deleted (page update / delete / cancel), drop the file we just wrote
    # and exit so we don't leak orphans.
    final_check = db.get_tts_generation(page_id)
    if not final_check or final_check["id"] != generation_id:
        _delete_file_quietly(dest_path)
        _mark_tts_failed_quietly(generation_id, "Cancelled (superseded).")
        return

    if not db.mark_tts_completed(generation_id, filename, size):
        # Row vanished between the supersede check and the UPDATE: clean up.
        _delete_file_quietly(dest_path)


def run_generation_async(generation_id, page_id, language, content_hash, text,
                         tts_folder, *, replace_existing=False):
    """Queue a background generation and return a joinable handle.

    Production web processes should not synthesize audio.  By default this
    function returns an already-complete handle and leaves the durable
    ``pending`` DB row for ``scripts/tts_worker.py``.  Tests and small
    single-process dev runs may opt back into the historical in-process queue
    with ``BW_TTS_INLINE_WORKER=1``.

    Nothing is queued while the host has TTS switched off; the row stays
    pending (see :func:`tts_generation_disabled_by_host`).
    """
    if not _inline_tts_worker_enabled() or tts_generation_disabled_by_host():
        handle = _TtsGenerationHandle(generation_id)
        handle._mark_done()
        return handle

    _ensure_tts_workers_started()
    with _TTS_ENQUEUED_LOCK:
        existing = _TTS_ENQUEUED_HANDLES.get(generation_id)
        if existing is not None and not replace_existing:
            return existing
        handle = _TtsGenerationHandle(generation_id)
        _TTS_ENQUEUED_HANDLES[generation_id] = handle
        _TTS_QUEUE.put(
            _TtsGenerationJob(
                handle,
                generation_id,
                page_id,
                language,
                content_hash,
                text,
                tts_folder,
            )
        )
    return handle


def process_next_tts_generation(tts_folder, *, scan_limit=10):
    """Claim and process one pending TTS row from the durable DB queue.

    Returns ``True`` when a row was claimed or cleaned up, ``False`` when no
    pending work was available.  The function is safe to run from multiple
    worker processes; ``db.mark_tts_processing`` inside ``_run_generation``
    is the atomic claim.

    Returns ``False`` without touching the queue while the host has TTS
    switched off.
    """
    if tts_generation_disabled_by_host():
        return False
    try:
        rows = db.list_pending_tts_generations(limit=scan_limit)
    except Exception:  # noqa: BLE001 - worker loop should keep polling
        _logger.warning("Could not list pending TTS generations", exc_info=True)
        return False

    for row in rows:
        args = _generation_args_from_row(row, tts_folder)
        if not args:
            return True

        _wait_for_generation_start_slot()
        _run_generation(
            args["generation_id"],
            args["page_id"],
            args["language"],
            args["content_hash"],
            args["text"],
            args["tts_folder"],
        )
        return True
    return False


def run_tts_worker_loop(tts_folder, *, poll_interval=None, once=False,
                        max_jobs=None, recover=True):
    """Run the standalone durable TTS worker loop.

    This function belongs in a separate process from Flask/Gunicorn.  It polls
    ``tts_generations`` for ``pending`` rows, claims one row at a time, and
    synthesizes audio locally with the configured backend.

    While the host has TTS switched off the loop stays idle: it neither
    recovers nor claims rows. It does not exit, because the hosting
    supervisors restart a worker that exits, which would only turn an idle
    worker into a restart loop.
    """
    if poll_interval is None:
        poll_interval = TTS_WORKER_DEFAULT_POLL_INTERVAL_SECONDS
    try:
        poll_interval = max(0.1, float(poll_interval))
    except (TypeError, ValueError):
        poll_interval = TTS_WORKER_DEFAULT_POLL_INTERVAL_SECONDS

    if recover and not tts_generation_disabled_by_host():
        try:
            recovered = recover_stuck_generations(tts_folder)
            if recovered:
                _logger.info(
                    "TTS worker recovered %s generation row(s)", recovered,
                )
        except Exception:  # noqa: BLE001 - keep the worker alive
            _logger.warning("TTS worker startup recovery failed", exc_info=True)

    processed = 0
    while True:
        did_work = process_next_tts_generation(tts_folder)
        if did_work:
            processed += 1
            if max_jobs is not None and processed >= int(max_jobs):
                return processed
            continue
        if once:
            return processed
        if tts_generation_disabled_by_host():
            # The policy comes from the environment and cannot change under
            # a running process, so there is no point polling often.
            time.sleep(max(poll_interval, TTS_WORKER_DISABLED_POLL_INTERVAL_SECONDS))
            continue
        time.sleep(poll_interval)



def enqueue_auto_generation(page, tts_folder, *, requested_by=None,
                            language=None):
    """Kick off an automatic TTS generation for *page*.

    Used by the ``after_page_create`` / ``after_page_update`` hooks when the
    admin has enabled the ``tts_auto_generate_enabled`` site setting.

    Returns ``"queued"`` when a new worker was started, ``"cached"`` when an
    identical completed cache row already covers the request, and ``None``
    when generation was skipped (host policy, empty page, missing slug, etc.).

    The function is intentionally defensive: it must never raise from a
    request hook because that would prevent the page-save itself from
    completing.
    """
    if tts_generation_disabled_by_host():
        return None
    if not page:
        return None
    slug = page["slug"] if hasattr(page, "__getitem__") else getattr(page, "slug", None)
    if not slug:
        return None
    page_id = page["id"] if hasattr(page, "__getitem__") else getattr(page, "id", None)
    if not page_id:
        return None
    title = page["title"] if hasattr(page, "__getitem__") else getattr(page, "title", "")
    content = page["content"] if hasattr(page, "__getitem__") else getattr(page, "content", "")

    spoken = tts_normalize_text(title, content)
    if not spoken.strip():
        # Empty page: no point synthesising silence.
        return None

    if not language:
        language = detect_tts_language(spoken)
    else:
        language = normalize_tts_language(
            language, default=TTS_FALLBACK_LANGUAGE, allow_auto=False,
        )
    if language not in TTS_SUPPORTED_LANGUAGE_SET:
        language = _pick_allowed_fallback(
            TTS_FALLBACK_LANGUAGE, TTS_SUPPORTED_LANGUAGE_SET,
        )
    if language not in TTS_SUPPORTED_LANGUAGE_SET:
        # Belt-and-braces: ``normalize_tts_language`` always returns a
        # supported code, but a future caller might pass through a value
        # that bypassed it (e.g. a raw row from ``request_tts_generation``).
        language = _pick_allowed_fallback(
            TTS_FALLBACK_LANGUAGE, TTS_SUPPORTED_LANGUAGE_SET,
        )

    content_hash = tts_content_hash_for_text(spoken, language)

    try:
        row, created = db.request_tts_generation(
            page_id=page_id,
            language=language,
            content_hash=content_hash,
            requested_by=requested_by,
        )
    except Exception:  # noqa: BLE001  never break the calling request
        _logger.warning("TTS auto-enqueue failed for page %s", page_id, exc_info=True)
        return None

    if not created:
        # Either we already have the audio cached or another worker is
        # already producing it for this exact content.
        return "cached" if row and row["status"] == "completed" else "in_progress"

    run_generation_async(
        generation_id=row["id"],
        page_id=page_id,
        language=language,
        content_hash=content_hash,
        text=spoken,
        tts_folder=tts_folder,
    )
    return "queued"



def cleanup_orphan_tts_files(tts_folder):
    """Remove on-disk audio files that are no longer referenced by any DB row.

    Returns the number of files deleted.  Safe to call when the folder
    does not exist (returns 0).
    """
    if not tts_folder or not os.path.isdir(tts_folder):
        return 0
    referenced = set(db.list_tts_filenames())
    removed = 0
    for fname in os.listdir(tts_folder):
        if fname.startswith("."):
            continue
        if fname in referenced:
            continue
        fpath = os.path.join(tts_folder, fname)
        if os.path.isfile(fpath):
            try:
                os.remove(fpath)
                removed += 1
            except OSError:
                _logger.warning("Failed to remove orphan TTS file: %s", fpath,
                                exc_info=True)
    return removed


def remove_tts_files(tts_folder, filenames):
    """Delete each filename in *filenames* from *tts_folder* if present."""
    if not tts_folder or not filenames:
        return
    for fname in filenames:
        if not fname:
            continue
        # Defence in depth: never let a stored filename escape the folder.
        # ``os.path.basename`` strips any directory traversal.
        safe_name = os.path.basename(fname)
        if safe_name != fname:
            continue
        _delete_file_quietly(os.path.join(tts_folder, safe_name))


# On restart, rows left in a queued or running state belong to a process
# that no longer exists, so they are reclaimed here rather than stranded.

def _row_generation_is_already_enqueued(generation_id):
    """Return True when this process already owns a queued/running job."""
    with _TTS_ENQUEUED_LOCK:
        return generation_id in _TTS_ENQUEUED_HANDLES


def _generation_args_from_row(row, tts_folder, *, reset=False, was_failed=False):
    """Validate a persisted TTS row and return generation arguments.

    Stale/empty/deleted pages are cleaned up so they do not sit in the admin
    status table forever.  Failed/resumable rows are moved back to ``pending``
    here; the caller decides whether to process inline or leave them for a
    standalone worker poll.
    """
    generation_id = _row_get(row, "id")
    page_id = _row_get(row, "page_id")
    language = _row_get(row, "language")
    stored_hash = _row_get(row, "content_hash")
    if not generation_id or not page_id or not language or not stored_hash:
        return None

    if _row_generation_is_already_enqueued(generation_id):
        return None

    page_content = _row_get(row, "page_content")
    page_title = _row_get(row, "page_title", "")
    if page_content is None:
        db.mark_tts_failed(generation_id, "The page no longer exists.")
        return None

    spoken = tts_normalize_text(page_title, page_content)
    if not spoken.strip():
        db.delete_tts_generation(page_id)
        return None

    current_hash = tts_content_hash_for_text(spoken, language)
    if current_hash != stored_hash:
        fname, _ = db.delete_tts_generation(page_id)
        if fname:
            remove_tts_files(tts_folder, [fname])
        return None

    old_filename = _row_get(row, "filename")
    if old_filename:
        remove_tts_files(tts_folder, [old_filename])

    retry_count = _row_get(row, "retry_count", 0)
    if was_failed:
        error_message = (
            _row_get(row, "error_message")
            or "Resuming failed TTS generation."
        )
        if _is_rate_limited_generation_error(error_message):
            retry_row = db.mark_tts_rate_limited_pending(
                generation_id, error_message,
            )
            if retry_row:
                _note_rate_limited_backend()
                retry_count = _row_get(retry_row, "retry_count", retry_count) or 1
        else:
            retry_row = db.mark_tts_retry_pending(
                generation_id,
                error_message,
                _configured_auto_resume_attempts(),
            )
        if not retry_row:
            return None
        if not _is_rate_limited_generation_error(error_message):
            retry_count = _row_get(retry_row, "retry_count", 0)
    elif reset:
        db.reset_to_pending(generation_id)

    return {
        "generation_id": generation_id,
        "page_id": page_id,
        "language": language,
        "content_hash": stored_hash,
        "text": spoken,
        "tts_folder": tts_folder,
        "retry_count": retry_count,
    }


def _requeue_tts_row(row, tts_folder, *, reset=False, was_failed=False):
    """Validate and re-enqueue a persisted TTS row.

    Returns ``True`` when a worker was queued.  With the production default
    ``BW_TTS_INLINE_WORKER=0``, queuing means the row is simply left pending
    for the standalone worker process.
    """
    args = _generation_args_from_row(
        row, tts_folder, reset=reset, was_failed=was_failed,
    )
    if not args:
        return False

    if was_failed and _inline_tts_worker_enabled():
        _schedule_generation_resume(
            generation_id=args["generation_id"],
            page_id=args["page_id"],
            language=args["language"],
            content_hash=args["content_hash"],
            text=args["text"],
            tts_folder=args["tts_folder"],
            retry_count=args["retry_count"],
        )
    else:
        run_generation_async(
            generation_id=args["generation_id"],
            page_id=args["page_id"],
            language=args["language"],
            content_hash=args["content_hash"],
            text=args["text"],
            tts_folder=args["tts_folder"],
        )
    return True


def recover_orphaned_pending_generations(tts_folder):
    """Re-enqueue pending TTS rows that no live process-local queue owns.

    This is safe to call repeatedly.  Rows already queued in this process are
    skipped, and duplicate workers for the same DB row are harmless because
    only one can transition ``pending`` -> ``processing``.
    """
    if tts_generation_disabled_by_host():
        return 0
    try:
        rows = db.list_stuck_tts_generations()
    except Exception:  # noqa: BLE001 - background watchdog must stay quiet
        _logger.warning("Could not list pending TTS rows for recovery", exc_info=True)
        return 0

    re_queued = 0
    for row in rows:
        if _row_get(row, "status") != "pending":
            continue
        try:
            if _requeue_tts_row(row, tts_folder):
                re_queued += 1
        except Exception:  # noqa: BLE001 - one bad row should not stop recovery
            _logger.warning(
                "Could not requeue pending TTS generation %s",
                _row_get(row, "id"),
                exc_info=True,
            )
    if re_queued:
        _logger.info("Recovered %s orphaned pending TTS generation(s)", re_queued)
    return re_queued


def _tts_pending_recovery_watchdog(tts_folder):
    """Periodically rescue pending rows that lost their in-memory queue job."""
    while True:
        interval = _configured_pending_recovery_interval_seconds()
        if interval <= 0:
            return
        time.sleep(interval)
        recover_orphaned_pending_generations(tts_folder)


def start_tts_pending_recovery_watchdog(tts_folder):
    """Start the process-local pending-row recovery watchdog once."""
    global _TTS_RECOVERY_WATCHDOG_STARTED
    if _configured_pending_recovery_interval_seconds() <= 0:
        return False
    if _TTS_RECOVERY_WATCHDOG_STARTED:
        return False
    with _TTS_RECOVERY_WATCHDOG_LOCK:
        if _TTS_RECOVERY_WATCHDOG_STARTED:
            return False
        thread = threading.Thread(
            target=_tts_pending_recovery_watchdog,
            args=(tts_folder,),
            name="tts-pending-recovery",
            daemon=True,
        )
        thread.start()
        _TTS_RECOVERY_WATCHDOG_STARTED = True
        return True


def recover_stuck_generations(tts_folder):
    """Auto-recover TTS generation rows orphaned by a server restart.

    Iterates over every ``pending`` / ``processing`` row in the DB (these
    are the workers that were killed mid-flight), plus ``failed`` rows that
    still have automatic retry budget left.  For each one:

    * If the page still exists and its content hasn't changed → re-queues
      the generation by resetting the row to ``pending`` and spawning a
      fresh worker thread.
    * If the page content has changed since the row was created → the
      old row is stale; it is removed so a future generate or auto-gen
      can create a fresh one.
    * If a partial audio file from the previous attempt exists on disk it
      is removed before re-queuing so the new worker starts clean.

    The user sees no failed state and needs no manual action. The new
    worker transitions through ``pending`` → ``processing`` → ``completed``
    as if nothing happened.

    Returns the number of rows that were successfully re-queued.  Rows are
    left untouched while the host has TTS switched off.
    """
    if tts_generation_disabled_by_host():
        return 0
    max_retries = _configured_auto_resume_attempts()
    stuck = db.list_stuck_tts_generations()
    resumable_failed = db.list_resumable_failed_tts_generations(max_retries)
    recoverable = [(row, False) for row in stuck]
    recoverable.extend((row, True) for row in resumable_failed)

    if not recoverable:
        return 0

    re_queued = 0
    for row, was_failed in recoverable:
        if was_failed and not _is_retryable_generation_error(
            _row_get(row, "error_message")
        ):
            continue
        if _requeue_tts_row(row, tts_folder, reset=True, was_failed=was_failed):
            re_queued += 1

    return re_queued

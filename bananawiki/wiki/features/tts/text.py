"""Page text for speech: normalisation, cache hashes, languages and speeds.

The normalisation and the hash are byte-for-byte those of BananaWiki 1.4, so
audio generated before the upgrade stays valid (``tts_generations.content_hash``
is ``sha256(spoken_text + "\\0" + language)``).

Language detection uses ``langdetect`` when it is installed and falls back to
an English/Italian stop-word heuristic. The result is always one of the
languages the administrator enabled (``site_settings.tts_enabled_languages``).
"""

from __future__ import annotations

import hashlib
import importlib.util
import logging
import re
import unicodedata
from collections.abc import Iterable

log = logging.getLogger("bananawiki.tts")

MAX_INPUT_CHARS = 20_000
FALLBACK_LANGUAGE = "en"
DEFAULT_ENABLED_LANGUAGES = ("en", "it")
SPEED_PRESETS = (0.75, 1.0, 1.25, 1.5, 1.75, 2.0)
DEFAULT_SPEED = 1.0
LANGDETECT_MIN_LATIN_WORDS = 12

# The language catalogue of 1.4; stored rows and settings use these codes.
SUPPORTED_LANGUAGES: tuple[str, ...] = (
    "af", "am", "ar", "bg", "bn", "bs", "ca", "cs", "cy", "da", "de", "el", "en", "es", "et", "eu", "fa", "fi",
    "fr", "fr-CA", "gl", "gu", "ha", "hi", "hr", "hu", "id", "is", "it", "iw", "ja", "jw", "ka", "kk", "km",
    "kn", "ko", "ku", "la", "lb", "lt", "lv", "ml", "mr", "ms", "my", "ne", "nl", "no", "pa", "pl", "pt",
    "pt-PT", "ro", "ru", "si", "sk", "sl", "sq", "sr", "su", "sv", "sw", "ta", "te", "th", "tl", "tr", "uk",
    "ur", "vi", "yue", "zh", "zh-CN", "zh-TW",
)

LANGUAGE_LABELS: dict[str, str] = {
    "af": "Afrikaans",
    "am": "አማርኛ (Amharic)",
    "ar": "العربية (Arabic)",
    "bg": "Български (Bulgarian)",
    "bn": "বাংলা (Bengali)",
    "bs": "Bosanski (Bosnian)",
    "ca": "Català (Catalan)",
    "cs": "Čeština (Czech)",
    "cy": "Cymraeg (Welsh)",
    "da": "Dansk (Danish)",
    "de": "Deutsch (German)",
    "el": "Ελληνικά (Greek)",
    "en": "English",
    "es": "Español (Spanish)",
    "et": "Eesti (Estonian)",
    "eu": "Euskara (Basque)",
    "fa": "فارسی (Persian)",
    "fi": "Suomi (Finnish)",
    "fr": "Français (French)",
    "fr-CA": "Français canadien (French - Canada)",
    "gl": "Galego (Galician)",
    "gu": "ગુજરાતી (Gujarati)",
    "ha": "Hausa",
    "hi": "हिन्दी (Hindi)",
    "hr": "Hrvatski (Croatian)",
    "hu": "Magyar (Hungarian)",
    "id": "Bahasa Indonesia (Indonesian)",
    "is": "Íslenska (Icelandic)",
    "it": "Italiano (Italian)",
    "iw": "עברית (Hebrew)",
    "ja": "日本語 (Japanese)",
    "jw": "Basa Jawa (Javanese)",
    "ka": "ქართული (Georgian)",
    "kk": "Қазақ тілі (Kazakh)",
    "km": "ខ្មែរ (Khmer)",
    "kn": "ಕನ್ನಡ (Kannada)",
    "ko": "한국어 (Korean)",
    "ku": "Kurdî (Kurdish)",
    "la": "Latina (Latin)",
    "lb": "Lëtzebuergesch (Luxembourgish)",
    "lt": "Lietuvių (Lithuanian)",
    "lv": "Latviešu (Latvian)",
    "ml": "മലയാളം (Malayalam)",
    "mr": "मराठी (Marathi)",
    "ms": "Bahasa Melayu (Malay)",
    "my": "မြန်မာ (Myanmar / Burmese)",
    "ne": "नेपाली (Nepali)",
    "nl": "Nederlands (Dutch)",
    "no": "Norsk (Norwegian)",
    "pa": "ਪੰਜਾਬੀ (Punjabi)",
    "pl": "Polski (Polish)",
    "pt": "Português (Portuguese - Brazil)",
    "pt-PT": "Português europeu (Portuguese - Portugal)",
    "ro": "Română (Romanian)",
    "ru": "Русский (Russian)",
    "si": "සිංහල (Sinhala)",
    "sk": "Slovenčina (Slovak)",
    "sl": "Slovenščina (Slovenian)",
    "sq": "Shqip (Albanian)",
    "sr": "Српски (Serbian)",
    "su": "Basa Sunda (Sundanese)",
    "sv": "Svenska (Swedish)",
    "sw": "Kiswahili (Swahili)",
    "ta": "தமிழ் (Tamil)",
    "te": "తెలుగు (Telugu)",
    "th": "ไทย (Thai)",
    "tl": "Tagalog (Filipino)",
    "tr": "Türkçe (Turkish)",
    "uk": "Українська (Ukrainian)",
    "ur": "اردو (Urdu)",
    "vi": "Tiếng Việt (Vietnamese)",
    "yue": "粵語 (Cantonese)",
    "zh": "中文 (Chinese)",
    "zh-CN": "中文（简体） (Chinese - Simplified)",
    "zh-TW": "中文（繁體） (Chinese - Traditional)",
}

LANGUAGE_NAMES: dict[str, str] = {
    "afrikaans": "af", "amharic": "am", "arabic": "ar", "bulgarian": "bg", "bengali": "bn", "bosnian": "bs",
    "catalan": "ca", "czech": "cs", "welsh": "cy", "danish": "da", "german": "de", "greek": "el",
    "english": "en", "spanish": "es", "estonian": "et", "basque": "eu", "persian": "fa", "farsi": "fa",
    "finnish": "fi", "french": "fr", "french canadian": "fr-CA", "canadian french": "fr-CA", "galician": "gl",
    "gujarati": "gu", "hausa": "ha", "hindi": "hi", "croatian": "hr", "hungarian": "hu", "indonesian": "id",
    "icelandic": "is", "italian": "it", "hebrew": "iw", "japanese": "ja", "javanese": "jw", "georgian": "ka",
    "kazakh": "kk", "khmer": "km", "kannada": "kn", "korean": "ko", "kurdish": "ku", "latin": "la",
    "luxembourgish": "lb", "lithuanian": "lt", "latvian": "lv", "malayalam": "ml", "marathi": "mr",
    "malay": "ms", "burmese": "my", "nepali": "ne", "dutch": "nl", "norwegian": "no", "punjabi": "pa",
    "polish": "pl", "portuguese": "pt", "portuguese brazilian": "pt", "brazilian portuguese": "pt",
    "portuguese european": "pt-PT", "european portuguese": "pt-PT", "romanian": "ro", "russian": "ru",
    "sinhala": "si", "slovak": "sk", "slovenian": "sl", "albanian": "sq", "serbian": "sr", "sundanese": "su",
    "swedish": "sv", "swahili": "sw", "tamil": "ta", "telugu": "te", "thai": "th", "filipino": "tl",
    "tagalog": "tl", "turkish": "tr", "ukrainian": "uk", "urdu": "ur", "vietnamese": "vi", "cantonese": "yue",
    "chinese": "zh", "mandarin": "zh", "chinese simplified": "zh-CN", "simplified chinese": "zh-CN",
    "chinese traditional": "zh-TW", "traditional chinese": "zh-TW",
}

_IT_STOPWORDS = frozenset({
    "abbiamo", "agli", "ai", "al", "alla", "alle", "allo", "allora", "anche", "anni", "anno", "avere", "avete",
    "che", "chi", "coi", "col", "come", "con", "così", "cui", "dagli", "dai", "dal", "dalla", "dalle", "dallo",
    "degli", "dei", "del", "della", "delle", "dello", "di", "dove", "essere", "fra", "giorni", "giorno", "già",
    "giù", "gli", "hai", "hanno", "ho", "i", "il", "la", "le", "lo", "là", "lì", "ma", "mai", "mentre",
    "molto", "negli", "nei", "nel", "nella", "nelle", "nello", "niente", "ogni", "oppure", "per", "perché",
    "però", "più", "poco", "qua", "qualcosa", "quando", "quella", "quelle", "quelli", "quello", "questa",
    "queste", "questi", "questo", "qui", "quindi", "sempre", "senza", "siamo", "siete", "sono", "stata",
    "stato", "su", "sugli", "sui", "sul", "sulla", "sulle", "sullo", "tra", "tutte", "tutti", "tutto", "un",
    "una", "uno",
})

_EN_STOPWORDS = frozenset({
    "a", "already", "also", "although", "always", "am", "an", "and", "are", "at", "be", "because", "been",
    "being", "but", "by", "can", "could", "did", "do", "does", "doing", "everything", "for", "from", "had",
    "has", "have", "having", "he", "her", "here", "hers", "herself", "him", "himself", "his", "how", "however",
    "in", "into", "is", "it", "its", "itself", "may", "might", "never", "nor", "nothing", "of", "on", "or",
    "our", "ours", "ourselves", "shall", "she", "should", "so", "something", "than", "that", "the", "their",
    "theirs", "them", "themselves", "then", "there", "therefore", "these", "they", "this", "those", "though",
    "thus", "to", "today", "was", "we", "were", "what", "when", "where", "which", "while", "who", "whom",
    "whose", "why", "will", "with", "would", "yet", "you", "your", "yours", "yourself", "yourselves",
})

SUPPORTED_SET = frozenset(SUPPORTED_LANGUAGES)

# ── Normalisation ─────────────────────────────────────────────────────────────

_FENCED_CODE = re.compile(r"```(?:\w*\n?)?(.*?)```", re.DOTALL)
_INLINE_CODE = re.compile(r"`([^`]*)`")
_IMAGE = re.compile(r"!\[([^\]]*)\]\([^)]*\)")
_LINK = re.compile(r"\[([^\]]+)\]\([^)]*\)")
_REF_DEF = re.compile(r"^\s*\[[^\]]+\]:\s*\S+.*$", re.MULTILINE)
_HTML_TAG = re.compile(r"<[^>]+>")
_LINE_PREFIX = re.compile(r"^[ \t]*(?:#+|[>\-*+]|\d+[.)])\s+(?:\[[ xX]\]\s+)?", re.MULTILINE)
_RULE = re.compile(r"^[ \t]*(?:[-*_]\s*){3,}\s*$", re.MULTILINE)
_TABLE_SEP = re.compile(r"^[ \t]*\|?[ \t]*:?-{2,}:?(?:[ \t]*\|[ \t]*:?-{2,}:?)+[ \t]*\|?[ \t]*$", re.MULTILINE)
_EMPHASIS = re.compile(r"(\*{1,3}|_{1,3}|~~)(.+?)\1")
_STRAY_PUNCT = re.compile(r"[*_~`|]+")
_SPACES = re.compile(r"\s+")


def normalize_text(title: str | None, content: str | None) -> str:
    """The plain text that is read aloud: Markdown and HTML removed, code dropped."""
    title = (title or "").strip()
    body = content or ""
    body = _FENCED_CODE.sub(" ", body)
    body = _REF_DEF.sub(" ", body)
    body = _IMAGE.sub(r"\1", body)
    body = _LINK.sub(r"\1", body)
    body = _INLINE_CODE.sub(r"\1", body)
    body = _EMPHASIS.sub(r"\2", body)
    body = _TABLE_SEP.sub(" ", body)
    body = _RULE.sub(" ", body)
    body = _LINE_PREFIX.sub("", body)
    body = _HTML_TAG.sub(" ", body)
    body = _STRAY_PUNCT.sub(" ", body)
    body = unicodedata.normalize("NFC", body)
    body = _SPACES.sub(" ", body).strip()
    spoken = (f"{title}. {body}" if body else title) if title else body
    return spoken[:MAX_INPUT_CHARS]


def content_hash(spoken: str, language: str) -> str:
    return hashlib.sha256(((spoken or "") + "\x00" + language).encode("utf-8")).hexdigest()


# ── Languages ─────────────────────────────────────────────────────────────────


def parse_enabled_languages(value: str | Iterable[str] | None) -> tuple[str, ...]:
    """Enabled codes from the stored CSV, in catalogue order (default: en, it)."""
    if not value:
        return DEFAULT_ENABLED_LANGUAGES
    raw = value.split(",") if isinstance(value, str) else [str(item) for item in value]
    chosen = {code.strip() for code in raw} & SUPPORTED_SET
    if not chosen:
        return DEFAULT_ENABLED_LANGUAGES
    return tuple(code for code in SUPPORTED_LANGUAGES if code in chosen)


def serialize_enabled_languages(codes: Iterable[str]) -> str:
    chosen = set(codes) & SUPPORTED_SET
    return ",".join(code for code in SUPPORTED_LANGUAGES if code in chosen)


def fallback_language(default: str, allowed: Iterable[str]) -> str:
    allowed_set = frozenset(allowed)
    if default in allowed_set:
        return default
    if FALLBACK_LANGUAGE in allowed_set:
        return FALLBACK_LANGUAGE
    return sorted(allowed_set)[0] if allowed_set else FALLBACK_LANGUAGE


def normalize_language(value: object, *, allowed: Iterable[str] = SUPPORTED_LANGUAGES,
                       default: str = FALLBACK_LANGUAGE) -> str | None:
    """Canonical code for *value* (code or English name) if it is allowed, else None."""
    allowed_set = frozenset(allowed)
    text = str(value or "").strip()
    if not text:
        return None
    if "-" in text:
        lang, region = text.split("-", 1)
        canonical = f"{lang.lower()}-{region.upper()}"
    else:
        canonical = text.lower()
    candidates = [canonical]
    if "-" in canonical:
        candidates.append(canonical.split("-", 1)[0])
    candidates.append(LANGUAGE_NAMES.get(canonical.replace("-", " ").replace("_", " "), ""))
    for code in candidates:
        if code in SUPPORTED_SET and code in allowed_set:
            return code
    return None


_IT_CHARS = re.compile(r"[àèéìòùîêë]", re.IGNORECASE)
_IT_DIGRAPHS = re.compile(r"\b(?:gli|gn|sci|sce|ź)", re.IGNORECASE)
_WORD = re.compile(r"[A-Za-zÀ-ſ]{2,}")
_LANGDETECT_CODES = {"he": "iw", "jv": "jw", "zh-cn": "zh-CN", "zh-tw": "zh-TW", "pt-br": "pt"}


def _heuristic_en_it(text: str, default: str) -> str:
    sample = text[:MAX_INPUT_CHARS].lower()
    words = _WORD.findall(sample)
    if len(words) < 5:
        return default
    it_score = sum(1 for word in words if word in _IT_STOPWORDS)
    en_score = sum(1 for word in words if word in _EN_STOPWORDS)
    it_score += len(_IT_CHARS.findall(sample)) + min(len(_IT_DIGRAPHS.findall(sample)), 20)
    if it_score == en_score:
        return default
    return "it" if it_score > en_score else "en"


def _has_non_latin_letter(text: str) -> bool:
    for char in text:
        if unicodedata.category(char).startswith("L") and not (
            "A" <= char <= "Z" or "a" <= char <= "z" or "À" <= char <= "ſ"
        ):
            return True
    return False


def langdetect_available() -> bool:
    return importlib.util.find_spec("langdetect") is not None


def _langdetect(sample: str) -> str | None:
    """langdetect's guess mapped onto the catalogue, or None (lazy optional import)."""
    try:
        from langdetect import DetectorFactory, detect  # type: ignore[import-not-found]
        from langdetect.lang_detect_exception import LangDetectException  # type: ignore[import-not-found]
    except ImportError:
        return None
    DetectorFactory.seed = 0
    try:
        raw = str(detect(sample)).lower()
    except LangDetectException:
        return None
    except Exception:  # noqa: BLE001 - detection must never break a page save
        log.warning("langdetect failed", exc_info=True)
        return None
    code = _LANGDETECT_CODES.get(raw, raw)
    if code in SUPPORTED_SET:
        return code
    family = code.split("-", 1)[0]
    return family if family in SUPPORTED_SET else None


def detect_language(text: str, *, allowed: Iterable[str], default: str = FALLBACK_LANGUAGE) -> str:
    """Best-guess language of *text*, always one of *allowed*.

    Short Latin-script snippets skip langdetect, which guesses wildly on a
    few words (file names, titles), and use the stop-word heuristic.
    """
    allowed_set = frozenset(allowed)
    if not text:
        return fallback_language(default, allowed_set)
    sample = text[:MAX_INPUT_CHARS]
    words = _WORD.findall(sample.lower())
    if len(words) >= LANGDETECT_MIN_LATIN_WORDS or _has_non_latin_letter(sample):
        detected = _langdetect(sample)
        if detected in allowed_set:
            return detected  # type: ignore[return-value]
    guess = _heuristic_en_it(sample, default)
    return guess if guess in allowed_set else fallback_language(default, allowed_set)


# ── Playback speed ────────────────────────────────────────────────────────────


def normalize_speed(value: object, default: float = DEFAULT_SPEED) -> float:
    """Snap *value* to one of :data:`SPEED_PRESETS` (anything else gives *default*)."""
    try:
        number = float(str(value).strip().replace(",", "."))
    except (TypeError, ValueError):
        return float(default)
    for preset in SPEED_PRESETS:
        if abs(number - preset) < 0.05:
            return float(preset)
    return float(default)


def speed_tag(speed: float) -> str:
    """``1.25`` -> ``"1.25"``, ``2.0`` -> ``"2"`` (file names and labels)."""
    return f"{speed:.2f}".rstrip("0").rstrip(".")

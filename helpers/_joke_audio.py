"""Joke audio format detection and conversion.

When users upload .mp5 or .mp7 files (a tongue-in-cheek reference to the
MP5 and MP7 submachine guns), BananaWiki accepts them and quietly converts
them back to .mp3 using ffmpeg.  If the file is just a renamed MP3 this
works fine; if the file is genuinely corrupt or not audio at all,
the conversion fails and the user sees a joke error message.
"""

import logging
import os
import shutil
import subprocess
import tempfile

_logger = logging.getLogger("bananawiki")

# Joke audio extensions → the real extension they should become.
JOKE_AUDIO_MAP = {
    "mp5": "mp3",
    "mp7": "mp3",
}

JOKE_AUDIO_EXTENSIONS = frozenset(JOKE_AUDIO_MAP.keys())

# Silly messages shown to the user when conversion succeeds or fails.
_SUCCESS_MESSAGES = {
    "mp5": "Weapon acquired! MP5 locked and loaded, converting to MP3... 🔫",
    "mp7": "Target neutralized! MP7 armed and converting to MP3... 🎯",
}
_FAIL_MESSAGES = {
    "mp5": "Misfire! MP5 rounds depleted: file is corrupt or not valid audio.",
    "mp7": "Malfunction! MP7 out of ammo: file is corrupt or not valid audio.",
}


def is_joke_audio_extension(ext):
    """Return True if *ext* is a joke audio extension (mp5, mp7)."""
    return ext is not None and ext.lower() in JOKE_AUDIO_EXTENSIONS


def get_real_audio_ext(joke_ext):
    """Return the real audio extension for a joke extension.

    >>> get_real_audio_ext("mp5")
    'mp3'
    >>> get_real_audio_ext("mp7")
    'mp3'
    >>> get_real_audio_ext("mp3")
    """
    return JOKE_AUDIO_MAP.get((joke_ext or "").lower())


def get_joke_success_message(joke_ext):
    """Return the success flash message for a joke audio conversion."""
    return _SUCCESS_MESSAGES.get(joke_ext.lower(), "Audio file accepted!")


def get_joke_fail_message(joke_ext):
    """Return the failure flash message for a joke audio conversion."""
    return _FAIL_MESSAGES.get(
        joke_ext.lower(),
        "The file appears to be corrupt or not a valid audio file.",
    )


def _ffmpeg_binary():
    """Return the path to ffmpeg, or None if not found."""
    return shutil.which("ffmpeg")


def convert_joke_audio(src_path, dest_path):
    """Convert a joke audio file (mp5/mp7) to mp3 using ffmpeg.

    Writes to a temporary file first, then atomically moves it into
    *dest_path* on success so the original is never partially overwritten.

    Returns (ok, error_message) where *ok* is True on success and
    *error_message* is None on success or a human-readable string on failure.
    """
    binary = _ffmpeg_binary()
    if not binary:
        return False, "ffmpeg is not installed on this server; cannot convert audio."

    tmp_fd, tmp_path = tempfile.mkstemp(suffix=".mp3")
    os.close(tmp_fd)
    try:
        cmd = [
            binary, "-y",
            "-loglevel", "error",
            "-i", src_path,
            "-vn",
            "-acodec", "libmp3lame",
            "-q:a", "4",
            tmp_path,
        ]
        result = subprocess.run(
            cmd,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=120,
        )
        if (
            result.returncode != 0
            or not os.path.isfile(tmp_path)
            or os.path.getsize(tmp_path) == 0
        ):
            stderr = (result.stderr or b"").decode("utf-8", errors="replace").strip()
            _logger.warning(
                "ffmpeg joke-audio convert failed (rc=%s): %s",
                result.returncode,
                stderr[:400],
            )
            return False, f"ffmpeg failed to convert the audio file: {stderr[:200]}" if stderr else "ffmpeg failed to convert the audio file."

        # Success: move temp file to dest, replacing any existing file.
        shutil.move(tmp_path, dest_path)
        return True, None
    except (OSError, subprocess.SubprocessError) as exc:
        _logger.warning("ffmpeg joke-audio convert raised: %s", exc, exc_info=True)
        return False, f"Audio conversion failed: {exc}"
    finally:
        # Clean up temp file if it still exists (conversion failed or exception).
        if os.path.exists(tmp_path):
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

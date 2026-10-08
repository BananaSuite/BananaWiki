"""Local Piper: the text is read in pieces, in a child process bounded in time and memory."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
import time
import wave

import pytest

from bananawiki.wiki.features.tts import backends, options, piper_child, text

LONG_LIST = " ".join(f"item{number:04d}" for number in range(2_400))[:text.MAX_INPUT_CHARS]

# A stand-in for piper-tts: refuses pieces longer than the chunk size (a real
# voice would exhaust the memory) and reacts to a few command words.
FAKE_PIPER = textwrap.dedent("""
    import json, os, signal, time

    class SynthesisConfig:
        def __init__(self, **scales):
            self.scales = scales

    class PiperVoice:
        @staticmethod
        def load(model, config_path=None):
            import resource
            with open(model + ".seen", "w") as seen:
                json.dump({"pid": os.getpid(), "address_space": resource.getrlimit(resource.RLIMIT_AS)[0],
                           "arenas": os.environ.get("MALLOC_ARENA_MAX"),
                           "telemetry": os.environ.get("ORT_DISABLE_TELEMETRY")}, seen)
            return PiperVoice()

        def synthesize_wav(self, chunk, wav_file, syn_config=None):
            if len(chunk) > 500:
                raise MemoryError
            if "allocate" in chunk:
                bytearray(1024 ** 3)
            if "hang" in chunk:
                time.sleep(60)
            if "crash" in chunk:
                os.kill(os.getpid(), signal.SIGKILL)  # what the kernel does when memory runs out
            wav_file.setnchannels(1)
            wav_file.setsampwidth(2)
            wav_file.setframerate(16000)
            wav_file.writeframes(b"\\x01\\x00" * len(chunk))
""")

posix_only = pytest.mark.skipif(os.name != "posix", reason="Piper runs in a child process on POSIX only")


def config(tmp_path, **values):
    return options.TtsConfig(folder=str(tmp_path), piper_voice_dir=str(tmp_path / "voices"),
                             piper_output_format="wav", **values)


def frames(audio: backends.Audio) -> int:
    with wave.open(str(audio.path), "rb") as wav:
        return wav.getnframes()


# ── Pieces ────────────────────────────────────────────────────────────────────


def test_text_without_full_stops_is_cut_at_spaces():
    pieces = text.speech_chunks(LONG_LIST)
    assert len(LONG_LIST) == text.MAX_INPUT_CHARS
    assert all(len(piece) <= text.CHUNK_CHARS for piece in pieces)
    assert len(pieces) <= 2 * len(LONG_LIST) // text.CHUNK_CHARS + 1
    assert " ".join(pieces) == LONG_LIST


def test_pieces_end_after_a_sentence_when_they_can():
    spoken = ("This sentence about bananas is long enough to count for something. " * 40).strip()
    pieces = text.speech_chunks(spoken)
    assert len(pieces) > 1 and all(piece.endswith(".") and len(piece) <= text.CHUNK_CHARS for piece in pieces)
    assert " ".join(pieces) == spoken


def test_text_without_spaces_is_cut_at_the_limit():
    spoken = "汉" * 1_234
    assert [len(piece) for piece in text.speech_chunks(spoken)] == [500, 500, 234]
    assert text.speech_chunks("Hello there.") == ["Hello there."]
    assert text.speech_chunks("") == []


# ── In the worker (Windows, packaged app) ─────────────────────────────────────


class FakeVoice:
    def __init__(self):
        self.pieces: list[str] = []

    def synthesize_wav(self, chunk, wav_file, syn_config=None):
        if len(chunk) > text.CHUNK_CHARS:
            raise MemoryError("a sentence this long exhausts the server")
        self.pieces.append(chunk)
        if chunk == "silence":
            return
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(16000)
        wav_file.writeframes(b"\x01\x00" * len(chunk))


def test_in_process_piper_reads_a_long_page_in_pieces(tmp_path, monkeypatch):
    backend = backends.PiperBackend(config(tmp_path))
    voice = FakeVoice()
    monkeypatch.setattr(backends, "piper_in_child", lambda: False)
    monkeypatch.setattr(backend, "_voice", lambda _language: voice)
    monkeypatch.setattr(backend, "_syn_config", lambda: None)
    audio = backend.synthesize(LONG_LIST, "en", tmp_path)
    assert audio.extension == "wav" and len(voice.pieces) > 1
    assert frames(audio) == sum(len(piece) for piece in voice.pieces)
    with wave.open(str(audio.path), "rb") as wav:
        assert wav.getframerate() == 16000


def test_pieces_without_audio_are_skipped(tmp_path):
    output = tmp_path / "out.wav"
    assert piper_child.write_wav(FakeVoice(), ["silence", "Hello"], None, str(output)) == 5
    assert piper_child.write_wav(FakeVoice(), ["silence"], None, str(output)) == 0


# ── In a child process (POSIX) ────────────────────────────────────────────────


@pytest.fixture
def child_piper(tmp_path, monkeypatch):
    """A PiperBackend whose child process imports the stand-in above."""
    package = tmp_path / "fake" / "piper"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text(FAKE_PIPER, encoding="utf-8")
    monkeypatch.setenv("PYTHONPATH", str(package.parent))
    voices = tmp_path / "voices"
    voices.mkdir()
    (voices / "en_US-lessac-medium.onnx").write_bytes(b"")
    (voices / "en_US-lessac-medium.onnx.json").write_text("{}", encoding="utf-8")

    def make(**values):
        return backends.PiperBackend(config(tmp_path, **values))
    return make


def seen(tmp_path):
    return json.loads((tmp_path / "voices" / "en_US-lessac-medium.onnx.seen").read_text(encoding="utf-8"))


@posix_only
def test_piper_runs_in_a_child_process_with_an_address_space_limit(tmp_path, child_piper):
    audio = child_piper(piper_memory_mb=1024).synthesize(LONG_LIST, "en", tmp_path)
    assert frames(audio) == sum(len(piece) for piece in text.speech_chunks(LONG_LIST))
    report = seen(tmp_path)
    assert report["pid"] != os.getpid()
    assert report["address_space"] == 1024 * 1024 * 1024 and report["arenas"] == "2"
    assert report["telemetry"] == "1"
    audio.path.unlink()
    with pytest.raises(backends.SynthesisError, match="out of memory") as caught:
        child_piper(piper_memory_mb=256).synthesize("Please allocate a lot.", "en", tmp_path)
    assert caught.value.retryable is False
    assert not [name for name in os.listdir(tmp_path) if name.startswith(".part-")]


@posix_only
def test_piper_child_that_runs_too_long_is_stopped(tmp_path, child_piper):
    started = time.monotonic()
    with pytest.raises(backends.SynthesisError, match="longer than 1 seconds") as caught:
        child_piper(max_job_seconds=1).synthesize("Please hang here.", "en", tmp_path)
    assert caught.value.retryable is False and time.monotonic() - started < 10


@posix_only
def test_piper_crash_fails_the_job_not_the_worker(tmp_path, child_piper):
    with pytest.raises(backends.SynthesisError, match="signal") as caught:
        child_piper().synthesize("This will crash.", "en", tmp_path)
    assert caught.value.retryable is True


@posix_only
def test_piper_child_leaves_with_its_worker(tmp_path, child_piper):
    request = {"model": str(tmp_path / "voices" / "en_US-lessac-medium.onnx"), "config": "x", "chunks": ["hang"],
               "scales": [1, 0.5, 0.5], "output": str(tmp_path / "out.wav"), "memory_mb": 0}
    worker = textwrap.dedent(f"""
        import os, subprocess, sys, time
        child = subprocess.Popen([sys.executable, "-P", {str(backends.PIPER_CHILD)!r}], stdin=subprocess.PIPE,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        child.stdin.write({json.dumps(request).encode()!r})
        child.stdin.close()
        print(child.pid, flush=True)
        time.sleep(1.5)
        os._exit(0)
    """)
    pid = int(subprocess.run([sys.executable, "-c", worker], capture_output=True, timeout=30).stdout)
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline and _running(pid):
        time.sleep(0.2)
    assert not _running(pid)


def _running(pid: int) -> bool:
    try:
        with open(f"/proc/{pid}/stat", encoding="ascii") as stat:
            return stat.read().rsplit(")", 1)[1].split()[0] != "Z"
    except FileNotFoundError:
        return False

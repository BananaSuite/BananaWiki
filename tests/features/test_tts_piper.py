"""Local Piper: the text is read in pieces, in a child process bounded in time and memory."""

from __future__ import annotations

import errno
import json
import os
import subprocess
import sys
import textwrap
import time
import wave
from pathlib import Path

import pytest

from bananawiki.wiki.features.tts import backends, options, piper_child, text

LONG_LIST = " ".join(f"item{number:04d}" for number in range(2_400))[:text.MAX_INPUT_CHARS]

# A stand-in for piper-tts: refuses pieces longer than the chunk size (a real
# voice would exhaust the memory) and reacts to a few command words.
# FAKE_PIPER_IMPORT makes the import itself fail as it does without memory or a library.
FAKE_PIPER = textwrap.dedent("""
    import errno, json, os, signal, time

    if os.environ.get("FAKE_PIPER_IMPORT") == "memory":
        raise MemoryError  # numpy starting its threads
    if os.environ.get("FAKE_PIPER_IMPORT") == "map":
        raise ImportError("libscipy_openblas64_.so: failed to map segment from shared object")
    if os.environ.get("FAKE_PIPER_IMPORT") == "enomem":
        raise OSError(errno.ENOMEM, "Cannot allocate memory")
    if os.environ.get("FAKE_PIPER_IMPORT") == "missing":
        raise ImportError("No module named 'onnxruntime'")

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
                           "blas_threads": os.environ.get("OPENBLAS_NUM_THREADS"),
                           "telemetry": os.environ.get("ORT_DISABLE_TELEMETRY")}, seen)
            return PiperVoice()

        def synthesize_wav(self, chunk, wav_file, syn_config=None):
            if len(chunk) > 500:
                raise MemoryError
            if "allocate" in chunk:
                bytearray(1024 ** 3)
            if "arena" in chunk:  # how ONNX Runtime reports an allocation that failed
                raise RuntimeError(
                    "[ONNXRuntimeError] : 1 : FAIL : Non-zero status code returned while running Conv node. "
                    "Name:'/dec/resblocks.8/convs.1/Conv' Status Message: /onnxruntime_src/onnxruntime/core/"
                    "framework/bfc_arena.cc:360 void* onnxruntime::BFCArena::AllocateRawInternal(size_t, bool, "
                    "onnxruntime::Stream*) Failed to allocate memory for requested buffer of size 4194304")
            if "bad alloc" in chunk:
                raise RuntimeError("[ONNXRuntimeError] : 6 : RUNTIME_EXCEPTION : Non-zero status code returned "
                                   "while running Add node. Name:'/dec/resblocks.8/Add' Status Message: std::bad_alloc")
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


# Pieces


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


# In the worker (Windows, packaged app)


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


class FailingVoice:
    def __init__(self, error: Exception):
        self.error = error

    def synthesize_wav(self, chunk, wav_file, syn_config=None):
        raise self.error


@pytest.mark.parametrize(("error", "retryable"), [
    (MemoryError(), False),
    (RuntimeError("[ONNXRuntimeError] : 1 : FAIL : Load model from it.onnx failed:std::bad_alloc"), False),
    (RuntimeError("Status Message: onnxruntime::BFCArena::AllocateRawInternal Failed to allocate memory"), False),
    (OSError(errno.ENOMEM, "Cannot allocate memory"), False),
    (RuntimeError("[ONNXRuntimeError] : 2 : INVALID_ARGUMENT : Unexpected input data type"), True),
])
def test_in_process_piper_out_of_memory_is_not_retried(tmp_path, monkeypatch, error, retryable):
    """The same page would run out of memory again: only other engine errors are worth a retry."""
    backend = backends.PiperBackend(config(tmp_path))
    monkeypatch.setattr(backends, "piper_in_child", lambda: False)
    monkeypatch.setattr(backend, "_voice", lambda _language: FailingVoice(error))
    monkeypatch.setattr(backend, "_syn_config", lambda: None)
    with pytest.raises(backends.SynthesisError) as caught:
        backend.synthesize("Hello there.", "en", tmp_path)
    assert caught.value.retryable is retryable
    assert ("out of memory" in str(caught.value)) is not retryable
    assert not [name for name in os.listdir(tmp_path) if name.startswith(".part-")]


# In a child process (POSIX)


@pytest.fixture
def child_piper(tmp_path, monkeypatch):
    """A PiperBackend whose child process imports the stand-in above."""
    package = tmp_path / "fake" / "piper"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text(FAKE_PIPER, encoding="utf-8")
    monkeypatch.setenv("PYTHONPATH", str(package.parent))
    for name in ("MALLOC_ARENA_MAX", "OPENBLAS_NUM_THREADS"):  # what the worker sets, not the developer's own
        monkeypatch.delenv(name, raising=False)
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
    assert report["blas_threads"] == "1" and report["telemetry"] == "1"
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
@pytest.mark.parametrize("words", ["The arena is full.", "A bad alloc here."])
def test_onnx_runtime_allocation_failures_are_out_of_memory(tmp_path, child_piper, words):
    """ONNX Runtime turns a failed allocation into an ordinary error: the same page would fail again."""
    with pytest.raises(backends.SynthesisError, match="out of memory") as caught:
        child_piper(piper_memory_mb=1024).synthesize(words, "en", tmp_path)
    assert caught.value.retryable is False
    assert not [name for name in os.listdir(tmp_path) if name.startswith(".part-")]


@posix_only
@pytest.mark.parametrize(("failure", "message"), [
    ("memory", "out of memory"), ("map", "out of memory"), ("enomem", "out of memory"), ("missing", "not installed"),
])
def test_piper_that_cannot_be_imported_is_not_retried(tmp_path, child_piper, monkeypatch, failure, message):
    monkeypatch.setenv("FAKE_PIPER_IMPORT", failure)
    with pytest.raises(backends.SynthesisError, match=message) as caught:
        child_piper().synthesize("Hello there.", "en", tmp_path)
    assert caught.value.retryable is False


@posix_only
def test_piper_threads_are_bounded_under_the_worker_address_space_limit(tmp_path, child_piper, monkeypatch):
    """BW_MEMORY_LIMIT_MB is the worker's own RLIMIT_AS, which Piper inherits without BW_TTS_PIPER_MEMORY_MB."""
    observed = []
    for limited in (False, True):
        monkeypatch.setattr(backends, "_address_space_limited", lambda limited=limited: limited)
        child_piper().synthesize("Hello there.", "en", tmp_path).path.unlink()
        observed.append((seen(tmp_path)["arenas"], seen(tmp_path)["blas_threads"]))
    assert observed == [(None, None), ("2", "1")]
    monkeypatch.setenv("OPENBLAS_NUM_THREADS", "4")  # an operator's own value is kept
    child_piper().synthesize("Hello there.", "en", tmp_path).path.unlink()
    assert seen(tmp_path)["blas_threads"] == "4"


@posix_only
def test_the_worker_address_space_limit_is_detected():
    script = ("import resource; from bananawiki.wiki.features.tts import backends; "
              "_soft, hard = resource.getrlimit(resource.RLIMIT_AS); "
              "resource.setrlimit(resource.RLIMIT_AS, (64 * 1024 ** 3 if hard == resource.RLIM_INFINITY else hard, hard)); "
              "print(backends._address_space_limited())")
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=60,
                            cwd=Path(__file__).resolve().parents[2])
    assert result.stdout.strip() == "True", result.stderr


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


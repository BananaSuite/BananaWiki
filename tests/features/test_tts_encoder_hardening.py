"""Speech conversion rejects unrelated formats and bounds failed encoder work."""

from __future__ import annotations

import shutil
import subprocess
import sys
import time
import wave
from pathlib import Path

import pytest

from bananawiki.wiki.features.tts import backends


def make_wave(path: Path) -> None:
    with wave.open(str(path), "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(22_050)
        audio.writeframes(b"\0\0" * 22_050)


@pytest.mark.parametrize("header", [b"#EXTM3U\nhttp://localhost/audio", b"<svg>bad</svg>", b"RIFFbad AVI ", b""])
def test_unrelated_input_is_rejected_before_running_encoder(tmp_path, monkeypatch, header):
    source = tmp_path / "audio.wav"
    source.write_bytes(header)
    monkeypatch.setattr(backends, "ffmpeg_binary", lambda: "ffmpeg")
    monkeypatch.setattr(backends, "_run_encoder", lambda command: pytest.fail("Unsupported input reached FFmpeg"))
    with pytest.raises(backends.SynthesisError, match="only MP3 or PCM WAV") as rejected:
        backends.encode_mp3(source, tmp_path / "out.mp3")
    assert rejected.value.retryable is False


@pytest.mark.parametrize("speed", [0.5, 1.0, 1.25, 2.0])
def test_real_pcm_and_mp3_conversion_keeps_speech_and_speed_downloads(tmp_path, monkeypatch, speed):
    binary = shutil.which("ffmpeg")
    if binary is None:
        pytest.skip("This check requires the deployment FFmpeg binary")
    monkeypatch.setattr(backends, "ffmpeg_binary", lambda: binary)
    source, mp3, variant = (tmp_path / name for name in ("input.wav", "normal.mp3", "variant.mp3"))
    make_wave(source)
    backends.encode_mp3(source, mp3)
    backends.encode_mp3(mp3, variant, speed=speed)
    assert backends.looks_like_audio(mp3.read_bytes()[:16]) == "mp3"
    assert backends.looks_like_audio(variant.read_bytes()[:16]) == "mp3"
    assert variant.stat().st_size > 100
    if speed != 1.0:
        assert variant.stat().st_size != mp3.stat().st_size


def test_encoder_allows_only_local_audio_and_removes_metadata(tmp_path, monkeypatch):
    source, target = tmp_path / "input.wav", tmp_path / "output.mp3"
    make_wave(source)
    monkeypatch.setattr(backends, "ffmpeg_binary", lambda: "ffmpeg")
    commands = []

    def run(command):
        commands.append(command)
        target.write_bytes(b"ID3" + b"\0" * 100)
        return 0, b""

    monkeypatch.setattr(backends, "_run_encoder", run)
    backends.encode_mp3(source, target, speed=1.5)
    command = commands[0]
    for name, value in (("-protocol_whitelist", "file"), ("-codec_whitelist", backends._AUDIO_DECODERS),
                        ("-f", "wav"), ("-map", "0:a:0"), ("-map_metadata", "-1"),
                        ("-filter_threads", "1"), ("-filter:a", "atempo=1.5000")):
        assert command[command.index(name) + 1] == value
    assert command.count("-threads") == 2
    assert "-vn" in command and "-sn" in command and "-dn" in command


def test_encoder_flood_is_killed_without_retaining_unbounded_diagnostics(monkeypatch):
    monkeypatch.setattr(backends, "MAX_ENCODER_DIAGNOSTICS", 8192)
    script = "import os; chunk=b'x'*4096\nwhile True: os.write(2,chunk)"
    started = time.monotonic()
    with pytest.raises(backends.SynthesisError, match="diagnostic limit") as rejected:
        backends._run_encoder([sys.executable, "-c", script])
    assert rejected.value.retryable is False
    assert time.monotonic() - started < 5


def test_encoder_timeout_reaps_process_and_next_conversion_recovers(monkeypatch):
    monkeypatch.setattr(backends, "FFMPEG_TIMEOUT", 0.1)
    started = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        backends._run_encoder([sys.executable, "-c", "import time; time.sleep(60)"])
    assert time.monotonic() - started < 5
    assert backends._run_encoder([sys.executable, "-c", "import sys; sys.stderr.write('ready')"]) == (0, b"ready")

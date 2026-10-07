"""Run in either built container: docker run ... -i --entrypoint python IMAGE - < this-file.

Checks the real application encoder for every allowed WAV sample format and
playback speed, MP3 input, the standalone GPU service's pipe command, and the
compiled feature boundary. Requires only the runtime image, no test packages.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import shutil
import struct
import subprocess
import tempfile
from pathlib import Path

from bananawiki.wiki.features.tts import backends


def run(*arguments: str, data: bytes | None = None) -> bytes:
    result = subprocess.run(
        ["ffmpeg", "-hide_banner", *arguments], input=data, capture_output=True, timeout=30, check=True
    )
    return result.stdout


def listed(option: str, flag_width: int) -> set[str]:
    lines = run(option).decode().splitlines()
    return {
        line.split()[1] for line in lines
        if len(line.split()) >= 2 and len(line.split()[0]) == flag_width
        and line.split()[1] != "=" and not line.split()[0].startswith("=")
    }


def wave_bytes(codec: str, rate: int = 22_050, channels: int = 1) -> bytes:
    values = [0.25 * math.sin(2 * math.pi * 220 * sample / rate) for sample in range(rate * 2)]
    if codec == "pcm_u8":
        audio = bytes(round(128 + value * 127) for value in values for _ in range(channels))
        bits, kind = 8, 1
    elif codec == "pcm_s24le":
        audio = b"".join(
            round(value * ((1 << 23) - 1)).to_bytes(3, "little", signed=True)
            for value in values for _ in range(channels)
        )
        bits, kind = 24, 1
    else:
        fmt, bits, kind, scale = {
            "pcm_s16le": ("h", 16, 1, (1 << 15) - 1),
            "pcm_s32le": ("i", 32, 1, (1 << 31) - 1),
            "pcm_f32le": ("f", 32, 3, 1),
            "pcm_f64le": ("d", 64, 3, 1),
        }[codec]
        audio = b"".join(
            struct.pack("<" + fmt, round(value * scale) if kind == 1 else value)
            for value in values for _ in range(channels)
        )
    align = channels * bits // 8
    header = struct.pack("<HHIIHH", kind, channels, rate, rate * align, align, bits)
    body = b"WAVEfmt " + struct.pack("<I", len(header)) + header + b"data" + struct.pack("<I", len(audio)) + audio
    return b"RIFF" + struct.pack("<I", len(body)) + body


def mp3_duration(data: bytes) -> float:
    # Parse every MPEG Layer III frame independently, without relying on FFmpeg's
    # success exit status or file length as proof that tempo changed correctly.
    position = 0
    if data.startswith(b"ID3"):
        position = 10 + sum((data[6 + index] & 0x7F) << (7 * (3 - index)) for index in range(4))
    duration, frames = 0.0, 0
    while position + 4 <= len(data):
        header = int.from_bytes(data[position:position + 4], "big")
        assert header >> 21 == 0x7FF, f"Invalid MP3 frame at {position}"
        version, layer = (header >> 19) & 3, (header >> 17) & 3
        assert version in (0, 2, 3) and layer == 1
        bitrate_index, sample_index, padding = (header >> 12) & 15, (header >> 10) & 3, (header >> 9) & 1
        assert 0 < bitrate_index < 15 and sample_index < 3
        bitrates = (0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320) if version == 3 else (
            0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160
        )
        rate = (44_100, 48_000, 32_000)[sample_index] // {3: 1, 2: 2, 0: 4}[version]
        size = ((144 if version == 3 else 72) * bitrates[bitrate_index] * 1000) // rate + padding
        assert position + size <= len(data), "Truncated MP3 frame"
        duration += (1152 if version == 3 else 576) / rate
        frames += 1
        position += size
    assert position == len(data) and frames > 2
    return duration


def main() -> None:
    assert os.getuid() != 0, "Run this verification as an unprivileged UID"
    assert set(run("-protocols").decode().split()) == {"Supported", "file", "pipe", "Input:", "Output:", "protocols:"}
    assert listed("-demuxers", 1) == {"wav", "mp3"}
    assert listed("-muxers", 1) == {"mp3"}
    assert listed("-decoders", 6) == set(backends._AUDIO_DECODERS.split(","))
    assert listed("-encoders", 6) == {"libmp3lame"}
    # FFmpeg's CLI selects atrim plus built-in video transforms/buffers even in
    # an audio-only codec build. Keep this upstream requirement explicit; no
    # video decoder, encoder or input format can supply video frames.
    assert listed("-filters", 2) == {
        "abuffer", "abuffersink", "aformat", "anull", "aresample", "atempo", "atrim",
        "buffer", "buffersink", "crop", "format", "hflip", "null", "rotate", "transpose", "trim", "vflip",
    }
    media = Path("/usr/local/share/bananawiki/media")
    assert hashlib.sha256((media / "sources/ffmpeg-9.0.2.tar.xz").read_bytes()).hexdigest() == (
        "8c3850283eb25fa026482078a04051e0be17347b09ef81a0849bec15a96e002e"
    )
    assert "VALIDSIG FCF986EA15E6E293A5644F10B4322F04D67658D8" in (media / "signature-verification.txt").read_text()
    assert (media / "COPYING.LGPLv2.1").is_file()
    assert list((media / "sources").glob("lame_*.dsc"))
    backends.ffmpeg_binary = lambda: "/usr/local/bin/ffmpeg"
    conversions = []
    with tempfile.TemporaryDirectory() as folder:
        directory = Path(folder)
        for codec in sorted(set(backends._AUDIO_DECODERS.split(",")) - {"mp3", "mp3float"}):
            source = directory / (codec + ".wav")
            source.write_bytes(wave_bytes(codec))
            for speed in (0.5, 0.75, 1.0, 1.25, 1.5, 1.75, 2.0):
                target = directory / f"{codec}-{speed}.mp3"
                backends.encode_mp3(source, target, speed=speed)
                duration = mp3_duration(target.read_bytes())
                assert abs(duration - 2 / speed) < 0.25, (codec, speed, duration)
                conversions.append({"input": codec, "speed": speed, "duration": round(duration, 4)})
        source = directory / "pcm_s16le-1.0.mp3"
        for speed in (0.5, 0.75, 1.0, 1.25, 1.5, 1.75, 2.0):
            target = directory / f"mp3-{speed}.mp3"
            backends.encode_mp3(source, target, speed=speed)
            duration = mp3_duration(target.read_bytes())
            assert abs(duration - 2 / speed) < 0.3, (speed, duration)
            conversions.append({"input": "mp3", "speed": speed, "duration": round(duration, 4)})
        # The GPU service uses this exact pipe-to-pipe command.
        data = run("-nostdin", "-loglevel", "error", "-i", "pipe:0", "-vn", "-acodec", "libmp3lame",
                   "-q:a", "4", "-f", "mp3", "pipe:1", data=wave_bytes("pcm_s16le", 48_000, 2))
        assert abs(mp3_duration(data) - 2) < 0.25
        # Both enabled MP3 decoder variants can read the produced audio.
        for decoder in ("mp3", "mp3float"):
            output = run("-loglevel", "error", "-c:a", decoder, "-f", "mp3", "-i", "pipe:0",
                         "-c:a", "libmp3lame", "-f", "mp3", "pipe:1", data=data)
            assert abs(mp3_duration(output) - 2) < 0.3
        bad = directory / "invalid.wav"
        bad.write_bytes(b"RIFF\x10\x00\x00\x00WAVEfmt broken")
        try:
            backends.encode_mp3(bad, directory / "invalid.mp3")
        except backends.SynthesisError:
            pass
        else:
            raise AssertionError("Malformed WAV accepted")
        # Encoder still succeeds after rejected input.
        backends.encode_mp3(directory / "pcm_s16le.wav", directory / "recovered.mp3")
        if destination := os.environ.get("MEDIA_VERIFY_OUTPUT"):
            shutil.copytree(directory, destination, dirs_exist_ok=True)
    print(json.dumps({"uid": os.getuid(), "ffmpeg": run("-version").decode().splitlines()[0],
                      "conversions": conversions, "gpu_pipe_stereo": "passed", "mp3_decoders": "passed",
                      "malformed_input_recovery": "passed", "features_sources_licenses": "passed"}, indent=2))


if __name__ == "__main__":
    main()

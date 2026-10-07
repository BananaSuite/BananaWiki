"""Independently decode verifier artifacts with a full reference FFmpeg build.

Usage: python verify-decoded-audio.py /path/to/MEDIA_VERIFY_OUTPUT
Checks actual sound, duration and pitch, independent of the minimal encoder's
success status and the frame parser in verify-ffmpeg.py. This reference tool
needs the PCM output codec/demuxer of a normal development FFmpeg installation.
"""

from __future__ import annotations

import array
import json
import math
import subprocess
import sys
from pathlib import Path


def main() -> None:
    directory = Path(sys.argv[1])
    results = []
    for source in sorted(directory.glob("*-*.mp3")):
        speed = float(source.stem.rsplit("-", 1)[1])
        completed = subprocess.run(
            ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-i", str(source),
             "-ac", "1", "-ar", "22050", "-acodec", "pcm_s16le", "-f", "s16le", "pipe:1"],
            capture_output=True, check=True, timeout=30,
        )
        samples = array.array("h", completed.stdout)
        if sys.byteorder != "little":
            samples.byteswap()
        duration = len(samples) / 22_050
        assert abs(duration - 2 / speed) < 0.15, (source.name, speed, duration)
        # Skip filter transients and MP3 encoder delay around the edges.
        middle = samples[5512:-5512]
        rms = math.sqrt(sum(sample * sample for sample in middle) / len(middle)) / 32768
        crossings = sum(first <= 0 < second for first, second in zip(middle, middle[1:], strict=False))
        pitch = crossings * 22_050 / len(middle)
        assert 0.15 < rms < 0.20, (source.name, rms)
        assert abs(pitch - 220) < 4, (source.name, pitch)
        results.append({"file": source.name, "duration": round(duration, 4),
                        "rms": round(rms, 4), "pitch_hz": round(pitch, 2)})
    assert len(results) == 49, f"Expected all 49 conversion artifacts, found {len(results)}"
    print(json.dumps({"decoded_conversions": results, "sound_duration_pitch": "passed"}, indent=2))


if __name__ == "__main__":
    main()

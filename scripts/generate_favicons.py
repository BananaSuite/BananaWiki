"""
BananaWiki, Favicon Generator (v2, supersampled emoji-style banana)
======================================================================
Regenerates the eight default banana favicons (32×32 PNG) stored in
``app/static/favicons/`` using the same supersampled + Lanczos renderer
as ``helpers/_favicon.py``.

Usage::

    python scripts/generate_favicons.py

Requires Pillow (already listed in requirements.txt).
"""

from __future__ import annotations

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.join(_HERE, "..")
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from helpers._favicon import BANANA_FAVICON_COLORS, generate_banana_favicon  # noqa: E402

OUTPUT_DIR = os.path.join(_HERE, "..", "app", "static", "favicons")


def main() -> None:
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    for name in BANANA_FAVICON_COLORS:
        img = generate_banana_favicon(name)
        out_path = os.path.join(OUTPUT_DIR, f"banana_{name}.png")
        img.save(out_path, format="PNG", optimize=True)
        print(f"  Saved banana_{name}.png  ({os.path.getsize(out_path)} bytes)")
    print(f"\nAll {len(BANANA_FAVICON_COLORS)} favicons written to {OUTPUT_DIR}")


if __name__ == "__main__":
    main()

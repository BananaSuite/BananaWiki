"""ZIP members the standard library unpacks safely, for every import of an uploaded archive.

A whole-site import, a board import, a hosted wiki's import, a platform
restore and a desktop restore all refuse the same members, each before it is
read: the whole-site, hosted wiki and desktop imports check the whole list up
front, a platform restore and a board import each member as they come to it:

* compressed with anything but store or deflate: ``zipfile`` bounds each read
  of a deflated member, but hands bzip2 and LZMA data to decompressors without
  an output limit, so a few kilobytes expand to gigabytes in memory before any
  size check sees them;
* with a general purpose flag ``zipfile`` cannot unpack: encryption (bit 0),
  compressed patched data (bit 5) and strong encryption (bit 6). It raises
  ``NotImplementedError`` on the last two only once the member is opened.

Exports only ever write stored and deflated members.
"""

from __future__ import annotations

import zipfile

READABLE_COMPRESSION = frozenset({zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED})
UNREADABLE_FLAGS = 0x61


def readable_member(info: zipfile.ZipInfo) -> bool:
    """Stored or deflated, neither encrypted nor patched: a member unpacked in bounded memory."""
    return info.compress_type in READABLE_COMPRESSION and not info.flag_bits & UNREADABLE_FLAGS

"""Smoke-test artifacts must not extract files through an escaping tar link."""

import io
import tarfile

import pytest

from scripts.smoke_test_easy_deployment import _extract_tar


@pytest.mark.parametrize("kind", [tarfile.SYMTYPE, tarfile.LNKTYPE])
def test_smoke_archive_rejects_external_links(tmp_path, kind):
    outside = tmp_path / "outside"
    outside.write_text("keep this")
    archive = tmp_path / "artifact.tar"
    with tarfile.open(archive, "w") as target:
        link = tarfile.TarInfo("escape")
        link.type = kind
        link.linkname = str(outside)
        target.addfile(link)
        member = tarfile.TarInfo("escape")
        member.size = len(b"replaced")
        target.addfile(member, io.BytesIO(b"replaced"))
    extracted = tmp_path / "extracted"
    extracted.mkdir()
    with pytest.raises(tarfile.FilterError):
        _extract_tar(archive, extracted)
    assert outside.read_text() == "keep this"

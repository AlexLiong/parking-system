import hashlib
import zipfile

import cv2
import numpy as np
import pytest

from tools import prepare_treeandgrass as importer


def _archive(tmp_path, monkeypatch, members):
    archive = tmp_path / "photos.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        for name, data in members.items():
            bundle.writestr(name, data)
    license_path = tmp_path / "LICENSE"
    license_path.write_text("MIT fixture", encoding="utf-8")
    monkeypatch.setattr(importer, "ARCHIVE_SHA256",
                        hashlib.sha256(archive.read_bytes()).hexdigest())
    monkeypatch.setattr(importer, "LICENSE_SHA256",
                        hashlib.sha256(license_path.read_bytes()).hexdigest())
    return archive


def test_import_keeps_unverified_labels_and_excludes_unreadable(tmp_path, monkeypatch):
    ok, encoded = cv2.imencode(".jpg", np.zeros((20, 40, 3), dtype=np.uint8))
    assert ok
    archive = _archive(tmp_path, monkeypatch, {
        "Chineselicenseplatepicture/京A12345.jpg": encoded.tobytes(),
        "Chineselicenseplatepicture/坏图.jpg": b"not a JPEG",
    })
    output_dir = tmp_path / "Chineselicenseplatepicture"
    result = importer.prepare(archive, output_dir)
    assert result["summary"]["images"] == 1
    assert result["summary"]["excluded"] == 1
    assert result["samples"][0]["plate"] is None
    assert result["samples"][0]["annotation_status"] == "filename_unverified"
    assert result["samples"][0]["photo_type"] == "unreviewed"
    assert importer.prepare(archive, output_dir)["samples"] == result["samples"]


def test_import_rejects_archive_path_escape(tmp_path, monkeypatch):
    archive = _archive(tmp_path, monkeypatch, {"../outside.jpg": b"data"})
    with pytest.raises(ValueError, match="unexpected archive path"):
        importer.prepare(archive, tmp_path / "Chineselicenseplatepicture")

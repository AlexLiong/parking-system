from __future__ import annotations

import hashlib

import pytest

from tools.eval_lpr_real import evaluate
from tools.prepare_easypr import parse_annotations


def test_easypr_xml_encoding_mismatch_is_decoded_as_utf8(tmp_path):
    xml = tmp_path / "labels.xml"
    xml.write_bytes(('<?xml version="1.0" encoding="ISO-8859-1"?>'
                     '<tagset><image><imageName>京A12345</imageName>'
                     '<taggedRectangles><taggedRectangle x="1" y="2" width="3" height="4">'
                     '蓝牌:京A12345</taggedRectangle></taggedRectangles></image></tagset>').encode("utf-8"))
    assert parse_annotations(xml)["京A12345"][0]["bbox_xywh"] == [1, 2, 3, 4]


def test_easypr_identical_duplicate_annotations_are_accepted(tmp_path):
    xml = tmp_path / "labels.xml"
    image = ('<image><imageName>京A12345</imageName><taggedRectangles>'
             '<taggedRectangle x="1" y="2" width="3" height="4">'
             '蓝牌:京A12345</taggedRectangle></taggedRectangles></image>')
    xml.write_text(f"<tagset>{image}{image}</tagset>", encoding="utf-8")
    assert len(parse_annotations(xml)) == 1


class _Result:
    plate = "京A1234"
    success = False
    need_manual = True
    message = "low confidence"
    overall_conf = 0.2


class _Recognizer:
    def recognize(self, path):
        return _Result()


def test_real_eval_counts_missing_character_and_checks_hash(tmp_path):
    image = tmp_path / "photo.jpg"
    image.write_bytes(b"photo")
    manifest = {"format_version": 1, "license": "GDSL-0.1", "source": "EasyPR",
                "source_commit": "abc", "samples": [{
                    "sample_id": "1", "file": image.name, "sha256": hashlib.sha256(b"photo").hexdigest(),
                    "plate": "京A12345", "split": "test", "source_kind": "real_photo_source",
                    "photo_type": "unreviewed", "condition": "unreviewed"}]}
    report = evaluate(manifest, tmp_path, "test", _Recognizer())
    assert report["metrics"]["char_total"] == 7
    assert report["metrics"]["char_correct"] == 6
    assert report["pr2_eligible"] is False
    image.write_bytes(b"changed")
    with pytest.raises(ValueError, match="hash mismatch"):
        evaluate(manifest, tmp_path, "test", _Recognizer())

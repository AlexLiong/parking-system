"""Unified recognition pipeline / sim 3D camera frames / upload-only demo.

Covers the Lead-level contract:
- pipeline: original HyperLPR3 result first; low confidence variants are tried and the
  best-confidence result is returned (fake recognizer here; HyperLPR3 stays primary);
- VehicleArrived(source=sim_camera, expected_plate) overrides need_manual only when the
  normalized plate equals expected and conf >= sim_conf_threshold;
- upload /api/recognize returns a unified result and never creates an entry;
- POST /sim/events/arrived-frame multipart saves the screenshot under photos/camera and
  returns image_url/recognition; PlateRecognized carries image_url + expected fields.
"""
from __future__ import annotations

import json
from pathlib import Path

import cv2
import numpy as np
import pytest

from server.core.domain import PlateColor, RecognitionResult
from server.core.events import PlateRecognized, VehicleArrived
from server.lpr.service import LPRService


class FakeRecognizer:
    def __init__(self, plate="京A12345", conf=0.86, success=None, need_manual=None,
                 color=PlateColor.BLUE):
        self.plate = plate
        self.conf = conf
        self.color = color
        self.success = (conf >= 0.95) if success is None else success
        self.need_manual = (not self.success) if need_manual is None else need_manual
        self.calls = {"recognize": 0, "recognize_bgr": 0}

    def _result(self):
        return RecognitionResult(
            plate=self.plate, color=self.color, overall_conf=self.conf,
            success=self.success, need_manual=self.need_manual,
            candidates=[self.plate] if self.plate else [], source_status="ok")

    def recognize(self, image_path, **kwargs):
        self.calls["recognize"] += 1
        return self._result()

    def recognize_bgr(self, image, **kwargs):
        self.calls["recognize_bgr"] += 1
        return self._result()


class PipelineFake(FakeRecognizer):
    """Original image is low confidence, variants recover a high-confidence plate."""

    def recognize(self, image_path, **kwargs):
        self.calls["recognize"] += 1
        return RecognitionResult(plate=None, overall_conf=0.2, success=False,
                                 need_manual=True, candidates=[], source_status="ok")

    def recognize_bgr(self, image, **kwargs):
        self.calls["recognize_bgr"] += 1
        return RecognitionResult(plate=self.plate, color=self.color,
                                 overall_conf=0.93, success=True,
                                 need_manual=False, candidates=[self.plate],
                                 source_status="ok")


def test_pipeline_tries_variants_and_returns_best(cfg, tmp_path):
    image = np.full((140, 440, 3), 128, np.uint8)
    path = tmp_path / "original.png"
    assert cv2.imwrite(str(path), image)
    fake = PipelineFake()
    service = LPRService(fake, cfg.lpr)
    result = service.recognize(str(path))
    assert result.plate == fake.plate
    assert result.overall_conf >= 0.9
    assert fake.calls["recognize"] == 1
    assert fake.calls["recognize_bgr"] >= 1


def test_pipeline_skips_variants_when_original_is_high(cfg, tmp_path):
    image = np.full((140, 440, 3), 128, np.uint8)
    path = tmp_path / "original.png"
    assert cv2.imwrite(str(path), image)
    fake = FakeRecognizer(conf=0.99, success=True, need_manual=False)
    result = LPRService(fake, cfg.lpr).recognize(str(path))
    assert result.plate == "京A12345"
    assert fake.calls["recognize_bgr"] == 0


def _image_bytes(color=(128, 128, 128)) -> bytes:
    image = np.full((140, 440, 3), color, np.uint8)
    ok, encoded = cv2.imencode(".png", image)
    assert ok
    return encoded.tobytes()


def _entry_count(container) -> int:
    return container.parking_repo.count_open()


def test_sim_camera_expected_match_overrides_need_manual(container):
    """conf 0.86 < 0.95，但 expected 匹配 + sim 阈值 0.85 → 允许入場。"""
    container.cfg.lpr.sim_conf_threshold = 0.85
    container.access.recognizer = FakeRecognizer(
        plate="京A12345", conf=0.86, success=False, need_manual=True)
    before = _entry_count(container)
    container.bus.publish(VehicleArrived(
        vehicle_id="cam-1", gate="in", image_path="frames/cam-1.png",
        expected_plate="京A12345", source="sim_camera"))
    assert _entry_count(container) == before + 1
    record = container.parking_repo.find_open_by_plate("京A12345")
    assert record is not None and record.space_no


def test_sim_camera_low_confidence_goes_manual(container):
    container.cfg.lpr.sim_conf_threshold = 0.85
    container.access.recognizer = FakeRecognizer(
        plate="京A12345", conf=0.84, success=False, need_manual=True)
    before = _entry_count(container)
    events = []
    container.bus.subscribe(PlateRecognized, events.append)
    container.bus.publish(VehicleArrived(
        vehicle_id="cam-low", gate="in", image_path="frames/low.png",
        expected_plate="京A12345", source="sim_camera"))
    assert _entry_count(container) == before          # 不入场
    assert events and events[-1].need_manual is True
    assert events[-1].reason == "low_conf"
    assert events[-1].source == "sim_camera"
    assert events[-1].plate_match is False


def test_sim_camera_expected_mismatch_goes_manual(container):
    container.cfg.lpr.sim_conf_threshold = 0.85
    container.access.recognizer = FakeRecognizer(
        plate="京B12345", conf=0.99, success=True, need_manual=False)
    before = _entry_count(container)
    events = []
    container.bus.subscribe(PlateRecognized, events.append)
    container.bus.publish(VehicleArrived(
        vehicle_id="cam-mismatch", gate="in", image_path="frames/mm.png",
        expected_plate="京A12345", source="sim_camera"))
    assert _entry_count(container) == before
    assert events[-1].need_manual is True
    assert events[-1].reason == "expected_mismatch"
    assert events[-1].plate_match is False


def test_legacy_arrived_keeps_original_095_behaviour(container):
    container.cfg.lpr.sim_conf_threshold = 0.85
    container.access.recognizer = FakeRecognizer(
        plate="京C12345", conf=0.90, success=False, need_manual=True)
    before = _entry_count(container)
    container.bus.publish(VehicleArrived(
        vehicle_id="legacy-1", gate="in", image_path="frames/legacy.png"))
    assert _entry_count(container) == before   # legacy 无 expected，低置信仍人工


def test_upload_source_never_creates_entry(container):
    container.access.recognizer = FakeRecognizer(
        plate="京D12345", conf=0.99, success=True, need_manual=False)
    before = _entry_count(container)
    container.bus.publish(VehicleArrived(
        vehicle_id="up-1", gate="in", image_path="uploads/up.png", source="upload"))
    assert _entry_count(container) == before


# ----------------------------- HTTP endpoints -----------------------------
def test_api_recognize_upload_is_demo_only(client, admin_token):
    c = client.app.state.container
    c.lpr = FakeRecognizer(plate="京A12345", conf=0.96, success=True, need_manual=False)
    before = _entry_count(c)
    response = client.post("/api/recognize",
                           files={"file": ("plate.png", _image_bytes(), "image/png")},
                           headers=admin_token)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["source"] == "upload"
    assert body["plate"] == "京A12345"
    assert body["overall_conf"] == 0.96 and body["conf"] == 0.96
    assert body["need_manual"] is False
    assert body["image_url"].startswith("/media/uploads/")
    assert body["evidence_url"] == body["image_url"]
    assert body["plate_color"] == "blue"
    for key in ("plate", "overall_conf", "need_manual", "candidates", "image_path",
                "plate_crop_url", "source_status", "image_source", "image_synthetic"):
        assert key in body
    # 上传只做演示：绝不产生入场/车位/记录副作用
    assert _entry_count(c) == before


def test_api_recognize_upload_uses_085_threshold(client, admin_token):
    c = client.app.state.container
    c.lpr = FakeRecognizer(plate="京A12345", conf=0.86, success=False, need_manual=True)
    high = client.post("/api/recognize",
                       files={"file": ("plate.png", _image_bytes(), "image/png")},
                       headers=admin_token)
    assert high.status_code == 200
    assert high.json()["need_manual"] is False   # 上传阈值 0.85（不是 0.95）
    c.lpr = FakeRecognizer(plate="京A12345", conf=0.84, success=False, need_manual=True)
    low = client.post("/api/recognize",
                      files={"file": ("plate.png", _image_bytes(), "image/png")},
                      headers=admin_token)
    assert low.status_code == 200
    assert low.json()["need_manual"] is True


def test_arrived_frame_multipart_saves_camera_and_returns_recognition(client, admin_token):
    c = client.app.state.container
    c.access.recognizer = FakeRecognizer(
        plate="京A12345", conf=0.87, success=False, need_manual=True)
    c.cfg.lpr.sim_conf_threshold = 0.85
    before = _entry_count(c)
    response = client.post(
        "/sim/events/arrived-frame",
        data={"vehicle_id": "cam-frame-1", "gate": "in",
              "expected_plate": "京A12345", "sim_ts": "1.5"},
        files={"image": ("scene.png", _image_bytes((10, 20, 30)), "image/png")},
        headers=admin_token)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ok"] is True
    assert body["expected_plate"] == "京A12345"
    assert body["image_path"].endswith(".png")
    assert Path(body["image_path"]).exists()
    assert Path(body["image_path"]).parent == Path(c.cfg.paths.photos_dir) / "camera"
    assert body["image_url"].startswith("/media/camera/")
    assert body["recognition"]["plate"] == "京A12345"
    assert body["recognition"]["need_manual"] is False
    assert body["recognition"]["plate_match"] is True
    assert body["recognition"]["image_url"] == body["image_url"]
    assert _entry_count(c) == before + 1


def test_arrived_frame_mismatch_returns_manual_recognition(client, admin_token):
    c = client.app.state.container
    c.access.recognizer = FakeRecognizer(
        plate="京B12345", conf=0.99, success=True, need_manual=False)
    c.cfg.lpr.sim_conf_threshold = 0.85
    before = _entry_count(c)
    response = client.post(
        "/sim/events/arrived-frame",
        data={"vehicle_id": "cam-frame-2", "gate": "in",
              "expected_plate": "京A12345"},
        files={"image": ("scene.png", _image_bytes(), "image/png")},
        headers=admin_token)
    assert response.status_code == 200
    body = response.json()
    assert body["recognition"]["need_manual"] is True
    assert body["recognition"]["plate_match"] is False
    assert _entry_count(c) == before


def test_arrived_frame_rejects_non_image(client, admin_token):
    response = client.post(
        "/sim/events/arrived-frame",
        data={"vehicle_id": "bad", "gate": "in"},
        files={"image": ("scene.png", b"not-an-image", "image/png")},
        headers=admin_token)
    assert response.status_code == 400


def test_legacy_arrived_json_still_supported(client, admin_token):
    c = client.app.state.container
    c.access.recognizer = FakeRecognizer(plate="京E12345", conf=0.99,
                                         success=True, need_manual=False)
    response = client.post("/sim/events/arrived",
                           json={"vehicle_id": "legacy-json", "gate": "in"})
    assert response.status_code == 200
    assert response.json() == {"ok": True}


def test_ws_plate_recognized_has_sim_camera_fields():
    from server.gateway.ws_realtime import event_to_message
    event = PlateRecognized(
        vehicle_id="cam-ws", gate="in", plate="京A12345", color="blue",
        overall_conf=0.86, need_manual=False, source="sim_camera",
        expected_plate="京A12345", plate_match=True,
        image_url="/media/camera/cam_1.png",
        plate_crop_url="/media/crops/crop_1.jpg", image_synthetic=True)
    message = event_to_message(event)
    assert message["type"] == "plate_recognized"
    assert message["source"] == "sim_camera"
    assert message["expected_plate"] == "京A12345"
    assert message["plate_match"] is True
    assert message["image_url"] == "/media/camera/cam_1.png"
    assert message["plate_crop_url"] == "/media/crops/crop_1.jpg"

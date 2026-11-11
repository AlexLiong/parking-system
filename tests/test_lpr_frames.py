"""Multi-frame recognition is bounded and fails closed on conflicting evidence."""
from __future__ import annotations

import cv2
import numpy as np

from server.core.domain import PlateColor, RecognitionResult
from server.lpr.service import LPRService


class FakeRecognizer:
    def __init__(self, plates):
        self.plates = iter(plates)

    def recognize(self, path, **kwargs):
        plate = next(self.plates)
        return RecognitionResult(plate=plate, color=PlateColor.BLUE,
                                 overall_conf=0.98, success=True,
                                 need_manual=False, char_confs=[0.98] * 7)


class FakeLogs:
    def __init__(self):
        self.rows = []

    def add(self, **kwargs):
        self.rows.append(kwargs)


def _image(tmp_path, name):
    path = tmp_path / name
    cv2.imwrite(str(path), np.full((40, 80, 3), 100 + ord(name[0]) % 20,
                                   dtype=np.uint8))
    return str(path)


def test_two_matching_frames_pass_and_each_frame_is_logged(cfg, tmp_path):
    logs = FakeLogs()
    service = LPRService(FakeRecognizer(["京A12345", "京A12345"]), cfg.lpr,
                         log_repo=logs)
    frames = []
    result = service.recognize_frames([_image(tmp_path, "1.jpg"),
                                       _image(tmp_path, "2.jpg")], frame_results=frames)
    assert result.success and not result.need_manual
    assert result.plate == "京A12345"
    assert len(frames) == 2 and len(logs.rows) == 3


def test_conflicting_frames_require_manual(cfg, tmp_path):
    service = LPRService(FakeRecognizer(["京A12345", "京A12346"]), cfg.lpr)
    result = service.recognize_frames([_image(tmp_path, "1.jpg"),
                                       _image(tmp_path, "2.jpg")])
    assert not result.success and result.need_manual
    assert "冲突" in result.message


def test_invalid_plate_cannot_pass_even_if_backend_trusts_it(cfg, tmp_path):
    service = LPRService(FakeRecognizer(["京A12?45", "京A12?45"]), cfg.lpr)
    result = service.recognize_frames([_image(tmp_path, "1.jpg"),
                                       _image(tmp_path, "2.jpg")])
    assert not result.success and result.need_manual


def test_single_good_frame_with_bad_frame_requires_manual(cfg, tmp_path):
    service = LPRService(FakeRecognizer(["京A12345"]), cfg.lpr)
    result = service.recognize_frames([_image(tmp_path, "good.jpg"),
                                       str(tmp_path / "missing.jpg")])
    assert not result.success and result.need_manual


def test_copied_frame_cannot_count_as_independent_evidence(cfg, tmp_path):
    first = _image(tmp_path, "1.jpg")
    second = tmp_path / "2.jpg"
    second.write_bytes((tmp_path / "1.jpg").read_bytes())
    service = LPRService(FakeRecognizer(["京A12345", "京A12345"]), cfg.lpr)
    result = service.recognize_frames([first, str(second)])
    assert not result.success and result.need_manual


def test_frames_endpoint_returns_fused_and_individual_results(client, cfg, admin_token):
    c = client.app.state.container
    c.lpr = LPRService(FakeRecognizer(["京A12345", "京A12345"]), cfg.lpr)
    ok, encoded = cv2.imencode(".jpg", np.full((40, 80, 3), 100, dtype=np.uint8))
    assert ok
    frame = encoded.tobytes()
    altered = np.full((40, 80, 3), 101, dtype=np.uint8)
    ok, second = cv2.imencode(".jpg", altered)
    assert ok
    files = [("files", ("0.jpg", frame, "image/jpeg")),
             ("files", ("1.jpg", second.tobytes(), "image/jpeg"))]
    response = client.post("/api/recognize/frames", data={"gate": "in"}, files=files)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["fused"]["success"] is True
    assert len(body["frames"]) == 2
    assert all(row["image_path"] for row in body["frames"])
    assert client.post("/api/recognize/frames", files=files[:1]).status_code == 400
    repeated = [("files", (f"{i}.jpg", frame, "image/jpeg")) for i in range(2)]
    assert client.post("/api/recognize/frames", files=repeated).status_code == 400
    bad_files = [("files", (f"{i}.jpg", b"not an image", "image/jpeg"))
                 for i in range(2)]
    assert client.post("/api/recognize/frames", files=bad_files).status_code == 400
    cfg.lpr.multiframe_enabled = False
    assert client.post("/api/recognize/frames", files=files).status_code == 503

"""P2：上传识别/多帧融合写 recognition_log；视频 API/抽帧（P3-06）测试。"""
from __future__ import annotations

import os
from pathlib import Path

import cv2
import numpy as np
import pytest

from server.core.domain import PlateColor, RecognitionResult


class FakeLPR:
    """只实现 API 所需方法的识别服务替身。"""

    def __init__(self, plate="湘N7D071", conf=0.97):
        self.plate = plate
        self.conf = conf
        self.frame_calls = 0

    def _result(self, plate=None, conf=None, message=""):
        return RecognitionResult(
            plate=plate or self.plate, color=PlateColor.BLUE,
            char_confs=[0.95] * len(plate or self.plate),
            overall_conf=self.conf if conf is None else conf,
            candidates=[self.plate], success=True, need_manual=False,
            elapsed_ms=42, message=message, source_status="ok")

    def recognize(self, image_path, **kwargs):
        return self._result()

    def recognize_frames(self, image_paths, *, gate="in", crop_dir=None,
                         frame_results=None):
        self.frame_calls += 1
        results = [self._result(plate=self.plate) for _ in image_paths]
        if frame_results is not None:
            frame_results.extend(results)
        return self._result(message=f"多帧融合({len(image_paths)}帧)")


def _png_bytes(color=(120, 120, 120)) -> bytes:
    ok, encoded = cv2.imencode(".png", np.full((120, 360, 3), color, np.uint8))
    assert ok
    return encoded.tobytes()


def _entry_count(c) -> int:
    return c.parking_repo.count_open()


def _make_video(path: Path, *, n=24, fps=12, size=(320, 240)) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"MJPG"), fps, size)
    assert writer.isOpened(), "OpenCV MJPG writer 不可用"
    for i in range(n):
        frame = np.full((size[1], size[0], 3), i * 7 % 255, np.uint8)
        cv2.putText(frame, f"F{i}", (20, 80), cv2.FONT_HERSHEY_SIMPLEX, 1.5,
                    (255, 255, 255), 3)
        writer.write(frame)
    writer.release()
    return path


def test_upload_writes_recognition_log_without_entry(client, admin_token):
    c = client.app.state.container
    c.lpr = FakeLPR(conf=0.96)
    before = _entry_count(c)
    response = client.post(
        "/api/recognize",
        files={"file": ("plate.png", _png_bytes(), "image/png")},
        headers=admin_token)
    assert response.status_code == 200, response.text
    body = response.json()
    assert isinstance(body["recognition_log_id"], int)
    row = c.recog_repo.get(body["recognition_log_id"])
    assert row is not None
    assert row["gate"] == "in"
    assert row["plate"] == "湘N7D071"
    assert row["image_path"] == body["image_path"]
    assert row["success"] is True
    assert row["overall_conf"] == pytest.approx(body["overall_conf"], abs=1e-4)
    assert _entry_count(c) == before
    # 管理端识别日志能看到上传记录
    logs = client.get("/api/recognition-logs", headers=admin_token).json()["items"]
    assert any(item["id"] == body["recognition_log_id"] for item in logs)


def test_frames_writes_fused_recognition_log_and_no_entry(client, admin_token):
    c = client.app.state.container
    c.lpr = FakeLPR(plate="京A12345", conf=0.93)
    before = _entry_count(c)
    response = client.post(
        "/api/recognize/frames",
        data={"gate": "in"},
        files=[
            ("files", ("f0.png", _png_bytes((10, 20, 30)), "image/png")),
            ("files", ("f1.png", _png_bytes((30, 40, 50)), "image/png")),
        ],
        headers=admin_token)
    assert response.status_code == 200, response.text
    body = response.json()
    assert isinstance(body["recognition_log_id"], int)
    row = c.recog_repo.get(body["recognition_log_id"])
    assert row is not None and row["plate"] == "京A12345"
    assert row["image_path"] == body["frames"][0]["image_path"]
    assert row["success"] is True
    assert c.lpr.frame_calls == 1
    assert _entry_count(c) == before


def test_upload_log_failure_does_not_break_recognition(client, admin_token, monkeypatch):
    c = client.app.state.container
    c.lpr = FakeLPR(conf=0.96)
    original = c.recog_repo.add

    def boom(**kwargs):
        raise RuntimeError("db down")

    monkeypatch.setattr(c.recog_repo, "add", boom)
    response = client.post(
        "/api/recognize",
        files={"file": ("plate.png", _png_bytes(), "image/png")},
        headers=admin_token)
    assert response.status_code == 200, response.text
    assert response.json()["recognition_log_id"] is None
    assert response.json()["plate"] == "湘N7D071"
    monkeypatch.setattr(c.recog_repo, "add", original)


def test_sample_video_file_returns_frames(tmp_path):
    from server.lpr.video import sample_video
    video = _make_video(tmp_path / "sample.avi")
    sample = sample_video(str(video), out_dir=tmp_path / "frames", frames=3,
                          interval=0.2)
    assert sample.kind == "file"
    assert len(sample.frames) == 3
    assert sample.fps > 0 and sample.width == 320 and sample.height == 240
    assert all(Path(p).exists() and Path(p).stat().st_size > 0 for p in sample.frames)
    assert cv2.imread(sample.frames[0]) is not None


def test_sample_video_errors(tmp_path):
    from server.lpr.video import VideoSourceError, is_rtsp_source, sample_video
    assert is_rtsp_source("rtsp://127.0.0.1/x")
    with pytest.raises(VideoSourceError):
        sample_video(str(tmp_path / "missing.mp4"), out_dir=tmp_path / "f")
    bad = tmp_path / "whatever.txt"
    bad.write_text("not a video", encoding="utf-8")
    with pytest.raises(VideoSourceError):
        sample_video(str(bad), out_dir=tmp_path / "f")
    tiny_limit = _make_video(tmp_path / "small.avi", n=4)
    with pytest.raises(VideoSourceError):
        sample_video(str(tiny_limit), out_dir=tmp_path / "f", frames=2,
                     max_file_bytes=16)


def test_recognize_video_endpoint_fuses_and_logs(client, admin_token):
    c = client.app.state.container
    c.lpr = FakeLPR(plate="京A12345", conf=0.92)
    before = _entry_count(c)
    video = _make_video(Path(c.cfg.paths.photos_dir) / "uploads" / "endpoint.avi")
    response = client.post("/api/recognize/video",
                           json={"path": str(video), "frames": 3,
                                 "interval": 0.2, "gate": "in"},
                           headers=admin_token)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ok"] is True and body["source_type"] == "file"
    assert len(body["frames"]) == 3
    assert body["fused"]["plate"] == "京A12345"
    assert body["fused"]["need_manual"] is False
    assert isinstance(body["recognition_log_id"], int)
    row = c.recog_repo.get(body["recognition_log_id"])
    assert row is not None and row["plate"] == "京A12345"
    assert _entry_count(c) == before


def test_recognize_video_endpoint_errors(client, admin_token):
    c = client.app.state.container
    c.lpr = FakeLPR()
    # 本地路径越界
    outside = Path(os.environ.get("WINDIR", "C:/Windows")) / "notepad.exe"
    r = client.post("/api/recognize/video",
                    json={"path": str(outside)}, headers=admin_token)
    assert r.status_code == 403
    # 不存在的本地视频
    r = client.post("/api/recognize/video",
                    json={"path": str(Path(c.cfg.paths.photos_dir) / "nope.avi")},
                    headers=admin_token)
    assert r.status_code == 400
    # 非法 rtsp 协议
    r = client.post("/api/recognize/video",
                    json={"rtsp_url": "http://127.0.0.1/x"}, headers=admin_token)
    assert r.status_code == 400
    # 帧数越界由 Pydantic 拒绝
    r = client.post("/api/recognize/video",
                    json={"path": "x.avi", "frames": 1}, headers=admin_token)
    assert r.status_code == 422
    # 同时提供 path 与 rtsp_url
    r = client.post("/api/recognize/video",
                    json={"path": "a.avi", "rtsp_url": "rtsp://x/y"},
                    headers=admin_token)
    assert r.status_code == 400


def test_recognize_video_rtsp_unreachable_clear_error(client, admin_token):
    c = client.app.state.container
    c.lpr = FakeLPR()
    r = client.post("/api/recognize/video",
                    json={"rtsp_url": "rtsp://127.0.0.1:1/none",
                          "timeout_sec": 1}, headers=admin_token)
    assert r.status_code == 400
    assert "RTSP" in r.json()["detail"]


def test_cli_help_runs():
    import os
    import subprocess
    import sys
    root = Path(__file__).resolve().parents[1]
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    proc = subprocess.run([sys.executable, str(root / "tools" / "recognize_video.py"),
                           "--help"], capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=60, env=env)
    assert proc.returncode == 0, proc.stderr
    assert "--frames" in (proc.stdout or "")

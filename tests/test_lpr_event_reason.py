from unittest.mock import Mock

import cv2
import numpy as np

from server.core.domain import PlateColor, RecognitionResult
from server.core.events import PlateRecognized, VehicleArrived
from server.gateway.ws_realtime import event_to_message
from server.main import _wire_watchdog


def test_ws_preserves_reason_and_message():
    msg = event_to_message(PlateRecognized(
        vehicle_id="v1", gate="in", need_manual=True,
        reason="duplicate_entry", message="已有在场记录"))
    assert msg["type"] == "plate_recognized"
    assert msg["reason"] == "duplicate_entry"
    assert msg["message"] == "已有在场记录"


def test_low_confidence_events_do_not_fault_camera(container):
    _wire_watchdog(container)
    for i in range(container.cfg.recovery.camera_fail_streak + 1):
        container.bus.publish(PlateRecognized(
            vehicle_id=f"v{i}", gate="in", need_manual=True,
            reason="low_conf", message="识别置信度不足"))
    assert container.watchdog.down("in") is False


def test_missing_images_fault_camera_and_business_rejection_does_not_count(container):
    _wire_watchdog(container)
    streak = container.cfg.recovery.camera_fail_streak
    for i in range(streak - 1):
        container.bus.publish(PlateRecognized(
            vehicle_id=f"v{i}", gate="in", need_manual=True,
            reason="camera_missing"))
    container.bus.publish(PlateRecognized(
        vehicle_id="duplicate", gate="in", need_manual=True,
        reason="duplicate_entry"))
    assert container.watchdog.down("in") is False
    container.bus.publish(PlateRecognized(
        vehicle_id="v-last", gate="in", need_manual=True,
        reason="image_unreadable"))
    assert container.watchdog.down("in") is True


def test_arrived_without_image_emits_camera_missing(container):
    events = []
    container.bus.subscribe(PlateRecognized, events.append)
    container.access.on_vehicle_arrived(VehicleArrived(vehicle_id="v1", gate="in"))
    assert events[-1].reason == "camera_missing"
    assert events[-1].need_manual is True


def test_recognizer_exception_emits_manual_event_without_camera_fault(container):
    events = []
    container.bus.subscribe(PlateRecognized, events.append)
    _wire_watchdog(container)
    container.access.recognizer = Mock()
    container.access.recognizer.recognize.side_effect = RuntimeError("inference failed")
    container.access.on_vehicle_arrived(VehicleArrived(
        vehicle_id="v1", gate="in", image_path="frame.jpg"))
    assert events[-1].reason == "recognize_error"
    assert events[-1].need_manual is True
    assert container.watchdog.down("in") is False


def test_duplicate_entry_reason_survives_api_event_store(client, admin_token):
    c = client.app.state.container
    c.access.manual_register("京A12345", "in", "tester")
    c.access.recognizer = Mock()
    c.access.recognizer.recognize.return_value = RecognitionResult(
        plate="京A12345", color=PlateColor.BLUE, overall_conf=0.99,
        success=True, need_manual=False)

    response = client.post("/sim/events/arrived", json={
        "vehicle_id": "duplicate", "gate": "in", "image_path": str(c.cfg.paths.photos_dir / "frame.jpg")})
    assert response.status_code == 200
    events = c.state.events()["items"]
    rejection = [e["data"] for e in events if e["name"] == "plate_recognized"
                 and e["data"].get("vehicle_id") == "duplicate"]
    assert len(rejection) == 1
    assert rejection[0]["reason"] == "duplicate_entry"
    assert "已有未出场记录" in rejection[0]["message"]
    assert c.watchdog.down("in") is False


def test_black_frames_trigger_camera_fault_and_reset_api(client, tmp_path):
    c = client.app.state.container
    black = c.cfg.paths.photos_dir / "black.png"
    assert cv2.imwrite(str(black), np.zeros((80, 160, 3), dtype=np.uint8))
    token = client.post("/api/auth/login", json={
        "username": "admin", "password": "admin123"}).json()["token"]
    headers = {"Authorization": f"Bearer {token}"}
    for i in range(c.cfg.recovery.camera_fail_streak):
        response = client.post("/sim/events/arrived", headers=headers, json={
            "vehicle_id": f"black-{i}", "gate": "in", "image_path": str(black)})
        assert response.status_code == 200
    client.cookies.clear()
    assert client.get("/api/camera/in/status").status_code == 401
    status = client.get("/api/camera/in/status", headers=headers).json()
    assert status["down"] is True
    assert status["source_fail_streak"] == c.cfg.recovery.camera_fail_streak
    assert len(status["incident_ids"]) == 1
    black_events = [item["data"] for item in c.state.events()["items"]
                    if item["name"] == "plate_recognized"
                    and item["data"]["reason"] == "camera_blank"]
    assert len(black_events) == c.cfg.recovery.camera_fail_streak
    assert all(item["need_manual"] for item in black_events)

    recovered = client.post("/api/camera/in/reset", json={}, headers=headers)
    assert recovered.status_code == 200
    assert client.get("/api/camera/in/status", headers=headers).json()["source_fail_streak"] == 0
    assert c.watchdog.down("in") is False


def test_blurry_but_nonblank_frame_does_not_fault_camera(container, tmp_path):
    _wire_watchdog(container)
    frame = tmp_path / "blur.png"
    image = np.full((80, 160, 3), 120, dtype=np.uint8)
    cv2.rectangle(image, (20, 20), (140, 60), (150, 150, 150), -1)
    assert cv2.imwrite(str(frame), image)
    container.lpr.recognizer = Mock()
    container.lpr.recognizer.recognize.return_value = RecognitionResult(
        need_manual=True, message="未定位到车牌")
    for i in range(container.cfg.recovery.camera_fail_streak + 1):
        container.access.on_vehicle_arrived(VehicleArrived(
            vehicle_id=f"blur-{i}", gate="in", image_path=str(frame)))
    assert container.watchdog.down("in") is False


def test_recognize_api_exposes_blank_image_health(client, admin_token):
    image = np.zeros((80, 160, 3), dtype=np.uint8)
    ok, encoded = cv2.imencode(".png", image)
    assert ok
    response = client.post("/api/recognize", files={
        "file": ("black.png", encoded.tobytes(), "image/png")})
    assert response.status_code == 200
    body = response.json()
    assert body["source_status"] == "blank"
    assert body["image_metrics"]["mean"] == 0
    assert body["need_manual"] is True


def test_corrupt_frame_is_source_failure(container, tmp_path):
    _wire_watchdog(container)
    events = []
    container.bus.subscribe(PlateRecognized, events.append)
    corrupt = tmp_path / "corrupt.jpg"
    corrupt.write_bytes(b"not a JPEG")
    container.access.on_vehicle_arrived(VehicleArrived(
        vehicle_id="broken", gate="in", image_path=str(corrupt)))
    assert events[-1].reason == "image_unreadable"
    assert events[-1].need_manual is True
    assert container.watchdog.details("in")["source_fail_streak"] == 1

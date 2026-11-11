"""Plate grammar must be enforced at every authorization boundary."""
from unittest.mock import Mock

import pytest

from server.core.domain import PlateColor, RecognitionResult
from server.core.errors import ValidationError
from server.core.events import VehicleArrived


@pytest.mark.parametrize("plate,color", [
    ("京O12345", "blue"),
    ("京A1234", "blue"),
    ("京AD12345", "blue"),
    ("京A12345", "green"),
    ("京A12345", "yellow"),
    ("京A123456", "green"),
])
def test_manual_register_rejects_invalid_plate(container, plate, color):
    with pytest.raises(ValidationError):
        container.access.manual_register(plate, "in", "admin", color)
    assert container.parking_repo.find_open_by_plate(plate) is None
    assert container.access._pending["in"] == 0


def test_manual_register_normalizes_separator(container):
    result = container.access.manual_register("京A·12345", "in", "admin")
    assert result["plate"] == "京A12345"
    assert container.parking_repo.find_open_by_plate("京A12345") is not None


def test_invalid_auto_recognition_creates_pending_without_authorizing(container):
    container.access.recognizer = Mock()
    container.access.recognizer.recognize.return_value = RecognitionResult(
        plate="京O12345", color=PlateColor.BLUE, overall_conf=0.99,
        success=True, need_manual=False)
    container.access.on_vehicle_arrived(VehicleArrived(
        vehicle_id="invalid-auto", gate="in", image_path="frame.jpg"))
    assert container.access.list_pending()[0]["vehicle_id"] == "invalid-auto"
    assert container.parking_repo.find_open_by_plate("京O12345") is None
    assert container.access._pending["in"] == 0


def test_manual_register_api_reports_invalid_plate(client, admin_token):
    response = client.post("/api/manual/register", headers=admin_token,
                           json={"plate": "京O12345", "color": "blue"})
    assert response.status_code == 422
    assert "第 2 位" in response.json()["message"]
    assert client.app.state.container.access._pending["in"] == 0

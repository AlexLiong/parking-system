"""想法一：识别输入图片事件字段与 URL 映射（新增测试）。

不依赖 DB/LPR：直接构造 AccessControlService 的空壳并注入假总线，
验证 PlateRecognized.image_path/image_url/plate_crop_url/来源/合成标记，
以及 WS 消息（event_to_message）与前端约定字段一致。
"""
from __future__ import annotations

from pathlib import Path

from server.access.service import AccessControlService
from server.core.config import PROJECT_ROOT
from server.core.events import PlateRecognized
from server.gateway.ws_realtime import event_to_message


class _Bus:
    def __init__(self):
        self.events = []

    def publish(self, event):
        self.events.append(event)


def _service(cfg):
    svc = object.__new__(AccessControlService)
    svc.cfg = cfg
    svc.bus = _Bus()
    return svc


def test_public_image_url_maps_datasets_and_photos(cfg):
    svc = _service(cfg)
    assert svc._public_image_url("datasets/demo-lpr/blue-000-clean.png") == \
        "/datasets/demo-lpr/blue-000-clean.png"
    assert svc._public_image_url(str(PROJECT_ROOT / "datasets/demo-lpr/green_car-000-clean.png")) == \
        "/datasets/demo-lpr/green_car-000-clean.png"
    crop = Path(cfg.paths.photos_dir) / "crops" / "crop_x.jpg"
    assert svc._public_image_url(str(crop)) == "/media/crops/crop_x.jpg"
    # 已在静态挂载命名空间内的 URL 原样保留
    assert svc._public_image_url("/media/uploads/a.jpg") == "/media/uploads/a.jpg"
    # 外部/越界路径不暴露
    assert svc._public_image_url("C:/Windows/system.ini") is None
    assert svc._public_image_url(None) is None


def test_recognized_event_carries_input_image_urls(cfg):
    svc = _service(cfg)
    svc._publish_recognized(
        "in", "E1", plate="湘N7D071", color="blue", conf=0.97,
        image_path="datasets/demo-lpr/blue-000-clean.png",
        plate_crop_path=str(Path(cfg.paths.photos_dir) / "crops" / "crop_in.jpg"),
    )
    assert len(svc.bus.events) == 1
    event = svc.bus.events[0]
    assert isinstance(event, PlateRecognized)
    assert event.image_path == "datasets/demo-lpr/blue-000-clean.png"
    assert event.image_url == "/datasets/demo-lpr/blue-000-clean.png"
    assert event.plate_crop_url == "/media/crops/crop_in.jpg"
    assert event.image_source == "demo-lpr 合成演示样例"
    assert event.image_synthetic is True

    message = event_to_message(event)
    assert message["type"] == "plate_recognized"
    assert message["image_path"] == event.image_path
    assert message["image_url"] == event.image_url
    assert message["plate_crop_url"] == event.plate_crop_url
    assert message["image_source"] == event.image_source
    assert message["image_synthetic"] is True


def test_recognized_event_without_image_still_valid(cfg):
    svc = _service(cfg)
    svc._publish_recognized("in", "E2", need_manual=True, reason="camera_fault")
    event = svc.bus.events[0]
    assert event.image_url is None and event.plate_crop_url is None
    message = event_to_message(event)
    assert message["image_url"] is None
    assert message["image_synthetic"] is None

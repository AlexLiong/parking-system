"""进出场控制场景测试（access/service.py）——事件驱动全流程。

用 FakeRecognizer 注入确定识别结果，验证：
- 入口授权（落库 + 占位 + 抬杆）、低置信度转人工（禁止静默放行）、摄像头故障降级、
  重复进场拦截、满位拒绝；
- 出口报价 → 缴费 → 出场落库 + 释放车位 + 抬杆（先落库后动作）。
"""
from __future__ import annotations

from server.core.domain import PlateColor, RecognitionResult
from server.core.events import VehicleArrived


class FakeRecognizer:
    """按车牌返回确定识别结果（与真实 CV 流水线解耦，仅测业务编排）。"""

    def __init__(self, plate="琼A12345", color=PlateColor.BLUE, conf=0.97,
                 need_manual=False, success=True):
        self.plate = plate
        self.color = color
        self.conf = conf
        self.need_manual = need_manual
        self.success = success

    def recognize(self, image_path, **kwargs):
        return RecognitionResult(
            plate=self.plate if not self.need_manual else self.plate,
            color=self.color, char_confs=[self.conf] * 7,
            overall_conf=self.conf, candidates=[self.plate],
            success=self.success and not self.need_manual,
            need_manual=self.need_manual)


def _arrive(container, gate, vehicle_id="v1", image_path="/tmp/plate.jpg"):
    container.bus.publish(VehicleArrived(vehicle_id=vehicle_id, gate=gate,
                                         image_path=image_path))


def test_entry_authorizes_and_opens_barrier(container):
    container.access.recognizer = FakeRecognizer("琼A12345")
    _arrive(container, "in")
    rec = container.parking_repo.find_open_by_plate("琼A12345")
    assert rec is not None
    assert rec.status == "in"
    # 车位被占用且车牌一致
    sp = container.space_repo.get(rec.space_no)
    assert sp.status == "occupied" and sp.current_plate == "琼A12345"
    # 抬杆（先落库后动作）
    assert container.access.barriers["in"].state.value == "OPENING"


def test_low_confidence_goes_manual_no_entry(container):
    container.access.recognizer = FakeRecognizer("琼A12345", conf=0.4, need_manual=True)
    _arrive(container, "in")
    # 禁止静默放行：不建记录、不抬杆
    assert container.parking_repo.find_open_by_plate("琼A12345") is None
    assert container.access.barriers["in"].state.value == "CLOSED"


def test_no_image_goes_manual(container):
    container.access.recognizer = FakeRecognizer("琼A12345")
    _arrive(container, "in", image_path=None)
    assert container.parking_repo.find_open_by_plate("琼A12345") is None


def test_camera_fault_degrades_to_manual(container):
    container.access.recognizer = FakeRecognizer("琼A12345")
    container.watchdog.inject_fault("in", reason="test")
    _arrive(container, "in")
    # 摄像头故障：不建记录（转人工录入）
    assert container.parking_repo.find_open_by_plate("琼A12345") is None
    assert container.access.barriers["in"].state.value == "CLOSED"


def test_duplicate_entry_rejected(container):
    container.access.recognizer = FakeRecognizer("琼A12345")
    _arrive(container, "in", vehicle_id="v1")
    _arrive(container, "in", vehicle_id="v2")   # 同车牌再次进场
    # 仍只有 1 条在场记录
    assert container.parking_repo.count_open() == 1


def test_lot_full_rejected(container):
    with container.db.scope():
        for sp in container.space_repo.list_active():
            container.space_repo.occupy(sp.space_no, f"X{sp.space_no}")
    container.access.recognizer = FakeRecognizer("琼A12345")
    _arrive(container, "in")
    assert container.parking_repo.find_open_by_plate("琼A12345") is None
    assert container.access.barriers["in"].state.value == "CLOSED"


def test_exit_quote_and_payment_flow(container):
    container.access.recognizer = FakeRecognizer("琼A12345")
    _arrive(container, "in")
    rec = container.parking_repo.find_open_by_plate("琼A12345")
    space_no = rec.space_no

    # 实现 A：出口识别 → 自动结算（AutoPaid）→ out + 释放车位 + 开出口
    _arrive(container, "out", vehicle_id="v1")
    rec2 = container.parking_repo.get(rec.id)
    assert rec2.status == "out" and rec2.exit_time is not None
    pay = container.payment_repo.get_by_parking(rec.id)
    assert pay is not None and pay.amount_paid >= 0.0 and pay.method == "auto"
    assert container.space_repo.get(space_no).status == "free"
    assert container.access.barriers["out"].state.value == "OPENING"
    # 剧本 /api/pay 幂等兼容：返回已存在支付，不再重复扣费
    again = container.billing.pay(rec.id, method="mock_scan", operator="admin")
    assert again.id == pay.id


def test_payment_idempotent(container):
    container.access.recognizer = FakeRecognizer("琼A12345")
    _arrive(container, "in")
    rec = container.parking_repo.find_open_by_plate("琼A12345")
    p1 = container.billing.pay(rec.id, operator="admin")
    p2 = container.billing.pay(rec.id, operator="admin")   # 重复缴费
    assert p1.id == p2.id


def test_exit_without_record_does_not_crash(container):
    container.access.recognizer = FakeRecognizer("琼A99999")
    # 直接出场（无在场记录）→ 业务拒绝，不崩溃、不抬杆
    _arrive(container, "out")
    assert container.access.barriers["out"].state.value == "CLOSED"


def test_manual_register_and_exit(container):
    # 摄像头故障降级：人工录入进场
    res = container.access.manual_register("琼B88888", gate="in",
                                           operator="admin", color="blue")
    assert res["plate"] == "琼B88888"
    assert container.parking_repo.find_open_by_plate("琼B88888") is not None
    # 人工放行出场
    out = container.access.manual_exit("琼B88888", operator="admin", reason="设备故障")
    assert out["plate"] == "琼B88888"
    rec = container.parking_repo.get(out["record_id"])
    assert rec.status == "out"
    assert container.payment_repo.get_by_parking(rec.id) is not None

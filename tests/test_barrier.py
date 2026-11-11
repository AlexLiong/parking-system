"""道闸状态机 / 防砸 / 跟车 / 故障锁定测试（access/barrier.py）。

纯 dt 驱动，确定性验证：抬杆→回执→保持→落杆→回执；落杆打断复抬；
指令无回执→重试→FAULT；断连安全侧；恢复复位。
"""
from __future__ import annotations

import pytest

from server.access.barrier import BarrierController, TailgatingPolicy
from server.core.config import AccessConfig
from server.core.domain import BarrierState
from server.core.events import (BarrierCommand, BarrierStateChanged, EventBus,
                                IncidentRaised)


@pytest.fixture
def cfg():
    return AccessConfig(keep_open_sec=1.0, close_delay_sec=0.5,
                        barrier_ack_timeout_sec=1.0, barrier_retry=1)


class Recorder:
    def __init__(self, bus: EventBus):
        self.cmds: list[BarrierCommand] = []
        self.states: list[str] = []
        self.incidents: list[IncidentRaised] = []
        bus.subscribe(BarrierCommand, lambda e: self.cmds.append(e))
        bus.subscribe(BarrierStateChanged, lambda e: self.states.append(e.state))
        bus.subscribe(IncidentRaised, lambda e: self.incidents.append(e))


def _make(cfg, pending=0):
    bus = EventBus()
    rec = Recorder(bus)
    policy = TailgatingPolicy(pending_provider=lambda g: pending)
    b = BarrierController("in", cfg, bus, policy)
    return b, bus, rec


def test_initial_state_closed(cfg):
    b, _, _ = _make(cfg)
    assert b.state == BarrierState.CLOSED


def test_open_ack_keep_open_then_close(cfg):
    b, _, rec = _make(cfg)
    b.request_open("entry")
    assert b.state == BarrierState.OPENING
    assert any(c.cmd == "open" for c in rec.cmds)

    b.on_ack("open")
    assert b.state == BarrierState.KEEP_OPEN

    # 保持计时到期（杆区空、无后车）→ 落杆
    b.tick(1.2)
    assert b.state == BarrierState.CLOSING
    assert any(c.cmd == "close" for c in rec.cmds)

    b.on_ack("closed")
    assert b.state == BarrierState.CLOSED


def test_keep_open_extended_while_zone_occupied(cfg):
    b, _, _ = _make(cfg)
    b.request_open("entry")
    b.on_ack("open")
    b.on_zone_occupied(True, "v1")     # 杆区有车
    b.tick(2.0)                          # 计时到期也不落杆
    assert b.state == BarrierState.KEEP_OPEN


def test_keep_open_extended_by_pending_vehicle(cfg):
    b, _, _ = _make(cfg, pending=1)      # 有已授权待通过后车
    b.request_open("entry")
    b.on_ack("open")
    b.tick(2.0)
    assert b.state == BarrierState.KEEP_OPEN


def test_closing_interrupted_by_zone_reopens(cfg):
    b, _, rec = _make(cfg)
    b.request_open("entry")
    b.on_ack("open")
    b.tick(1.2)
    assert b.state == BarrierState.CLOSING
    # 落杆中杆区闯入 → 防砸：立即复抬
    b.on_zone_occupied(True, "v1")
    assert b.state == BarrierState.OPENING
    assert any(c.cmd == "interrupt" for c in rec.cmds)


def test_ack_timeout_retries_then_fault(cfg):
    b, _, rec = _make(cfg)
    b.request_open("entry")
    assert b.state == BarrierState.OPENING
    # 第一次超时 → 重试（barrier_retry=1）
    b.tick(1.1)
    assert b.state == BarrierState.OPENING
    assert any(c.cmd == "open" and c.reason == "retry" for c in rec.cmds)
    # 重试仍无回执 → FAULT + 登记异常
    b.tick(1.1)
    assert b.state == BarrierState.FAULT
    assert any(i.kind == "barrier" for i in rec.incidents)


def test_fault_ignores_request_open(cfg):
    b, _, _ = _make(cfg)
    b.request_open("entry")
    b.tick(1.1)
    b.tick(1.1)
    assert b.state == BarrierState.FAULT
    b.request_open("entry")            # FAULT 锁定下忽略
    assert b.state == BarrierState.FAULT


def test_clear_fault_returns_closed(cfg):
    b, _, _ = _make(cfg)
    b.request_open("entry")
    b.tick(1.1)
    b.tick(1.1)
    assert b.state == BarrierState.FAULT
    b.clear_fault()
    assert b.state == BarrierState.CLOSING
    b.on_ack("closed", b._ack_cmd_id)
    assert b.state == BarrierState.CLOSED


def test_force_safe_locks_keep_open(cfg):
    b, _, _ = _make(cfg)
    b.request_open("entry")
    b.on_ack("open")
    b.force_safe()                     # 断连 → 安全侧保持开启
    assert b.state == BarrierState.KEEP_OPEN
    b.tick(10.0)                       # 断连期间不落杆
    assert b.state == BarrierState.KEEP_OPEN


def test_reset_returns_closed(cfg):
    b, _, _ = _make(cfg)
    b.request_open("entry")
    b.on_ack("open")
    b.reset()
    assert b.state == BarrierState.CLOSED
    assert b.zone_occupied is False


def test_tick_in_fault_does_not_advance(cfg):
    b, _, _ = _make(cfg)
    b.request_open("entry")
    b.tick(1.1)
    b.tick(1.1)
    assert b.state == BarrierState.FAULT
    # FAULT 下 tick 直接返回，不改变状态
    b.tick(5.0)
    assert b.state == BarrierState.FAULT

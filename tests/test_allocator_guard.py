"""车位分配与一致性守卫测试（access/allocator.py, access/guard.py）。"""
from __future__ import annotations

import pytest

from server.core.errors import DuplicateEntryError, NoParkingRecordError
from server.core.timeutil import utcnow


def test_green_prefers_c_zone(container):
    with container.db.scope():
        sp = container.allocator.allocate("浙FD12345", "green")
    assert sp is not None
    assert sp.zone == "C"


def test_blue_prefers_a_zone(container):
    with container.db.scope():
        sp = container.allocator.allocate("琼A12345", "blue")
    assert sp is not None
    assert sp.zone == "A"


def test_allocate_idempotent_for_same_plate(container):
    with container.db.scope():
        sp1 = container.allocator.allocate("琼A12345", "blue")
        sp2 = container.allocator.allocate("琼A12345", "blue")
    assert sp1.space_no == sp2.space_no


def test_release_frees_space(container):
    with container.db.scope():
        sp = container.allocator.allocate("琼A12345", "blue")
        no = sp.space_no
        container.allocator.release(no)
    assert container.space_repo.get(no).status == "free"


def test_c_reserve_blocks_blue_from_last_slots(container):
    # 设置 C 区保留 2 个给新能源：把 C 区占到只剩 2 个空位后，蓝牌不得再进 C
    container.allocator.c_reserve = 2
    c_spaces = container.space_repo.free_in_zone("C")
    with container.db.scope():
        # 占用到只剩 2 个
        for sp in c_spaces[:-2]:
            container.space_repo.occupy(sp.space_no, f"占{sp.space_no}")
        # 蓝牌分配：C 区仅剩 2（== reserve）→ 不给蓝牌，应落到其它区
        sp = container.allocator.allocate("琼A99999", "blue")
    assert sp.zone != "C"


def test_lot_full_returns_none(container):
    with container.db.scope():
        for sp in container.space_repo.list_active():
            container.space_repo.occupy(sp.space_no, f"P{sp.space_no}")
        result = container.allocator.allocate("琼A00001", "blue")
    assert result is None


# ----------------------------- 守卫 -----------------------------
def test_check_entry_rejects_duplicate(container):
    with container.db.scope():
        container.parking_repo.create(plate="琼A12345", plate_color="blue",
                                      entry_time=utcnow(), space_no="A-01", status="in")
        with pytest.raises(DuplicateEntryError):
            container.guard.check_entry("琼A12345")


def test_check_exit_returns_record(container):
    with container.db.scope():
        container.parking_repo.create(plate="琼A12345", plate_color="blue",
                                      entry_time=utcnow(), space_no="A-01", status="in")
        rec = container.guard.check_exit("琼A12345")
    assert rec.plate == "琼A12345"


def test_check_exit_raises_when_no_record(container):
    with pytest.raises(NoParkingRecordError):
        container.guard.check_exit("琼A00000")


def test_reconcile_consistent(container):
    with container.db.scope():
        container.parking_repo.create(plate="琼A12345", plate_color="blue",
                                      entry_time=utcnow(), space_no="A-01", status="in")
        container.space_repo.occupy("A-01", "琼A12345")
        report = container.guard.reconcile()
    assert report["consistent"] is True


def test_reconcile_detects_orphan_occupied_space(container):
    # 车位被占但无在场记录
    with container.db.scope():
        container.space_repo.occupy("A-01", "琼A12345")
        report = container.guard.reconcile()
    assert report["consistent"] is False
    assert "A-01" in report["space_without_record"]


def test_reconcile_detects_record_without_occupied_space(container):
    # 在场记录声明的车位未被占用
    with container.db.scope():
        container.parking_repo.create(plate="琼A12345", plate_color="blue",
                                      entry_time=utcnow(), space_no="A-01", status="in")
        report = container.guard.reconcile()
    assert report["consistent"] is False
    assert any(r["reason"] == "space_not_occupied"
               for r in report["record_without_space"])

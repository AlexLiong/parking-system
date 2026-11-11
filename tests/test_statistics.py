"""统计报表服务测试（report/statistics.py, FR-6）。

用固定 anchor 日期构造数据，验证周期统计、峰值在场扫描线、按天明细、分页查询。
"""
from __future__ import annotations

from datetime import datetime

from server.core.domain import FeeBreakdown

ANCHOR = "2026-05-10"


def _mk_out(container, plate, entry, exit, amount):
    """创建一条已出场记录 + 对应缴费。"""
    with container.db.scope():
        rec = container.parking_repo.create(
            plate=plate, plate_color="blue", entry_time=entry,
            space_no=None, status="out")
        container.parking_repo.mark_exit(rec, exit_time=exit)
        container.payment_repo.create(
            parking_record_id=rec.id, plate=plate, minutes=60, billable_units=1,
            amount_due=amount, amount_paid=amount, method="mock_scan",
            operator="admin", breakdown=FeeBreakdown(minutes=60, amount_due=amount),
            paid_at=exit)
        rid = rec.id
    return rid


def test_day_stats_counts_and_revenue(container):
    _mk_out(container, "琼A11111",
            datetime(2026, 5, 10, 8, 0), datetime(2026, 5, 10, 9, 0), 5.0)
    _mk_out(container, "琼A22222",
            datetime(2026, 5, 10, 10, 0), datetime(2026, 5, 10, 11, 30), 8.0)
    s = container.stats.stats("day", ANCHOR)
    assert s.count_in == 2
    assert s.count_out == 2
    assert abs(s.revenue - 13.0) < 0.01
    # 平均时长 (60 + 90) / 2 = 75
    assert abs(s.avg_minutes - 75.0) < 0.01


def test_peak_occupancy_scanline(container):
    # 三条记录在 09:00-10:00 同时在场 → 峰值 3
    _mk_out(container, "琼A33333",
            datetime(2026, 5, 10, 8, 0), datetime(2026, 5, 10, 12, 0), 20.0)
    _mk_out(container, "琼A44444",
            datetime(2026, 5, 10, 9, 0), datetime(2026, 5, 10, 11, 0), 10.0)
    _mk_out(container, "琼A55555",
            datetime(2026, 5, 10, 9, 30), datetime(2026, 5, 10, 10, 0), 5.0)
    s = container.stats.stats("day", ANCHOR)
    assert s.peak_occupancy == 3


def test_by_day_breakdown_present(container):
    _mk_out(container, "琼A66666",
            datetime(2026, 5, 10, 8, 0), datetime(2026, 5, 10, 9, 0), 5.0)
    s = container.stats.stats("day", ANCHOR)
    assert len(s.by_day) >= 1
    day = s.by_day[0]
    assert day.date == ANCHOR
    assert day.count_in == 1 and day.count_out == 1
    assert abs(day.revenue - 5.0) < 0.01


def test_records_outside_range_excluded(container):
    _mk_out(container, "琼A77777",
            datetime(2026, 5, 9, 8, 0), datetime(2026, 5, 9, 9, 0), 5.0)   # 前一天
    s = container.stats.stats("day", ANCHOR)
    assert s.count_in == 0 and s.revenue == 0.0


def test_search_pagination(container):
    for i in range(5):
        with container.db.scope():
            container.parking_repo.create(
                plate=f"琼A9000{i}", plate_color="blue",
                entry_time=datetime(2026, 5, 10, 8, i), space_no=None, status="in")
    page1 = container.stats.search_records(page=1, page_size=2)
    assert page1["total"] == 5
    assert len(page1["items"]) == 2
    assert page1["page"] == 1 and page1["page_size"] == 2
    page3 = container.stats.search_records(page=3, page_size=2)
    assert len(page3["items"]) == 1


def test_search_filter_by_plate_and_status(container):
    with container.db.scope():
        container.parking_repo.create(plate="琼B12345", plate_color="blue",
                                      entry_time=datetime(2026, 5, 10, 8, 0),
                                      space_no=None, status="in")
        container.parking_repo.create(plate="粤B99999", plate_color="blue",
                                      entry_time=datetime(2026, 5, 10, 9, 0),
                                      space_no=None, status="out")
    r = container.stats.search_records(plate="琼B")
    assert r["total"] == 1 and r["items"][0]["plate"] == "琼B12345"
    r2 = container.stats.search_records(status="out")
    assert r2["total"] == 1 and r2["items"][0]["plate"] == "粤B99999"


def test_page_size_clamped(container):
    r = container.stats.search_records(page=0, page_size=9999)
    assert r["page"] == 1
    assert r["page_size"] <= 200


def test_sql_day_projection_matches_exact_timezone_fallback(container):
    from server.core.timeutil import date_range
    _mk_out(container,'京A12345',datetime(2026,5,9,15,59),datetime(2026,5,9,16,1),5)
    _mk_out(container,'京B12345',datetime(2026,5,9,16,2),datetime(2026,5,10,16,1),8)
    start,end=date_range('day',datetime(2026,5,10).date())
    entries=container.parking_repo.statistics_entries(start,end)
    exits=container.parking_repo.statistics_exits(start,end)
    assert container.stats._by_day_sql(start,end)==container.stats._by_day(start,end,entries,exits)


# ----------------------- P1-13：SQL 聚合与 Python 基准等价性 -----------------------
def test_sql_exit_summary_matches_python_count_and_avg(container):
    """`statistics_exit_summary` 必须与 len(exits)/_avg_minutes 完全一致（含 temporary）。"""
    from server.core.timeutil import date_range
    start, end = date_range('day', datetime(2026, 5, 10).date())
    _mk_out(container, '琼C10001', datetime(2026, 5, 10, 8, 0),
            datetime(2026, 5, 10, 9, 0), 5.0)
    _mk_out(container, '琼C10002', datetime(2026, 5, 10, 8, 30),
            datetime(2026, 5, 10, 10, 10), 8.0)      # 100 分钟
    _mk_out(container, '琼C10003', datetime(2026, 5, 10, 12, 0, 0, 300000),
            datetime(2026, 5, 10, 12, 0, 0, 900000), 1.0)  # 0.6 秒 → ceil=1 分钟
    with container.db.scope():
        rec = container.parking_repo.create(
            plate='琼CTEMP', plate_color='blue', entry_time=datetime(2026, 5, 10, 11, 0),
            space_no=None, status='out', temporary=True)
        container.parking_repo.mark_exit(rec, exit_time=datetime(2026, 5, 10, 11, 30))
    exits = container.parking_repo.statistics_exits(start, end)
    summary = container.parking_repo.statistics_exit_summary(start, end)
    assert summary['count'] == len(exits) == 4
    py_avg = container.stats._avg_minutes(exits)
    sql_avg = summary['minutes_sum'] / summary['minutes_count']
    assert abs(sql_avg - py_avg) < 1e-9
    assert summary['minutes_count'] == 3          # temporary 不计入平均


def test_sql_peak_occupancy_matches_python_scanline(container):
    """`statistics_peak_occupancy` 必须与 `_peak_occupancy` 扫描线一致（在途/开库/临时/零时长）。"""
    from server.core.timeutil import date_range
    start, end = date_range('day', datetime(2026, 5, 10).date())

    def mk(plate, entry, exit_time=None, temporary=False):
        with container.db.scope():
            rec = container.parking_repo.create(
                plate=plate, plate_color='blue', entry_time=entry, space_no=None,
                status='in', temporary=temporary)
            if exit_time is not None:
                container.parking_repo.mark_exit(rec, exit_time=exit_time)

    mk('琼P00001', datetime(2026, 5, 10, 8, 0), datetime(2026, 5, 10, 12, 0))
    mk('琼P00002', datetime(2026, 5, 10, 9, 0), datetime(2026, 5, 10, 11, 0))
    mk('琼P00003', datetime(2026, 5, 9, 23, 0), datetime(2026, 5, 10, 10, 0))  # 跨起点在途
    mk('琼P00004', datetime(2026, 5, 10, 9, 30))                               # 未出场
    mk('琼PTEMP1', datetime(2026, 5, 10, 9, 0), datetime(2026, 5, 10, 10, 0), temporary=True)
    mk('琼PZERO1', datetime(2026, 5, 10, 9, 15), datetime(2026, 5, 10, 9, 15))  # 零时长
    assert (container.parking_repo.statistics_peak_occupancy(start, end)
            == container.stats._peak_occupancy(start, end) == 4)


def test_exit_summary_old_sqlite_fallback_matches_python(container, monkeypatch):
    """P1-13：SQLite <3.42 无 unixepoch(subsec) 时退回 julianday，仍需与 Python 语义一致。"""
    from server.core.timeutil import date_range
    from server.repository import parking_repo as repo_mod
    start, end = date_range('day', datetime(2026, 5, 10).date())
    _mk_out(container, '琼F10001', datetime(2026, 5, 10, 8, 0),
            datetime(2026, 5, 10, 9, 0), 5.0)
    _mk_out(container, '琼F10002', datetime(2026, 5, 10, 8, 30),
            datetime(2026, 5, 10, 10, 10), 8.0)
    monkeypatch.setattr(repo_mod, '_UNIXEPOCH_SUBSEC', False)
    exits = container.parking_repo.statistics_exits(start, end)
    summary = container.parking_repo.statistics_exit_summary(start, end)
    assert summary['count'] == len(exits) == 2
    py_avg = container.stats._avg_minutes(exits)
    sql_avg = summary['minutes_sum'] / summary['minutes_count']
    assert abs(sql_avg - py_avg) < 1e-9


def test_statistics_query_indexes_created(container):
    """P1-13：新库（以及已有库迁移）必须存在统计区间/覆盖索引。"""
    from sqlalchemy import text
    with container.db.scope():
        parking = {r[1] for r in container.db.session.execute(
            text("PRAGMA index_list('parking_record')")).all()}
        payment = {r[1] for r in container.db.session.execute(
            text("PRAGMA index_list('payment_record')")).all()}
    assert {'idx_exit_time', 'idx_entry_cover', 'idx_exit_cover'} <= parking
    assert {'idx_paid_at', 'idx_paid_at_cover'} <= payment

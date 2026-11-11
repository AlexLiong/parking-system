"""P1：人工抬杆保持（hold）+ PDF 报表 day/week 横向分页。

1) manual_open / 故障人工抬杆：keep_open=True 并持久化，跨 keep_open_sec 不自动落杆；
   管理员落杆（close）后恢复自动行为；杆区有车 409；黑名单绕行/普通业务开闸不受影响。
2) PDF：day/week（level vehicle_day/day）横向 A4；明细全部 Paragraph（时间两行）；
   列宽和 <= 可用宽度；LongTable 自动分页重复表头；月/季/年报保持纵向。
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta

from reportlab.platypus import LongTable, Paragraph

from server.core.domain import FeeBreakdown
from server.report.pdf import (DETAIL_PAGESIZE, DETAIL_USABLE_WIDTH,
                               _ensure_font, report_pagesize, vehicle_col_widths)

# ---------------------------------------------------------------------------
# 1) 人工抬杆保持
# ---------------------------------------------------------------------------
def _ack(client, gate, state, cmd_id):
    return client.post("/sim/events/barrier_ack",
                       json={"gate": gate, "state": state, "cmd_id": cmd_id})


def _tick_past_keep_open(c):
    c.access.tick(c.cfg.access.keep_open_sec + c.cfg.access.close_delay_sec + 5.0)


def test_manual_open_holds_across_keep_open_sec(client, admin_token):
    c = client.app.state.container
    opened = client.post("/api/manual/open-barrier", headers=admin_token,
                         json={"gate": "in", "reason": "manual_hold_test"})
    assert opened.status_code == 200, opened.text
    body = opened.json()
    assert body["hold"] is True and body["keep_open"] is True
    assert _ack(client, "in", "open", body["cmd_id"]).json()["accepted"]
    barrier = c.access.barriers["in"]
    assert barrier.state.value == "KEEP_OPEN" and barrier.keep_open is True
    snap = client.get("/api/state/snapshot", headers=admin_token).json()["barriers"]["in"]
    assert snap["keep_open"] is True
    # 跨 keep_open_sec 仍保持抬升（不会自动落杆）
    _tick_past_keep_open(c)
    assert barrier.state.value == "KEEP_OPEN" and barrier.keep_open is True
    # 管理员点"落杆" → 关闭并清除 keep_open
    closed = client.post("/api/manual/close-barrier", headers=admin_token,
                         json={"gate": "in"}).json()
    assert closed["keep_open"] is False
    assert _ack(client, "in", "closed", closed["cmd_id"]).json()["accepted"]
    assert barrier.state.value == "CLOSED" and barrier.keep_open is False
    snap2 = client.get("/api/state/snapshot", headers=admin_token).json()["barriers"]["in"]
    assert snap2["keep_open"] is False


def test_manual_hold_close_rejected_when_zone_occupied(client, admin_token):
    c = client.app.state.container
    opened = client.post("/api/manual/open-barrier", headers=admin_token,
                         json={"gate": "in", "reason": "hold"}).json()
    _ack(client, "in", "open", opened["cmd_id"])
    c.access.barriers["in"].on_zone_occupied(True)
    rejected = client.post("/api/manual/close-barrier", headers=admin_token,
                           json={"gate": "in"})
    assert rejected.status_code == 409
    assert rejected.json()["error"] == "zone_occupied"
    c.access.barriers["in"].on_zone_occupied(False)


def test_fault_manual_open_holds(client, admin_token):
    c = client.app.state.container
    assert client.post("/api/barrier/in/fault", headers=admin_token,
                       json={"mode": "stuck_closed"}).status_code == 200
    lifted = client.post("/api/manual/open-barrier", headers=admin_token,
                         json={"gate": "in", "reason": "fault_manual_lift"})
    assert lifted.status_code == 200, lifted.text
    body = lifted.json()
    assert body["hold"] is True and body["keep_open"] is True
    assert _ack(client, "in", "open", body["cmd_id"]).json()["accepted"]
    barrier = c.access.barriers["in"]
    assert barrier.state.value == "KEEP_OPEN" and barrier.keep_open is True
    _tick_past_keep_open(c)
    assert barrier.state.value == "KEEP_OPEN" and barrier.keep_open is True


def test_blacklist_gate_does_not_hold(container):
    c = container
    plate = "京A44444"
    with c.db.scope():
        c.list_repo.upsert(plate=plate, list_type="black", note="P1")
    result = c.access.blacklist_gate("blk-1", plate, "in", "open")
    assert result["hold"] is False and result["keep_open"] is False
    assert c.access.on_barrier_ack("in", "open", result["cmd_id"], require_cmd=True)
    barrier = c.access.barriers["in"]
    assert barrier.state.value == "KEEP_OPEN" and barrier.keep_open is False
    _tick_past_keep_open(c)
    assert barrier.state.value in ("CLOSING", "CLOSED")
    assert barrier.keep_open is False


def test_normal_business_open_not_held_after_ack(container):
    c = container
    c.access.manual_register("京B12345", "in", "admin")
    barrier = c.access.barriers["in"]
    assert barrier.state.value == "OPENING" and barrier.keep_open is False
    assert c.access.on_barrier_ack("in", "open", barrier._ack_cmd_id, require_cmd=True)
    # 业务开闸的 permit 由车辆越过闸杆后递减，之后才允许自动落杆
    from server.core.events import VehiclePassedGate
    c.access.on_vehicle_passed(VehiclePassedGate(vehicle_id="car-1", gate="in"))
    assert c.access._pending["in"] == 0
    _tick_past_keep_open(c)
    assert barrier.state.value in ("CLOSING", "CLOSED")
    assert barrier.keep_open is False


# ---------------------------------------------------------------------------
# 2) PDF 报表：横向 + Paragraph + 分页
# ---------------------------------------------------------------------------
def _mk_complete(container, plate, entry, exit_, amount=5.0, method="auto"):
    with container.db.scope():
        rec = container.parking_repo.create(
            plate=plate, plate_color="blue", entry_time=entry,
            space_no="A-01", status="in", entry_sim_sec=0.0)
        container.parking_repo.mark_exit(rec, exit_time=exit_, status="out")
        container.payment_repo.create(
            parking_record_id=rec.id, plate=plate, minutes=60, billable_units=1,
            amount_due=amount, amount_paid=amount, method=method,
            operator="system",
            breakdown=FeeBreakdown(minutes=60, amount_due=amount), paid_at=exit_)
        return rec.id


def _page_count(data: bytes) -> int:
    return len(re.findall(rb"/Type\s*/Page[^s]", data))


def test_vehicle_detail_table_uses_paragraphs_and_fits_landscape(container):
    gen = container.pdf
    font = _ensure_font("STSong-Light")
    by_day = [{
        "date": "2026-11-06",
        "vehicles": [{
            "plate": "粤F4K1CZ",
            "entry_time": "2026-11-06T09:01:02+08:00",
            "exit_time": "2026-11-06T10:11:12+08:00",
            "duration_minutes": 70,
            "amount": 5.0,
            "payment_method": "auto",
            "space_no": "A-01",
        }],
    }]
    table = gen._vehicle_table(by_day, font, with_date=True)
    assert isinstance(table, LongTable)
    widths = list(table._colWidths)
    assert len(widths) == 8
    assert sum(widths) <= DETAIL_USABLE_WIDTH + 1e-6
    first_row = table._cellvalues[0]
    assert all(isinstance(cell, Paragraph) for cell in first_row)
    data_row = table._cellvalues[1]
    assert all(isinstance(cell, Paragraph) for cell in data_row)
    # 时间拆成日期/时间两行
    assert "<br/>" in data_row[2].text and "<br/>" in data_row[3].text
    table_no_date = gen._vehicle_table(by_day, font, with_date=False)
    assert len(table_no_date._colWidths) == 7
    assert sum(table_no_date._colWidths) <= DETAIL_USABLE_WIDTH + 1e-6


def test_day_pdf_landscape_and_multipage(container, tmp_path):
    c = container
    # 同一天 300 条完整记录（长数据 → 多页）
    for index in range(300):
        entry = datetime(2026, 11, 6, 1, 0) + timedelta(minutes=index)
        _mk_complete(c, f"京C{index:05d}", entry, entry + timedelta(minutes=60),
                     amount=5.0)
    preview = c.reports.preview("day", "2026-11-06", vehicle_limit=2000)
    assert report_pagesize(preview) == DETAIL_PAGESIZE
    assert preview["summary"]["count_out"] == 300
    path = tmp_path / "day_detail.pdf"
    c.pdf.generate_from_preview(preview, path)
    data = path.read_bytes()
    assert data.startswith(b"%PDF") and len(data) > 5000
    pages = _page_count(data)
    assert pages >= 2, pages
    # 所有明细单元格为 Paragraph 已在表格单测覆盖；这里校验列宽不超可用宽度
    assert sum(vehicle_col_widths(with_date=False)) <= DETAIL_USABLE_WIDTH + 1e-6
    # 日报纵向对比：月报应为纵向 A4
    month = c.reports.preview("month", "2026-11-06", vehicle_limit=2000)
    assert report_pagesize(month) == (595.2755905511812, 841.8897637795277)


def test_week_pdf_landscape_multipage_and_month_portrait(container, tmp_path):
    c = container
    # 一周每天 20 条 → 140 条明细（多页）
    for day_offset in range(7):
        day = datetime(2026, 11, 2, 1, 0) + timedelta(days=day_offset)
        for index in range(20):
            entry = day + timedelta(minutes=index * 5)
            _mk_complete(c, f"京D{day_offset}{index:04d}", entry,
                         entry + timedelta(minutes=60), amount=6.0)
    week = c.reports.preview("week", "2026-11-04", vehicle_limit=2000)
    assert week["level"] == "day" and len(week["by_day"]) == 7
    assert report_pagesize(week) == DETAIL_PAGESIZE
    week_path = tmp_path / "week_detail.pdf"
    c.pdf.generate_from_preview(week, week_path)
    week_data = week_path.read_bytes()
    assert week_data.startswith(b"%PDF")
    assert _page_count(week_data) >= 2
    # 月报纵向（A4 portrait）
    month = c.reports.preview("month", "2026-11-06", vehicle_limit=2000)
    month_path = tmp_path / "month_summary.pdf"
    c.pdf.generate_from_preview(month, month_path)
    month_data = month_path.read_bytes()
    assert month_data.startswith(b"%PDF")
    assert _page_count(month_data) >= 1
    assert report_pagesize(month) != DETAIL_PAGESIZE

def test_pdf_csv_payment_method_translation_and_readable_range(container):
    """P1 补充：PDF/CSV 展示翻译支付方式；PDF 区间用 YYYY-MM-DD HH:MM 可读格式；
    preview 数据保持 raw（前端自行翻译）。"""
    from server.report.pdf import payment_method_label as pdf_label
    from server.report.reports import PAYMENT_METHOD_LABELS, payment_method_label

    expected = {"auto": "自动扣费", "mock_scan": "扫码支付", "free": "免费",
                "monthly": "月租", "cash": "现金"}
    assert PAYMENT_METHOD_LABELS == expected
    for raw, label in expected.items():
        assert payment_method_label(raw) == label
        assert pdf_label(raw) == label
    assert payment_method_label("void") == "void"
    assert payment_method_label(None) == ""

    preview = {
        "period": "day", "anchor": "2026-11-06",
        "start": "2026-11-06T00:00:00+08:00",
        "end": "2026-11-07T00:00:00+08:00",
        "level": "vehicle_day",
        "summary": {"count_in": 3, "count_out": 3, "revenue": 6.0,
                    "avg_minutes": 60.0, "peak_occupancy": 1},
        "by_day": [{
            "date": "2026-11-06", "count_in": 3, "count_out": 3, "revenue": 6.0,
            "vehicles": [
                {"plate": "京A00001", "entry_time": "2026-11-06T09:00:00+08:00",
                 "exit_time": "2026-11-06T10:00:00+08:00",
                 "duration_minutes": 60, "amount": 2.0,
                 "payment_method": "auto", "space_no": "A-01"},
                {"plate": "京A00002", "entry_time": "2026-11-06T09:00:00+08:00",
                 "exit_time": "2026-11-06T10:00:00+08:00",
                 "duration_minutes": 60, "amount": 3.0,
                 "payment_method": "cash", "space_no": "A-02"},
                {"plate": "京A00003", "entry_time": "2026-11-06T09:00:00+08:00",
                 "exit_time": "2026-11-06T10:00:00+08:00",
                 "duration_minutes": 60, "amount": 1.0,
                 "payment_method": "void", "space_no": "A-03"},
            ],
        }],
        "truncated": False, "generated_at": "2026-11-06T10:00:00+08:00",
    }
    csv_text = container.reports.csv_text(preview)
    assert "自动扣费" in csv_text and "现金" in csv_text and "void" in csv_text
    assert ",auto," not in csv_text and ",cash," not in csv_text
    # preview 数据保持 raw 不变
    vehicles = preview["by_day"][0]["vehicles"]
    assert vehicles[0]["payment_method"] == "auto"
    assert vehicles[1]["payment_method"] == "cash"
    # PDF 顶部区间改为本地可读格式
    assert container.pdf._format_range(preview) == \
        "2026-11-06 00:00 ~ 2026-11-07 00:00"
    # PDF 明细单元格使用翻译后的支付方式
    from server.report.pdf import _ensure_font
    font = _ensure_font("STSong-Light")
    table = container.pdf._vehicle_table(preview["by_day"], font, with_date=False)
    payment_cells = [row[5].text for row in table._cellvalues[1:]]
    assert payment_cells == ["自动扣费", "现金", "void"]

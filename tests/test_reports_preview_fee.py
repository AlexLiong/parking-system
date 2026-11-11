"""N2：报表预览/CSV/PDF + 费率当前/历史。

- 只统计完整记录（status=out 且 entry/exit 非空；aborted/异常不计）；
- day: by_day 1 天 + vehicles；week: 7 天 + vehicles；
- month: weeks 周汇总；quarter: 3 个月；year: 12 个月（无车辆）；
- CSV 中文表头/公开下载；PDF 公开下载且为合法 PDF；
- 费率历史 effective_to/created_at；PUT current 编辑当前（内部新建版本，历史保留）；
- 记录详情保留 fee_rule_id + payment.breakdown。
"""
from __future__ import annotations

from datetime import datetime, timedelta

from server.core.domain import FeeBreakdown


def _mk_complete(container, plate, entry, exit_, amount=5.0, method="auto",
                 fee_rule_id=None, space_no="A-01"):
    """完整记录：status=out 且 entry/exit 均非空，并写支付。"""
    with container.db.scope():
        rec = container.parking_repo.create(
            plate=plate, plate_color="blue", entry_time=entry,
            space_no=space_no, status="in", fee_rule_id=fee_rule_id,
            entry_sim_sec=0.0)
        container.parking_repo.mark_exit(rec, exit_time=exit_, status="out")
        container.payment_repo.create(
            parking_record_id=rec.id, plate=plate, minutes=60, billable_units=1,
            amount_due=amount, amount_paid=amount, method=method,
            operator="admin", breakdown=FeeBreakdown(minutes=60, amount_due=amount),
            paid_at=exit_)
        rid = rec.id
    return rid


def _mk_open(container, plate, entry):
    with container.db.scope():
        rec = container.parking_repo.create(
            plate=plate, plate_color="blue", entry_time=entry,
            space_no=None, status="in")
    return rec.id


def _mk_abnormal(container, plate, entry, exit_):
    with container.db.scope():
        rec = container.parking_repo.create(
            plate=plate, plate_color="blue", entry_time=entry,
            space_no=None, status="in")
        container.parking_repo.mark_exit(rec, exit_time=exit_, status="abnormal")
        rec.aborted_reason = "blacklist_exit"
    return rec.id


# ---------------------------------------------------------------------------
# 1) 完整记录过滤 + day 车辆明细
# ---------------------------------------------------------------------------
def test_preview_day_only_complete_records_with_vehicles(container):
    # UTC+8 业务日 2026-05-10：UTC 05-09 16:00 ~ 05-10 16:00
    r1 = _mk_complete(container, "京A11111", datetime(2026, 5, 10, 1, 0),
                      datetime(2026, 5, 10, 2, 0), 5.0, method="auto")
    r2 = _mk_complete(container, "京B22222", datetime(2026, 5, 10, 3, 0),
                      datetime(2026, 5, 10, 4, 30), 8.0, method="cash")
    open_id = _mk_open(container, "京C33333", datetime(2026, 5, 10, 5, 0))
    abnormal_id = _mk_abnormal(container, "京D44444", datetime(2026, 5, 10, 6, 0),
                               datetime(2026, 5, 10, 6, 30))
    result = container.reports.preview("day", "2026-05-10", vehicle_limit=100)
    assert result["period"] == "day" and result["level"] == "vehicle_day"
    assert result["anchor"] == "2026-05-10"
    assert result["start"] and result["end"]
    summary = result["summary"]
    assert summary["count_in"] == 2 and summary["count_out"] == 2
    assert summary["revenue"] == 13.0
    assert summary["avg_minutes"] == 75.0
    assert summary["peak_occupancy"] >= 1
    assert len(result["by_day"]) == 1
    day = result["by_day"][0]
    assert day["date"] == "2026-05-10"
    assert day["count_in"] == 2 and day["count_out"] == 2
    assert day["revenue"] == 13.0
    vehicles = {v["plate"]: v for v in day["vehicles"]}
    assert set(vehicles) == {"京A11111", "京B22222"}
    assert open_id not in [v.get("record_id") for v in day["vehicles"]]
    assert abnormal_id not in [v.get("record_id") for v in day["vehicles"]]
    v = vehicles["京A11111"]
    for key in ("plate", "entry_time", "exit_time", "duration_minutes",
                "amount", "payment_method", "space_no"):
        assert key in v
    assert v["duration_minutes"] == 60 and v["amount"] == 5.0
    assert v["payment_method"] == "auto" and v["space_no"] == "A-01"
    assert r1 and r2


def test_preview_week_has_7_days_with_vehicles(container):
    # 2026-05-13（周三）→ 业务周 2026-05-11 ~ 05-17
    _mk_complete(container, "京E55555", datetime(2026, 5, 11, 2, 0),
                 datetime(2026, 5, 11, 3, 0), 5.0)
    _mk_complete(container, "京F66666", datetime(2026, 5, 12, 2, 0),
                 datetime(2026, 5, 12, 3, 0), 6.0)
    _mk_complete(container, "京G77777", datetime(2026, 5, 13, 2, 0),
                 datetime(2026, 5, 13, 4, 0), 7.0)
    result = container.reports.preview("week", "2026-05-13", vehicle_limit=100)
    assert result["level"] == "day"
    assert len(result["by_day"]) == 7
    by_date = {d["date"]: d for d in result["by_day"]}
    assert by_date["2026-05-11"]["vehicles"][0]["plate"] == "京E55555"
    assert by_date["2026-05-12"]["count_out"] == 1
    assert by_date["2026-05-13"]["revenue"] == 7.0
    assert all("vehicles" in d for d in result["by_day"])
    assert result["summary"]["count_out"] == 3
    assert result["summary"]["revenue"] == 18.0


# ---------------------------------------------------------------------------
# 2) month/quarter/year 汇总层级
# ---------------------------------------------------------------------------
def _mk_months(container):
    _mk_complete(container, "京H11111", datetime(2026, 4, 15, 2, 0),
                 datetime(2026, 4, 15, 3, 0), 5.0)
    _mk_complete(container, "京H22222", datetime(2026, 5, 10, 2, 0),
                 datetime(2026, 5, 10, 3, 0), 6.0)
    _mk_complete(container, "京H33333", datetime(2026, 6, 20, 2, 0),
                 datetime(2026, 6, 20, 3, 0), 7.0)
    _mk_complete(container, "京H44444", datetime(2026, 1, 8, 2, 0),
                 datetime(2026, 1, 8, 3, 0), 4.0)


def test_preview_month_weeks_summary_without_vehicles(container):
    _mk_months(container)
    result = container.reports.preview("month", "2026-05-10", vehicle_limit=100)
    assert result["level"] == "week"
    assert "weeks" in result and len(result["weeks"]) >= 4
    assert "by_day" not in result
    for week in result["weeks"]:
        assert set(week) == {"week_start", "week_end", "count_in",
                             "count_out", "revenue"}
        assert week["week_start"][:7] == "2026-05"
        assert week["week_end"][:7] == "2026-05"
    assert sum(w["count_out"] for w in result["weeks"]) == 1
    assert result["summary"]["count_out"] == 1
    assert result["summary"]["revenue"] == 6.0


def test_preview_quarter_has_3_months(container):
    _mk_months(container)
    result = container.reports.preview("quarter", "2026-05-10", vehicle_limit=100)
    assert result["level"] == "month"
    assert [m["month"] for m in result["months"]] == ["2026-04", "2026-05", "2026-06"]
    assert sum(m["count_out"] for m in result["months"]) == 3
    assert result["summary"]["count_out"] == 3
    assert result["summary"]["revenue"] == 18.0
    assert "vehicles" not in result["months"][0]


def test_preview_year_has_12_months(container):
    _mk_months(container)
    result = container.reports.preview("year", "2026-05-10", vehicle_limit=100)
    assert result["level"] == "month"
    months = result["months"]
    assert len(months) == 12
    assert months[0]["month"] == "2026-01" and months[-1]["month"] == "2026-12"
    assert {m["month"] for m in months if m["count_out"]} == {
        "2026-01", "2026-04", "2026-05", "2026-06"}
    assert result["summary"]["count_out"] == 4
    assert result["summary"]["revenue"] == 22.0


def test_preview_limit_truncates_vehicles_but_keeps_summary(container):
    for index in range(5):
        _mk_complete(container, f"京J{index:05d}", datetime(2026, 5, 10, 1, index),
                     datetime(2026, 5, 10, 2, index), 1.0)
    result = container.reports.preview("day", "2026-05-10", vehicle_limit=2)
    assert result["truncated"] is True
    day = result["by_day"][0]
    assert len(day["vehicles"]) == 2
    assert day["count_out"] == 5 and result["summary"]["count_out"] == 5


# ---------------------------------------------------------------------------
# 3) CSV / PDF 导出（公开下载，无需 Bearer）
# ---------------------------------------------------------------------------
def test_csv_exports_chinese_headers_and_public(client, admin_token):
    container = client.app.state.container
    _mk_complete(container, "京K11111", datetime(2026, 5, 10, 1, 0),
                 datetime(2026, 5, 10, 2, 0), 5.0)
    client.cookies.clear()
    r = client.get("/api/reports/export.csv", params={"period": "day",
                                                      "anchor": "2026-05-10"})
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/csv")
    text = r.content.decode("utf-8-sig")
    header = text.splitlines()[0]
    assert "车牌" in header and "入场时间" in header and "金额(元)" in header
    assert "京K11111" in text
    # week：日期 + 车辆明细
    r2 = client.get("/api/reports/export.csv", params={"period": "week",
                                                       "anchor": "2026-05-13"})
    assert r2.status_code == 200
    week_text = r2.content.decode("utf-8-sig")
    assert week_text.splitlines()[0].startswith("日期,车牌")
    # month：周汇总
    r3 = client.get("/api/reports/export.csv", params={"period": "month",
                                                       "anchor": "2026-05-10"})
    assert "周起始" in r3.content.decode("utf-8-sig").splitlines()[0]
    # year：月汇总
    r4 = client.get("/api/reports/export.csv", params={"period": "year",
                                                       "anchor": "2026-05-10"})
    year_header = r4.content.decode("utf-8-sig").splitlines()[0]
    assert year_header.startswith("月份")
    assert 'report_year_2026-05-10_cn.csv' in r4.headers.get("content-disposition", "")


def test_pdf_export_public_and_valid(client, admin_token):
    container = client.app.state.container
    _mk_complete(container, "京L11111", datetime(2026, 5, 10, 1, 0),
                 datetime(2026, 5, 10, 2, 0), 5.0)
    client.cookies.clear()
    for period in ("day", "week", "month", "quarter", "year"):
        r = client.get("/api/reports/export", params={"period": period,
                                                      "anchor": "2026-05-10"})
        assert r.status_code == 200, (period, r.text[:200])
        assert r.headers["content-type"].startswith("application/pdf")
        assert r.content.startswith(b"%PDF")
        assert len(r.content) > 800


def test_preview_api_and_invalid_params(client, admin_token):
    r = client.get("/api/reports/preview", params={"period": "week",
                                                   "anchor": "2026-05-13"},
                   headers=admin_token)
    assert r.status_code == 200
    body = r.json()
    assert body["level"] == "day" and "by_day" in body
    bad = client.get("/api/reports/preview", params={"period": "decade"},
                     headers=admin_token)
    assert bad.status_code == 422
    bad_anchor = client.get("/api/reports/preview",
                            params={"period": "day", "anchor": "not-a-date"},
                            headers=admin_token)
    assert bad_anchor.status_code == 422


# ---------------------------------------------------------------------------
# 4) 费率：当前编辑 + 历史区间
# ---------------------------------------------------------------------------
def _fee_payload(**overrides):
    data = {"free_minutes": 20, "first_hour_price": 6.0, "unit_minutes": 30,
            "unit_price": 2.5, "daily_cap": 50.0, "ne_discount": 0.85,
            "round_up": True, "note": "编辑当前费率"}
    data.update(overrides)
    return data


def test_fee_rules_history_intervals_and_put_current(client, admin_token):
    items = client.get("/api/fee-rules", headers=admin_token).json()["items"]
    assert items and items[0]["effective_to"] is None
    for key in ("id", "effective_from", "effective_to", "created_at", "operator",
                "note", "free_minutes", "first_hour_price", "unit_minutes",
                "unit_price", "daily_cap", "ne_discount", "round_up"):
        assert key in items[0], key
    old = items[0]

    r = client.put("/api/fee-rules/current", headers=admin_token,
                   json=_fee_payload())
    assert r.status_code == 200, r.text
    current = r.json()
    assert current["id"] != old["id"]
    assert current["first_hour_price"] == 6.0 and current["free_minutes"] == 20
    assert current["effective_to"] is None
    # 历史版本保留且区间正确
    items2 = client.get("/api/fee-rules", headers=admin_token).json()["items"]
    assert len(items2) == len(items) + 1
    new = items2[0]
    old2 = next(item for item in items2 if item["id"] == old["id"])
    assert old2["effective_to"] == new["effective_from"]
    assert old2["first_hour_price"] == old["first_hour_price"]
    assert old2["id"] == old["id"]
    # GET current 使用新值（契约不变）
    cur = client.get("/api/fee-rules/current", headers=admin_token).json()
    assert cur["first_hour_price"] == 6.0 and cur["free_minutes"] == 20
    # 相同内容再 PUT 不制造无意义版本
    r2 = client.put("/api/fee-rules/current", headers=admin_token,
                    json=_fee_payload())
    assert r2.status_code == 200
    assert len(client.get("/api/fee-rules",
                          headers=admin_token).json()["items"]) == len(items2)


def test_record_detail_exposes_fee_rule_id_and_payment_breakdown(client, admin_token):
    container = client.app.state.container
    fee_rule_id = container.fee_repo.list_rules()[0].id
    rid = _mk_complete(container, "京M11111", datetime(2026, 5, 10, 1, 0),
                       datetime(2026, 5, 10, 2, 0), 5.0,
                       fee_rule_id=fee_rule_id)
    detail = client.get(f"/api/records/{rid}", headers=admin_token).json()
    assert detail["fee_rule_id"] == fee_rule_id
    assert "payment" in detail and detail["payment"]["breakdown"]
    breakdown = detail["payment"]["breakdown"]
    for key in ("minutes", "free_minutes_applied", "billable_minutes",
                "billable_units", "unit_price", "first_hour_price",
                "daily_cap_applied", "new_energy_discount", "amount_due"):
        assert key in breakdown, key
    assert "fee_rule" not in detail and "current_fee_rule" not in detail

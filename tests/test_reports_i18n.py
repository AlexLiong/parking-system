"""国际化报表（2026-11-08）：PDF/CSV 中英双语 + 文件名 _cn/_en。"""
from __future__ import annotations

from datetime import datetime

from reportlab.platypus import Paragraph

from server.core.domain import FeeBreakdown


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


def _seed_day(client):
    c = client.app.state.container
    _mk_complete(c, "京A90001", datetime(2026, 11, 4, 2, 0),
                 datetime(2026, 11, 4, 3, 0), 5.0, method="auto")
    return c


# ---------------------------------------------------------------------------
# CSV
# ---------------------------------------------------------------------------
def test_csv_zh_default_and_filename_cn(client):
    c = _seed_day(client)
    response = client.get("/api/reports/export.csv",
                          params={"period": "day", "anchor": "2026-11-04"})
    assert response.status_code == 200
    disposition = response.headers["content-disposition"]
    assert "report_day_2026-11-04_cn.csv" in disposition
    text = response.content.decode("utf-8-sig")
    header = text.splitlines()[0]
    assert header == "车牌,入场时间,出场时间,停放分钟,金额(元),支付方式,车位号"
    assert "自动扣费" in text
    # filename helper 与约定一致
    preview = c.reports.preview("day", "2026-11-04")
    assert c.reports.filename("day", preview, "zh") == \
        "report_day_2026-11-04_cn.csv"
    assert c.reports.filename("week", {"anchor": "2026-11-02"}, "zh",
                              ext="pdf") == "report_week_2026-11-02_cn.pdf"


def test_csv_en_headers_payment_and_filename(client):
    _seed_day(client)
    response = client.get("/api/reports/export.csv",
                          params={"period": "day", "anchor": "2026-11-04",
                                  "lang": "en"})
    assert response.status_code == 200
    disposition = response.headers["content-disposition"]
    assert "report_day_2026-11-04_en.csv" in disposition
    text = response.content.decode("utf-8-sig")
    header = text.splitlines()[0]
    assert header == ("Plate,Entry time,Exit time,Duration (min),"
                      "Amount (CNY),Payment method,Space no.")
    assert "Auto-paid" in text and "自动扣费" not in text
    # 英文 month/quarter 表头
    c = client.app.state.container
    assert c.reports.csv_text({"period": "quarter", "months": [], "weeks": []},
                              "en").splitlines()[0] == \
        "Month,Entries,Exits,Revenue (CNY)"


def test_csv_invalid_lang_returns_422(client):
    bad = client.get("/api/reports/export.csv",
                     params={"period": "day", "lang": "fr"})
    assert bad.status_code == 422


# ---------------------------------------------------------------------------
# PDF 端点
# ---------------------------------------------------------------------------
def test_pdf_endpoint_zh_en_disposition_and_valid(client):
    _seed_day(client)
    zh = client.get("/api/reports/export",
                    params={"period": "day", "anchor": "2026-11-04"})
    assert zh.status_code == 200
    assert "report_day_2026-11-04_cn.pdf" in zh.headers["content-disposition"]
    assert zh.content.startswith(b"%PDF")

    en = client.get("/api/reports/export",
                    params={"period": "day", "anchor": "2026-11-04",
                            "lang": "en"})
    assert en.status_code == 200
    assert "report_day_2026-11-04_en.pdf" in en.headers["content-disposition"]
    assert en.content.startswith(b"%PDF") and len(en.content) > 1000

    unknown = client.get("/api/reports/export",
                         params={"period": "day", "lang": "zz"})
    assert unknown.status_code == 422


def test_pdf_en_labels_structural(container):
    from server.report.pdf import _ensure_font, pdf_labels

    c = container
    en = pdf_labels("en")
    zh = pdf_labels("zh")
    assert en["title"] == "Parking Operation Report"
    assert zh["title"] == "停车场运营统计报表"
    assert en["vehicle_headers"]["plate"] == "Plate"
    assert zh["vehicle_headers"]["plate"] == "车牌"
    font = _ensure_font("STSong-Light")
    by_day = [{
        "date": "2026-11-04",
        "vehicles": [{
            "plate": "京A90001",
            "entry_time": "2026-11-04T10:00:00+08:00",
            "exit_time": "2026-11-04T11:00:00+08:00",
            "duration_minutes": 60, "amount": 5.0,
            "payment_method": "auto", "space_no": "A-01",
        }],
    }]
    table_en = c.pdf._vehicle_table(by_day, font, with_date=True, lang="en")
    header_en = [cell.text for cell in table_en._cellvalues[0]]
    assert header_en == ["Date", "Plate", "Entry time", "Exit time",
                         "Duration (min)", "Amount (CNY)", "Payment method",
                         "Space no."]
    assert all(isinstance(cell, Paragraph) for cell in table_en._cellvalues[1])
    assert table_en._cellvalues[1][6].text == "Auto-paid"
    # 默认/zh 保持中文
    table_zh = c.pdf._vehicle_table(by_day, font, with_date=True)
    assert [cell.text for cell in table_zh._cellvalues[0]][:2] == ["日期", "车牌"]
    assert table_zh._cellvalues[1][6].text == "自动扣费"
    # 摘要表英文标签
    summary = {"count_in": 1, "count_out": 1, "revenue": 5.0,
               "avg_minutes": 60.0, "peak_occupancy": 1}
    summary_en = c.pdf._summary_table_preview(summary, font, lang="en")
    labels_en = [row[0] for row in summary_en._cellvalues]
    assert "Total revenue (CNY)" in labels_en
    assert "Average duration (minutes)" in labels_en
    # 每日/周/月汇总表英文表头
    daily = c.pdf._daily_table(by_day, font, lang="en")
    assert list(daily._cellvalues[0]) == en["daily_headers"]
    # 英文 PDF 可生成且合法
    preview = c.reports.preview("day", "2026-11-04")
    out = c.cfg.paths.reports_dir / "i18n_en.pdf"
    c.pdf.generate_from_preview(preview, out, lang="en")
    data = out.read_bytes()
    assert data.startswith(b"%PDF") and len(data) > 1000
    # 中文默认输出仍与旧标签一致
    out_zh = c.cfg.paths.reports_dir / "i18n_zh.pdf"
    c.pdf.generate_from_preview(preview, out_zh)
    assert out_zh.read_bytes().startswith(b"%PDF")

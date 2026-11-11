"""API and reporting boundaries use business-local time over stored UTC."""
from datetime import date, datetime

from server.core.domain import FeeBreakdown
from server.core.timeutil import date_range, iso_local, parse_local_iso


def _paid_record(c, plate, entry, exit_time, amount):
    with c.db.scope():
        rec = c.parking_repo.create(plate=plate, plate_color="blue",
                                    entry_time=entry, space_no=None, status="out")
        c.parking_repo.mark_exit(rec, exit_time=exit_time)
        c.payment_repo.create(
            parking_record_id=rec.id, plate=plate, minutes=30, billable_units=1,
            amount_due=amount, amount_paid=amount, method="mock_scan",
            operator="admin", breakdown=FeeBreakdown(minutes=30, amount_due=amount),
            paid_at=exit_time)


def test_local_input_and_offset_input_refer_to_same_utc_instant(cfg):
    expected = datetime(2026, 5, 9, 16, 30)
    assert parse_local_iso("2026-05-10T00:30:00") == expected
    assert parse_local_iso("2026-05-10T00:30:00+08:00") == expected
    assert parse_local_iso("2026-05-09T16:30:00Z") == expected
    assert iso_local(expected) == "2026-05-10T00:30:00+08:00"
    assert date_range("day", date(2026, 5, 10)) == (
        datetime(2026, 5, 9, 16), datetime(2026, 5, 10, 16))


def test_business_day_counts_both_local_edges(container):
    _paid_record(container, "京A11111", datetime(2026, 5, 9, 16, 30),
                 datetime(2026, 5, 9, 17), 5)
    _paid_record(container, "京A22222", datetime(2026, 5, 10, 15, 30),
                 datetime(2026, 5, 10, 15, 50), 7)
    _paid_record(container, "京A33333", datetime(2026, 5, 10, 16, 30),
                 datetime(2026, 5, 10, 17), 9)
    report = container.stats.stats("day", "2026-05-10")
    assert (report.count_in, report.count_out, report.revenue) == (2, 2, 12)
    assert len(report.by_day) == 1
    assert report.by_day[0].date == "2026-05-10"
    assert report.by_day[0].revenue == 12


def test_api_filter_and_list_expiry_use_business_local_time(client, admin_token):
    c = client.app.state.container
    with c.db.scope():
        c.parking_repo.create(plate="京A12345", plate_color="blue",
                              entry_time=datetime(2026, 5, 9, 16, 30),
                              space_no=None, status="in")
    records = client.get("/api/records", params={
        "start": "2026-05-10T00:00:00", "end": "2026-05-10T01:00:00"})
    assert records.status_code == 200
    assert records.json()["total"] == 1
    assert records.json()["items"][0]["entry_time"] == "2026-05-10T00:30:00+08:00"

    created = client.post("/api/lists", headers=admin_token, json={
        "plate": "京B12345", "list_type": "monthly",
        "valid_until": "2026-05-10T00:30:00"})
    assert created.status_code == 200
    row = c.list_repo.get("京B12345")
    assert row.valid_until == datetime(2026, 5, 9, 16, 30)
    assert client.get("/api/lists").json()["items"][0]["valid_until"] == (
        "2026-05-10T00:30:00+08:00")


def test_configured_timezone_handles_dst_day(cfg):
    cfg.server.timezone = "America/New_York"
    assert date_range("day", date(2026, 3, 8)) == (
        datetime(2026, 3, 8, 5), datetime(2026, 3, 9, 4))


def test_state_snapshot_and_event_history_have_explicit_local_offset(client, admin_token):
    response = client.get("/api/state/snapshot", headers=admin_token)
    assert response.status_code == 200
    assert response.json()["server_time"].endswith("+08:00")
    entry = client.post("/api/manual/register", headers=admin_token, json={
        "plate": "京A12345", "color": "blue"})
    assert entry.status_code == 200
    history = client.get("/api/events?last=50", headers=admin_token)
    assert history.status_code == 200
    assert history.json()["items"]
    assert all(item["ts"].endswith("+08:00") for item in history.json()["items"])

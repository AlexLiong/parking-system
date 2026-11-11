def _auth(client):
    response = client.post("/api/auth/login", json={
        "username": "admin", "password": "admin123"})
    assert response.status_code == 200
    return {"Authorization": f"Bearer {response.json()['token']}"}


def test_correction_is_validated_audited_and_idempotent(client):
    c = client.app.state.container
    with c.db.scope():
        row = c.recog_repo.add(
            gate="in", image_path="frame.jpg", plate="京A12345",
            plate_color="blue", char_confs=[], overall_conf=0.71,
            elapsed_ms=15, success=False)
        log_id = row.id
    path = f"/api/recognition-logs/{log_id}/correct"
    headers = _auth(client)
    client.cookies.clear()

    assert client.post(path, json={"plate": "京B12345"}).status_code == 401
    assert client.post(path, json={"plate": "invalid"}, headers=headers).status_code == 422
    assert client.post("/api/recognition-logs/999999/correct",
                       json={"plate": "京B12345"}, headers=headers).status_code == 404
    assert c.recog_repo.get(log_id)["manual_corrected"] is False

    response = client.post(path, json={"plate": "京B·12345"}, headers=headers)
    assert response.status_code == 200, response.text
    assert response.json()["plate"] == "京A12345"
    assert response.json()["corrected_plate"] == "京B12345"
    assert response.json()["manual_corrected"] is True
    assert client.post(path, json={"plate": "京B12345"}, headers=headers).status_code == 200
    assert len([o for o in c.oplog_repo.recent() if o["action"] == "recognition_correct"]) == 1

    changed = client.post(path, json={"plate": "京C12345"}, headers=headers)
    assert changed.status_code == 200
    audits = [o for o in c.oplog_repo.recent() if o["action"] == "recognition_correct"]
    assert len(audits) == 2
    assert audits[0]["detail"] == {
        "original_plate": "京A12345",
        "previous_corrected_plate": "京B12345",
        "corrected_plate": "京C12345",
    }
    assert c.recog_repo.get(log_id)["plate"] == "京A12345"


def test_green_plate_correction_does_not_mutate_parking_record(client):
    c = client.app.state.container
    c.access.manual_register("京A12345", "in", "tester")
    with c.db.scope():
        row = c.recog_repo.add(
            gate="in", image_path="green.jpg", plate=None,
            plate_color=None, char_confs=[], overall_conf=0,
            elapsed_ms=10, success=False)
        log_id = row.id
    response = client.post(f"/api/recognition-logs/{log_id}/correct",
                           json={"plate": "京AD12345"}, headers=_auth(client))
    assert response.status_code == 200, response.text
    assert response.json()["corrected_plate"] == "京AD12345"
    assert c.parking_repo.find_open_by_plate("京A12345") is not None
    assert c.parking_repo.find_open_by_plate("京AD12345") is None

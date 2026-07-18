from radar import google_sheets as gs


def _configure(monkeypatch):
    for name, value in {
        "GOOGLE_CLIENT_ID": "cid", "GOOGLE_CLIENT_SECRET": "secret",
        "GOOGLE_REFRESH_TOKEN": "refresh", "GOOGLE_SHEET_ID": "sheet",
    }.items():
        monkeypatch.setenv(name, value)


def test_sheet_sync_appends_then_marks_metadata(monkeypatch):
    _configure(monkeypatch)
    monkeypatch.setattr(gs, "_access_token", lambda: "access")
    monkeypatch.setattr(gs, "_values", lambda token: [gs.HEADERS])
    appended = []
    monkeypatch.setattr(gs, "_append", lambda token, rows: appended.extend(rows))
    monkeypatch.setattr(gs, "_put", lambda *a, **k: None)
    entry = {"id": "j1", "company": "Acme", "title": "Engineer", "stage": "saved",
             "url": "u", "locations": ["Newark"], "source": "greenhouse"}
    assert gs.sync_applied([entry]) == 1
    assert appended[0][:4] == ["j1", "Acme", "Engineer", "saved"]
    assert entry["sheet_synced"] and entry["sheet_stage"] == "saved"


def test_sheet_readback_is_id_matched_and_stage_validated(monkeypatch):
    _configure(monkeypatch)
    monkeypatch.setattr(gs, "_access_token", lambda: "access")
    monkeypatch.setattr(gs, "_values", lambda token: [gs.HEADERS,
        ["j1", "Acme", "Engineer", "Interview"],
        ["j2", "Other", "Engineer", "invented-stage"]])
    entries = [{"id": "j1", "stage": "applied"}, {"id": "j2", "stage": "applied"}]
    assert gs.sync_from_sheet(entries) == 1
    assert entries[0]["stage"] == "interview" and entries[0]["responded_at"]
    assert entries[1]["stage"] == "applied"

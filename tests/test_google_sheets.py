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


def test_create_tracker_builds_three_tabs_and_returns_url(monkeypatch):
    for name, value in {
        "GOOGLE_CLIENT_ID": "cid", "GOOGLE_CLIENT_SECRET": "secret",
        "GOOGLE_REFRESH_TOKEN": "refresh",
    }.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(gs, "_access_token", lambda: "access")
    writes = []
    monkeypatch.setattr(gs, "_put_for", lambda token, sheet_id, tab, cell_range, rows:
                        writes.append((sheet_id, tab, cell_range, rows)))

    class Response:
        def __init__(self, payload=None):
            self.payload = payload or {}

        def raise_for_status(self):
            return None

        def json(self):
            return self.payload

    calls = []

    def post(url, **kwargs):
        calls.append((url, kwargs))
        if url == gs.SHEETS_API:
            return Response({"spreadsheetId": "new-sheet", "sheets": [
                {"properties": {"title": "Applications", "sheetId": 1}},
                {"properties": {"title": "User Applications", "sheetId": 2}},
                {"properties": {"title": "Accounts", "sheetId": 3}},
                {"properties": {"title": "Guide", "sheetId": 4}},
            ]})
        return Response()

    monkeypatch.setattr(gs.requests, "post", post)

    result = gs.create_tracker("Test Tracker")

    assert result["spreadsheet_id"] == "new-sheet"
    assert result["url"].endswith("/new-sheet/edit")
    assert [row[1] for row in writes] == ["Applications", "User Applications", "Accounts", "Guide"]
    assert len(calls) == 2  # create + one formatting batch

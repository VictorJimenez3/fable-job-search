from radar.discovery import record_result
from radar.main import _select_companies


def test_company_selection_rotates_overdue_boards_ahead_of_fresh_priority_rows(monkeypatch):
    """A successful priority board must not permanently consume the cap."""
    monkeypatch.setenv("RADAR_COMPANY_ROTATION_SECONDS", "3600")
    now = 10_000
    registry = {
        "priority": {"name": "Priority Co", "status": "active", "origin": "seed",
                     "pm_interest": True, "last_polled_at": now - 60},
        "stale": {"name": "Stale Co", "status": "active", "origin": "harvest:x",
                  "last_polled_at": now - 7200},
        "fresh": {"name": "Fresh Co", "status": "active", "origin": "harvest:x",
                  "last_polled_at": now - 60},
    }

    monkeypatch.setattr("radar.main.time.time", lambda: now)
    assert _select_companies(registry, 1)[0]["name"] == "Stale Co"


def test_record_result_advances_poll_cursor_on_failure(monkeypatch):
    monkeypatch.setattr("radar.discovery.time.time", lambda: 1234)
    entry = {"status": "active", "failures": 0, "last_ok": 900}

    record_result(entry, False)

    assert entry["last_polled_at"] == 1234
    assert entry["last_ok"] == 900
    assert entry["failures"] == 1


def test_overdue_rotation_reaches_lower_priority_boards_when_all_are_stale(monkeypatch):
    """An all-overdue crawl must still make progress through the whole cap."""
    monkeypatch.setenv("RADAR_COMPANY_ROTATION_SECONDS", "3600")
    now = 10_000
    registry = {
        str(index): {
            "name": f"Company {index}",
            "status": "active",
            "origin": "seed" if index == 0 else "harvest:feed",
            "pm_interest": index == 1,
            "sector": "healthtech" if index == 2 else "other",
            "last_polled_at": now - 7200,
        }
        for index in range(6)
    }
    monkeypatch.setattr("radar.main.time.time", lambda: now)

    selected_names = set()
    for cycle in range(3):
        selected = _select_companies(registry, 2)
        selected_names.update(entry["name"] for entry in selected)
        for entry in selected:
            # Model each bounded fetch completing before the next crawl. All
            # entries remain overdue; oldest poll time, not priority, must
            # therefore move the cursor forward.
            entry["last_polled_at"] = now - 7200 + cycle + 1

    assert selected_names == {f"Company {index}" for index in range(6)}

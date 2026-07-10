import email
import time

import pytest

from radar import email_watch as ew
from radar import state


@pytest.fixture
def tmp_state(tmp_path, monkeypatch):
    monkeypatch.setattr(state, "STATE_DIR", tmp_path)
    return tmp_path


def _msg(from_hdr: str, subject: str) -> email.message.Message:
    m = email.message.Message()
    m["From"] = from_hdr
    m["Subject"] = subject
    m["Message-ID"] = "<test@example.com>"
    return m


def test_confirmation_regex_matches_common_phrasing():
    assert ew.CONFIRMATION_RE.search("Thank you for applying to Software Engineer at Stripe")
    assert ew.CONFIRMATION_RE.search("We've received your application")
    assert ew.CONFIRMATION_RE.search("Your application to Tempus has been received")
    assert ew.CONFIRMATION_RE.search("Application Confirmation - Data Scientist")
    assert not ew.CONFIRMATION_RE.search("Your interview is scheduled for Monday")
    assert not ew.CONFIRMATION_RE.search("Weekly newsletter from Stripe")


def test_guess_company_candidates_from_display_name_and_domain():
    msg = _msg("Tempus Careers <careers-noreply@tempus.com>", "Thank you for applying to Data Scientist 1")
    candidates = ew.guess_company_candidates(msg)
    assert "Tempus" in candidates


def test_guess_company_candidates_skips_ats_domain_uses_subject():
    msg = _msg("no-reply <no-reply@myworkday.com>", "Thank you for applying to Software Engineer at Medtronic")
    candidates = ew.guess_company_candidates(msg)
    assert any("Medtronic" in c for c in candidates)


def test_match_company_prefers_shortlist_over_jobs():
    shortlist = [{"id": "s1", "company": "Tempus Labs", "title": "DS", "url": "u1",
                 "locations": [], "score": 80, "source": "simplify"}]
    jobs = {"j1": {"id": "j1", "company": "Tempus Labs Inc", "title": "SWE", "url": "u2",
                   "locations": [], "score": 70, "source": "greenhouse", "first_seen": int(time.time())}}
    match = ew.match_company(["Tempus"], shortlist, jobs)
    assert match["id"] == "s1"


def test_match_company_falls_back_to_jobs_when_not_shortlisted():
    jobs = {"j1": {"id": "j1", "company": "Neuralink", "title": "SWE", "url": "u2",
                   "locations": [], "score": 70, "source": "greenhouse", "first_seen": int(time.time())}}
    match = ew.match_company(["Neuralink Careers"], [], jobs)
    assert match["id"] == "j1"


def test_match_company_returns_none_when_nothing_close():
    match = ew.match_company(["Completely Unrelated Corp"], [], {})
    assert match is None


def test_token_overlap_basic():
    assert ew._token_overlap("Uber", "Uber Technologies, Inc.") == 1.0
    assert ew._token_overlap("Stripe", "Tempus") == 0.0


class _FakeIMAP:
    """Minimal imaplib.IMAP4_SSL stand-in for testing run()."""
    def __init__(self, messages):
        self._messages = messages  # {uid_bytes: raw_message_bytes}

    def login(self, *a, **k):
        return "OK", [b""]

    def select(self, *a, **k):
        return "OK", [str(len(self._messages)).encode()]

    def search(self, charset, *criteria):
        return "OK", [b" ".join(self._messages.keys())]

    def fetch(self, uid, spec):
        raw = self._messages.get(uid)
        if raw is None:
            return "NO", [None]
        return "OK", [(b"1 (BODY[HEADER])", raw)]

    def logout(self):
        return "OK", [b""]


def _raw(from_hdr, subject, msg_id):
    return (f"From: {from_hdr}\r\nSubject: {subject}\r\nMessage-ID: {msg_id}\r\n\r\n").encode()


def test_run_matches_shortlisted_job_and_syncs(tmp_state, monkeypatch):
    monkeypatch.setenv("EMAIL_ADDRESS", "vmj@njit.edu")
    monkeypatch.setenv("EMAIL_APP_PASSWORD", "fake")
    monkeypatch.delenv("NOTION_TOKEN", raising=False)

    state.save("shortlist.json", [{"id": "abc123", "company": "Tempus", "title": "Data Scientist 1",
                                   "url": "https://tempus.com/jobs/1", "locations": ["Chicago, IL"],
                                   "score": 88, "source": "simplify"}])
    state.save("jobs.json", {})

    fake_messages = {
        b"1": _raw("Tempus Careers <careers@tempus.com>",
                   "Thank you for applying to Data Scientist 1", "<m1@tempus.com>"),
        b"2": _raw("newsletter@randomsite.com", "Weekly digest", "<m2@randomsite.com>"),
    }
    monkeypatch.setattr(ew.imaplib, "IMAP4_SSL", lambda host, port: _FakeIMAP(fake_messages))

    result = ew.run(lookback_days=5)
    assert result["matched"] == 1
    applied = state.applied()
    assert len(applied) == 1
    assert applied[0]["company"] == "Tempus"
    assert applied[0]["via"] == "email"
    assert state.shortlist() == []  # promoted out


def test_run_is_idempotent_on_rerun(tmp_state, monkeypatch):
    monkeypatch.setenv("EMAIL_ADDRESS", "vmj@njit.edu")
    monkeypatch.setenv("EMAIL_APP_PASSWORD", "fake")
    monkeypatch.delenv("NOTION_TOKEN", raising=False)
    state.save("shortlist.json", [{"id": "abc123", "company": "Tempus", "title": "Data Scientist 1",
                                   "url": "u", "locations": [], "score": 88, "source": "simplify"}])
    state.save("jobs.json", {})
    fake_messages = {b"1": _raw("Tempus Careers <careers@tempus.com>",
                                "Thank you for applying to Data Scientist 1", "<m1@tempus.com>")}
    monkeypatch.setattr(ew.imaplib, "IMAP4_SSL", lambda host, port: _FakeIMAP(fake_messages))
    ew.run(lookback_days=5)
    assert len(state.applied()) == 1
    # re-running must not double-log the same message
    monkeypatch.setattr(ew.imaplib, "IMAP4_SSL", lambda host, port: _FakeIMAP(fake_messages))
    ew.run(lookback_days=5)
    assert len(state.applied()) == 1


def test_run_skips_without_credentials(tmp_state, monkeypatch):
    monkeypatch.delenv("EMAIL_ADDRESS", raising=False)
    monkeypatch.delenv("EMAIL_APP_PASSWORD", raising=False)
    result = ew.run()
    assert result == {"checked": 0, "matched": 0, "synced": 0}

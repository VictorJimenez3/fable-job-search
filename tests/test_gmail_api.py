import base64
import email

from radar import gmail_api, state


class Response:
    def __init__(self, status, body):
        self.status_code = status
        self.body = body

    def json(self):
        return self.body


class Session:
    def __init__(self, raw):
        self.raw = raw
        self.gets = []

    def post(self, url, **kwargs):
        assert kwargs["timeout"] == 20
        return Response(200, {"access_token": "access"})

    def get(self, url, **kwargs):
        self.gets.append((url, kwargs))
        if url.endswith("/profile"):
            return Response(200, {"historyId": "200", "messagesTotal": 2})
        if url.endswith("/messages"):
            return Response(200, {"messages": [{"id": "m1"}]})
        if url.endswith("/messages/m1"):
            return Response(200, {"raw": self.raw})
        raise AssertionError(url)

    def close(self):
        pass


def test_gmail_api_initial_query_fetches_raw_and_commits_cursor(tmp_path, monkeypatch):
    monkeypatch.setattr(state, "STATE_DIR", tmp_path)
    monkeypatch.setenv("GMAIL_REFRESH_TOKEN", "refresh")
    monkeypatch.setenv("GOOGLE_AUTH_CLIENT_ID", "client")
    monkeypatch.setenv("GOOGLE_AUTH_CLIENT_SECRET", "secret")
    raw = base64.urlsafe_b64encode(
        b"From: Careers <jobs@example.com>\r\nSubject: Interview\r\nMessage-ID: <m1@example.com>\r\n\r\n"
    ).decode().rstrip("=")
    conn = gmail_api.GmailAPIConnection(Session(raw))
    assert conn.select("INBOX", readonly=True)[0] == "OK"
    status, values = conn.search(None, "ignored")
    assert status == "OK" and values == [b"m1"]
    fetched = conn.fetch(b"m1", "BODY.PEEK[]")
    message = email.message_from_bytes(fetched[1][0][1])
    assert message["Message-ID"] == "<m1@example.com>"
    conn.commit()
    assert state.load(gmail_api.CURSOR_FILE, {})["history_id"] == "200"

import uuid

from radar.worker import run_one


class FakeRepository:
    def __init__(self, item):
        self.item = item
        self.finished = []

    def lease_work(self, owner):
        item, self.item = self.item, None
        return item

    def finish_work(self, item_id, owner, *, error=""):
        self.finished.append((item_id, owner, error))


def test_worker_completes_known_item():
    item_id = uuid.uuid4()
    repo = FakeRepository({"id": item_id, "kind": "example", "payload": {"value": 4}})
    seen = []
    assert run_one(repo, owner="worker-1", available_handlers={"example": lambda payload: seen.append(payload)})
    assert seen == [{"value": 4}]
    assert repo.finished == [(item_id, "worker-1", "")]


def test_worker_records_unknown_kind_for_retry():
    item_id = uuid.uuid4()
    repo = FakeRepository({"id": item_id, "kind": "unknown", "payload": {}})
    assert run_one(repo, owner="worker-1", available_handlers={})
    assert repo.finished[0][:2] == (item_id, "worker-1")
    assert "unsupported work kind" in repo.finished[0][2]


def test_worker_returns_false_when_queue_is_empty():
    assert not run_one(FakeRepository(None), owner="worker-1", available_handlers={})

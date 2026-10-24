import pytest

from modules.poll import PollManager
from modules.signal_client import DaemonUnreachable

GROUP = "kicker-group"
FRESH = object()


class FakeClient:
    """Stands in for SignalClient. `fail[method]` is a queue of exceptions to
    raise on successive calls; a None entry lets that call succeed."""

    def __init__(self):
        self.groups = [{"id": GROUP, "name": "Kicker", "isMember": True}]
        self.polls = []
        self.messages = []
        self.calls = []
        self.fail = {}
        self.poll_result = FRESH  # what send_poll returns; FRESH: a success with a new timestamp

    def _call(self, method):
        self.calls.append(method)
        queue = self.fail.get(method)
        return queue.pop(0) if queue else None

    def list_groups(self):
        if error := self._call("list_groups"):
            raise error
        return self.groups

    def send_poll(self, group_id, question, options):
        error = self._call("send_poll")
        # Like the real thing: only a refused connection proves nothing went out
        if not isinstance(error, DaemonUnreachable):
            self.polls.append((group_id, question, options))
        if error:
            raise error
        if self.poll_result is FRESH:
            return {"timestamp": 1000 + len(self.polls), "results": [{"type": "SUCCESS"}]}
        return self.poll_result

    def send_message(self, group_id, message, **kwargs):
        if error := self._call("send_message"):
            raise error
        self.messages.append((group_id, message))
        return {}


@pytest.fixture
def client():
    return FakeClient()


@pytest.fixture
def config(tmp_path):
    return {
        "group_id": GROUP,
        "admin_group_id": "admin-group",
        "poll": {"state_file": str(tmp_path / "polls.log"), "weeks_ahead": 2, "retry_delay": 0},
    }


@pytest.fixture
def sleeps():
    return []


@pytest.fixture
def make_pm(client, config, sleeps):
    def make(**poll_overrides):
        config["poll"].update(poll_overrides)
        return PollManager(client, config, sleep=sleeps.append)
    return make


@pytest.fixture
def pm(make_pm):
    return make_pm()

"""The `poll` command: who gets told what, and the exit code systemd sees."""

from datetime import date

import pytest

import marvin
from modules.chat import ChatHandler
from modules.signal_client import DaemonUnreachable, NoResponse


@pytest.fixture
def run(monkeypatch, client, config):
    pings = []
    monkeypatch.setattr(marvin, "SignalClient", lambda config: client)
    monkeypatch.setattr(marvin.requests, "post", lambda url, **kw: pings.append(url))
    monkeypatch.setattr(marvin.PollManager, "run", _run_on(date(2026, 10, 24)))
    config["healthcheck_url"] = "https://hc.example/abc"

    def run():
        return marvin.run_poll(config, dry_run=False), pings
    return run


def _run_on(today):
    original = marvin.PollManager.run
    return lambda self: original(self, today)


def admin_messages(client):
    return [text for group, text in client.messages if group == "admin-group"]


def test_success_reports_to_admin_and_pings(run, client):
    code, pings = run()
    assert code == 0
    assert admin_messages(client) == ["✅ Umfragen gepostet: KW44 (26.10.–01.11.), KW45 (02.11.–08.11.)"]
    assert pings == ["https://hc.example/abc"]


def test_nothing_due_stays_quiet_but_pings(run, client):
    run()
    client.messages.clear()
    code, pings = run()
    assert code == 0
    assert admin_messages(client) == []
    assert pings[-1] == "https://hc.example/abc"


def test_problem_fails_the_unit_and_alerts(run, client):
    client.fail["send_poll"] = [NoResponse("timeout")]
    code, pings = run()
    assert code == 1
    assert "⚠️" in admin_messages(client)[0]
    assert pings == ["https://hc.example/abc/fail"]


def test_crash_still_alerts(run, client, monkeypatch):
    monkeypatch.setattr(marvin.PollManager, "run", lambda self: 1 / 0)
    code, pings = run()
    assert code == 1
    assert "ZeroDivisionError" in admin_messages(client)[0]
    assert pings == ["https://hc.example/abc/fail"]


def test_dry_run_sends_nothing(client, config, monkeypatch, capsys):
    monkeypatch.setattr(marvin, "SignalClient", lambda config: client)
    assert marvin.run_poll(config, dry_run=True) == 0
    assert client.calls == []
    assert "Due:" in capsys.readouterr().out


def test_chat_reconnects_with_backoff(client, config, monkeypatch):
    """Exiting when the daemon went away left the bot dead for good: exit code
    0 does not trigger Restart=on-failure."""
    attempts, delays = [], []

    def subscribe():
        attempts.append(1)
        raise DaemonUnreachable("refused")

    def sleep(seconds):
        delays.append(seconds)
        if len(delays) == 6:
            raise KeyboardInterrupt

    client.subscribe = subscribe
    monkeypatch.setattr("modules.chat.time.sleep", sleep)
    config.update(bot_number="+49", llm_url="http://llm", history_file=str(config["poll"]["state_file"]) + ".hist")
    with pytest.raises(KeyboardInterrupt):
        ChatHandler(client, config).run()
    assert len(attempts) == 6
    assert delays == [5, 10, 20, 40, 60, 60]

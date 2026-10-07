import socket

from jev_ultrafast import doctor


def free_port():
    # The real server may be running on 8766; the checks must not depend on that.
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def test_doctor_reports_each_requirement_and_fails_without_chrome(monkeypatch, capsys):
    monkeypatch.setenv("OPENROUTER_API_KEY", "offline-test-key")
    monkeypatch.setenv("BU_CDP_URL", "http://127.0.0.1:1")  # nothing listens here
    assert doctor.run(free_port()) == 1
    out = capsys.readouterr().out
    assert "OK   OpenRouter key" in out and "FAIL Chrome debugging" in out and "1 check(s) failed." in out


def test_doctor_passes_when_chrome_answers(monkeypatch, capsys):
    monkeypatch.setenv("OPENROUTER_API_KEY", "offline-test-key")
    monkeypatch.setenv("BU_CDP_URL", "http://127.0.0.1:9")
    monkeypatch.setattr(doctor, "probe", lambda url: "Chrome/test")
    assert doctor.run(free_port()) == 0
    assert "All checks passed." in capsys.readouterr().out

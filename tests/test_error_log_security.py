"""Untrusted exception text must never execute in an administrator's browser."""

import config


def test_error_log_escapes_payloads_and_reads_only_a_bounded_tail(logged_in_admin, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "INSTANCE_DIR", str(tmp_path))
    path = tmp_path / "errors.log"
    with path.open("wb") as output:
        output.write(b"old log prefix" + b"x" * 1_000_000)
        output.write(b"<script>window.stolen=true</script>&<img src=x onerror=alert(1)>")
    response = logged_in_admin.get("/admin/error-log")
    assert response.status_code == 200
    assert b"<script>" not in response.data and b"<img " not in response.data
    assert b"&lt;script&gt;window.stolen=true&lt;/script&gt;" in response.data
    assert b"old log prefix" not in response.data
    assert len(response.data) < 21000
    assert "no-store" in response.headers["Cache-Control"]


def test_error_log_requires_an_administrator(client, regular_user, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "INSTANCE_DIR", str(tmp_path))
    (tmp_path / "errors.log").write_text("private diagnostics")
    assert b"private diagnostics" not in client.get("/admin/error-log").data
    with client.session_transaction() as session:
        session["user_id"] = regular_user
    assert b"private diagnostics" not in client.get("/admin/error-log").data

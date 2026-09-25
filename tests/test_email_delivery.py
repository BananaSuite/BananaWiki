"""Transactional email rendering and delivery regressions."""

from hosting import email_delivery


def test_render_email_html_is_branded_responsive_and_escaped():
    output = email_delivery.render_email_html(
        title="Reset <account>",
        eyebrow="SECURITY",
        text="Hello <script>alert(1)</script>",
        detail_label="Username",
        detail_value="user<admin>",
        action_url="https://example.com/reset?token=a&mode=secure",
        action_label="Reset password",
    )

    assert "BananaWiki" in output
    assert "Reset password" in output
    assert "width:100%" in output
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in output
    assert "user&lt;admin&gt;" in output
    assert "token=a&amp;mode=secure" in output
    assert "<script>alert(1)</script>" not in output


def test_text_only_resend_message_gets_html_alternative(monkeypatch):
    sent = {}

    monkeypatch.setattr(email_delivery.config, "HOSTING_EMAIL_PROVIDER", "resend")
    monkeypatch.setattr(email_delivery.config, "HOSTING_EMAIL_API_KEY", "test-key")
    monkeypatch.setattr(
        email_delivery.config,
        "HOSTING_EMAIL_FROM",
        "BananaWiki <noreply@bananawiki.com>",
    )
    monkeypatch.setattr(
        email_delivery.config,
        "HOSTING_EMAIL_REPLY_TO",
        "contact@bananawiki.com",
    )

    def capture_post(url, headers, payload):
        sent.update(url=url, headers=headers, payload=payload)
        return True, ""

    monkeypatch.setattr(email_delivery, "_api_post", capture_post)
    monkeypatch.setattr(email_delivery, "_recipient_counts", {})

    ok, error = email_delivery.send_email(
        to="person@example.com",
        subject="Account restored",
        text="Your account is active again.",
    )

    assert ok is True
    assert error == ""
    assert sent["payload"]["text"] == "Your account is active again."
    assert sent["payload"]["html"].startswith("<!doctype html>")
    assert "Account restored" in sent["payload"]["html"]

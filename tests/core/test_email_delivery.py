from email import policy
from email.parser import BytesParser
from unittest.mock import MagicMock

import pytest
from pydantic import ValidationError

from app.core.config import Settings
from app.core.email import ConfiguredEmailSender


def settings(**kwargs):
    return Settings(_env_file=None, **kwargs)


@pytest.mark.parametrize("url", ["http://example.com", "https://example.com/redirect", "https://user:password@example.com", "https://example.com?next=bad"])
def test_untrusted_verification_origin_is_rejected(url):
    with pytest.raises(ValidationError):
        settings(EMAIL_VERIFICATION_ENABLED=True, EMAIL_DELIVERY_MODE="file", EMAIL_VERIFICATION_BASE_URL=url)


def test_verification_cannot_use_fake_delivery_or_nonproduction_preview():
    with pytest.raises(ValidationError):
        settings(EMAIL_VERIFICATION_ENABLED=True)
    with pytest.raises(ValidationError):
        settings(ENVIRONMENT="production", JWT_SECRET_KEY="independent-test-secret", EMAIL_DELIVERY_MODE="file")
    with pytest.raises(ValidationError):
        settings(EMAIL_DELIVERY_MODE="smtp")


async def test_local_mail_is_readable_without_external_delivery(tmp_path):
    sender = ConfiguredEmailSender(settings(EMAIL_DELIVERY_MODE="file", EMAIL_PREVIEW_DIRECTORY=str(tmp_path)))
    await sender.send("learner@example.com", "Verify Tamakkan", "https://local.example/verify-email#token=example\nتأكيد البريد")
    files = list(tmp_path.glob("*.eml"))
    assert len(files) == 1
    message = BytesParser(policy=policy.default).parsebytes(files[0].read_bytes())
    assert message["To"] == "learner@example.com"
    assert "تأكيد البريد" in message.get_content()


@pytest.mark.parametrize("security", ["starttls", "ssl"])
async def test_smtp_uses_tls_and_configured_sender(monkeypatch, security):
    factory = MagicMock()
    smtp = factory.return_value.__enter__.return_value
    smtp.send_message.return_value = {}
    monkeypatch.setattr(f"app.core.email.smtplib.{'SMTP_SSL' if security == 'ssl' else 'SMTP'}", factory)
    sender = ConfiguredEmailSender(settings(EMAIL_DELIVERY_MODE="smtp", SMTP_HOST="smtp.example.com",
                                           SMTP_SECURITY=security, SMTP_FROM_EMAIL="team@example.com",
                                           SMTP_USERNAME="team", SMTP_PASSWORD="test-only"))
    await sender.send("learner@example.com", "Verify", "Open your link")
    smtp.login.assert_called_once_with("team", "test-only")
    message = smtp.send_message.call_args.args[0]
    assert message["From"] == "team@example.com"
    assert message["To"] == "learner@example.com"
    if security == "starttls":
        smtp.starttls.assert_called_once()
    else:
        assert "context" in factory.call_args.kwargs

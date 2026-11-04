"""Configured email delivery. File previews are an explicit local-only mode."""

from email.message import EmailMessage
from pathlib import Path
import smtplib
import ssl
from typing import Protocol
from uuid import uuid4

from fastapi.concurrency import run_in_threadpool
from loguru import logger

from app.core.config import Settings, get_settings


class EmailSender(Protocol):
    async def send(self, to: str, subject: str, body: str) -> None: ...


class LoggingEmailSender:
    async def send(self, to: str, subject: str, body: str) -> None:
        # Never log `body` verbatim - it can carry a password-reset token
        # (see AuthService.request_password_reset). Recipient + subject is
        # enough to confirm a send happened without leaking one into logs.
        logger.info("EMAIL preview only (no delivery) to={} subject={!r}", to, subject)


class ConfiguredEmailSender:
    def __init__(self, settings: Settings):
        self.settings = settings

    async def send(self, to: str, subject: str, body: str) -> None:
        await run_in_threadpool(self._send, to, subject, body)

    def _send(self, to: str, subject: str, body: str) -> None:
        settings = self.settings
        message = EmailMessage()
        message["From"] = settings.SMTP_FROM_EMAIL or "preview@tamakkan.example.com"
        message["To"] = to
        message["Subject"] = subject
        message.set_content(body)
        if settings.EMAIL_DELIVERY_MODE == "file":
            if settings.ENVIRONMENT != "development":
                raise ValueError("Email file previews are disabled outside development")
            path = Path(settings.EMAIL_PREVIEW_DIRECTORY)
            path.mkdir(parents=True, exist_ok=True)
            with (path / f"{uuid4()}.eml").open("xb") as stream:
                stream.write(message.as_bytes())
            logger.info("Email saved to local preview directory; no external email sent")
            return
        context = ssl.create_default_context()
        client_type = smtplib.SMTP_SSL if settings.SMTP_SECURITY == "ssl" else smtplib.SMTP
        kwargs = {"context": context} if settings.SMTP_SECURITY == "ssl" else {}
        with client_type(settings.SMTP_HOST, settings.SMTP_PORT, timeout=15, **kwargs) as client:
            if settings.SMTP_SECURITY == "starttls":
                client.ehlo()
                client.starttls(context=context)
                client.ehlo()
            if settings.SMTP_USERNAME:
                client.login(settings.SMTP_USERNAME, settings.SMTP_PASSWORD)
            if client.send_message(message):
                raise smtplib.SMTPException("Email recipient was refused")


def get_email_sender() -> EmailSender:
    settings = get_settings()
    if settings.EMAIL_DELIVERY_MODE == "logging":
        return LoggingEmailSender()
    return ConfiguredEmailSender(settings)

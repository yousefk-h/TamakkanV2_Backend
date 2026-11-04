"""One-use email proofs with atomic, database-backed resend limits.

The hash and verification transition live on the same User document so two
confirmations cannot both succeed, even with multiple API workers.
"""
from datetime import timedelta

from loguru import logger
from pymongo import ReturnDocument

from app.core.config import get_settings
from app.core.email import EmailSender, get_email_sender
from app.core.exceptions import UnprocessableException
from app.models.user import User
from app.repositories.user_repository import UserRepository
from app.utils.time import utcnow
from app.utils.tokens import generate_token, hash_token

INVALID_LINK = "This verification link is invalid, expired, or already used. Request a new email."
RESEND_SECONDS = 60
MAX_SENDS_PER_HOUR = 5


class EmailVerificationService:
    def __init__(self, sender: EmailSender):
        self.sender = sender

    async def request(self, email: str) -> None:
        """Called after the generic HTTP response; never exposes account state."""
        try:
            user = await UserRepository().get_by_email(email)
            if user is not None:
                await self.send_for_user(user)
        except Exception as error:
            # Provider errors may contain secrets/addresses. Log only the type.
            logger.error("Verification email request failed ({})", type(error).__name__)

    async def send_for_user(self, user: User) -> User:
        settings = get_settings()
        if (not settings.EMAIL_VERIFICATION_ENABLED or not user.is_active
                or user.is_deleted or user.email_verification_status != "pending"):
            return user
        now = utcnow()
        collection = User.get_pymongo_collection()
        eligible = {"_id": user.id, "email": user.email, "is_active": True,
                    "is_deleted": False, "email_verification_required": True,
                    "email_verified_at": None}
        # Reset an elapsed window conditionally, then reserve one delivery slot.
        # Mongo's atomic writes prevent concurrent requests bypassing either cap.
        await collection.update_one(
            {**eligible, "$or": [{"email_verification_window_at": None},
                                {"email_verification_window_at": {"$lte": now - timedelta(hours=1)}}]},
            {"$set": {"email_verification_window_at": now, "email_verification_send_count": 0}},
        )
        raw = generate_token()
        token_hash = hash_token(raw)
        reserved = await collection.find_one_and_update(
            {**eligible, "email_verification_send_count": {"$lt": MAX_SENDS_PER_HOUR},
             "$or": [{"email_verification_sent_at": None},
                     {"email_verification_sent_at": {"$lte": now - timedelta(seconds=RESEND_SECONDS)}}]},
            {"$set": {"email_verification_hash": token_hash,
                      "email_verification_email": user.email,
                      "email_verification_expires_at": now + timedelta(hours=settings.EMAIL_VERIFICATION_TTL_HOURS),
                      "email_verification_sent_at": now,
                      "email_verification_delivery": "not_sent", "updated_at": now},
             "$inc": {"email_verification_send_count": 1}},
            return_document=ReturnDocument.AFTER,
        )
        if reserved is None:
            return await User.get(user.id) or user
        # Fragment stays out of HTTP access logs and Referer headers. The page
        # asks for an explicit click before POSTing, so email scanners don't
        # consume a link just by fetching it. Never use the request Host header.
        url = f"{settings.EMAIL_VERIFICATION_BASE_URL.rstrip('/')}/verify-email#token={raw}"
        body = (
            f"Verify your email address for {settings.EMAIL_BRAND_NAME}\n\n"
            f"An account has been created for this email address. To confirm that it belongs to you, open:\n{url}\n\n"
            f"This link expires in {settings.EMAIL_VERIFICATION_TTL_HOURS} hours and can be used once. "
            "Only the newest verification email works.\n"
            "After verifying, sign in with the password provided by your teacher or administrator.\n"
            "If you did not expect this account, ignore this email.\n\n"
            "لتأكيد بريدك الإلكتروني، افتح الرابط أعلاه ثم اضغط تأكيد البريد الإلكتروني. "
            "إذا لم تطلب هذا الحساب، تجاهل هذه الرسالة."
        )
        delivery = "sent"
        try:
            await self.sender.send(to=user.email, subject=f"Verify your {settings.EMAIL_BRAND_NAME} email", body=body)
        except Exception as error:
            delivery = "failed"
            logger.error("Verification email delivery failed ({})", type(error).__name__)
        update = {"email_verification_delivery": delivery}
        if delivery == "failed":
            # Keep the cooldown/hourly budget on failure to prevent retry storms.
            update["email_verification_hash"] = None
            update["email_verification_expires_at"] = None
        await collection.update_one(
            {"_id": user.id, "email_verification_hash": token_hash}, {"$set": update},
        )
        return await User.get(user.id) or user

    async def confirm(self, raw_token: str) -> None:
        token_hash = hash_token(raw_token)
        collection = User.get_pymongo_collection()
        candidate = await collection.find_one({"email_verification_hash": token_hash})
        if candidate is None:
            raise UnprocessableException(INVALID_LINK)
        now = utcnow()
        result = await collection.update_one(
            {"_id": candidate["_id"], "email_verification_hash": token_hash,
             "email": candidate.get("email_verification_email"),
             "email_verification_required": True, "email_verified_at": None,
             "email_verification_expires_at": {"$gt": now},
             "is_active": True, "is_deleted": False},
            {"$set": {"email_verified_at": now, "email_verification_hash": None,
                      "email_verification_email": None, "email_verification_expires_at": None,
                      "updated_at": now}},
        )
        if result.modified_count != 1:
            raise UnprocessableException(INVALID_LINK)


def get_email_verification_service() -> EmailVerificationService:
    return EmailVerificationService(get_email_sender())

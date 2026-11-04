"""Create a guarded, isolated Tamakkan V2 local API configuration once."""

from pathlib import Path
import secrets

backend = Path(__file__).resolve().parents[2]
target = backend / ".env"
if target.exists():
    raise SystemExit(f"Local configuration already exists: {target}")
target.write_text("\n".join([
    "PROJECT_NAME=Tamakkan V2 Local",
    "ENVIRONMENT=development",
    "LOCAL_DEV_MODE=true",
    "MONGODB_URI=mongodb://127.0.0.1:27049",
    "MONGODB_DB_NAME=tamakkan_v2_dev",
    "JWT_SECRET_KEY=" + secrets.token_urlsafe(64),
    "AGENT_INTERNAL_API_KEY=" + secrets.token_urlsafe(48),
    "LIVEKIT_AGENT_NAME=tamakkan-v2-local",
    "LIVEKIT_VOICE_AGENT_NAME=tamakkan-v2-voice-local",
    "LIVEKIT_URL=",
    'CORS_ORIGINS=["http://127.0.0.1:5188"]',
    "EMAIL_DELIVERY_MODE=file",
    "EMAIL_PREVIEW_DIRECTORY=.tamakkan/local/mail-preview",
    "EMAIL_VERIFICATION_BASE_URL=http://127.0.0.1:5188",
    "EMAIL_VERIFICATION_ENABLED=false",
]) + "\n", encoding="utf-8")
print(f"Created V2-only local configuration: {target}")

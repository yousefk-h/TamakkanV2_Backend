# Tamakkan email verification (Task 7)

Implemented on the legacy integration branch using the existing account-creation model:
teachers create students and Root creates teachers. Public self-registration is
not enabled. Real inbox delivery requires Tamakkan SMTP configuration; the local
profile currently writes email previews instead of sending external messages.

## Account flow

1. Create a student from **Students → Invite student**, or a teacher through the
   existing Root API. New accounts in the enabled profile start **pending**.
2. A verification email contains a link to the configured frontend's
   `/verify-email` page. The email does not contain the account password; the
   existing secure password handoff remains necessary.
3. Open the email link and press **Verify email address**. Merely fetching the
   page does not consume the link, so ordinary email-link scanners cannot
   accidentally confirm the account.
4. Return to sign-in and enter the account credentials. Unverified accounts
   cannot log in, refresh a session, or use protected API routes.
5. Use **Resend verification email** on the sign-in/verification page if needed.
   Teachers can also request a new email from the student's detail page. The
   roster and student detail show the account's verification status.

The verification and recovery pages support English, Arabic, phones and tablets.
Expired, replaced, invalid and consumed links offer resend or sign-in. Network
failures allow retry without discarding an otherwise valid link.

## Local testing

Start the local platform with `tamakkan start` (see `docs/LOCAL_DEVELOPMENT.md`) and open http://127.0.0.1:5186.

The local backend profile (`backend/.env`) can enable it with:

```dotenv
EMAIL_VERIFICATION_ENABLED=true
EMAIL_VERIFICATION_BASE_URL=http://127.0.0.1:5186
EMAIL_VERIFICATION_TTL_HOURS=24
EMAIL_BRAND_NAME=Tamakkan
EMAIL_DELIVERY_MODE=file
EMAIL_PREVIEW_DIRECTORY=.tamakkan/local/mail-preview
```

Create a new test account, then open its newest `.eml` file inside
`backend/.tamakkan/local/mail-preview` with an email viewer. Copy/open the link
and complete verification. Opening the raw file in VS Code can show MIME line
wrapping; an email viewer decodes it correctly. No real mailbox delivery occurs
in this mode. These files contain verification secrets and are excluded from Git
and Docker builds. They may be deleted after local testing.

The seeded `.example.com` accounts are existing local fixtures, not real inboxes.
They retain their existing sign-in behavior. Use a newly created account to test
the pending-to-verified flow. Restart the backend after editing SMTP/profile
settings so its cached configuration reloads.

## SMTP configuration

In the private Tamakkan backend environment, choose `EMAIL_DELIVERY_MODE=smtp`
and set `SMTP_HOST`, `SMTP_PORT`, `SMTP_SECURITY`, `SMTP_FROM_EMAIL`,
`SMTP_USERNAME`, and `SMTP_PASSWORD`. Use a sender verified by your chosen email
provider. `SMTP_SECURITY=starttls` commonly uses port 587; `ssl` commonly uses
465. The provider determines the correct settings. TLS certificate validation is
enabled; plaintext SMTP is not supported. Username and password must both be
present or both omitted for an authenticated network relay.

Set `EMAIL_VERIFICATION_BASE_URL` to the actual Tamakkan HTTPS origin before
hosting. Verification links use only that configured origin, never an incoming
Host header. Local HTTP is accepted only for loopback hosts in development.
File preview mode is rejected outside development, and enabling verification
with the old logging-only sender is rejected.

SMTP delivery uses the shared email sender, so configuring it also enables real
delivery for existing email notifications in this deployment. Verify provider
delivery using a mailbox you control before inviting students. SMTP delivery,
domain/sender verification and inbox/spam behavior have not been tested against
a real provider yet. No SMTP credentials were copied from the production deployment.

## Existing accounts and deployment defaults

- The feature defaults off for the existing production environment; Tamakkan's
  template and local launcher enable it for newly created accounts.
- Existing accounts have `email_verification_required=false` and
  `email_verified_at=null`. They are shown as **Verification not required**,
  never falsely labelled verified. Seeded Root accounts use this bootstrap
  exemption. Existing accounts are not bulk-enrolled or emailed.
- Once an account is marked as requiring verification, disabling the creation
  flag does not bypass its login/API restriction. Complete its verification or
  restore delivery configuration.
- A password reset does not mark an email verified. Application requests cannot
  set verification fields through profile or account-creation payloads.

## Security and failure handling

- Links use 256-bit random opaque tokens; only SHA-256 hashes are stored in the
  database. Verification consumes the token and marks the email verified in one
  conditional MongoDB update. Links expire after 24 hours by default.
- Tokens bind to one account and its email address. Disabled/deleted accounts,
  changed email addresses and expired links cannot be confirmed.
- Resending replaces the previous link. Atomic per-account limits allow one
  request per minute and five per hour across API workers. Limits also apply
  when delivery fails; allow a minute before retrying.
- The public resend endpoint always returns the same HTTP 202 body and performs
  account lookup/delivery after the response. Unknown, exempt, inactive and
  verified accounts do not receive an email.
- SMTP failure leaves the created account pending and records `failed` delivery
  for its teacher to see. It invalidates that undelivered token; a later resend
  can recover the account. It does not claim verification succeeded or erase
  the account.
- Tokens travel in the URL fragment, which the browser removes from its address
  and current history entry after reading. Confirmation sends the token in a
  POST body. The page uses `no-referrer` and disables indexing; auth responses
  use `Cache-Control: no-store`. No token/body is logged by the email sender.
- Background resend jobs run in the API process, not a durable queue. If the
  process stops during delivery, the user can request another email after the
  cooldown. Larger deployments should add durable delivery jobs and gateway
  rate limits as needed.

Token and recovery controls follow the applicable principles in the
[OWASP recovery guidance](https://cheatsheetseries.owasp.org/cheatsheets/Forgot_Password_Cheat_Sheet.html).

## Validation

123 relevant backend tests passed. Targeted frontend ESLint, the Tamakkan
production build, and the local browser checks passed. The standalone TypeScript
check still reports the pre-existing unresolved `cloudflare:workers` import in
the frontend's `db/index.ts`; the build retains its existing large-chunk warning.

Automated tests cover creation by both privileged roles, login/API/refresh
blocking, valid/expired/replaced/reused links, concurrent confirmation/resend,
resend cooldown and hourly reset, inactive/deleted/changed-email accounts,
delivery failure and recovery, legacy compatibility, stale profile writes,
SMTP TLS wiring and local email rendering.

Local browser QA uses a temporary test student and actual API/file delivery,
then removes only its own account, refresh records and preview email. It checks
login blocking, successful verification and sign-in, teacher status changes,
reused-link recovery, responsive English/Arabic views and a simulated network
failure. Provider inbox delivery remains the final external check.

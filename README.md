# Management System — V51

Flask + SQLAlchemy application intended for Vercel with a PostgreSQL/Neon database. This release is based on the current V50 code and adds the admin Email Campaign Center while keeping existing account, registration OTP, password-reset OTP, analytics, audit-log and admin-user flows.

## Highlights in V51

- Admin-only **Email Campaign Center** with all-users, active-users, selected-users, and single-user audience modes.
- Searchable recipient picker, seven branded message templates, safe live email preview, CTA link to the real sign-in page, test email, and campaign-level recipient reports.
- Sent/failed/pending tracking, failed-recipient retry, duplicate-submit guard, a configurable recipient cap and broadcast cooldown, future scheduling, edit/cancel for not-yet-sent scheduled campaigns, and protected cron endpoint.
- Refined registration OTP / forgot-password OTP screens with expiry guidance, resend countdown and safer form states.
- Additive campaign database tables; no `DROP TABLE` or intentional deletion of existing database rows.

## Deploy

1. Extract the ZIP with `app.py`, `requirements.txt`, `vercel.json`, `templates/`, and `static/` at the repository root.
2. Connect the repository to Vercel and retain the correct project root.
3. Configure `SECRET_KEY`, `DATABASE_URL` for the existing Neon database, `ADMIN_USERNAME`, `ADMIN_PASSWORD`, `ADMIN_EMAIL`, `BREVO_API_KEY`, and `BREVO_SENDER_EMAIL`. Alternatively configure the documented SMTP settings.
4. For campaign CTA URLs set `PUBLIC_APP_URL` to the actual production domain. Set a random `CRON_SECRET` for scheduled campaign processing.
5. Redeploy, check `/healthz`, sign in as admin, then open **Dashboard → Email Campaigns**.

## Scheduled campaigns

The app stores future delivery times as UTC while the UI accepts IST. Automatic delivery needs a scheduler to call `GET /api/cron/email-campaigns` with `Authorization: Bearer <CRON_SECRET>`. See `README_DEPLOY.md` for details and the Vercel Hobby frequency limitation. Until the scheduler is configured, future campaigns stay safely saved with status `scheduled`.

## Email configuration

Use either Brevo (`BREVO_API_KEY`, `BREVO_SENDER_EMAIL`, optional `BREVO_SENDER_NAME`) or SMTP (`SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD`, `SMTP_FROM`). For Gmail SMTP, use an app password rather than the normal account password. Never commit real secrets to the repository or paste them into chat.

## Verification notes

`CODE_TEST_REPORT_V51.md` records checks run on the source in this workspace. Syntax, template parsing, and JavaScript syntax checks do not prove real email delivery or a successful live Neon/Vercel deployment. Use a test campaign after you configure valid provider credentials.

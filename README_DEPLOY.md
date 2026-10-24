# Management System V51 — Email Campaign Center + Auth OTP Polish

This package is a repaired continuation of the uploaded Management System V20 project. It keeps the existing Flask + SQLAlchemy architecture and templates, updates the landing/auth experience, and adds guarded database-startup behavior. It is **not** the separate Library Management System project.

## Stack and project layout

- Python 3.12, Flask, Flask-SQLAlchemy / SQLAlchemy
- PostgreSQL (Neon) for persistent production data
- HTML templates, shared CSS/JavaScript in `public/` and `static/`
- Brevo transactional email for registration and password-reset OTP
- Root-level `app.py`, `requirements.txt`, `vercel.json`, `templates/`, `public/`

## Deploy to Vercel

1. Download and extract the ZIP. Make sure `app.py`, `requirements.txt`, `vercel.json`, `templates/`, and `public/` are at the repository root (not inside an extra nested folder).
2. Push the extracted project to the GitHub repository connected to Vercel, or import that repository into Vercel. Keep Vercel's **Root Directory** set to the folder containing `app.py`.
3. In Vercel → Project → Settings → Environment Variables, add the variables below. Set them for Production and for Preview if you use preview deployments.
4. Redeploy Production after saving the variables.
5. Visit `/healthz`. The expected healthy response is `{"status":"ok","database":"ok"}`. If it returns an error/503, check Vercel → Deployments → the deployment → Functions / Runtime Logs; the endpoint avoids exposing secrets/raw database errors.

### Required environment variables

- `SECRET_KEY`: a strong random secret. Generate one locally with `python -c "import secrets; print(secrets.token_hex(32))"`.
- `DATABASE_URL`: connection string for the **existing Neon database containing your current users/data**. Use Neon’s pooled connection string if appropriate for your database/workload; ensure SSL is enabled.
- `ADMIN_USERNAME`: environment-managed admin username.
- `ADMIN_PASSWORD`: strong password (at least 8 characters with uppercase, lowercase, number, and special character).
- `ADMIN_EMAIL`: the admin email address; keep it distinct from any other account email.
- `PGSSLMODE=require`
- `FLASK_DEBUG=0`
- `BREVO_API_KEY`: private Brevo API key.
- `BREVO_SENDER_EMAIL`: sender email verified with Brevo.
- `BREVO_SENDER_NAME`: e.g. `Management System`.

Never paste production secrets into chat or commit them to GitHub. `.env.example` contains placeholders only. Do not create a new empty Neon project if you must preserve existing data. Check `DATABASE_URL` carefully before deployment; an incorrect database URL can make the app look like users disappeared even though the old database remains intact.

## First-deploy and schema safety

- On Vercel, an absent `DATABASE_URL` is treated as a configuration error instead of silently selecting ephemeral SQLite.
- Startup migrations are additive and do not issue `DROP TABLE` or bulk-delete OTP/reset history.
- Existing employee IDs are retained. Legacy rows with missing employee IDs are assigned collision-checked IDs only when needed.
- Case-insensitive unique indexes are checked/created for identity columns. If the old database already contains duplicates that would violate these constraints (e.g. `Aman` and `aman`), startup fails with an explanatory error rather than deleting or silently merging records. Back up and manually reconcile duplicates before retrying.
- An incomplete legacy OTP/reset schema fails safely rather than discarding rows. Back up Neon before applying the build to a production database.
- The environment-managed admin password must be changed via `ADMIN_PASSWORD` in Vercel and followed by a redeploy; the application does not override that policy with an admin self-service password reset.

## Function/feature smoke test after deployment

- [ ] `/healthz` reports database ready.
- [ ] Landing page CSS and JS load without 404 errors; test the 12 contextualized animation patterns (see `CODE_TEST_REPORT.md`).
- [ ] Admin login succeeds with the configured environment-managed credentials.
- [ ] New registration → OTP email → OTP verification → account creation works.
- [ ] Forgot password → OTP email → verification → set password → sign in works.
- [ ] Profile view, permitted profile updates, change-password flow, and POST logout work.
- [ ] Admin member management (create/edit/activate/deactivate/unlock/delete non-admin users) works.
- [ ] Analytics, audit logs, log-management actions, and CSV export work.
- [ ] Check the underlying Neon tables after a safe test action to confirm expected rows are persisted.

## Local run

1. `python -m venv .venv`, activate the virtual environment.
2. `pip install -r requirements.txt`.
3. Copy `.env.example` to `.env`; replace **all** placeholders with test/local values.
4. Run `python app.py` and open `http://127.0.0.1:5000`.

## Verification boundary

`CODE_TEST_REPORT.md` lists the automated static/smoke checks actually run. In this build environment Flask dependencies could not be installed because package-index DNS/network access was unavailable. Therefore the packaged source passed static checks plus a separate SQLite migration smoke test, but this environment did not verify a live Flask server, the real Neon database, Brevo email delivery, or your live Vercel deployment. Do not interpret the static test pass as live production sign-off.

## Admin Email Campaign Center (added in V51)

- Open **Dashboard → Admin → Email Campaigns**. Admin authorization is enforced on the server; normal users cannot open or submit campaign actions.
- Audience choices: all valid-email accounts, active accounts, selected users (search by name/username/email), or one selected account. Recipient email addresses are de-duplicated and stored with the campaign, and each recipient has a `pending`, `sent`, or `failed` outcome.
- Branded HTML templates: announcement, welcome, come-back, system update, reminder, maintenance, and custom. The browser preview escapes message text; email bodies are also HTML-escaped server-side. A CTA button can link to the real sign-in URL.
- Send a test email to one address before sending broadly. Test sends are recorded in campaign history. Provider acceptance is recorded as `sent`; it does not guarantee inbox placement.
- Large campaigns are processed in batches. Open the campaign report and choose **Send next batch** if needed; failed recipients can be retried without resending to successful recipients. Duplicate form posts are guarded by a unique submission key and a per-campaign unique recipient constraint. Each recipient gets an individual send (no exposed recipient list/BCC). A default 5,000-recipient cap and 60-second cooldown between immediate all/active-user broadcasts reduce accidental bulk sends; tune `EMAIL_CAMPAIGN_MAX_RECIPIENTS` and `EMAIL_CAMPAIGN_BROADCAST_COOLDOWN_SECONDS` when needed.
- Scheduled campaigns are stored in the database in UTC (the composer takes India Standard Time). For automatic delivery, configure a scheduler to call `GET /api/cron/email-campaigns` with header `Authorization: Bearer <CRON_SECRET>` at the cadence supported by your host. The endpoint fails closed when `CRON_SECRET` is missing/wrong. On Vercel plans supporting minutely cron, add this under `crons` in `vercel.json`: `{"path":"/api/cron/email-campaigns","schedule":"* * * * *"}`. Vercel Hobby has a once-per-day cron frequency restriction; for near-minute delivery, use a plan that supports minutely jobs or an external scheduler that calls this secure endpoint. Until a scheduler is configured, scheduled campaigns remain safely queued in the database.
- Add `CRON_SECRET`, `PUBLIC_APP_URL` (your actual production domain), `EMAIL_CAMPAIGN_MAX_RECIPIENTS`, `EMAIL_CAMPAIGN_BROADCAST_COOLDOWN_SECONDS`, and optionally `EMAIL_CAMPAIGN_BATCH_SIZE` in Vercel Environment Variables, in addition to the existing Brevo/SMTP sender settings. Keep secrets private.
- New tables are `email_campaign` and `email_campaign_recipient`; the code uses additive `create_all` behavior and does not drop existing user, OTP, reset, book, payment, or audit data. Back up the existing Neon database before deploying any application update.

## OTP screens

Registration OTP and forgot-password OTP now use clearer verification steps, mobile-friendly 6-digit inputs, expiry guidance, resend countdowns, protected resend forms, and confirmation/loading states. The password-recovery “Start over” link explicitly clears only the reset session state; it does not delete user or OTP history.

# Management System V21 — Vercel + Neon Deployment

## Included stack
- Python 3.12 + Flask
- SQLAlchemy / Flask-SQLAlchemy
- Neon PostgreSQL
- HTML, CSS, JavaScript
- Brevo transactional email for registration and password-reset OTP

## 1. Push to GitHub
Extract this ZIP and push its contents to the root of your repository. These paths must be at the root: `app.py`, `requirements.txt`, `vercel.json`, `templates/`, and `public/`.

## 2. Import to Vercel
Import the repository in Vercel. Use the repository root as the Root Directory. Flask is detected from the root-level `app.py`; the minimal `vercel.json` only configures function duration. Vercel serves `public/` assets from its static layer.

## 3. Set Production Environment Variables
Set these in Vercel → Project → Settings → Environment Variables. Apply to Production (and Preview if you test Preview deployments):

- `SECRET_KEY`: a strong random secret; generate locally with `python -c "import secrets; print(secrets.token_hex(32))"`.
- `DATABASE_URL`: the Neon PostgreSQL connection string for the **existing database containing your users**. Use Neon’s pooled connection string when available.
- `ADMIN_USERNAME`: desired environment-managed admin username.
- `ADMIN_PASSWORD`: strong password (8+ characters, uppercase, lowercase, number, special character).
- `ADMIN_EMAIL`: admin email address.
- `PGSSLMODE`: `require`.
- `FLASK_DEBUG`: `0`.
- `BREVO_API_KEY`: private Brevo API key.
- `BREVO_SENDER_EMAIL`: sender verified in Brevo.
- `BREVO_SENDER_NAME`: e.g. `Management System`.

Do not paste secrets into chat, commit them, or show them in screenshots. `DATABASE_URL` must point to the correct Neon project. Do not create a new empty Neon project if you need to preserve existing user data.

## 4. Redeploy and verify
After setting environment variables, redeploy Production. Visit `/healthz`; success returns `{"status":"ok","database":"ok"}`. A 503 indicates a database/readiness failure; use Vercel Function Logs for the detailed server-side error. The endpoint intentionally does not expose credentials or raw database errors.

## 5. Feature smoke-test checklist
1. Landing page and static CSS/JS assets load.
2. Admin login using the exact `ADMIN_USERNAME` / `ADMIN_PASSWORD` values.
3. Registration → 6-digit OTP email → OTP verification → account creation.
4. Forgot password → OTP email → verify → set new password → login.
5. Profile view/edit, password change, and logout.
6. Admin dashboard: create user with OTP, edit, activate/deactivate, unlock, delete non-admin user.
7. Analytics, audit logs, clear logs, and CSV export.

## Data safety and migration behavior
- On Vercel, missing `DATABASE_URL` is treated as a configuration error rather than silently falling back to ephemeral SQLite.
- Startup migration adds compatible missing columns without dropping existing user records. If essential identity columns are missing or unique data conflicts, startup fails with a clear log rather than fabricating credentials or deleting users.
- Automatic migration never deletes pending OTP/reset rows or rewrites existing employee IDs. If an old OTP/reset table contains incomplete rows, startup stops safely and logs a reconciliation error instead of discarding data.
- Admin credentials are synchronized from environment variables at database initialization. Changing `ADMIN_PASSWORD` requires a redeploy.

## Local development
1. `python -m venv .venv`
2. Activate the environment.
3. `pip install -r requirements.txt`
4. Copy `.env.example` to `.env` and replace every placeholder.
5. `python app.py` then open `http://127.0.0.1:5000`.

## Validation limitation
Static checks are included in `CODE_TEST_REPORT.md`. The project’s private Neon database, Brevo sender configuration, and Vercel production deployment cannot be live-tested from the build environment; run the smoke-test checklist after deployment.

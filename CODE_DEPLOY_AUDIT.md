# Code and deployment audit — V41

## Checks completed
- Python source compiled successfully with the available Python interpreter.
- All 23 HTML templates parsed successfully as Jinja templates.
- Every template named in `render_template(...)` exists.
- Active route/template references were reviewed.
- `vercel.json`, `.vercelignore`, `requirements.txt`, and root-level `app.py` were checked.
- ZIP integrity was tested after packaging.

## Important limits
- The environment used for this audit does not have Flask / Flask-SQLAlchemy / psycopg installed, and outbound package downloads are unavailable. A live Flask test-client run could therefore not be completed.
- No access to the user's private Vercel or Neon dashboards is available here. Environment-variable values, live Neon availability, Brevo delivery, and the current production deployment cannot be authenticated from source code alone.
- The old `settings.html`, `books.html`, `cart.html`, `demo_payment.html`, and `my_orders.html` files are legacy/orphan templates: this source does not register all their referenced endpoints/models, and no current route renders these templates. They should not be treated as working purchase/order/settings flows. They were retained rather than deleted.

## Changes in V41
- Bumped schema marker to version 34 so a V33-marked database performs one further idempotent migration pass.
- Made legacy role migration explicitly skip a fresh database with no `user` table and re-raise real migration errors.
- Added `GET /login` redirect alias; the sign-in form still posts to `POST /login`.
- Reduced SQLAlchemy per-instance pool from 5+5 connections to 1 connection for serverless deployment.
- `/healthz` now checks database configuration, runs database initialization, then checks connectivity; it will not mistake Vercel's SQLite fallback for a persistent Neon database.
- `/init-status` no longer lets every anonymous request force a migration retry; detailed errors still require admin authentication or a `CRON_SECRET` token.
- Database init failure blocks write operations with an actionable 503 response rather than letting downstream requests produce confusing partial failures.
- Raw SQL driver error details are hidden by default; use Vercel Function Logs for private diagnostic information.
- Logged forgot-password OTP and resend exceptions for diagnosis.
- Added `.gitignore` for credentials and local DB files. Updated Vercel deployment documentation.

## Deployment checks the owner must still perform
1. Put the extracted contents at the repository root so `app.py`, `vercel.json`, and `requirements.txt` sit at the same level.
2. Confirm Vercel's connected Git repository/branch and production alias are the intended project. The visible registration error message in earlier screenshots matched the V38/V39 source, not V40, so the live alias may still point at an older deployment.
3. Set Vercel Production variables: `DATABASE_URL` (correct Neon PostgreSQL project), `SECRET_KEY`, `ADMIN_USERNAME`, `ADMIN_PASSWORD`, and email variables `BREVO_API_KEY`, `BREVO_SENDER_EMAIL`, `BREVO_SENDER_NAME`; add `ADMIN_EMAIL` if admin notifications are wanted. Never paste secrets into chat or commit them.
4. Redeploy. Visit `/healthz`: expect HTTP 200 with `database_connection: ok` and `initialization_ready: true`. If not, inspect Function Logs and `/init-status` (use an authenticated admin session or `?token=<CRON_SECRET>` for details; do not share the token).
5. Test Create Account OTP, Forgot Password OTP, existing member login, admin login, book issue/return, payment receipt and refund in the deployed site. This ZIP has not been live-tested against your private Neon/Brevo services.

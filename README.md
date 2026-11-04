# Library Management System — V60

Flask + SQLAlchemy application with the existing V51 account-management, registration OTP, password-reset OTP, analytics, audit log and admin email-campaign workflows preserved. V60 adds the requested library circulation, fines, manual UPI and backup modules through additive tables and non-destructive handling of historical books/members.

## V60 modules

- **Roles:** Admin, Librarian and User. Admin manages roles, system settings, payment verification, reports and backups. Librarian manages books, issue/return inspections and book requests; additional-fine, adjustment and waiver permission is off by default and can be granted by Admin in Library Settings. Users browse the catalog, self-issue available books, see their loans/requests/fines, submit sold-out requests, payments and fine disputes.
- **Circulation:** direct user self-issue, maximum 5 active books, 14-day due date, duplicate/stock checks, member lookup for staff, issue/return history.
- **Book identity:** generated `BK-000001` ID after database insert, on-demand QR and Code 128 barcode PNGs, mobile browser scanner where `BarcodeDetector` is supported. QR encodes a same-site book detail page; it does not contain member data.
- **Sold-out requests:** members can place one pending request per book; staff approve (which issues a book when stock is available) or reject; users can cancel pending requests.
- **Fines and inspections:** configurable default ₹10/day late fine and 0-day grace period, additional fines, return condition (good/damaged/lost), damage type/remarks/optional image, fine adjustment/waiver, damage history and disputes. The daily reminder cron accrues a cumulative late fine while a loan is overdue; return inspection recalculates the final amount. If cron is not configured, the fine is calculated at return.
- **Manual UPI:** Admin configures UPI ID/name. Members submit a transaction/reference ID and optional payment screenshot. Admin approval is required before the fine balance changes. Partial payments are supported. Approved payments have PDF receipts; rejected submissions do not reduce the balance. This is manual verification, not a live payment-gateway integration.
- **Notifications:** SMTP email for issue, return, due-soon, overdue, request, availability, fine, dispute and payment status. Gmail SMTP requires a Gmail App Password, not the normal account password. Existing OTP/campaign providers remain available.
- **Reports and audit:** stock, available, issued, overdue, pending requests/payments, outstanding fines and monthly/total verified collections. Sensitive actions write to the existing audit log.
- **Backups:** admin can download a logical JSON backup and perform a guarded merge-only restore. Scheduled off-site backups upload to a private S3-compatible bucket with server-side encryption. Restore inserts non-conflicting rows; it never overwrites or deletes existing records.

## Local setup

1. Extract the archive to a folder. Open that folder in VS Code.
2. Create a virtual environment: `python -m venv .venv`
3. Activate it (PowerShell): `.venv\Scripts\Activate.ps1`
4. Install: `pip install -r requirements.txt`
5. Copy `.env.example` to `.env` and set `SECRET_KEY`, `DATABASE_URL`, `ADMIN_USERNAME`, `ADMIN_PASSWORD`, and `ADMIN_EMAIL`. For local-only testing, use `DATABASE_URL=sqlite:///management.db`.
6. Configure Gmail SMTP if you want book/fine emails: `SMTP_HOST=smtp.gmail.com`, `SMTP_PORT=587`, `SMTP_USER`, `SMTP_PASSWORD` (16-character App Password), and `SMTP_FROM`.
7. For Manual UPI, configure the actual VPA in Admin → Library Center → Library Settings. Do not leave a placeholder UPI ID.
8. Run: `python app.py`, then open `http://127.0.0.1:5000`.

## Verification status

The source package is statically checked for Python syntax, Jinja template parsing, JavaScript syntax, JSON configuration and template endpoint references. Runtime integration against a real Neon database, Gmail SMTP, Vercel Cron and S3 cannot be proven without the deployment environment and credentials. Test first against a staging copy or a fresh database backup.

## Vercel / Neon deployment

- Keep the existing Neon `DATABASE_URL` and current account/admin environment variables. V60 does not drop or reset existing tables. New library tables are created additively during the existing database initialization path.
- Add the new requirements and deploy the whole folder (including `library_v60.py`, all new templates, and `vercel.json`).
- Configure the `SMTP_*` variables for Gmail notifications. Use a Gmail App Password.
- Set `LIBRARY_UPI_ID` (or save it in the Admin Library Settings page).
- Scheduled reminders and backup jobs require `CRON_SECRET` with at least 24 characters. Vercel Cron sends `Authorization: Bearer <CRON_SECRET>`. The included schedules call reminders daily at 03:00 UTC and backup daily at 02:00 UTC.
- Durable automatic backups require a private S3-compatible bucket and `BACKUP_S3_BUCKET`, `AWS_REGION`, `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`; for an S3-compatible provider, optionally set `BACKUP_S3_ENDPOINT`. The IAM identity should be restricted to that bucket/prefix. Without these settings, the scheduled backup endpoint returns `not_configured`; it does not silently save backups to Vercel's ephemeral filesystem.
- Check `/__version` for `V60` and `/healthz` after deployment. Test with a staging/backup database before replacing the live site.

## Important behavior and safeguards

- Existing member accounts, loans and catalog rows are not intentionally deleted. User deletion and book deletion now archive/deactivate records so loan/payment/audit history can remain linked.
- Fine default is ₹10 per overdue day with no grace period; Admin may configure both. Confirm the policy with your organization before collecting money.
- Manual UPI screenshots and payment references are stored in the database. Restrict staff access and back up the database securely.
- The app does not verify UPI payments with a bank API. Only an Admin can approve a submitted transaction reference.
- The JSON backup is a logical application-data snapshot, not a full PostgreSQL cluster/role/schema backup. Store downloads privately. The merge restore skips conflicts and does not overwrite current records.
- Camera QR/barcode scanning requires HTTPS and a browser implementing `BarcodeDetector`; otherwise use the phone camera app or open the generated QR/barcode image.
- Do not commit `.env`, real SMTP credentials, database URLs or AWS secrets.

Project credit: **Developed by Aman Kumar.**

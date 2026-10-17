# Library Management System

Flask + SQLAlchemy + PostgreSQL/SQLite library project.

## Vercel
`vercel.json` is included (Python/Flask on Vercel + a daily reminder cron). Import the repo in Vercel and set the environment variables below.

Required: `DATABASE_URL` (Neon *pooled* URL), `SECRET_KEY`, `ADMIN_USERNAME`, `ADMIN_PASSWORD`; optional `ADMIN_NAME`, `PGSSLMODE`. Plus your Neon object-storage variables for book PDFs/covers.

## Email notifications
Emails are sent for: registration OTP, welcome, book issue, return, additional fine, payment receipt, refund, password change, role change, member removal, due-soon and overdue reminders. `ADMIN_EMAIL` also receives a copy of issue/return/fine/payment/refund/role/removal emails.
Every email is saved in `email_log`; staff can open **Email Notifications** on the dashboard (`/admin/email-logs`) to see status, send a test email or run reminders.
Emails are sent before the response (required on Vercel serverless); a failed email never blocks the library action.

Variables (one provider): Brevo `BREVO_API_KEY` + `BREVO_SENDER_EMAIL` (+ `BREVO_SENDER_NAME`), or SMTP `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD` (+ `SMTP_FROM`). Optional: `ADMIN_EMAIL`, `LATE_FEE_PER_DAY`, `DUE_SOON_DAYS` (default 2), `CRON_SECRET`.
Reminders: `vercel.json` runs `/cron/send-reminders` daily (09:00 IST). Set `CRON_SECRET` in Vercel — Vercel sends it automatically as a Bearer token.
Vercel limits: request bodies max ~4.5 MB (big book-PDF uploads fail), function timeout, large PyMuPDF bundle.

## Roles
- Member: browse books, issue/return own books, change password. No community/member list.
- Librarian: manage books and issue/return for members.
- Admin: all librarian functions + member management and promote/demote librarian.

Registration intentionally has no Department field; new accounts are created as Library Members.


Database migration note: the app preserves existing PostgreSQL book data and adds missing ORM columns (including description and cover_url) when needed.

## V12 database fixes
- `user.id` is migrated to database column `user_id`; the legacy `employee_id` column is removed.
- Payment/return schema is upgraded in-place and payment foreign keys are checked.
- `audit_log` is created/upgraded and registration, login, issue, return/payment and additional-fine events are recorded.
- Existing member numeric IDs are preserved during the `id` -> `user_id` rename.

## Final Add Book UI
- Premium responsive Add New Book screen.
- PDF upload with Analyze PDF & Auto Fill.
- Gallery/Album cover upload with live preview.
- If no cover is supplied, the first PDF page is used as a cover when possible.
- When both PDF and cover are supplied, the uploaded cover photo is stored and preferred.
- Book PDF and cover are stored in Neon Object Storage; metadata and file paths are stored in PostgreSQL.

## Email notifications (Brevo)
Set these environment variables for automatic Gmail/email notifications:
- `BREVO_API_KEY`
- `BREVO_SENDER_EMAIL`
- `BREVO_SENDER_NAME` (example: `Library Management System`)
- `ADMIN_EMAIL` (optional: receives new-member notifications)

The system sends separate emails for:
- Congratulations / account created
- New book issued (with due date)
- Book returned / fine summary
- Payment slip / receipt (Payment ID + Transaction ID + amount)
- Refund processed (Refund ID + original transaction)
- Password changed security alert

Payment in this internship project is a simulated internal payment record; it does not charge real money.

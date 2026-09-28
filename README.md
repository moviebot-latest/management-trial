# Library Management System

Flask + SQLAlchemy + PostgreSQL/SQLite library project.

## Vercel
Build command: `pip install -r requirements.txt`
Start command: `gunicorn app:app`

Required environment variables:
- `DATABASE_URL` = PostgreSQL connection string
- `SECRET_KEY` = long random secret
- `ADMIN_USERNAME` = admin login username
- `ADMIN_PASSWORD` = admin login password
- `ADMIN_NAME` = optional admin display name
- `PGSSLMODE` = optional, defaults to `require`

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

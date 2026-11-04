# Library Management System V60 — Deployment Guide

This guide supersedes the historical V51/V52 deployment notes. V60 retains the existing Flask account, registration OTP, password-reset OTP, admin member management, analytics, audit-log and email-campaign flows, and adds library circulation, requests, QR/barcodes, fines, inspections, disputes, manual UPI and backup modules.

## Deploy safely

1. Keep a current backup of the existing Neon database before deployment. Test first with a staging copy.
2. Extract this ZIP so `app.py`, `library_v60.py`, `requirements.txt`, `vercel.json`, `templates/` and `static/` are at the repository root.
3. Commit the full project to the GitHub repository connected to Vercel and redeploy. Do not upload only `app.py`; the new module and templates are required.
4. Preserve the current production `DATABASE_URL`, `SECRET_KEY`, admin settings and existing OTP/email provider variables. Never paste secrets into chat or commit `.env`.
5. Add the new SMTP, UPI and (for durable scheduled backups) S3 settings listed in `.env.example`.
6. Verify `/__version` reports `V60`, then `/healthz` reports a healthy database. Complete the role-by-role smoke test in `README.md` against staging before promoting to production.

## Important configuration

- Gmail: use `SMTP_HOST=smtp.gmail.com`, `SMTP_PORT=587`, `SMTP_USER`, `SMTP_PASSWORD` (Google App Password), and `SMTP_FROM`. Use the App Password, not your usual Gmail password.
- Manual UPI: set a real `LIBRARY_UPI_ID` or configure it in Admin → Library Settings. Payment is only recorded as paid after Admin verification; this is not bank-API verification.
- Cron: set a high-entropy `CRON_SECRET` (at least 24 characters). Vercel cron jobs call the secured reminders and backup endpoints.
- Durable backups: configure a private S3-compatible bucket plus `BACKUP_S3_BUCKET`, `AWS_REGION`, `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`; optionally `BACKUP_S3_ENDPOINT`. Vercel local disk is not a durable backup destination.
- Fine defaults: ₹10/day, zero grace days. Admin can change the settings. Confirm the policy before collecting fines.

## Data safety

V60 uses additive library tables and non-destructive book/member archival. The JSON restore is merge-only: existing rows are not overwritten or deleted. It is not a full PostgreSQL cluster/role/schema backup. Keep a separate Neon/provider backup as well. Review and test database migrations on a copy before production rollout.

## Verification boundary

Static source checks do not prove production behavior. Runtime app/database, Gmail delivery, Vercel cron and S3 backup tests require your actual deployment environment and credentials. Test with a staging database before replacing the live deployment.

Project credit: **Developed by Aman Kumar.**

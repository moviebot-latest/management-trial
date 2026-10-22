# Vercel Preview checklist

- [ ] Project root points to this repository root, not `frontend/`.
- [ ] Framework preset is Vite.
- [ ] `DATABASE_URL` points to a staging Neon branch and includes SSL.
- [ ] `SESSION_SECRET` is at least 32 characters and only server-side.
- [ ] `APP_ENV=production` is configured.
- [ ] Email provider variables are configured if registration/reset OTP must send email.
- [ ] `LATE_FEE_PER_DAY` is set to the agreed amount.
- [ ] Build log confirms Vite build and Go function compilation.
- [ ] `/api/health` returns database-connected status.
- [ ] Login, OTP, CSRF-protected POST, book add/edit/delete, issue, fine, return and payment ledger are tested.
- [ ] A return with a fine writes a demo record only; no real payment gateway is integrated.
- [ ] Audit log contains library actions.
- [ ] Existing production records remain intact.
- [ ] Known PDF/file-upload/evidence-photo gaps are resolved before declaring full feature parity.

# V58 migration status — library module

## Implemented in this workspace
- Go API handler refactor to support local server and a Vercel Go function entrypoint.
- React/TypeScript responsive Library Operations screen.
- Book catalog list/search/add/edit/delete with role checks; deletion blocked if any loan history exists.
- Issue transaction with row lock, available-copy check and duplicate-active-loan guard.
- Return transaction with late fine calculation, staff-added fines, payment record creation and stock restoration only on transaction success.
- Demo-only payment ledger and library audit events.
- Additive schema bootstrap for book, loan, return_record and payment tables if absent.
- Vercel build/output/function/rewrite configuration and environment example.
- Original Flask source ZIP and prior V54 rollback ZIP retained.

## Still not ported / not verified
- PDF file upload, automatic PDF metadata extraction, bulk catalog PDF import and extracted cover generation.
- Cover image file upload, Neon Object Storage signing/URL serving and return evidence-photo upload.
- Full legacy settings, campaign scheduler/cron/test-send, complete member editing/export and all legacy templates/workflows.
- No full frontend production build or Go linked test completed in this restricted environment (external dependency downloads unavailable).
- No staging Neon, SMTP/Brevo or Vercel Preview integration tests performed.

## Deployment gate
Use Vercel Preview + a separate Neon staging branch. Do not replace the existing production deployment until the build succeeds and route-by-route regression testing is complete. No claim of complete parity or production certification is made by this report.

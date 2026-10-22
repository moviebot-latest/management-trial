# Library Management System — V59 Home + Authentication Flow (Go + React + TypeScript)

This is the V58 redesign/migration workspace for the uploaded Library Management System. The original V54 rollback ZIP and the uploaded Flask source ZIP are kept inside this package as references. Existing production data must not be reset or dropped.

## V59 Home and authentication flow
- Public landing page is the first screen for signed-out visitors.
- Home provides clear Login and Create Account actions.
- New account flow uses email OTP verification, then returns to Login.
- Successful login opens the dashboard. Existing authenticated sessions open the dashboard directly.
- Logout returns to the public Home page.

## Stack
- Frontend: React 18 + TypeScript + Vite
- Backend: Go 1.23 `net/http` API
- Database: PostgreSQL / Neon via pgx
- Authentication: signed HTTP-only session cookie, CSRF double-submit token, password policy, lockout and role-based API checks
- Deployment target: Vercel static Vite build + Go API function

## Library features added in V58
- Redesigned Library Operations area integrated into the premium ManageOS dashboard
- Book catalog search/list, add, edit, delete (deletion is blocked when loan history exists), book code, ISBN, category, year, copies, description, cover URL and PDF URL/storage-key fields
- Issue flow with available-stock locking, due date, active-loan duplicate guard and staff/member access rules
- Return flow with late-fine calculation, librarian/admin fine records, stock restoration in a database transaction, and payment record history
- Demo-only payment records (UPI/card/cash method metadata); this is not a live payment gateway and does not charge money
- Library actions are written to the existing audit log; existing security/activity page remains available
- Mobile-responsive library dashboard, circulation and payment ledger UI
- Additive `CREATE TABLE IF NOT EXISTS` for missing library tables only; no `DROP`, `TRUNCATE`, reset or data wipe statements

## Important parity note
The uploaded Flask version includes extra file-based workflows (PDF upload/automatic PDF metadata extraction, bulk catalog PDF import, cover-file upload to Neon Object Storage, and return-evidence photo uploads). The V58 Go migration currently retains cover/PDF URL or storage-key references, but does **not yet port the original file upload, PDF parsing/bulk-import or evidence-photo upload workflows**. The original Flask source ZIP is included unchanged for reference/rollback. Do not claim complete feature parity until those workflows are ported and tested. Email campaign scheduling, admin settings and other legacy management workflows also still have migration gaps listed in `MIGRATION_STATUS.md`.

## Vercel setup
`vercel.json` builds the Vite app and forwards `/api/*` requests to a single Go function while restoring the original API path before dispatch. Vercel's Go runtime is currently documented as Beta; check the official Go runtime docs before production deployment: https://vercel.com/docs/functions/runtimes/go

1. Import this folder as the Vercel project root (not `frontend/`).
2. Set the Framework Preset to **Vite** and keep the repo's `vercel.json` commands.
3. Add these server-side environment variables in Vercel for Preview and Production:
   - `DATABASE_URL` — Neon PostgreSQL connection string, with SSL enabled.
   - `SESSION_SECRET` — at least 32 random characters; never use a public `VITE_` variable.
   - `APP_ENV=production`
   - Optional email credentials: `BREVO_API_KEY`, `BREVO_SENDER_EMAIL`, or `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD`, `SMTP_FROM`.
   - Optional fine rate: `LATE_FEE_PER_DAY=10`.
4. Use a **staging Neon branch/database first**. Take a backup before testing against the existing production database.
5. Deploy Preview, verify `/api/health`, then test login, registration OTP, book CRUD, issue, return, fine and payment-record history using real test accounts.
6. Confirm email delivery, database schema compatibility and all required legacy routes before switching production traffic.

## Local development
- Frontend: `cd frontend && npm install && npm run dev` (Vite proxies `/api` to `http://localhost:8080`).
- Backend: from the project root, run `go run ./cmd/server` with the required environment variables set.
- Build frontend: `cd frontend && npm run build`.
- Go dependencies are defined in root `go.mod` and `backend/go.mod`; run `go mod download` where network access is available.

## Known validation limits
- TSX transpilation/syntax check and CSS parsing pass in this environment.
- Full `npm run build` / TypeScript type-check could not be completed because React/Vite dependencies are not installed and external package downloads are blocked in this environment.
- Go formatting/parser checks pass, but `go test` and full linking could not run because the environment cannot download pgx and `x/crypto` modules; no live Neon database or email provider is connected here.
- Therefore this package is prepared for Vercel deployment configuration, but it is **not honestly certified production-ready** until Vercel Preview build and integration tests pass.

## Data safety
Never run an unverified rewrite against the only production database. Keep both reference archives. This application does not delete existing library data during startup, but any migration should be validated against a database copy first.

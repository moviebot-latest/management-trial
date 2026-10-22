# Vercel deployment note — V58

V58 now uses a Vite static frontend plus one Go serverless function under `api/index.go`. `vercel.json` rewrites `/api/*` into that function and passes the original route in `__path`, which the handler restores before dispatching to the Go router. The root `go.mod` declares the Go runtime dependencies.

Vercel documents its Go runtime as Beta. Verify the current runtime docs and run a Preview deployment before production. The old V54 Flask app is retained as a rollback archive and is not overwritten by this migration.

Required environment variables: `DATABASE_URL`, `SESSION_SECRET` (32+ characters), and `APP_ENV=production`. Email provider variables are required for OTP delivery. Use a staging Neon branch first. The full production cutover gate is in `VERCEL_DEPLOY_CHECKLIST.md` and the known migration gaps are in `MIGRATION_STATUS.md`.

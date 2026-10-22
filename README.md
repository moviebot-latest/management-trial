# Library Management System — V59

Clean V59 source package for the existing Library Management System.

## Stack
- Frontend: React + TypeScript + Vite
- Backend: Go HTTP API
- Database: PostgreSQL / Neon (`DATABASE_URL`)
- Deployment: Vercel

## Important safety notes
- Keep `DATABASE_URL` pointed at the existing Neon database to retain existing records.
- Do not reset, drop, or truncate production tables.
- Set `SESSION_SECRET` (at least 32 random characters) and `APP_ENV=production` in Vercel.
- For registration and password-reset OTP email, configure either Brevo (`BREVO_API_KEY`, `BREVO_SENDER_EMAIL`) or the documented SMTP variables from `.env.example`. Keep all secrets server-side.
- Test on a Vercel Preview deployment and a staging Neon branch before production rollout.

## Local development
- Frontend: `cd frontend && npm install && npm run dev`
- Backend: `go run ./cmd/server` from the repository root with required environment variables set.
- Frontend production build: `cd frontend && npm run build`

## Validation status
Go syntax/format and frontend syntax checks were reported as passing in the source package. A full dependency-backed build/test and live Neon, OTP email, and Vercel integration test were not completed in the restricted environment. Do not treat this package as production-certified until those checks pass.

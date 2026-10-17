# 🚀 Library Management System — Neon + Vercel Deployment Guide

Is guide ko follow karo aur 15 minute mein website **live** ho jaayegi!

---

## 📋 Kya Chahiye?

- [x] GitHub account (free) → https://github.com
- [x] Neon account (free) → https://neon.tech
- [x] Vercel account (free) → https://vercel.com

---

## STEP 1 — GitHub pe Code Upload Karo

1. **GitHub.com pe jaao** → New Repository banao
   - Name: `management-system-v20`
   - Visibility: **Private** (recommended)
   - Click **Create Repository**

2. **Code upload karo** (apne computer pe terminal/CMD kholo):

```bash
# Is folder mein jaao
cd management_system_v20

# Git setup
git init
git add .
git commit -m "Initial commit - Library Management System v20"

# GitHub se connect karo (apna repo URL paste karo)
git remote add origin https://github.com/TUMHARA_USERNAME/management-system-v20.git
git branch -M main
git push -u origin main
```

> ⚠️ `.env` file ko KABHI GitHub pe push mat karo! `.gitignore` already set hai.

---

## STEP 2 — Neon Database Banao (Free PostgreSQL)

1. **https://neon.tech** pe jaao → Sign up (GitHub se bhi ho sakta hai)

2. **New Project** banao:
   - Project Name: `management-system`
   - Region: `AWS Singapore` (ya koi bhi)
   - Click **Create Project**

3. Dashboard pe **Connection Details** section mein jaao

4. **Connection String** copy karo — kuch aisa dikhega:
   ```
   postgresql://neondb_owner:abcXYZ123@ep-cool-bird-123456.ap-southeast-1.aws.neon.tech/neondb?sslmode=require
   ```

5. Is string ko **safe jagah save karo** — abhi Vercel mein paste karenge.

> ✅ Neon free tier mein 0.5 GB storage milta hai — kafi hai!

---

## STEP 3 — Vercel pe Deploy Karo

1. Vercel dashboard kholo → **Add New → Project** → apna GitHub repository import karo. Vercel par Render-style “Web Service” select nahi karna hai.
2. Repository ka **Root Directory** woh folder set karo jahan `app.py`, `vercel.json`, aur `requirements.txt` ek hi level par hon. Is ZIP mein ye files ZIP ke root mein hain—unhe kisi extra nested folder mein mat rakho.
3. Framework detection ko default rehne do. `vercel.json` Flask Python function aur daily reminder cron configure karta hai; alag Gunicorn Start Command set karne ki zaroorat nahi hai.
4. **Settings → Environment Variables** mein values add karo. Secrets apne account mein hi rakho; unhe GitHub, screenshots, ya chat mein share mat karo.

   | Key | Value |
   |-----|-------|
   | `DATABASE_URL` | Intended Neon PostgreSQL pooled connection string (`sslmode=require`) |
   | `SECRET_KEY` | Strong random string; generate with the command below |
   | `ADMIN_USERNAME` | Admin login username |
   | `ADMIN_PASSWORD` | Strong admin password; do not use the example/default password |
   | `ADMIN_NAME` | Optional display name |
   | `ADMIN_EMAIL` | Optional admin notification inbox |
   | `BREVO_API_KEY` | Brevo transactional API key (OTP and email notifications) |
   | `BREVO_SENDER_EMAIL` | Verified sender address in Brevo |
   | `BREVO_SENDER_NAME` | Optional sender display name |
   | `PGSSLMODE` | `require` |
   | `FLASK_DEBUG` | `0` |
   | `CRON_SECRET` | Strong secret for scheduled reminders |

   Generate `SECRET_KEY` locally:
   ```bash
   python3 -c "import secrets; print(secrets.token_hex(32))"
   ```

5. Ensure variables are assigned to **Production** (and Preview if you test Preview deployments). Save the variables, then deploy/redeploy so the new deployment receives them.
6. Once the deployment completes, open the generated `https://<your-project>.vercel.app` URL. Check `/healthz`; a healthy deployment should report `database_connection: ok` and `initialization_ready: true`.
7. Test in order: sign-in page, existing admin login, member login, Create Account OTP, Forgot Password OTP, then book issue/return. Also inspect the selected deployment's Function Logs if `/healthz` returns 503.

> Important: `management_trial` and `management-system` are separate Neon projects in your screenshots. `DATABASE_URL` must point to the same project that contains your existing member/book/payment records. Do not reset or delete the database while diagnosing login.

## STEP 4 — Environment changes and redeploy

Vercel environment variable changes apply to new deployments, not to already-created deployments. After changing any variable, redeploy the intended Production branch/project and confirm the production domain points to that deployment.

## 🔧 Local Development (Optional)

```bash
# 1. Virtual environment banao
python3 -m venv venv
source venv/bin/activate        # Mac/Linux
venv\Scripts\activate           # Windows

# 2. Packages install karo
pip install -r requirements.txt

# 3. .env file banao
cp .env.example .env
# .env file kholo aur apni values fill karo

# 4. Run karo
python app.py
# Browser mein: http://localhost:5000
```

---

## 🗄️ Neon Database — Useful Info

| Feature | Details |
|---------|----------|
| Database type | PostgreSQL 16 |
| Free storage | 512 MB |
| Free compute | 191.9 compute hours/month |
| Auto-suspend | 5 min inactivity ke baad (free tier) |
| Backup | Automatic |
| SSL | Always required (sslmode=require) |
| Dashboard | https://console.neon.tech |

> ⚠️ **Auto-suspend**: Free tier mein Neon 5 min baad DB suspend kar deta hai.
> Pehli request slow ho sakti hai (cold start ~1-2 sec). Production ke liye
> Neon Pro le lo ya connection pooling enable karo.

---

## 🚨 Troubleshooting

### "could not connect to server"
- Neon Dashboard pe check karo ki project active hai
- DATABASE_URL sahi copy ki hai? Poori string chahiye
- PGSSLMODE=require set hai?

### "Application failed to respond" / HTTP 500
- Vercel → Project → Deployments se confirm karo ki correct commit latest Production deployment hai.
- Project Settings → Environment Variables mein `DATABASE_URL` ka hona, uska Production scope, aur sahi Neon project verify karo; phir redeploy karo.
- Website par `/healthz` kholo. `database_connection: failed` aaye to Function Logs inspect karo. Full `DATABASE_URL` ya secrets screenshot/chat mein share mat karo.
- Root Directory mein `app.py`, `vercel.json`, aur `requirements.txt` ek hi level par hone chahiye; kisi nested extracted ZIP folder ke andar nahi.

### Admin login nahi ho raha
- `ADMIN_USERNAME` aur `ADMIN_PASSWORD` values exactly verify karo; environment values badalne ke baad Production redeploy zaroor karo.
- Agar login ke baad dashboard par database error aata hai, to admin credentials ke bajay database/schema issue ho sakta hai. `/init-status` aur Function Logs dekho.

### Website slow / timeout
- First confirm `/healthz` reports `database_connection: ok`; then review Vercel Function Duration/Logs and Neon compute/usage.
- The app uses a small per-instance SQLAlchemy connection pool; for Vercel, use the Neon pooled connection string when available.

---

## 🔐 Production Security Checklist

- [ ] SECRET_KEY — strong random 32+ char string
- [ ] ADMIN_PASSWORD — strong password (uppercase + lowercase + number + special)
- [ ] FLASK_DEBUG=0 (kabhi 1 mat karna production mein)
- [ ] .env file GitHub pe push nahi hui
- [ ] Neon SSL enabled (PGSSLMODE=require)
- [ ] Vercel HTTPS automatic milta hai ✅

---

## 📱 Website URLs (after deployment)

| Page | URL |
|------|-----|
| 🌐 Landing | `https://your-project.vercel.app/` |
| 🔑 Login | `https://your-project.vercel.app/login` |
| 📊 Dashboard | `https://your-project.vercel.app/dashboard` |
| 👤 Profile | `https://your-project.vercel.app/profile` |
| 📈 Analytics | `https://your-project.vercel.app/admin/analytics` |
| 📋 Audit Logs | `https://your-project.vercel.app/admin/audit-logs` |

---

**Built with ❤️ using Flask + Neon PostgreSQL + Vercel**

## Brevo Email OTP

Normal self-registration now requires email verification before the account is created. The app sends a 6-digit, 10-minute OTP through Brevo's transactional email API. OTPs are hashed, attempts are limited, resend is rate-limited, and no OTP is stored in plaintext.

Set these Vercel Environment Variables: `BREVO_API_KEY`, `BREVO_SENDER_EMAIL`, and optional `BREVO_SENDER_NAME`. The Brevo API key must stay server-side; never put it in HTML/JavaScript or Git. Brevo requires a registered/verified sender for transactional email. Gmail addresses can be recipients; for reliable sending, use a verified sender/domain in Brevo.

Brevo's current Free plan provides 300 email sends per day and is free without a credit card.


### Internship payment workflow
This version records simulated library fine payments and refunds for the internship project. It does not charge or transfer real money. Payment/refund data is saved in the library database; it is separate from a bookstore cart/order checkout system.


## Authentication diagnostics (Vercel)

- `/healthz` reports only whether the database is reachable and required services are configured; it never shows secrets.
- `/init-status` reports safe initialization information. Detailed errors require admin login or the configured `CRON_SECRET` token.
- Configure `DATABASE_URL`, a stable `SECRET_KEY`, `ADMIN_USERNAME`, `ADMIN_PASSWORD`, `BREVO_API_KEY`, and `BREVO_SENDER_EMAIL` in Vercel Production Environment Variables, then redeploy. Do not share these values in screenshots or chat.

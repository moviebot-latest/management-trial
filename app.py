import os
import csv
import io
import secrets
import hashlib
import re
import json
import html as html_lib
import urllib.request
import urllib.error
import smtplib
from email.message import EmailMessage
import threading
import time
from datetime import date, timedelta, datetime, timezone
from zoneinfo import ZoneInfo
from functools import wraps
from flask import (
    Flask, render_template, request, redirect, url_for,
    session, flash, make_response, Response, jsonify, send_from_directory
)
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy import inspect, text, update, Text, ForeignKey, UniqueConstraint
from werkzeug.security import generate_password_hash, check_password_hash

try:
    from dotenv import load_dotenv
    load_dotenv()  # Local development only; Vercel uses dashboard environment variables.
except ImportError:
    pass

app = Flask(__name__)

# ── Secret Key / deployment configuration ────────────────────────────
# Keep the public landing page available if Vercel environment variables
# are missing. Never invent a production secret or silently use SQLite for
# production data; auth/database routes are gated below until configured.
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "").strip() or None
_config_errors = []
if not app.config["SECRET_KEY"]:
    _config_errors.append("SECRET_KEY is missing")

# ── Session & Cookie Security ──────────────────────────────────────────
is_production = os.environ.get("FLASK_DEBUG", "0") != "1"

app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SECURE=is_production,
    SESSION_COOKIE_SAMESITE="Lax",
    PERMANENT_SESSION_LIFETIME=timedelta(minutes=30),
    MAX_CONTENT_LENGTH=2 * 1024 * 1024,
)

# ── Database ──────────────────────────────────────────────────────────────
database_url = os.environ.get("DATABASE_URL", "").strip()
_database_configured = bool(database_url)
if not database_url:
    if os.environ.get("VERCEL"):
        _config_errors.append("DATABASE_URL is missing")
        # Placeholder only to let Flask import; request guard below prevents
        # any auth/database route from using this non-persistent fallback.
        database_url = "sqlite:///:memory:"
    else:
        database_url = "sqlite:///management.db"
if database_url.startswith("postgres://"):
    database_url = database_url.replace("postgres://", "postgresql://", 1)
# Neon URLs copied from newer dashboards may explicitly select psycopg v3
# (postgresql+psycopg://). This project installs psycopg2-binary, so normalize
# that driver name to psycopg2 before SQLAlchemy creates its engine.
if database_url.startswith("postgresql+psycopg://"):
    database_url = database_url.replace(
        "postgresql+psycopg://", "postgresql+psycopg2://", 1
    )
app.config["SQLALCHEMY_DATABASE_URI"] = database_url
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

if database_url.startswith(("postgresql://", "postgresql+psycopg2://")):
    is_serverless = bool(os.environ.get("VERCEL"))
    app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {
        "pool_pre_ping": True,
        "pool_recycle": 240,
        # A single connection was starving concurrent Vercel requests when the
        # landing-page stats and auth pages initialized at the same time.
        # Keep the pool modest, but allow a few concurrent DB operations.
        # Keep per-instance connections small for Neon + Vercel serverless.
        # Older deployments with pool_size=1/max_overflow=0 timed out when
        # landing-page stats and auth requests arrived together.
        "pool_timeout": 20 if is_serverless else 30,
        "pool_size": 2 if is_serverless else 5,
        "max_overflow": 1 if is_serverless else 5,
        "pool_use_lifo": True,
        "connect_args": {
            "connect_timeout": 10,
            "sslmode": os.environ.get("PGSSLMODE", "require"),
        },
    }

db = SQLAlchemy(app)

# ════════════════════════════ CONSTANTS ════════════════════════════

MAX_FAILED_ATTEMPTS = 5
LOCKOUT_MINUTES = 15

# Email OTP security
OTP_EXPIRY_MINUTES = 10
OTP_RESEND_SECONDS = 60
OTP_MAX_ATTEMPTS = 5

VALID_GENDERS = {"Male", "Female", "Other", "Prefer not to say"}
DEPARTMENT_CODES = {
    "Administration": "ADM",
    "Human Resources": "HR",
    "Finance": "FIN",
    "Sales": "SAL",
    "IT / Engineering": "ITE",
    "Operations": "OPS",
    "Marketing": "MKT",
    "Legal": "LGL",
    "Customer Support": "CUS",
    "Research & Development": "RND",
}

VALID_DEPARTMENTS = [
    "Administration", "Human Resources", "Finance", "Sales",
    "IT / Engineering", "Operations", "Marketing", "Legal",
    "Customer Support", "Research & Development",
]

# ════════════════════════════ MODELS ════════════════════════════

class User(db.Model):
    id              = db.Column("user_id", db.Integer, primary_key=True)
    employee_id     = db.Column(db.String(20),  unique=True,  nullable=False)
    name            = db.Column(db.String(120),  nullable=False)
    gender          = db.Column(db.String(30),   nullable=False)
    department      = db.Column(db.String(80),   nullable=False)
    email           = db.Column(db.String(160),  unique=True,  nullable=False)
    username        = db.Column(db.String(80),   unique=True,  nullable=False)
    password_hash   = db.Column(db.String(255),  nullable=False)
    role            = db.Column(db.String(20),   nullable=False, default="user")
    is_active       = db.Column(db.Boolean,      nullable=False, default=True)
    failed_attempts = db.Column(db.Integer,      nullable=False, default=0)
    locked_until    = db.Column(db.DateTime,     nullable=True)
    last_login      = db.Column(db.DateTime,     nullable=True)
    created_at      = db.Column(db.DateTime, server_default=db.func.now(), nullable=False)

    def is_locked(self):
        if self.locked_until:
            lu = self.locked_until
            if lu.tzinfo is None:
                lu = lu.replace(tzinfo=timezone.utc)
            return datetime.now(timezone.utc) < lu
        return False

    def record_failed_attempt(self):
        self.failed_attempts = (self.failed_attempts or 0) + 1
        if self.failed_attempts >= MAX_FAILED_ATTEMPTS:
            self.locked_until = datetime.utcnow() + timedelta(minutes=LOCKOUT_MINUTES)

    def unlock(self):
        self.failed_attempts = 0
        self.locked_until    = None

    def lockout_remaining_minutes(self):
        if not self.locked_until:
            return 0
        lu = self.locked_until
        if lu.tzinfo is None:
            lu = lu.replace(tzinfo=timezone.utc)
        secs = (lu - datetime.now(timezone.utc)).total_seconds()
        return max(0, int(secs // 60) + 1)


class Book(db.Model):
    """Library catalog item; additive model so existing account data stays untouched."""
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(180), nullable=False, index=True)
    author = db.Column(db.String(140), nullable=False)
    category = db.Column(db.String(80), nullable=False, default="General")
    isbn = db.Column(db.String(32), nullable=True, unique=True)
    publisher = db.Column(db.String(140), nullable=True)
    publication_year = db.Column(db.Integer, nullable=True)
    total_copies = db.Column(db.Integer, nullable=False, default=1)
    available_copies = db.Column(db.Integer, nullable=False, default=1)
    description = db.Column(Text, nullable=True)
    cover_data = db.Column(Text, nullable=True)
    created_by = db.Column(db.String(80), nullable=False, default="system")
    created_at = db.Column(db.DateTime, server_default=db.func.now(), nullable=False)


class EmailVerification(db.Model):
    id              = db.Column(db.Integer, primary_key=True)
    email           = db.Column(db.String(160), unique=True, nullable=False, index=True)
    username        = db.Column(db.String(80), nullable=False)
    name            = db.Column(db.String(120), nullable=False)
    gender          = db.Column(db.String(30), nullable=False)
    department      = db.Column(db.String(80), nullable=False)
    password_hash   = db.Column(db.String(255), nullable=False)
    otp_hash        = db.Column(db.String(255), nullable=False)
    expires_at      = db.Column(db.DateTime, nullable=False)
    last_sent_at    = db.Column(db.DateTime, nullable=False)
    attempts        = db.Column(db.Integer, nullable=False, default=0)
    created_at      = db.Column(db.DateTime, server_default=db.func.now(), nullable=False)


class PasswordReset(db.Model):
    id           = db.Column(db.Integer, primary_key=True)
    user_id      = db.Column(db.Integer, nullable=False, index=True)
    email        = db.Column(db.String(160), nullable=False, index=True)
    otp_hash     = db.Column(db.String(255), nullable=False)
    expires_at   = db.Column(db.DateTime, nullable=False)
    last_sent_at = db.Column(db.DateTime, nullable=False)
    attempts     = db.Column(db.Integer, nullable=False, default=0)
    verified_at  = db.Column(db.DateTime, nullable=True)
    created_at   = db.Column(db.DateTime, server_default=db.func.now(), nullable=False)


class AuditLog(db.Model):
    id         = db.Column(db.Integer,     primary_key=True)
    username   = db.Column(db.String(80),  nullable=False)
    action     = db.Column(db.String(80),  nullable=False)
    ip_address = db.Column(db.String(45),  nullable=True)
    details    = db.Column(db.String(500), nullable=True)
    timestamp  = db.Column(db.DateTime, server_default=db.func.now(), nullable=False)


class EmailCampaign(db.Model):
    """Admin announcements and scheduled email campaigns; additive to existing data."""
    id = db.Column(db.Integer, primary_key=True)
    submission_key = db.Column(db.String(80), unique=True, nullable=True, index=True)
    subject = db.Column(db.String(200), nullable=False)
    message = db.Column(Text, nullable=False)
    template = db.Column(db.String(40), nullable=False, default="announcement")
    cta_label = db.Column(db.String(80), nullable=True)
    cta_url = db.Column(db.String(500), nullable=True)
    recipient_mode = db.Column(db.String(20), nullable=False, default="all")
    status = db.Column(db.String(24), nullable=False, default="queued", index=True)
    created_by = db.Column(db.String(80), nullable=False)
    scheduled_at = db.Column(db.DateTime, nullable=True, index=True)
    claimed_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow, index=True)
    sent_at = db.Column(db.DateTime, nullable=True)
    total_recipients = db.Column(db.Integer, nullable=False, default=0)
    sent_count = db.Column(db.Integer, nullable=False, default=0)
    failed_count = db.Column(db.Integer, nullable=False, default=0)
    test_only = db.Column(db.Boolean, nullable=False, default=False)


class EmailCampaignRecipient(db.Model):
    __table_args__ = (UniqueConstraint("campaign_id", "email", name="uq_campaign_recipient_email"),)
    id = db.Column(db.Integer, primary_key=True)
    campaign_id = db.Column(db.Integer, ForeignKey("email_campaign.id", ondelete="CASCADE"), nullable=False, index=True)
    user_id = db.Column(db.Integer, nullable=True, index=True)
    email = db.Column(db.String(160), nullable=False)
    recipient_name = db.Column(db.String(120), nullable=False, default="there")
    status = db.Column(db.String(20), nullable=False, default="pending", index=True)
    error = db.Column(db.String(500), nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    sent_at = db.Column(db.DateTime, nullable=True)


# ════════════════════════════ HELPERS ════════════════════════════

def db_retry(fn):
    try:
        return fn()
    except OperationalError:
        db.session.rollback()
        db.engine.dispose()
        return fn()


def next_employee_id(department, joining_year=None):
    """Generate immutable DEPT-YY-NNNN IDs, unique per department/year.

    Existing legacy EMP### IDs are ignored for new numbering; they remain valid
    and unique. The database UNIQUE constraint is the final safety net.
    """
    code = DEPARTMENT_CODES.get(department, "EMP")
    year = int(joining_year or datetime.now().year)
    yy = year % 100
    prefix = f"{code}-{yy:02d}-"

    def query():
        rows = User.query.with_entities(User.employee_id).filter(
            User.employee_id.like(prefix + "%")
        ).all()
        max_n = 0
        for (eid,) in rows:
            try:
                n = int(eid.rsplit("-", 1)[1])
                max_n = max(max_n, n)
            except (ValueError, IndexError):
                continue
        n = max_n + 1
        return f"{prefix}{n:04d}"
    return db_retry(query)


def get_ip():
    return (
        request.headers.get("X-Forwarded-For", request.remote_addr or "unknown")
        .split(",")[0].strip()
    )


def log_action(username, action, details=None):
    try:
        entry = AuditLog(
            username=username or "unknown",
            action=action,
            ip_address=get_ip(),
            details=details,
        )
        db.session.add(entry)
        db.session.commit()
    except Exception:
        db.session.rollback()


def is_admin():
    return session.get("role") == "admin"


def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("user_id"):
            flash("Please login to access this page.", "error")
            return redirect(url_for("login_page"))
        return f(*args, **kwargs)
    return decorated


def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("user_id"):
            flash("Please login.", "error")
            return redirect(url_for("login_page"))
        if not is_admin():
            flash("Admin access required.", "error")
            return redirect(url_for("dashboard"))
        return f(*args, **kwargs)
    return decorated


def validate_password(password):
    if len(password) < 8:
        return False, "Password must be at least 8 characters."
    if not re.search(r'[A-Z]', password):
        return False, "Password must contain at least one uppercase letter."
    if not re.search(r'[a-z]', password):
        return False, "Password must contain at least one lowercase letter."
    if not re.search(r'\d', password):
        return False, "Password must contain at least one digit."
    if not re.search(r'[^A-Za-z0-9]', password):
        return False, "Password must contain at least one special character."
    return True, ""


def is_reserved_username(username):
    admin_u = os.environ.get("ADMIN_USERNAME", "admin").strip().casefold()
    u = username.strip().casefold()
    return u == admin_u or u == "admin"


def _utcnow_naive():
    return datetime.utcnow()


def _brevo_configured():
    return bool(os.environ.get("BREVO_API_KEY", "").strip() and
                os.environ.get("BREVO_SENDER_EMAIL", "").strip())


def _smtp_configured():
    return bool(os.environ.get("SMTP_HOST", "").strip() and
                os.environ.get("SMTP_USER", "").strip() and
                os.environ.get("SMTP_PASSWORD", "").strip() and
                (os.environ.get("SMTP_FROM", "").strip() or os.environ.get("SMTP_USER", "").strip()))


def _email_configured():
    """Return true when either supported transactional email provider is configured."""
    return _brevo_configured() or _smtp_configured()


def send_otp_email(email, otp, name, purpose="verification"):
    """Send an OTP using Brevo when configured, otherwise use configured SMTP."""
    api_key = os.environ.get("BREVO_API_KEY", "").strip()
    sender_email = os.environ.get("BREVO_SENDER_EMAIL", "").strip()
    sender_name = os.environ.get("BREVO_SENDER_NAME", "Management System").strip() or "Management System"
    use_brevo = bool(api_key and sender_email)
    if not use_brevo and not _smtp_configured():
        raise RuntimeError(
            "Email is not configured. Set BREVO_API_KEY and BREVO_SENDER_EMAIL, "
            "or configure SMTP_HOST, SMTP_USER, SMTP_PASSWORD and SMTP_FROM in Vercel."
        )
    if not sender_email:
        sender_email = os.environ.get("SMTP_FROM", "").strip() or os.environ.get("SMTP_USER", "").strip()

    raw_name = (name or "there").strip()[:120]
    safe_name = html_lib.escape(raw_name, quote=True)
    is_reset = purpose == "password_reset"
    is_admin_login = purpose == "admin_login"
    if is_admin_login:
        title = "🔐 Admin sign-in verification"
        intro = "A sign-in attempt passed the admin username and password check. Enter this one-time code to finish signing in to the Admin Panel:"
        subject = "Your Admin Panel sign-in code"
        plain = f"Your Admin Panel sign-in code is {otp}. It expires in {OTP_EXPIRY_MINUTES} minutes. If this wasn't you, change your admin password immediately."
    elif is_reset:
        title = "🔐 Reset your password"
        intro = "Use this one-time code to reset your Management System password:"
        subject = "Your Management System password reset code"
        plain = f"Your Management System password reset code is {otp}. It expires in {OTP_EXPIRY_MINUTES} minutes."
    else:
        title = "🔐 Verify your email"
        intro = "Use this one-time verification code to finish creating your Management System account:"
        subject = "Your Management System verification code"
        plain = f"Your Management System verification code is {otp}. It expires in {OTP_EXPIRY_MINUTES} minutes."
    html = f"""<!doctype html>
<html lang=\"en\"><body style=\"margin:0;padding:24px 12px;background:#f4f6ff;font-family:Arial,Helvetica,sans-serif;color:#172033\">
  <div style=\"max-width:540px;margin:0 auto;background:#fff;border:1px solid #e8eaf5;border-radius:22px;overflow:hidden;box-shadow:0 16px 48px rgba(35,42,84,.12)\">
    <div style=\"padding:28px 22px;background:linear-gradient(135deg,#101b4b,#4433a8);text-align:center;color:#fff\">
      <div style=\"width:54px;height:54px;line-height:54px;margin:0 auto 12px;border-radius:17px;background:rgba(255,255,255,.15);font-size:26px\">🔐</div>
      <div style=\"font-size:11px;letter-spacing:2px;text-transform:uppercase;color:#c7d2fe;font-weight:bold\">Management System</div>
      <h1 style=\"font-size:25px;line-height:1.25;margin:12px 0 0\">{title}</h1>
    </div>
    <div style=\"padding:28px 24px\">
      <p style=\"font-size:16px;margin:0 0 14px\">Hi {safe_name},</p>
      <p style=\"font-size:14px;line-height:1.75;color:#475467;margin:0 0 20px\">{intro}</p>
      <div style=\"font-size:32px;font-weight:800;letter-spacing:9px;text-align:center;padding:19px 12px;background:#f1efff;border:1px solid #e0ddff;border-radius:15px;color:#29246f\">{otp}</div>
      <p style=\"font-size:12px;line-height:1.7;color:#667085;margin:20px 0 0\">This code expires in <strong>{OTP_EXPIRY_MINUTES} minutes</strong>. Never share it with anyone. If you did not request this, you can safely ignore this email.</p>
    </div>
    <div style=\"padding:15px 20px;background:#fafbff;border-top:1px solid #eef0f7;text-align:center;color:#98a2b3;font-size:11px\">Management System · Secure verification</div>
  </div>
</body></html>"""
    brevo_error = None
    if use_brevo:
        payload = json.dumps({
            "sender": {"name": sender_name, "email": sender_email},
            "to": [{"email": email, "name": raw_name}],
            "subject": subject,
            "htmlContent": html,
            "textContent": plain
        }).encode("utf-8")
        req = urllib.request.Request(
            "https://api.brevo.com/v3/smtp/email",
            data=payload,
            headers={
                "accept": "application/json",
                "api-key": api_key,
                "content-type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as response:
                if response.status < 200 or response.status >= 300:
                    raise RuntimeError("Brevo rejected the email request.")
                return True
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="ignore")[:300]
            brevo_error = RuntimeError(f"Brevo email send failed ({exc.code}). {detail}")
        except urllib.error.URLError as exc:
            brevo_error = RuntimeError("Brevo email service is temporarily unavailable.")
        except Exception as exc:
            brevo_error = RuntimeError(f"Brevo email send failed: {type(exc).__name__}.")

        # If Brevo is configured but unavailable/rejects the message, try SMTP too
        # when SMTP credentials are present. Never log API keys or passwords.
        if _smtp_configured():
            app.logger.warning("Brevo OTP delivery failed; attempting configured SMTP fallback (%s).", type(brevo_error).__name__)
        else:
            raise brevo_error

    # SMTP fallback: port 465 uses implicit TLS; other ports use STARTTLS.
    host = os.environ.get("SMTP_HOST", "").strip()
    port = int(os.environ.get("SMTP_PORT", "587").strip() or "587")
    smtp_user = os.environ.get("SMTP_USER", "").strip()
    smtp_password = os.environ.get("SMTP_PASSWORD", "")
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = f"{sender_name} <{sender_email}>"
    message["To"] = email
    message.set_content(plain)
    message.add_alternative(html, subtype="html")
    try:
        if port == 465:
            with smtplib.SMTP_SSL(host, port, timeout=10) as server:
                server.login(smtp_user, smtp_password)
                server.send_message(message)
        else:
            with smtplib.SMTP(host, port, timeout=10) as server:
                server.ehlo()
                server.starttls()
                server.ehlo()
                server.login(smtp_user, smtp_password)
                server.send_message(message)
        return True
    except (OSError, smtplib.SMTPException, ValueError) as exc:
        smtp_error = RuntimeError(f"SMTP email send failed: {type(exc).__name__}.")
        if brevo_error is not None:
            raise RuntimeError(f"Both email providers failed. {brevo_error}; {smtp_error}") from exc
        raise smtp_error from exc


def send_account_created_email(email, name, username):
    """Send a welcome/confirmation email after the account is already committed."""
    api_key = os.environ.get("BREVO_API_KEY", "").strip()
    sender_email = os.environ.get("BREVO_SENDER_EMAIL", "").strip()
    sender_name = os.environ.get("BREVO_SENDER_NAME", "Management System").strip() or "Management System"
    use_brevo = bool(api_key and sender_email)
    if not use_brevo and not _smtp_configured():
        raise RuntimeError("No transactional email provider is configured.")
    if not sender_email:
        sender_email = os.environ.get("SMTP_FROM", "").strip() or os.environ.get("SMTP_USER", "").strip()

    safe_name = html_lib.escape((name or "there").strip()[:120], quote=True)
    safe_username = html_lib.escape((username or "").strip()[:80], quote=True)
    login_url = url_for("login_page", _external=True)
    safe_login_url = html_lib.escape(login_url, quote=True)
    subject = "Congratulations! Your Management System account is ready"
    plain = (
        f"Congratulations, {name or 'there'}! Your Management System account has been created successfully. "
        f"Your username is {username}. Sign in here: {login_url}"
    )
    html_body = f'''<!doctype html>
<html lang="en"><body style="margin:0;background:#f4f6ff;font-family:Arial,Helvetica,sans-serif;color:#172033;padding:24px 12px">
  <div style="max-width:560px;margin:0 auto;background:#ffffff;border:1px solid #e8eaf5;border-radius:22px;overflow:hidden;box-shadow:0 16px 48px rgba(35,42,84,.12)">
    <div style="background:linear-gradient(135deg,#101b4b,#4433a8);padding:30px 24px;text-align:center;color:#fff">
      <div style="width:64px;height:64px;margin:0 auto 16px;background:rgba(255,255,255,.14);border:1px solid rgba(255,255,255,.28);border-radius:20px;line-height:64px;font-size:32px">✓</div>
      <div style="font-size:12px;letter-spacing:2px;text-transform:uppercase;color:#c7d2fe;font-weight:bold">Account successfully created</div>
      <h1 style="font-size:27px;line-height:1.2;margin:10px 0 0">Congratulations, {safe_name}!</h1>
    </div>
    <div style="padding:28px 26px;text-align:center">
      <p style="font-size:16px;line-height:1.7;color:#475467;margin:0 0 20px">Welcome to the Management System. Your email has been verified and your account is ready to use.</p>
      <div style="background:#f7f7ff;border:1px solid #e4e5ff;border-radius:14px;padding:16px;margin:0 0 24px;text-align:left">
        <div style="font-size:11px;letter-spacing:1.4px;color:#72779a;font-weight:bold;text-transform:uppercase">Your username</div>
        <div style="font-size:21px;font-weight:bold;color:#29246f;word-break:break-word;margin-top:7px">{safe_username}</div>
      </div>
      <a href="{safe_login_url}" style="display:inline-block;background:linear-gradient(135deg,#4f46e5,#2563eb);color:#ffffff;text-decoration:none;font-weight:bold;padding:15px 24px;border-radius:12px;font-size:15px">Log In to Your Account &rarr;</a>
      <p style="font-size:12px;color:#98a2b3;line-height:1.6;margin:24px 0 0">If you did not create this account, please contact your system administrator.</p>
    </div>
    <div style="background:#fafbff;border-top:1px solid #eef0f7;text-align:center;padding:16px;font-size:12px;color:#7c8499">Management System Team</div>
  </div>
</body></html>'''

    brevo_error = None
    if use_brevo:
        payload = json.dumps({
            "sender": {"name": sender_name, "email": sender_email},
            "to": [{"email": email, "name": (name or "there")[:120]},],
            "subject": subject,
            "htmlContent": html_body,
            "textContent": plain,
        }).encode("utf-8")
        req = urllib.request.Request(
            "https://api.brevo.com/v3/smtp/email",
            data=payload,
            headers={"accept": "application/json", "api-key": api_key, "content-type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as response:
                if 200 <= response.status < 300:
                    return True
                raise RuntimeError("Brevo rejected the welcome email.")
        except Exception as exc:
            brevo_error = exc
            if not _smtp_configured():
                raise RuntimeError(f"Brevo welcome email failed ({type(exc).__name__}).") from exc
            app.logger.warning("Brevo welcome email failed; trying SMTP fallback (%s).", type(exc).__name__)

    host = os.environ.get("SMTP_HOST", "").strip()
    port = int(os.environ.get("SMTP_PORT", "587").strip() or "587")
    smtp_user = os.environ.get("SMTP_USER", "").strip()
    smtp_password = os.environ.get("SMTP_PASSWORD", "")
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = f"{sender_name} <{sender_email}>"
    message["To"] = email
    message.set_content(plain)
    message.add_alternative(html_body, subtype="html")
    try:
        if port == 465:
            with smtplib.SMTP_SSL(host, port, timeout=10) as server:
                server.login(smtp_user, smtp_password)
                server.send_message(message)
        else:
            with smtplib.SMTP(host, port, timeout=10) as server:
                server.ehlo()
                server.starttls()
                server.ehlo()
                server.login(smtp_user, smtp_password)
                server.send_message(message)
        return True
    except (OSError, smtplib.SMTPException, ValueError) as exc:
        if brevo_error is not None:
            raise RuntimeError("Both email providers failed to send the welcome email.") from exc
        raise RuntimeError(f"SMTP welcome email failed ({type(exc).__name__}).") from exc


def _campaign_provider_name():
    if _brevo_configured():
        return "brevo"
    if _smtp_configured():
        return "smtp"
    return ""


def _campaign_email_html(subject, message_body, recipient_name, cta_label="", cta_url=""):
    """Build safe, branded, mobile-friendly HTML from plain text."""
    safe_subject = html_lib.escape((subject or "Management System update")[:200], quote=True)
    safe_name = html_lib.escape((recipient_name or "there")[:120], quote=True)
    safe_body = html_lib.escape(message_body or "", quote=True).replace("\r\n", "\n").replace("\r", "\n").replace("\n", "<br>")
    safe_cta_label = html_lib.escape((cta_label or "")[:80], quote=True)
    safe_cta_url = html_lib.escape((cta_url or "")[:500], quote=True)
    button = ""
    if safe_cta_label and safe_cta_url and cta_url.startswith(("https://", "http://")):
        button = f'<div style="text-align:center;margin:28px 0 8px"><a href="{safe_cta_url}" style="display:inline-block;padding:14px 22px;background:#5b4de2;color:#fff;text-decoration:none;border-radius:12px;font-weight:800">{safe_cta_label} &rarr;</a></div>'
    return f'''<!doctype html><html lang="en"><head><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;background:#f3f5fc;padding:22px 10px;font-family:Inter,Arial,Helvetica,sans-serif;color:#17233f">
<div style="max-width:620px;margin:0 auto;background:#fff;border:1px solid #e6e9f5;border-radius:22px;overflow:hidden;box-shadow:0 16px 44px rgba(36,46,96,.10)">
<div style="padding:28px 24px;background:linear-gradient(130deg,#172554,#4f46e5 65%,#9333ea);color:#fff;text-align:center">
<div style="margin:0 auto 12px;width:54px;height:54px;line-height:54px;background:rgba(255,255,255,.16);border:1px solid rgba(255,255,255,.22);border-radius:17px;font-size:24px;font-weight:900">MS</div>
<div style="font-size:11px;letter-spacing:2px;font-weight:800;text-transform:uppercase;color:#dbeafe">Management System</div>
<h1 style="font-size:25px;line-height:1.28;margin:12px 0 0">{safe_subject}</h1></div>
<div style="padding:26px 24px"><p style="margin:0 0 16px;font-size:16px;font-weight:700">Hello {safe_name},</p>
<div style="font-size:15px;line-height:1.85;color:#475467;overflow-wrap:anywhere">{safe_body}</div>{button}
<p style="font-size:12px;line-height:1.7;color:#98a2b3;margin:26px 0 0">You are receiving this message from your Management System administrator. If you think this email was sent in error, you may safely ignore it.</p></div>
<div style="text-align:center;padding:15px 16px;background:#f8f9ff;border-top:1px solid #edf0f8;color:#8a94aa;font-size:11px">Management System · Official notification</div></div></body></html>'''


def send_campaign_email(email, recipient_name, subject, message_body, cta_label="", cta_url=""):
    """Send using the same Brevo-first / SMTP-fallback policy as OTP mail."""
    subject = (subject or "").strip()
    if not subject or len(subject) > 200 or "\r" in subject or "\n" in subject:
        raise ValueError("Email subject is invalid.")
    if not _email_configured():
        raise RuntimeError("Email service is not configured. Set BREVO_API_KEY + BREVO_SENDER_EMAIL or SMTP environment variables.")
    api_key = os.environ.get("BREVO_API_KEY", "").strip()
    sender_email = os.environ.get("BREVO_SENDER_EMAIL", "").strip() or os.environ.get("SMTP_FROM", "").strip() or os.environ.get("SMTP_USER", "").strip()
    sender_name = os.environ.get("BREVO_SENDER_NAME", "Management System").strip() or "Management System"
    plain = f"Hello {recipient_name or 'there'},\n\n{message_body or ''}"
    if cta_label and cta_url and cta_url.startswith(("https://", "http://")):
        plain += f"\n\n{cta_label}: {cta_url}"
    html = _campaign_email_html(subject, message_body, recipient_name, cta_label, cta_url)
    brevo_error = None
    if api_key and os.environ.get("BREVO_SENDER_EMAIL", "").strip():
        payload = json.dumps({
            "sender": {"name": sender_name, "email": sender_email},
            "to": [{"email": email, "name": (recipient_name or "there")[:120]},],
            "subject": subject, "htmlContent": html, "textContent": plain,
        }).encode("utf-8")
        req = urllib.request.Request("https://api.brevo.com/v3/smtp/email", data=payload,
            headers={"accept":"application/json", "api-key":api_key, "content-type":"application/json"}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=10) as response:
                if response.status < 200 or response.status >= 300:
                    raise RuntimeError("Brevo rejected the email request.")
                return True
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="ignore")[:180]
            brevo_error = RuntimeError(f"Brevo email send failed ({exc.code}). {detail}")
        except urllib.error.URLError:
            brevo_error = RuntimeError("Brevo email service is temporarily unavailable.")
        except Exception as exc:
            brevo_error = RuntimeError(f"Brevo email send failed: {type(exc).__name__}.")
        if not _smtp_configured():
            raise brevo_error
        app.logger.warning("Brevo campaign delivery failed; attempting SMTP fallback (%s).", type(brevo_error).__name__)

    host = os.environ.get("SMTP_HOST", "").strip()
    port = int(os.environ.get("SMTP_PORT", "587").strip() or "587")
    smtp_user = os.environ.get("SMTP_USER", "").strip()
    smtp_password = os.environ.get("SMTP_PASSWORD", "")
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = f"{sender_name} <{sender_email}>"
    msg["To"] = email
    msg.set_content(plain)
    msg.add_alternative(html, subtype="html")
    try:
        if port == 465:
            with smtplib.SMTP_SSL(host, port, timeout=10) as server:
                server.login(smtp_user, smtp_password)
                server.send_message(msg)
        else:
            with smtplib.SMTP(host, port, timeout=10) as server:
                server.ehlo(); server.starttls(); server.ehlo()
                server.login(smtp_user, smtp_password)
                server.send_message(msg)
        return True
    except (OSError, smtplib.SMTPException, ValueError) as exc:
        smtp_error = RuntimeError(f"SMTP email send failed: {type(exc).__name__}.")
        if brevo_error is not None:
            raise RuntimeError(f"Both email providers failed. {brevo_error}; {smtp_error}") from exc
        raise smtp_error from exc


def _campaign_base_url():
    configured = os.environ.get("PUBLIC_APP_URL", "").strip().rstrip("/")
    if configured.startswith(("https://", "http://")):
        return configured
    return request.url_root.rstrip("/") if request else ""


def _campaign_status_refresh(campaign):
    rows = db.session.query(EmailCampaignRecipient.status, db.func.count(EmailCampaignRecipient.id)).filter_by(
        campaign_id=campaign.id
    ).group_by(EmailCampaignRecipient.status).all()
    counts = {status: int(count) for status, count in rows}
    campaign.sent_count = counts.get("sent", 0)
    campaign.failed_count = counts.get("failed", 0)
    pending = counts.get("pending", 0)
    if pending:
        campaign.status = "queued"
        campaign.sent_at = None
    else:
        campaign.sent_at = datetime.utcnow()
        if campaign.failed_count and campaign.sent_count:
            campaign.status = "partial"
        elif campaign.failed_count:
            campaign.status = "failed"
        else:
            campaign.status = "sent"
    db.session.commit()
    return pending


def _process_campaign_batch(campaign_id, batch_size=None):
    """Claim one campaign atomically, send a bounded batch, and persist each outcome."""
    batch_size = max(1, min(int(batch_size or os.environ.get("EMAIL_CAMPAIGN_BATCH_SIZE", "10")), 50))
    now = datetime.utcnow()
    campaign = EmailCampaign.query.get(campaign_id)
    if not campaign or campaign.status in {"cancelled", "sent", "partial", "failed", "test_sent", "test_failed"}:
        return {"processed": 0, "status": campaign.status if campaign else "missing"}
    if campaign.status == "scheduled" and (not campaign.scheduled_at or campaign.scheduled_at > now):
        return {"processed": 0, "status": "scheduled"}
    # Recover a serverless worker that died after claiming the campaign.
    if campaign.status == "sending" and campaign.claimed_at and campaign.claimed_at > now - timedelta(minutes=5):
        return {"processed": 0, "status": "sending"}
    if campaign.status == "sending":
        campaign.status = "queued"
        db.session.commit()
    claimed = db.session.execute(update(EmailCampaign).where(
        EmailCampaign.id == campaign_id,
        EmailCampaign.status.in_(["queued", "scheduled"]),
    ).values(status="sending", claimed_at=now))
    db.session.commit()
    if claimed.rowcount != 1:
        return {"processed": 0, "status": "busy"}

    campaign = EmailCampaign.query.get(campaign_id)
    recipients = EmailCampaignRecipient.query.filter_by(campaign_id=campaign_id, status="pending").order_by(EmailCampaignRecipient.id.asc()).limit(batch_size).all()
    processed = 0
    cta_url = (campaign.cta_url or "").strip()
    for recipient in recipients:
        try:
            send_campaign_email(recipient.email, recipient.recipient_name, campaign.subject,
                                campaign.message, campaign.cta_label or "", cta_url)
            recipient.status = "sent"
            recipient.error = None
            recipient.sent_at = datetime.utcnow()
        except Exception as exc:
            recipient.status = "failed"
            recipient.error = str(exc)[:500]
            app.logger.warning("Email campaign %s recipient delivery failed (%s)", campaign_id, type(exc).__name__)
        processed += 1
        db.session.commit()  # Persist each result if serverless runtime stops mid-batch.
    campaign = EmailCampaign.query.get(campaign_id)
    if campaign:
        campaign.claimed_at = None
        pending = _campaign_status_refresh(campaign)
        if pending:
            campaign.status = "queued"
            db.session.commit()
    return {"processed": processed, "status": campaign.status if campaign else "missing"}

def mask_email(email):
    email = (email or '').strip()
    if '@' not in email:
        return 'your registered email'
    local, domain = email.split('@', 1)
    if len(local) <= 2:
        masked = local[:1] + '*' * max(1, len(local) - 1)
    else:
        masked = local[:2] + '*' * max(2, len(local) - 2)
    return f"{masked}@{domain}"


def create_password_reset_otp(user):
    now = _utcnow_naive()
    row = PasswordReset.query.filter_by(user_id=user.id).order_by(
        PasswordReset.created_at.desc(), PasswordReset.id.desc()
    ).first()
    if row and row.last_sent_at and (now - row.last_sent_at).total_seconds() < OTP_RESEND_SECONDS:
        wait = OTP_RESEND_SECONDS - int((now - row.last_sent_at).total_seconds())
        raise ValueError(f"Please wait {max(1, wait)} seconds before requesting another code.")

    otp = f"{secrets.randbelow(1000000):06d}"
    created_new = row is None
    previous = None
    if row is None:
        row = PasswordReset(
            user_id=user.id, email=user.email, otp_hash=generate_password_hash(otp),
            expires_at=now + timedelta(minutes=OTP_EXPIRY_MINUTES), last_sent_at=now,
            attempts=0, verified_at=None
        )
    else:
        previous = {key: getattr(row, key) for key in
                    ("email", "otp_hash", "expires_at", "last_sent_at", "attempts", "verified_at")}
        row.email = user.email
        row.otp_hash = generate_password_hash(otp)
        row.expires_at = now + timedelta(minutes=OTP_EXPIRY_MINUTES)
        row.last_sent_at = now
        row.attempts = 0
        row.verified_at = None
    db.session.add(row)
    db.session.commit()
    try:
        send_otp_email(user.email, otp, user.name, purpose="password_reset")
    except Exception:
        # Preserve a previously usable reset row if Brevo fails; only remove a new failed attempt.
        db.session.rollback()
        current = db.session.get(PasswordReset, row.id)
        if created_new:
            if current:
                db.session.delete(current)
        elif current and previous:
            for key, value in previous.items():
                setattr(current, key, value)
        db.session.commit()
        raise


def create_email_otp(email, name, gender, department, username, password_hash):
    now = _utcnow_naive()
    existing = EmailVerification.query.filter(
        db.func.lower(EmailVerification.email) == email.casefold()
    ).order_by(EmailVerification.created_at.desc(), EmailVerification.id.desc()).first()
    if existing and existing.last_sent_at and (now - existing.last_sent_at).total_seconds() < OTP_RESEND_SECONDS:
        wait = OTP_RESEND_SECONDS - int((now - existing.last_sent_at).total_seconds())
        raise ValueError(f"Please wait {max(1, wait)} seconds before requesting another code.")

    otp = f"{secrets.randbelow(1000000):06d}"
    created_new = existing is None
    previous = None
    row = existing or EmailVerification(
        email=email, username=username, name=name, gender=gender, department=department,
        password_hash=password_hash, otp_hash=generate_password_hash(otp)
    )
    if existing:
        previous = {key: getattr(row, key) for key in (
            "email", "username", "name", "gender", "department", "password_hash",
            "otp_hash", "expires_at", "last_sent_at", "attempts"
        )}
    row.email = email
    row.username = username
    row.name = name
    row.gender = gender
    row.department = department
    row.password_hash = password_hash
    row.otp_hash = generate_password_hash(otp)
    row.expires_at = now + timedelta(minutes=OTP_EXPIRY_MINUTES)
    row.last_sent_at = now
    row.created_at = now
    row.attempts = 0
    db.session.add(row)
    db.session.commit()
    try:
        send_otp_email(email, otp, name)
    except Exception:
        # Do not erase an existing pending registration when the email provider fails.
        db.session.rollback()
        if created_new:
            current = db.session.get(EmailVerification, row.id)
            if current:
                db.session.delete(current)
        else:
            current = db.session.get(EmailVerification, row.id)
            if current and previous:
                for key, value in previous.items():
                    setattr(current, key, value)
        db.session.commit()
        raise


def user_stats():
    def query():
        now   = datetime.utcnow()
        today = date.today()
        total    = User.query.count()
        active   = User.query.filter_by(is_active=True).count()
        inactive = total - active
        locked   = User.query.filter(User.locked_until > now).count()
        today_ct = User.query.filter(db.func.date(User.created_at) == today).count()
        dept_ct  = db.session.query(User.department).distinct().count()
        weekly = []
        for offset in range(6, -1, -1):
            day = today - timedelta(days=offset)
            cnt = User.query.filter(db.func.date(User.created_at) == day).count()
            weekly.append({"label": day.strftime("%a"), "date": day.strftime("%d %b"), "count": cnt})
        max_count = max((x["count"] for x in weekly), default=0) or 1
        return total, active, inactive, locked, today_ct, dept_ct, weekly, max_count
    return db_retry(query)


# ════════════════════════════ CSRF ══════════════════════════════

@app.context_processor
def inject_globals():
    return {
        "csrf_token": _csrf_token(),
        "current_year": datetime.utcnow().year,
        "valid_departments": VALID_DEPARTMENTS,
    }


def _csrf_token():
    # The public landing page can render during incomplete deployment setup.
    # Do not touch Flask's session when SECRET_KEY is absent.
    if not app.config.get("SECRET_KEY"):
        return ""
    if "_csrf_token" not in session:
        session["_csrf_token"] = secrets.token_urlsafe(32)
    return session["_csrf_token"]


@app.before_request
def before_request():
    # Static assets and the public landing page should remain available, while
    # every session/auth/database endpoint fails closed with an actionable 503.
    public_paths = {"/", "/__version", "/style.css", "/ui_advanced.js", "/favicon.ico"}
    if _config_errors and request.path not in public_paths and request.endpoint != "static":
        return jsonify({
            "status": "configuration_error",
            "message": "Deployment configuration is incomplete. Set SECRET_KEY and DATABASE_URL in Vercel Environment Variables, then redeploy.",
            "missing": list(_config_errors),
        }), 503
    if session.get("user_id"):
        session.modified = True
    # CSRF on POST (skip landing / static)
    if request.method == "POST":
        sent     = request.form.get("_csrf_token", "")
        expected = session.get("_csrf_token", "")
        if not expected or not sent or not secrets.compare_digest(sent, expected):
            return render_template("error.html", code=400,
                                   message="Invalid or missing security token."), 400


@app.after_request
def security_headers(response):
    h = response.headers
    h["X-Content-Type-Options"]    = "nosniff"
    h["X-Frame-Options"]           = "DENY"
    h["X-XSS-Protection"]         = "1; mode=block"
    h["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains; preload"
    h["Cache-Control"]             = "no-store, no-cache, must-revalidate, max-age=0"
    h["Pragma"]                    = "no-cache"
    h["Expires"]                   = "0"
    h["Referrer-Policy"]           = "strict-origin-when-cross-origin"
    h["Permissions-Policy"]        = "camera=(), microphone=(), geolocation=(), payment=()"
    h["Content-Security-Policy"]   = (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com https://fonts.gstatic.com; "
        "font-src 'self' https://fonts.gstatic.com data:; "
        "img-src 'self' data:; "
        "connect-src 'self'; "
        "frame-ancestors 'none'; "
        "base-uri 'self'; "
        "form-action 'self'"
    )
    return response


# ══════════════════════════ ERROR HANDLERS ══════════════════════════

@app.errorhandler(400)
def bad_request(e):
    return render_template("error.html", code=400, message="Bad request."), 400

@app.errorhandler(403)
def forbidden(e):
    return render_template("error.html", code=403, message="Access denied."), 403

@app.errorhandler(404)
def not_found(e):
    return render_template("error.html", code=404,
                           message="The page you’re looking for doesn’t exist."), 404

@app.errorhandler(413)
def too_large(e):
    return render_template("error.html", code=413,
                           message="File too large. Maximum size is 2 MB."), 413

@app.errorhandler(500)
def server_error(e):
    return render_template("error.html", code=500,
                           message="Internal server error. Please try again."), 500


# ══════════════════════════ PUBLIC ROUTES ══════════════════════════

@app.route("/style.css")
def public_stylesheet():
    return send_from_directory(os.path.join(app.root_path, "static"), "style.css", mimetype="text/css")


@app.route("/ui_advanced.js")
def public_script():
    return send_from_directory(os.path.join(app.root_path, "static"), "ui_advanced.js", mimetype="application/javascript")


@app.route("/__version")
def deployment_version():
    """Non-sensitive marker to verify which deployment is serving requests."""
    return jsonify({"version": "V52", "application": "Management System", "email_campaign_center": True}), 200


@app.route("/")
def index():
    """Landing page — redirect to dashboard if already logged in."""
    if session.get("user_id"):
        return redirect(url_for("dashboard"))
    return render_template("landing.html")


@app.route("/login", methods=["GET", "POST"])
def login_page():
    """Login form."""
    if session.get("user_id"):
        return redirect(url_for("dashboard"))

    if request.method == "GET":
        return render_template("login.html")

    username = request.form.get("username", "").strip()
    password = request.form.get("password", "")

    if not username or not password:
        flash("Username and password are required.", "error")
        return render_template("login.html"), 400

    def do_login():
        return User.query.filter(
            db.func.lower(User.username) == username.lower()
        ).first()

    user = db_retry(do_login)

    if not user:
        log_action(username, "LOGIN_FAILED", "Unknown username or invalid credentials")
        flash("Invalid username or password.", "error")
        return render_template("login.html"), 401

    if user.is_locked():
        log_action(user.username, "LOGIN_FAILED", "Login blocked: account is locked")
        flash(
            f"❌ Account locked. Try again in "
            f"{user.lockout_remaining_minutes()} minute(s).", "error"
        )
        return render_template("login.html"), 403

    if not user.is_active:
        log_action(user.username, "LOGIN_FAILED", "Login blocked: account is inactive")
        flash("Your account is inactive. Contact an administrator.", "error")
        return render_template("login.html"), 403

    if not check_password_hash(user.password_hash, password):
        user.record_failed_attempt()
        db.session.commit()
        remaining = MAX_FAILED_ATTEMPTS - user.failed_attempts
        if remaining > 0:
            flash(f"Invalid username or password. {remaining} attempt(s) remaining.",
                  "error")
        else:
            flash(f"❌ Account locked for {LOCKOUT_MINUTES} minutes due to too many failed attempts.",
                  "error")
        log_action(user.username, "LOGIN_FAILED", f"Invalid password; {max(0, remaining)} attempt(s) remaining")
        return render_template("login.html"), 401

    # Admin sign-in requires a second factor sent to the owner mailbox.
    # Do not create an authenticated session until the OTP is verified.
    if user.role == "admin":
        owner_email = os.environ.get("ADMIN_EMAIL", "").strip()
        if not owner_email:
            app.logger.error("Admin 2-step login blocked: ADMIN_EMAIL is not configured.")
            flash("Admin security email is not configured. Set ADMIN_EMAIL in Vercel Environment Variables.", "error")
            return render_template("login.html"), 503
        if not _email_configured():
            app.logger.error("Admin 2-step login blocked: email provider is not configured.")
            flash("Admin email verification is not configured. Set Brevo or SMTP environment variables in Vercel.", "error")
            return render_template("login.html"), 503
        otp = f"{secrets.randbelow(1000000):06d}"
        try:
            send_otp_email(owner_email, otp, "Admin", purpose="admin_login")
        except Exception as exc:
            app.logger.error("Admin OTP delivery failed: %s", type(exc).__name__)
            flash("Could not send the admin verification email. Check Vercel email settings and try again.", "error")
            return render_template("login.html"), 503

        user.unlock()
        db.session.commit()
        session.clear()
        session.permanent = True
        session["pending_admin_otp"] = {
            "user_id": user.id,
            "otp_hash": hashlib.sha256(otp.encode("utf-8")).hexdigest(),
            "expires_at": int(time.time()) + OTP_EXPIRY_MINUTES * 60,
            "attempts": 0,
            "sent_at": int(time.time()),
        }
        log_action(user.username, "ADMIN_OTP_SENT", "Admin second-factor code sent to configured owner mailbox")
        flash(f"Verification code sent to owner email {mask_email(owner_email)}. It expires in {OTP_EXPIRY_MINUTES} minutes.", "success")
        return redirect(url_for("admin_login_verify"))

    # Non-admin accounts keep the normal password-only login flow.
    user.unlock()
    user.last_login = datetime.utcnow()
    db.session.commit()

    session.clear()
    session.permanent = True
    session["user_id"]   = user.id
    session["username"]  = user.username
    session["name"]      = user.name
    session["role"]      = user.role
    log_action(user.username, "LOGIN_SUCCESS", f"Successful login | role={user.role}")

    return redirect(url_for("dashboard"))


@app.route("/admin-login/verify", methods=["GET", "POST"])
def admin_login_verify():
    """Second-factor verification for admin accounts only."""
    pending = session.get("pending_admin_otp")
    if not pending:
        flash("Please enter your admin username and password first.", "error")
        return redirect(url_for("login_page"))

    owner_email = os.environ.get("ADMIN_EMAIL", "").strip()
    if request.method == "GET":
        if int(time.time()) > int(pending.get("expires_at", 0)):
            session.pop("pending_admin_otp", None)
            flash("The admin verification code expired. Please sign in again.", "error")
            return redirect(url_for("login_page"))
        return render_template("admin_login_verify.html", owner_email=mask_email(owner_email), expiry_minutes=OTP_EXPIRY_MINUTES)

    action = request.form.get("action", "verify")
    now = int(time.time())
    if action == "resend":
        if now - int(pending.get("sent_at", 0)) < OTP_RESEND_SECONDS:
            flash("Please wait a moment before requesting another code.", "error")
            return redirect(url_for("admin_login_verify"))
        if not owner_email or not _email_configured():
            flash("Admin email delivery is not configured. Check Vercel environment variables.", "error")
            return redirect(url_for("login_page"))
        otp = f"{secrets.randbelow(1000000):06d}"
        try:
            send_otp_email(owner_email, otp, "Admin", purpose="admin_login")
        except Exception as exc:
            app.logger.error("Admin OTP resend failed: %s", type(exc).__name__)
            flash("Could not resend the code. Check email configuration and try again.", "error")
            return redirect(url_for("admin_login_verify"))
        pending.update({"otp_hash": hashlib.sha256(otp.encode("utf-8")).hexdigest(), "expires_at": now + OTP_EXPIRY_MINUTES * 60, "attempts": 0, "sent_at": now})
        session["pending_admin_otp"] = pending
        flash("A new verification code was sent to the owner email.", "success")
        return redirect(url_for("admin_login_verify"))

    code = re.sub(r"\s+", "", request.form.get("otp", ""))
    if now > int(pending.get("expires_at", 0)):
        session.pop("pending_admin_otp", None)
        flash("The verification code expired. Please sign in again.", "error")
        return redirect(url_for("login_page"))
    if int(pending.get("attempts", 0)) >= OTP_MAX_ATTEMPTS:
        session.pop("pending_admin_otp", None)
        flash("Too many incorrect codes. Please sign in again.", "error")
        return redirect(url_for("login_page"))
    candidate_hash = hashlib.sha256(code.encode("utf-8")).hexdigest()
    if not re.fullmatch(r"\d{6}", code) or not secrets.compare_digest(candidate_hash, pending.get("otp_hash", "")):
        pending["attempts"] = int(pending.get("attempts", 0)) + 1
        session["pending_admin_otp"] = pending
        remaining = max(0, OTP_MAX_ATTEMPTS - pending["attempts"])
        flash(f"Incorrect verification code. {remaining} attempt(s) remaining.", "error")
        return render_template("admin_login_verify.html", owner_email=mask_email(owner_email), expiry_minutes=OTP_EXPIRY_MINUTES), 401

    admin = db.session.get(User, int(pending.get("user_id", 0)))
    session.pop("pending_admin_otp", None)
    if not admin or not admin.is_active or admin.role != "admin":
        session.clear()
        flash("Admin account is no longer active. Contact the system owner.", "error")
        return redirect(url_for("login_page"))

    admin.unlock()
    admin.last_login = datetime.utcnow()
    db.session.commit()
    session.clear()
    session.permanent = True
    session["user_id"] = admin.id
    session["username"] = admin.username
    session["name"] = admin.name
    session["role"] = admin.role
    log_action(admin.username, "LOGIN_SUCCESS", "Admin login completed after email OTP verification")
    flash("Two-step verification complete. Welcome to the Admin Panel.", "success")
    return redirect(url_for("dashboard"))


@app.route("/register/check-username", methods=["POST"])
def check_registration_username():
    username = request.form.get("username", "").strip()
    valid = bool(re.fullmatch(r"[a-zA-Z0-9_.\-]+", username)) and not is_reserved_username(username)
    exists = bool(username and User.query.filter(db.func.lower(User.username) == username.casefold()).first())
    return jsonify({"available": bool(valid and not exists)})


@app.route("/register/check-email", methods=["POST"])
def check_registration_email():
    email = request.form.get("email", "").strip().lower()
    valid = bool(re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email))
    exists = bool(email and User.query.filter(db.func.lower(User.email) == email.casefold()).first())
    return jsonify({"available": bool(valid and not exists)})


@app.route("/register", methods=["GET", "POST"])
def register():
    if session.get("user_id"):
        return redirect(url_for("dashboard"))

    pending_email = session.get("pending_registration_email", "")
    otp_sent = bool(pending_email)

    if request.method == "GET":
        resend_wait = 0
        if pending_email:
            pending_row = EmailVerification.query.filter(
                db.func.lower(EmailVerification.email) == pending_email.casefold()
            ).order_by(EmailVerification.created_at.desc(), EmailVerification.id.desc()).first()
            if pending_row and pending_row.last_sent_at:
                elapsed = max(0, int((_utcnow_naive() - pending_row.last_sent_at).total_seconds()))
                resend_wait = max(0, OTP_RESEND_SECONDS - elapsed)
        return render_template("register.html", otp_sent=otp_sent, pending_email=pending_email, resend_wait=resend_wait)

    name       = request.form.get("name", "").strip()
    gender     = request.form.get("gender", "").strip()
    department = "Unassigned"
    email      = request.form.get("email", "").strip().lower()
    username   = request.form.get("username", "").strip()
    password   = request.form.get("password", "")
    confirm    = request.form.get("confirm", "")

    def err(msg):
        flash(msg, "error")
        return render_template("register.html", otp_sent=False, pending_email="", resend_wait=0), 400

    if not all([name, gender, email, username, password, confirm]):
        return err("All required fields must be completed.")
    if not _email_configured():
        return err("Email verification is not configured yet. Please contact the administrator.")
    if gender not in VALID_GENDERS:
        return err("Invalid gender selected.")
    if not re.match(r'^[^@]+@[^@]+\.[^@]+$', email):
        return err("Invalid email address.")
    if not re.match(r'^[a-zA-Z0-9_.\-]+$', username):
        return err("Username may only contain letters, numbers, underscores, hyphens, dots.")
    if is_reserved_username(username):
        return err("That username is reserved. Please choose another.")
    if password != confirm:
        return err("Passwords do not match.")
    ok, pw_err = validate_password(password)
    if not ok:
        return err(pw_err)

    # Final availability checks are server-side. Do not return any other user's data.
    if User.query.filter(db.func.lower(User.username) == username.casefold()).first() or User.query.filter(db.func.lower(User.email) == email.casefold()).first():
        return err("That username or email cannot be used. Please try a different one.")

    try:
        create_email_otp(email, name, gender, department, username, generate_password_hash(password))
        session["pending_registration_email"] = email
        flash("📧 Verification code sent to your email. Check your Gmail inbox.", "success")
        return render_template("register.html", otp_sent=True, pending_email=email, resend_wait=OTP_RESEND_SECONDS)
    except ValueError as exc:
        db.session.rollback()
        return err(str(exc))
    except Exception:
        db.session.rollback()
        app.logger.exception("Registration OTP request failed")
        return err("We could not send the verification email right now. Please check the email service configuration or try again later.")


@app.route("/register/verify-otp", methods=["POST"])
def verify_registration_otp():
    email = request.form.get("email", "").strip().lower()
    otp = request.form.get("otp", "").strip()
    if not email or not re.fullmatch(r"\d{6}", otp):
        flash("Enter the 6-digit verification code.", "error")
        return redirect(url_for("register"))

    row = EmailVerification.query.filter(db.func.lower(EmailVerification.email) == email.casefold()).order_by(EmailVerification.created_at.desc(), EmailVerification.id.desc()).first()
    if not row:
        flash("Verification session expired. Please request a new code.", "error")
        return redirect(url_for("register"))

    latest_for_username = EmailVerification.query.filter(
        db.func.lower(EmailVerification.username) == row.username.casefold()
    ).order_by(EmailVerification.created_at.desc(), EmailVerification.id.desc()).first()
    if latest_for_username and latest_for_username.id != row.id:
        flash("A newer registration request exists for this username. Use the latest email code.", "error")
        return redirect(url_for("register"))

    now = _utcnow_naive()
    if row.expires_at < now:
        db.session.delete(row)
        db.session.commit()
        session.pop("pending_registration_email", None)
        flash("⏰ Verification code expired. Please request a new code.", "error")
        return redirect(url_for("register"))
    if row.attempts >= OTP_MAX_ATTEMPTS:
        flash("Too many incorrect attempts. Please request a new code.", "error")
        return redirect(url_for("register"))

    if not check_password_hash(row.otp_hash, otp):
        row.attempts += 1
        db.session.commit()
        remaining = max(0, OTP_MAX_ATTEMPTS - row.attempts)
        flash("❌ Invalid verification code." + (f" {remaining} attempt(s) remaining." if remaining else " Please request a new code."), "error")
        return redirect(url_for("register"))

    # Re-check uniqueness immediately before account creation to close race conditions.
    if (User.query.filter(db.func.lower(User.username) == row.username.casefold()).first() or
        User.query.filter(db.func.lower(User.email) == row.email.casefold()).first()):
        db.session.delete(row)
        db.session.commit()
        session.pop("pending_registration_email", None)
        flash("That username or email cannot be used. Please choose different details.", "error")
        return redirect(url_for("register"))

    try:
        emp_id = next_employee_id(row.department)
        user = User(
            employee_id=emp_id, name=row.name, gender=row.gender, department=row.department,
            email=row.email, username=row.username, password_hash=row.password_hash, role="user", is_active=True
        )
        db.session.add(user)
        db.session.delete(row)
        db.session.commit()
        log_action(user.username, "REGISTER", f"Verified email and registered: {user.name} | {user.department} | EMP#{emp_id}")
        session.pop("pending_registration_email", None)
        welcome_email_sent = False
        try:
            send_account_created_email(user.email, user.name, user.username)
            welcome_email_sent = True
        except Exception as email_exc:
            # The account is already committed. An email delivery failure must not undo registration.
            app.logger.warning("Account created, but welcome email delivery failed (%s).", type(email_exc).__name__)
        session["registration_success"] = {
            "username": user.username,
            "employee_id": user.employee_id,
            "welcome_email_sent": welcome_email_sent,
        }
        return redirect(url_for("registration_success"))
    except IntegrityError:
        db.session.rollback()
        flash("Account could not be created. Please try again.", "error")
        return redirect(url_for("register"))


@app.route("/register/success", methods=["GET"])
def registration_success():
    """One-time post-registration success screen, shown only after a committed account creation."""
    result = session.pop("registration_success", None)
    if not result or not result.get("username"):
        return redirect(url_for("register"))
    return render_template(
        "account_success.html",
        username=result["username"],
        employee_id=result.get("employee_id"),
        welcome_email_sent=bool(result.get("welcome_email_sent")),
    )


@app.route("/register/resend-otp", methods=["POST"])
def resend_registration_otp():
    email = request.form.get("email", "").strip().lower()
    row = EmailVerification.query.filter(db.func.lower(EmailVerification.email) == email.casefold()).order_by(EmailVerification.created_at.desc(), EmailVerification.id.desc()).first() if email else None
    if not row:
        flash("Verification session expired. Please start registration again.", "error")
        return redirect(url_for("register"))
    try:
        create_email_otp(row.email, row.name, row.gender, row.department, row.username, row.password_hash)
        session["pending_registration_email"] = row.email
        flash("📧 A new verification code has been sent.", "success")
    except ValueError as exc:
        db.session.rollback()
        flash(str(exc), "error")
    except Exception:
        db.session.rollback()
        app.logger.exception("Registration OTP resend failed")
        flash("We could not resend the verification email. Please check the email service configuration or try again later.", "error")
    return redirect(url_for("register"))


@app.route("/logout", methods=["POST"])
@login_required
def logout():
    username = session.get("username", "unknown")
    log_action(username, "LOGOUT", "User logged out")
    session.clear()
    flash("✅ You have been securely logged out.", "success")
    return redirect(url_for("login_page"))


@app.route("/auth-status")
def auth_status():
    resp = jsonify({"authenticated": bool(session.get("user_id"))})
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.route("/public-stats")
def public_stats():
    """Return only aggregate, non-sensitive live landing-page statistics."""
    def query():
        today = date.today()
        total = User.query.count()
        active = User.query.filter_by(is_active=True).count()
        departments = db.session.query(User.department).distinct().count()
        new_today = User.query.filter(db.func.date(User.created_at) == today).count()
        registrations_7d = User.query.filter(db.func.date(User.created_at) >= today - timedelta(days=6)).count()
        registrations_30d = User.query.filter(db.func.date(User.created_at) >= today - timedelta(days=29)).count()
        last_7_days = []
        for offset in range(6, -1, -1):
            day = today - timedelta(days=offset)
            count = User.query.filter(db.func.date(User.created_at) == day).count()
            last_7_days.append({"label": day.strftime("%a"), "date": day.isoformat(), "count": count})
        return {
            "total_users": total,
            "active_users": active,
            "departments": departments,
            "new_today": new_today,
            "registrations_7d": registrations_7d,
            "registrations_30d": registrations_30d,
            "last_7_days": last_7_days,
            "updated_at": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
    try:
        data = db_retry(query)
        resp = jsonify(data)
        resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        return resp
    except Exception:
        resp = jsonify({"error": "stats_unavailable"}), 503
        return resp


@app.route("/health")
@app.route("/healthz")
def health():
    """Minimal readiness check; never expose database credentials/errors publicly."""
    try:
        db.session.execute(text("SELECT 1"))
        return jsonify({"status": "ok", "database": "ok"}), 200
    except Exception:
        db.session.rollback()
        app.logger.exception("Health check database query failed")
        return jsonify({"status": "error", "database": "unavailable"}), 503


# ═════════════════════════ DASHBOARD ══════════════════════════

@app.route("/dashboard")
@login_required
def dashboard():
    role         = session["role"]
    current_name = session["name"]
    current_user = session["username"]

    if role != "admin":
        return render_template("dashboard.html",
                               role=role,
                               current_name=current_name,
                               current_username=current_user,
                               book_count=Book.query.count())

    # Admin-only data
    search      = request.args.get("q",      "").strip()
    dept_filter = request.args.get("dept",   "").strip()
    status_filter = request.args.get("status", "").strip()
    page        = request.args.get("page",   1, type=int)

    (total_users, active_users, inactive_users,
     locked_users, today_registrations, departments,
     weekly_registrations, weekly_max) = user_stats()

    def build_query():
        q = User.query
        if search:
            pat = f"%{search}%"
            q = q.filter(
                db.or_(User.name.ilike(pat), User.email.ilike(pat),
                        User.username.ilike(pat), User.employee_id.ilike(pat))
            )
        if dept_filter:
            q = q.filter_by(department=dept_filter)
        if status_filter == "active":
            q = q.filter(User.is_active == True)
        elif status_filter == "inactive":
            q = q.filter(User.is_active == False)
        elif status_filter == "locked":
            q = q.filter(User.locked_until > datetime.utcnow())
        return q.order_by(User.created_at.desc())

    pagination = db_retry(lambda: build_query().paginate(page=page, per_page=15, error_out=False))
    users      = pagination.items

    recent_logs = AuditLog.query.order_by(AuditLog.timestamp.desc()).limit(12).all()
    all_departments = VALID_DEPARTMENTS

    return render_template("dashboard.html",
                           role=role,
                           current_name=current_name,
                           current_username=current_user,
                           users=users,
                           pagination=pagination,
                           total_users=total_users,
                           active_users=active_users,
                           inactive_users=inactive_users,
                           locked_users=locked_users,
                           today_registrations=today_registrations,
                           departments=departments,
                           weekly_registrations=weekly_registrations,
                           weekly_max=weekly_max,
                           recent_logs=recent_logs,
                           search=search,
                           dept_filter=dept_filter,
                           status_filter=status_filter,
                           all_departments=all_departments,
                           admin_pending_email=session.get("admin_pending_email", ""))


# ═════════════════════════ BOOK CATALOG ═════════════════════════
def staff_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("user_id"):
            flash("Please login to access the book catalog.", "error")
            return redirect(url_for("login_page"))
        if session.get("role") not in {"admin", "librarian"}:
            flash("Book management is available to Librarian and Admin accounts.", "error")
            return redirect(url_for("books_catalog"))
        return f(*args, **kwargs)
    return decorated


@app.route("/books")
@login_required
def books_catalog():
    q = request.args.get("q", "").strip()
    category = request.args.get("category", "").strip()
    query = Book.query
    if q:
        pat = f"%{q}%"
        query = query.filter(db.or_(Book.title.ilike(pat), Book.author.ilike(pat), Book.isbn.ilike(pat)))
    if category:
        query = query.filter_by(category=category)
    books = query.order_by(Book.created_at.desc()).limit(250).all()
    categories = [row[0] for row in db.session.query(Book.category).distinct().order_by(Book.category).all() if row[0]]
    return render_template("books_manage.html", books=books, q=q, category=category,
                           categories=categories, can_manage=session.get("role") in {"admin", "librarian"},
                           current_name=session.get("name", "Member"), role=session.get("role", "user"))


@app.route("/books/add", methods=["GET", "POST"])
@staff_required
def add_book():
    if request.method == "POST":
        title = request.form.get("title", "").strip()
        author = request.form.get("author", "").strip()
        category = request.form.get("category", "General").strip() or "General"
        isbn = request.form.get("isbn", "").strip() or None
        publisher = request.form.get("publisher", "").strip() or None
        year_text = request.form.get("publication_year", "").strip()
        copies_text = request.form.get("total_copies", "1").strip()
        description = request.form.get("description", "").strip() or None
        cover_data = request.form.get("cover_data", "").strip() or None
        if not title or not author:
            flash("Book title and author are required.", "error")
            return render_template("add_book.html", book=None)
        try:
            year = int(year_text) if year_text else None
            copies = int(copies_text)
            if year is not None and not (1000 <= year <= datetime.utcnow().year + 2):
                raise ValueError
            if copies < 1 or copies > 100000:
                raise ValueError
        except ValueError:
            flash("Enter a valid publication year and copy count.", "error")
            return render_template("add_book.html", book=None)
        if cover_data and (not cover_data.startswith("data:image/") or len(cover_data) > 1_300_000):
            flash("Cover must be an image under about 900 KB. Generate a cover or choose a smaller image.", "error")
            return render_template("add_book.html", book=None)
        if isbn and Book.query.filter_by(isbn=isbn).first():
            flash("That ISBN is already in the catalog.", "error")
            return render_template("add_book.html", book=None)
        book = Book(title=title, author=author, category=category, isbn=isbn,
                    publisher=publisher, publication_year=year, total_copies=copies,
                    available_copies=copies, description=description, cover_data=cover_data,
                    created_by=session.get("username", "staff"))
        db.session.add(book)
        db.session.commit()
        log_action(session.get("username", "staff"), "BOOK_CREATED", f"Added book: {title}")
        flash(f'"{title}" was added to the catalog.', "success")
        return redirect(url_for("books_catalog"))
    return render_template("add_book.html", book=None)


@app.route("/books/<int:book_id>/edit", methods=["GET", "POST"])
@staff_required
def edit_book(book_id):
    book = Book.query.get_or_404(book_id)
    if request.method == "POST":
        title = request.form.get("title", "").strip()
        author = request.form.get("author", "").strip()
        if not title or not author:
            flash("Book title and author are required.", "error")
            return render_template("add_book.html", book=book)
        isbn = request.form.get("isbn", "").strip() or None
        if isbn and Book.query.filter(Book.isbn == isbn, Book.id != book.id).first():
            flash("That ISBN is already used by another book.", "error")
            return render_template("add_book.html", book=book)
        try:
            year_text = request.form.get("publication_year", "").strip()
            copies = int(request.form.get("total_copies", book.total_copies))
            year = int(year_text) if year_text else None
            if copies < 1 or copies > 100000 or (year is not None and not (1000 <= year <= datetime.utcnow().year + 2)):
                raise ValueError
        except ValueError:
            flash("Enter a valid publication year and copy count.", "error")
            return render_template("add_book.html", book=book)
        cover_data = request.form.get("cover_data", "").strip()
        if cover_data and (not cover_data.startswith("data:image/") or len(cover_data) > 1_300_000):
            flash("Cover image is too large. Generate a cover or choose a smaller image.", "error")
            return render_template("add_book.html", book=book)
        already_issued = max(0, (book.total_copies or 0) - (book.available_copies or 0))
        book.title = title
        book.author = author
        book.category = request.form.get("category", "General").strip() or "General"
        book.isbn = isbn
        book.publisher = request.form.get("publisher", "").strip() or None
        book.publication_year = year
        book.total_copies = copies
        book.available_copies = max(0, copies - already_issued)
        book.description = request.form.get("description", "").strip() or None
        if cover_data:
            book.cover_data = cover_data
        db.session.commit()
        log_action(session.get("username", "staff"), "BOOK_UPDATED", f"Updated book: {title}")
        flash(f'"{title}" was updated.', "success")
        return redirect(url_for("books_catalog"))
    return render_template("add_book.html", book=book)


@app.route("/books/<int:book_id>/delete", methods=["POST"])
@staff_required
def delete_book(book_id):
    book = Book.query.get_or_404(book_id)
    title = book.title
    db.session.delete(book)
    db.session.commit()
    log_action(session.get("username", "staff"), "BOOK_DELETED", f"Deleted book: {title}")
    flash(f'"{title}" was removed from the catalog.', "success")
    return redirect(url_for("books_catalog"))


# ═════════════════════════ PROFILE ═══════════════════════════

@app.route("/profile")
@login_required
def profile():
    user = User.query.get_or_404(session["user_id"])

    # Account completion is derived only from fields that currently exist in the
    # database, so the premium profile UI does not require a schema migration.
    completion_fields = [
        bool((user.name or "").strip()),
        bool((user.email or "").strip()),
        bool((user.gender or "").strip()),
        bool((user.department or "").strip()),
        bool((user.employee_id or "").strip()),
    ]
    profile_completion = round(sum(completion_fields) / len(completion_fields) * 100)
    missing_items = []
    if not user.name: missing_items.append("Full name")
    if not user.email: missing_items.append("Email address")
    if not user.gender: missing_items.append("Gender")
    if not user.department: missing_items.append("Department")
    if not user.employee_id: missing_items.append("Employee ID")

    security_score = 100 if user.is_active and not user.is_locked() else 60
    return render_template("profile.html",
                           user=user,
                           role=session["role"],
                           valid_departments=VALID_DEPARTMENTS,
                           profile_completion=profile_completion,
                           missing_items=missing_items,
                           security_score=security_score)


@app.route("/profile/edit", methods=["POST"])
@login_required
def edit_profile():
    # Personal information is managed by administrators only.
    # Users must not be able to change name, email, gender or department,
    # even by manually posting to this endpoint.
    if session.get("role") != "admin":
        flash("Personal information can only be changed by an administrator.", "error")
        return redirect(url_for("profile"))

    user       = User.query.get_or_404(session["user_id"])
    name       = request.form.get("name",       "").strip()
    email      = request.form.get("email",      "").strip().lower()
    gender     = request.form.get("gender",     "").strip()
    department = request.form.get("department", "").strip()

    if not all([name, email, gender, department]):
        flash("All fields are required.", "error")
        return redirect(url_for("profile"))
    if gender not in VALID_GENDERS:
        flash("Invalid gender.", "error")
        return redirect(url_for("profile"))
    if department not in VALID_DEPARTMENTS:
        flash("Invalid department.", "error")
        return redirect(url_for("profile"))
    if not re.match(r'^[^@]+@[^@]+\.[^@]+$', email):
        flash("Invalid email address.", "error")
        return redirect(url_for("profile"))

    existing_email = User.query.filter(
        db.func.lower(User.email) == email.casefold(), User.id != user.id
    ).first()
    if existing_email:
        flash("That email is already in use by another account.", "error")
        return redirect(url_for("profile"))

    try:
        old_values = (user.name, user.email, user.gender, user.department)
        user.name       = name
        user.email      = email
        user.gender     = gender
        user.department = department
        db.session.commit()
        session["name"] = name
        log_action(user.username, "PROFILE_EDIT",
                   f"Profile updated | name/email/gender/department changed from {old_values[0]} / {old_values[1]} / {old_values[2]} / {old_values[3]}")
        flash("✅ Profile updated successfully. No Gmail OTP is required for profile edits.", "success")
    except IntegrityError:
        db.session.rollback()
        flash("That email is already in use by another account.", "error")
    return redirect(url_for("profile"))


# ════════════════════════ FORGOT PASSWORD ════════════════════════

@app.route("/forgot-password", methods=["GET", "POST"])
def forgot_password():
    if session.get("user_id"):
        default_username = session.get("username", "")
    else:
        default_username = request.args.get("username", "").strip()

    if request.method == "GET":
        if request.args.get("start_over") == "1":
            session.pop("password_reset_user_id", None)
            session.pop("password_reset_verified", None)
            session.pop("password_reset_masked_email", None)
        username = default_username
        reset_sent = bool(session.get("password_reset_user_id"))
        reset_verified = bool(session.get("password_reset_verified"))
        masked = session.get("password_reset_masked_email", "")
        resend_wait = 0
        reset_uid = session.get("password_reset_user_id")
        if reset_uid:
            latest_reset = PasswordReset.query.filter_by(user_id=reset_uid).order_by(PasswordReset.created_at.desc(), PasswordReset.id.desc()).first()
            if latest_reset and latest_reset.last_sent_at:
                elapsed = (datetime.utcnow() - latest_reset.last_sent_at).total_seconds()
                resend_wait = max(0, OTP_RESEND_SECONDS - int(elapsed))
        return render_template("forgot_password.html", username=username, reset_sent=reset_sent,
                               reset_verified=reset_verified, masked_email=masked, resend_wait=resend_wait)

    username = request.form.get("username", "").strip()
    if not username:
        flash("Enter your username.", "error")
        return render_template("forgot_password.html", username=username, reset_sent=False,
                               reset_verified=False, masked_email=""), 400

    user = User.query.filter(db.func.lower(User.username) == username.casefold()).first()
    if not user:
        # Do not reveal whether a username exists.
        flash("If the account exists, a password reset code will be sent to its registered email.", "success")
        return render_template("forgot_password.html", username=username, reset_sent=False,
                               reset_verified=False, masked_email=""), 200
    if not user.is_active:
        flash("This account is inactive. Please contact an administrator.", "error")
        return render_template("forgot_password.html", username=username, reset_sent=False,
                               reset_verified=False, masked_email=""), 403
    if user.role == "admin":
        flash("The admin password is managed in Vercel Environment Variables. Update ADMIN_PASSWORD there and redeploy.", "error")
        return render_template("forgot_password.html", username=username, reset_sent=False,
                               reset_verified=False, masked_email=""), 403
    if not _email_configured():
        flash("Password reset email is not configured. Please contact the administrator.", "error")
        return render_template("forgot_password.html", username=username, reset_sent=False,
                               reset_verified=False, masked_email=""), 503

    try:
        create_password_reset_otp(user)
        session["password_reset_user_id"] = user.id
        session["password_reset_masked_email"] = mask_email(user.email)
        session["password_reset_verified"] = False
        flash(f"📧 OTP sent to {mask_email(user.email)}. It expires in {OTP_EXPIRY_MINUTES} minutes.", "success")
        log_action(user.username, "PASSWORD_RESET_REQUEST", "Password reset OTP requested")
    except ValueError as exc:
        db.session.rollback()
        flash(str(exc), "error")
    except Exception:
        db.session.rollback()
        app.logger.exception("Password reset OTP request failed")
        flash("We could not send the reset email right now. Please check the email service configuration or try again later.", "error")

    return redirect(url_for("forgot_password"))


@app.route("/forgot-password/resend", methods=["POST"])
def resend_forgot_password_otp():
    user_id = session.get("password_reset_user_id")
    user = User.query.get(user_id) if user_id else None
    if not user:
        session.pop("password_reset_user_id", None)
        session.pop("password_reset_masked_email", None)
        session.pop("password_reset_verified", None)
        flash("Reset session expired. Please start again.", "error")
        return redirect(url_for("forgot_password"))
    if user.role == "admin":
        session.pop("password_reset_user_id", None)
        session.pop("password_reset_masked_email", None)
        session.pop("password_reset_verified", None)
        flash("Admin password is managed in Vercel Environment Variables.", "error")
        return redirect(url_for("forgot_password"))
    try:
        create_password_reset_otp(user)
        session["password_reset_verified"] = False
        flash(f"📧 A new OTP was sent to {mask_email(user.email)}.", "success")
    except ValueError as exc:
        db.session.rollback()
        flash(str(exc), "error")
    except Exception:
        db.session.rollback()
        app.logger.exception("Password reset OTP resend failed")
        flash("We could not resend the reset email. Please check the email service configuration or try again later.", "error")
    return redirect(url_for("forgot_password"))


@app.route("/forgot-password/verify", methods=["POST"])
def verify_forgot_password_otp():
    user_id = session.get("password_reset_user_id")
    otp = request.form.get("otp", "").strip()
    user = User.query.get(user_id) if user_id else None
    row = PasswordReset.query.filter_by(user_id=user_id).order_by(PasswordReset.created_at.desc(), PasswordReset.id.desc()).first() if user_id else None

    if not user or not row or not re.fullmatch(r"\d{6}", otp):
        flash("Enter the 6-digit OTP sent to your registered email.", "error")
        return redirect(url_for("forgot_password"))
    now = _utcnow_naive()
    if row.expires_at < now:
        db.session.delete(row)
        db.session.commit()
        session["password_reset_verified"] = False
        flash("⏰ OTP expired. Please request a new code.", "error")
        return redirect(url_for("forgot_password"))
    if row.attempts >= OTP_MAX_ATTEMPTS:
        flash("Too many incorrect attempts. Please resend a new OTP.", "error")
        return redirect(url_for("forgot_password"))
    if not check_password_hash(row.otp_hash, otp):
        row.attempts += 1
        db.session.commit()
        remaining = max(0, OTP_MAX_ATTEMPTS - row.attempts)
        flash("❌ Invalid OTP." + (f" {remaining} attempt(s) remaining." if remaining else " Please resend a new code."), "error")
        return redirect(url_for("forgot_password"))

    row.verified_at = now
    db.session.commit()
    session["password_reset_verified"] = True
    flash("✅ OTP verified. Enter your new password below.", "success")
    log_action(user.username, "PASSWORD_RESET_VERIFY", "Password reset OTP verified")
    return redirect(url_for("forgot_password"))


@app.route("/forgot-password/reset", methods=["POST"])
def reset_password_after_otp():
    user_id = session.get("password_reset_user_id")
    user = User.query.get(user_id) if user_id else None
    row = PasswordReset.query.filter_by(user_id=user_id).order_by(PasswordReset.created_at.desc(), PasswordReset.id.desc()).first() if user_id else None
    if not user or not row or not session.get("password_reset_verified") or not row.verified_at:
        flash("Please verify the OTP first.", "error")
        return redirect(url_for("forgot_password"))
    if user.role == "admin":
        flash("Admin password is managed in Vercel Environment Variables.", "error")
        return redirect(url_for("forgot_password"))
    if row.expires_at < _utcnow_naive() or row.email.casefold() != user.email.casefold():
        db.session.delete(row)
        db.session.commit()
        session.pop("password_reset_verified", None)
        session.pop("password_reset_user_id", None)
        session.pop("password_reset_masked_email", None)
        flash("The verification code has expired. Please request a new OTP.", "error")
        return redirect(url_for("forgot_password"))

    new_pw = request.form.get("new_password", "")
    confirm = request.form.get("confirm_password", "")
    if new_pw != confirm:
        flash("New passwords do not match.", "error")
        return redirect(url_for("forgot_password"))
    ok, pw_err = validate_password(new_pw)
    if not ok:
        flash(pw_err, "error")
        return redirect(url_for("forgot_password"))
    if check_password_hash(user.password_hash, new_pw):
        flash("New password must be different from the current password.", "error")
        return redirect(url_for("forgot_password"))

    user.password_hash = generate_password_hash(new_pw)
    user.unlock()
    db.session.delete(row)
    db.session.commit()
    log_action(user.username, "PASSWORD_RESET", "Password reset completed after email OTP verification")
    session.clear()
    flash("✅ Password reset successfully. Please sign in with your new password.", "success")
    return redirect(url_for("login_page"))


# ════════════════════════ CHANGE PASSWORD ════════════════════════

@app.route("/change-password", methods=["GET", "POST"])
@login_required
def change_password():
    if session.get("role") == "admin":
        flash("Admin password is managed only in Vercel Environment Variables.", "error")
        return redirect(url_for("dashboard"))
    user = User.query.get_or_404(session["user_id"])
    if request.method == "GET":
        return render_template("change_password.html",
                               prefill_username=user.username, current_email=user.email)

    current  = request.form.get("current_password",  "")
    new_pw   = request.form.get("new_password",      "")
    confirm  = request.form.get("confirm_password",  "")

    def err(msg):
        flash(msg, "error")
        return render_template("change_password.html",
                               prefill_username=user.username, current_email=user.email), 400

    if not check_password_hash(user.password_hash, current):
        return err("Current password is incorrect.")
    if new_pw != confirm:
        return err("New passwords do not match.")
    ok, pw_err = validate_password(new_pw)
    if not ok:
        return err(pw_err)
    if check_password_hash(user.password_hash, new_pw):
        return err("New password must be different from the current password.")

    user.password_hash = generate_password_hash(new_pw)
    db.session.commit()
    log_action(user.username, "PASSWORD_CHANGE", "User password changed successfully")
    session.clear()
    flash("✅ Password changed successfully. Please log in again.", "success")
    return redirect(url_for("login_page"))


# ═════════════════════ EMAIL CAMPAIGN CENTER (ADMIN) ═════════════════════

CAMPAIGN_PRESETS = {
    "announcement": ("Important announcement", "We have an update to share with you.\n\nPlease sign in to the Management System to review the latest information."),
    "welcome": ("Welcome to the Management System!", "Welcome aboard! Your account is ready. Sign in to explore your dashboard, account details, and available library services."),
    "come_back": ("Your Management System account is ready", "It has been a little while since your last visit. Sign in to check your dashboard and see what is new."),
    "update": ("A new system update is available", "We have made an update to the Management System. Please sign in to review the latest changes."),
    "reminder": ("A quick reminder from the Management System", "This is a friendly reminder to review any pending items or upcoming due dates in your account."),
    "maintenance": ("Scheduled maintenance notice", "The Management System may be temporarily unavailable during scheduled maintenance. Thank you for your patience."),
    "custom": ("A message from the Management System", "Write your message here. You can edit this message before sending."),
}


def _parse_campaign_schedule(raw_value):
    """Interpret browser datetime-local input in India time, store naive UTC in DB."""
    try:
        local_dt = datetime.fromisoformat((raw_value or "").strip())
    except (TypeError, ValueError):
        raise ValueError("Choose a valid scheduled date and time.")
    if local_dt.tzinfo is not None:
        local_dt = local_dt.astimezone(ZoneInfo("Asia/Kolkata")).replace(tzinfo=None)
    local_dt = local_dt.replace(tzinfo=ZoneInfo("Asia/Kolkata"))
    if local_dt <= datetime.now(ZoneInfo("Asia/Kolkata")):
        raise ValueError("Scheduled time must be in the future (India time).")
    return local_dt.astimezone(timezone.utc).replace(tzinfo=None)


def _valid_campaign_url(value):
    value = (value or "").strip()
    if not value:
        return ""
    if len(value) > 500 or not value.startswith(("https://", "http://")) or "\r" in value or "\n" in value:
        raise ValueError("CTA link must be a valid full http:// or https:// URL.")
    return value


def _campaign_email_counts(campaign):
    rows = db.session.query(EmailCampaignRecipient.status, db.func.count(EmailCampaignRecipient.id)).filter_by(
        campaign_id=campaign.id
    ).group_by(EmailCampaignRecipient.status).all()
    counts = {status: int(count) for status, count in rows}
    return {"sent": counts.get("sent", 0), "failed": counts.get("failed", 0), "pending": counts.get("pending", 0)}


@app.route("/admin/email-campaigns", methods=["GET"])
@admin_required
def email_campaigns():
    users = User.query.filter(User.email.isnot(None)).order_by(User.name.asc()).all()
    campaigns = EmailCampaign.query.order_by(EmailCampaign.created_at.desc(), EmailCampaign.id.desc()).limit(100).all()
    campaign_ids = [campaign.id for campaign in campaigns]
    count_map = {campaign_id: {"sent": 0, "failed": 0, "pending": 0} for campaign_id in campaign_ids}
    if campaign_ids:
        summary_rows = db.session.query(
            EmailCampaignRecipient.campaign_id, EmailCampaignRecipient.status,
            db.func.count(EmailCampaignRecipient.id)
        ).filter(EmailCampaignRecipient.campaign_id.in_(campaign_ids)).group_by(
            EmailCampaignRecipient.campaign_id, EmailCampaignRecipient.status
        ).all()
        for campaign_id, status, count in summary_rows:
            if status in {"sent", "failed", "pending"}:
                count_map[campaign_id][status] = int(count)
    for campaign in campaigns:
        campaign.report_counts = count_map[campaign.id]
    detail_id = request.args.get("detail", type=int)
    detail_campaign = EmailCampaign.query.get(detail_id) if detail_id else None
    detail_recipients = []
    if detail_campaign:
        detail_recipients = EmailCampaignRecipient.query.filter_by(campaign_id=detail_campaign.id).order_by(
            EmailCampaignRecipient.status.asc(), EmailCampaignRecipient.email.asc()).limit(500).all()
    app_url = _campaign_base_url()
    login_url = app_url + url_for("login_page") if app_url else url_for("login_page", _external=True)
    eligible_users = [u for u in users if re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", (u.email or "").strip()) and not (u.email or "").strip().casefold().endswith(".local")]
    eligible_active_users = [u for u in eligible_users if u.is_active]
    return render_template(
        "email_campaigns.html", current_name=session.get("name", "Admin"), users=users,
        eligible_count=len(eligible_users), eligible_active_count=len(eligible_active_users),
        campaigns=campaigns, detail_campaign=detail_campaign, detail_recipients=detail_recipients,
        login_url=login_url, provider=_campaign_provider_name(), submission_key=secrets.token_urlsafe(28),
        now_ist=datetime.now(ZoneInfo("Asia/Kolkata")).strftime("%Y-%m-%dT%H:%M"),
        utc_tz=timezone.utc, ist_tz=ZoneInfo("Asia/Kolkata"),
    )


@app.route("/admin/email-campaigns/create", methods=["POST"])
@admin_required
def create_email_campaign():
    subject = request.form.get("subject", "").strip()
    message = request.form.get("message", "").strip()
    template = request.form.get("template", "announcement").strip()
    mode = request.form.get("recipient_mode", "all").strip()
    action = request.form.get("campaign_action", "send_now").strip()
    submit_key = request.form.get("submission_key", "").strip()[:80]
    cta_label = request.form.get("cta_label", "Sign in to Management System").strip()[:80]
    try:
        cta_url = _valid_campaign_url(request.form.get("cta_url", ""))
    except ValueError as exc:
        flash(str(exc), "error")
        return redirect(url_for("email_campaigns"))
    if not submit_key:
        submit_key = secrets.token_urlsafe(24)
    existing = EmailCampaign.query.filter_by(submission_key=submit_key).first()
    if existing:
        flash("This form was already submitted. Open the campaign report to view the original result.", "success")
        return redirect(url_for("email_campaigns", detail=existing.id))
    if not subject or len(subject) > 200 or "\r" in subject or "\n" in subject:
        flash("Enter an email subject of 1–200 characters.", "error")
        return redirect(url_for("email_campaigns"))
    if not message or len(message) > 12000:
        flash("Enter an email message (maximum 12,000 characters).", "error")
        return redirect(url_for("email_campaigns"))
    if template not in CAMPAIGN_PRESETS:
        template = "custom"
    if mode not in {"all", "active", "selected", "single"}:
        flash("Choose a valid recipient group.", "error")
        return redirect(url_for("email_campaigns"))

    if mode == "all":
        selected_users = User.query.filter(User.email.isnot(None)).order_by(User.id.asc()).all()
    elif mode == "active":
        selected_users = User.query.filter_by(is_active=True).filter(User.email.isnot(None)).order_by(User.id.asc()).all()
    else:
        raw_ids = request.form.getlist("selected_user_ids")
        if mode == "single":
            raw_ids = [request.form.get("single_user_id", "").strip()]
        ids = []
        for raw in raw_ids:
            try:
                uid = int(raw)
                if uid > 0 and uid not in ids:
                    ids.append(uid)
            except (ValueError, TypeError):
                continue
        if not ids:
            flash("Select at least one user first.", "error")
            return redirect(url_for("email_campaigns"))
        selected_users = User.query.filter(User.id.in_(ids), User.email.isnot(None)).order_by(User.id.asc()).all()
        if mode == "single" and len(selected_users) != 1:
            flash("Choose one valid user for the single-recipient option.", "error")
            return redirect(url_for("email_campaigns"))

    # Emails are case-insensitively deduplicated before the campaign is persisted.
    unique_users = {}
    for user in selected_users:
        email = (user.email or "").strip()
        if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email) or email.casefold().endswith(".local"):
            continue
        unique_users.setdefault(email.casefold(), user)
    if not unique_users:
        flash("No valid email recipients were found in this selection.", "error")
        return redirect(url_for("email_campaigns"))
    try:
        recipient_cap = max(1, min(int(os.environ.get("EMAIL_CAMPAIGN_MAX_RECIPIENTS", "5000")), 50000))
    except ValueError:
        recipient_cap = 5000
    if len(unique_users) > recipient_cap:
        flash(f"This campaign has {len(unique_users)} recipients, above the configured safety cap of {recipient_cap}. Increase EMAIL_CAMPAIGN_MAX_RECIPIENTS only if you intend to send to a larger list.", "error")
        return redirect(url_for("email_campaigns"))

    scheduled_at = None
    status = "queued"
    if action == "schedule":
        try:
            scheduled_at = _parse_campaign_schedule(request.form.get("scheduled_at", ""))
            status = "scheduled"
        except ValueError as exc:
            flash(str(exc), "error")
            return redirect(url_for("email_campaigns"))
    elif action != "send_now":
        flash("Choose Send now or Schedule email.", "error")
        return redirect(url_for("email_campaigns"))
    elif mode in {"all", "active"} and len(unique_users) >= 10:
        try:
            cooldown = max(0, min(int(os.environ.get("EMAIL_CAMPAIGN_BROADCAST_COOLDOWN_SECONDS", "60")), 3600))
        except ValueError:
            cooldown = 60
        if cooldown:
            recent_broadcast = EmailCampaign.query.filter(
                EmailCampaign.created_by == session.get("username", "admin"),
                EmailCampaign.test_only.is_(False),
                EmailCampaign.recipient_mode.in_(["all", "active"]),
                EmailCampaign.scheduled_at.is_(None),
                EmailCampaign.status != "cancelled",
                EmailCampaign.created_at >= datetime.utcnow() - timedelta(seconds=cooldown),
            ).first()
            if recent_broadcast:
                flash(f"To prevent accidental duplicate announcements, wait {cooldown} seconds between immediate broadcasts. You can schedule the next one instead.", "error")
                return redirect(url_for("email_campaigns", detail=recent_broadcast.id))

    if not cta_url and cta_label:
        cta_url = _campaign_base_url() + url_for("login_page")
    campaign = EmailCampaign(
        submission_key=submit_key, subject=subject, message=message, template=template,
        cta_label=cta_label or None, cta_url=cta_url or None, recipient_mode=mode,
        status=status, created_by=session.get("username", "admin"), scheduled_at=scheduled_at,
        total_recipients=len(unique_users),
    )
    db.session.add(campaign)
    try:
        db.session.flush()
    except IntegrityError:
        db.session.rollback()
        duplicate = EmailCampaign.query.filter_by(submission_key=submit_key).first()
        if duplicate:
            flash("This campaign form was already submitted. The existing report is shown; no duplicate campaign was created.", "success")
            return redirect(url_for("email_campaigns", detail=duplicate.id))
        app.logger.exception("Email campaign insert failed due to a database integrity constraint")
        flash("The campaign could not be saved because of a database constraint. No duplicate send was started.", "error")
        return redirect(url_for("email_campaigns"))
    for user in unique_users.values():
        db.session.add(EmailCampaignRecipient(
            campaign_id=campaign.id, user_id=user.id, email=user.email.strip(),
            recipient_name=(user.name or user.username or "there")[:120], status="pending",
        ))
    db.session.commit()
    log_action(session.get("username", "admin"), "ADMIN_EMAIL_CAMPAIGN_CREATE",
               f"Campaign {campaign.id} created; mode={mode}; recipients={campaign.total_recipients}; status={status}")
    if status == "queued":
        result = _process_campaign_batch(campaign.id)
        flash(f"Campaign #{campaign.id} queued. First batch processed: {result.get('processed', 0)}; status: {result.get('status', 'queued')}. Open the report to continue or retry failed recipients.", "success")
    else:
        flash(f"Campaign #{campaign.id} scheduled for {scheduled_at.replace(tzinfo=timezone.utc).astimezone(ZoneInfo('Asia/Kolkata')).strftime('%d %b %Y, %I:%M %p')} IST. Automatic delivery requires the configured scheduler to be active.", "success")
    return redirect(url_for("email_campaigns", detail=campaign.id))


@app.route("/admin/email-campaigns/test", methods=["POST"])
@admin_required
def send_test_campaign_email():
    email = request.form.get("test_to", "").strip()
    subject = request.form.get("subject", "").strip()[:200]
    message = request.form.get("message", "").strip()[:12000]
    cta_label = request.form.get("cta_label", "Sign in to Management System").strip()[:80]
    try:
        cta_url = _valid_campaign_url(request.form.get("cta_url", ""))
    except ValueError as exc:
        flash(str(exc), "error")
        return redirect(url_for("email_campaigns"))
    if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email):
        flash("Enter a valid test email address.", "error")
        return redirect(url_for("email_campaigns"))
    if not subject or not message:
        flash("Enter a subject and message before sending a test.", "error")
        return redirect(url_for("email_campaigns"))
    if not cta_url and cta_label:
        cta_url = _campaign_base_url() + url_for("login_page")
    campaign = EmailCampaign(
        submission_key=secrets.token_urlsafe(28), subject=subject, message=message,
        template="test", cta_label=cta_label or None, cta_url=cta_url or None,
        recipient_mode="test", status="queued", created_by=session.get("username", "admin"),
        total_recipients=1, test_only=True,
    )
    db.session.add(campaign)
    db.session.flush()
    db.session.add(EmailCampaignRecipient(campaign_id=campaign.id, email=email, recipient_name="Admin / Test recipient", status="pending"))
    db.session.commit()
    result = _process_campaign_batch(campaign.id, batch_size=1)
    campaign = db.session.get(EmailCampaign, campaign.id)
    campaign.status = "test_sent" if campaign.sent_count == 1 else "test_failed"
    db.session.commit()
    log_action(session.get("username", "admin"), "ADMIN_EMAIL_TEST", f"Test email campaign {campaign.id}; status={campaign.status}")
    if campaign.status == "test_sent":
        flash(f"Test email was accepted by the email provider and recorded for {email}.", "success")
    else:
        flash("Test email failed. Open its report to see the delivery error and verify your Brevo/SMTP environment variables.", "error")
    return redirect(url_for("email_campaigns", detail=campaign.id))


@app.route("/admin/email-campaigns/<int:campaign_id>/process", methods=["POST"])
@admin_required
def process_email_campaign(campaign_id):
    campaign = EmailCampaign.query.get_or_404(campaign_id)
    result = _process_campaign_batch(campaign.id)
    flash(f"Batch processed: {result.get('processed', 0)} recipient(s). Campaign status: {result.get('status', 'unknown')}.", "success")
    return redirect(url_for("email_campaigns", detail=campaign.id))


@app.route("/admin/email-campaigns/<int:campaign_id>/retry", methods=["POST"])
@admin_required
def retry_email_campaign(campaign_id):
    campaign = EmailCampaign.query.get_or_404(campaign_id)
    failed = EmailCampaignRecipient.query.filter_by(campaign_id=campaign.id, status="failed").all()
    if not failed:
        flash("This campaign has no failed recipients to retry.", "error")
        return redirect(url_for("email_campaigns", detail=campaign.id))
    for recipient in failed:
        recipient.status = "pending"
        recipient.error = None
        recipient.sent_at = None
    campaign.status = "queued"
    campaign.claimed_at = None
    campaign.sent_at = None
    campaign.failed_count = 0
    db.session.commit()
    result = _process_campaign_batch(campaign.id)
    log_action(session.get("username", "admin"), "ADMIN_EMAIL_CAMPAIGN_RETRY", f"Campaign {campaign.id}; retry count={len(failed)}")
    flash(f"Retry batch processed: {result.get('processed', 0)}. Remaining retry recipients stay queued.", "success")
    return redirect(url_for("email_campaigns", detail=campaign.id))


@app.route("/admin/email-campaigns/<int:campaign_id>/schedule", methods=["POST"])
@admin_required
def update_email_campaign_schedule(campaign_id):
    campaign = EmailCampaign.query.get_or_404(campaign_id)
    if campaign.status != "scheduled":
        flash("Only scheduled, not-yet-sent campaigns can be rescheduled.", "error")
        return redirect(url_for("email_campaigns", detail=campaign.id))
    try:
        campaign.scheduled_at = _parse_campaign_schedule(request.form.get("scheduled_at", ""))
        db.session.commit()
        flash("Campaign schedule updated successfully.", "success")
    except ValueError as exc:
        db.session.rollback()
        flash(str(exc), "error")
    return redirect(url_for("email_campaigns", detail=campaign.id))


@app.route("/admin/email-campaigns/<int:campaign_id>/cancel", methods=["POST"])
@admin_required
def cancel_email_campaign(campaign_id):
    campaign = EmailCampaign.query.get_or_404(campaign_id)
    if campaign.status != "scheduled":
        flash("Only a scheduled campaign can be cancelled. Already queued/sent emails cannot be recalled.", "error")
        return redirect(url_for("email_campaigns", detail=campaign.id))
    campaign.status = "cancelled"
    campaign.scheduled_at = None
    db.session.commit()
    log_action(session.get("username", "admin"), "ADMIN_EMAIL_CAMPAIGN_CANCEL", f"Campaign {campaign.id} cancelled")
    flash("Scheduled campaign cancelled. No recipients were emailed by this campaign.", "success")
    return redirect(url_for("email_campaigns", detail=campaign.id))


@app.route("/api/cron/email-campaigns", methods=["GET"])
def process_scheduled_email_campaigns():
    """Vercel/external cron endpoint. Secured using CRON_SECRET; never exposes user data."""
    expected = os.environ.get("CRON_SECRET", "").strip()
    supplied = request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
    if not expected or not supplied or not secrets.compare_digest(expected, supplied):
        return jsonify({"ok": False, "error": "Unauthorized"}), 401
    now = datetime.utcnow()
    # A timed-out worker may have left a campaign in 'sending'; recover it after five minutes.
    EmailCampaign.query.filter(EmailCampaign.status == "sending", EmailCampaign.claimed_at < now - timedelta(minutes=5)).update(
        {"status": "queued", "claimed_at": None}, synchronize_session=False)
    EmailCampaign.query.filter(EmailCampaign.status == "scheduled", EmailCampaign.scheduled_at <= now).update(
        {"status": "queued"}, synchronize_session=False)
    db.session.commit()
    due = EmailCampaign.query.filter(EmailCampaign.status == "queued").order_by(EmailCampaign.created_at.asc()).limit(3).all()
    results = []
    for campaign in due:
        results.append({"campaign_id": campaign.id, **_process_campaign_batch(campaign.id)})
    db.session.remove()
    return jsonify({"ok": True, "checked_at_utc": now.isoformat() + "Z", "campaigns": results}), 200


# ════════════════════════ ANALYTICS (admin) ═════════════════════

@app.route("/admin/analytics")
@admin_required
def analytics():
    total, active, inactive, locked, _, dept_count, _, _ = user_stats()

    # Department breakdown
    today = date.today()
    dept_rows = db.session.query(
        User.department, db.func.count(User.id)
    ).group_by(User.department).all()
    dept_breakdown = []
    for dept, cnt in sorted(dept_rows, key=lambda x: -x[1]):
        pct = round(cnt / total * 100, 1) if total else 0
        dept_breakdown.append({"name": dept, "count": cnt, "pct": pct})

    # Monthly 6-month trend
    monthly = []
    for offset in range(5, -1, -1):
        first = (today.replace(day=1) - timedelta(days=offset * 28)).replace(day=1)
        if first.month == 12:
            last = first.replace(year=first.year + 1, month=1, day=1) - timedelta(days=1)
        else:
            last = first.replace(month=first.month + 1, day=1) - timedelta(days=1)
        cnt = User.query.filter(
            db.func.date(User.created_at) >= first,
            db.func.date(User.created_at) <= last
        ).count()
        monthly.append({"label": first.strftime("%b"), "count": cnt})
    monthly_max = max((m["count"] for m in monthly), default=0) or 1

    # This month
    first_of_month = today.replace(day=1)
    month_reg = User.query.filter(
        db.func.date(User.created_at) >= first_of_month
    ).count()

    # Recent 10 users
    recent_users = User.query.order_by(User.created_at.desc()).limit(10).all()

    return render_template("analytics.html",
                           current_name=session["name"],
                           total=total, active=active, inactive=inactive,
                           locked=locked, dept_count=dept_count,
                           month_reg=month_reg,
                           dept_breakdown=dept_breakdown,
                           monthly=monthly, monthly_max=monthly_max,
                           recent_users=recent_users)


# ════════════════════════ SETTINGS (admin) ══════════════════════

@app.route("/admin/settings")
@admin_required
def settings():
    db_url = app.config["SQLALCHEMY_DATABASE_URI"]
    db_type = "PostgreSQL" if db_url.startswith("postgresql") else "SQLite"
    return render_template("settings.html",
                           current_name=session["name"],
                           db_type=db_type,
                           total_users=User.query.count(),
                           total_logs=AuditLog.query.count(),
                           server_time=datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC"),
                           max_attempts=MAX_FAILED_ATTEMPTS,
                           lockout_mins=LOCKOUT_MINUTES,
                           valid_departments=VALID_DEPARTMENTS,
                           admin_username=os.environ.get("ADMIN_USERNAME", "admin").strip())


@app.route("/admin/settings/clear-logs", methods=["POST"])
@admin_required
def clear_logs():
    try:
        deleted = AuditLog.query.delete()
        db.session.commit()
        flash(f"✅ {deleted} audit log records have been permanently deleted.", "success")
    except Exception as e:
        db.session.rollback()
        flash(f"Error clearing logs: {str(e)}", "error")
    return redirect(url_for("settings"))


# ════════════════════════ AUDIT LOGS (admin) ════════════════════

@app.route("/admin/audit-logs")
@admin_required
def audit_logs():
    search = request.args.get("q", "").strip()
    action_filter = request.args.get("action", "").strip().upper()
    page = request.args.get("page", 1, type=int)

    q = AuditLog.query
    if search:
        pat = f"%{search}%"
        q = q.filter(db.or_(AuditLog.username.ilike(pat),
                            AuditLog.action.ilike(pat),
                            AuditLog.details.ilike(pat),
                            AuditLog.ip_address.ilike(pat)))

    if action_filter == "LOGIN_SUCCESS":
        q = q.filter(AuditLog.action == "LOGIN_SUCCESS")
    elif action_filter == "LOGIN_FAILED":
        q = q.filter(AuditLog.action == "LOGIN_FAILED")
    elif action_filter == "LOGOUT":
        q = q.filter(AuditLog.action == "LOGOUT")
    elif action_filter == "REGISTER":
        q = q.filter(AuditLog.action == "REGISTER")
    elif action_filter == "ADMIN":
        q = q.filter(AuditLog.action.like("ADMIN_%"))
    elif action_filter == "PASSWORD":
        q = q.filter(AuditLog.action.like("PASSWORD_%"))
    else:
        action_filter = ""

    pagination = q.order_by(AuditLog.timestamp.desc()).paginate(
        page=page, per_page=50, error_out=False)
    total_logs = AuditLog.query.count()

    return render_template("audit_logs.html",
                           current_name=session["name"],
                           logs=pagination.items,
                           pagination=pagination,
                           total_logs=total_logs,
                           search=search,
                           action_filter=action_filter)


# ════════════════════════ ADMIN USER MGMT ══════════════════════

@app.route("/admin/users/create", methods=["POST"])
@admin_required
def create_user():
    # Admin-created accounts use the same email-verification security as public registration.
    # The account is NOT inserted into User until the OTP is verified.
    name       = request.form.get("name",       "").strip()
    gender     = request.form.get("gender",     "").strip()
    department = "Unassigned"
    email      = request.form.get("email",      "").strip().lower()
    username   = request.form.get("username",   "").strip()
    password   = request.form.get("password",   "")
    confirm    = request.form.get("confirm",    "")

    def err(msg):
        flash(msg, "error")
        return redirect(url_for("dashboard"))

    if not all([name, gender, email, username, password, confirm]):
        return err("All fields are required.")
    if not _email_configured():
        return err("Email verification is not configured. Add the Brevo environment variables first.")
    if gender not in VALID_GENDERS:
        return err("Invalid gender.")
    if not re.match(r'^[^@]+@[^@]+\.[^@]+$', email):
        return err("Invalid email address.")
    if not re.match(r'^[a-zA-Z0-9_.\-]+$', username):
        return err("Username may only contain letters, numbers, underscores, hyphens, dots.")
    if is_reserved_username(username):
        return err("That username is reserved.")
    if password != confirm:
        return err("Passwords do not match.")
    ok, pw_err = validate_password(password)
    if not ok:
        return err(pw_err)

    # Never overwrite an existing account. Pending OTP records are also kept isolated
    # from this admin flow by requiring the same pending email in the current admin session.
    if (User.query.filter(db.func.lower(User.username) == username.casefold()).first() or
        User.query.filter(db.func.lower(User.email) == email.casefold()).first()):
        return err("That username or email already exists. Please choose different details.")

    existing_pending = EmailVerification.query.filter(
        db.func.lower(EmailVerification.email) == email.casefold()
    ).first()
    if existing_pending and session.get("admin_pending_email", "").casefold() != email.casefold():
        return err("This email already has a pending verification. Complete it or wait for it to expire.")

    try:
        create_email_otp(email, name, gender, department, username, generate_password_hash(password))
        session["admin_pending_email"] = email
        flash("📧 Verification code sent to the new user's email. Enter the OTP to create the account.", "success")
    except ValueError as exc:
        db.session.rollback()
        return err(str(exc))
    except Exception:
        db.session.rollback()
        return err("We could not send the verification email right now. Please try again later.")
    return redirect(url_for("dashboard"))


@app.route("/admin/users/verify-otp", methods=["POST"])
@admin_required
def verify_admin_user_otp():
    email = request.form.get("email", "").strip().lower()
    otp = request.form.get("otp", "").strip()
    pending = session.get("admin_pending_email", "").strip().lower()

    if not email or email != pending or not re.fullmatch(r"\d{6}", otp):
        flash("Enter the 6-digit verification code for the pending new user.", "error")
        return redirect(url_for("dashboard"))

    row = EmailVerification.query.filter(
        db.func.lower(EmailVerification.email) == email.casefold()
    ).first()
    if not row:
        session.pop("admin_pending_email", None)
        flash("Verification session expired. Please create the user again.", "error")
        return redirect(url_for("dashboard"))

    now = _utcnow_naive()
    if row.expires_at < now:
        db.session.delete(row)
        db.session.commit()
        session.pop("admin_pending_email", None)
        flash("⏰ Verification code expired. Please create the user again.", "error")
        return redirect(url_for("dashboard"))
    if row.attempts >= OTP_MAX_ATTEMPTS:
        flash("Too many incorrect attempts. Please resend a new verification code.", "error")
        return redirect(url_for("dashboard"))

    if not check_password_hash(row.otp_hash, otp):
        row.attempts += 1
        db.session.commit()
        remaining = max(0, OTP_MAX_ATTEMPTS - row.attempts)
        flash("❌ Invalid verification code." + (f" {remaining} attempt(s) remaining." if remaining else " Please resend a new code."), "error")
        return redirect(url_for("dashboard"))

    if (User.query.filter(db.func.lower(User.username) == row.username.casefold()).first() or
        User.query.filter(db.func.lower(User.email) == row.email.casefold()).first()):
        db.session.delete(row)
        db.session.commit()
        session.pop("admin_pending_email", None)
        flash("That username or email is no longer available. Please create the user again.", "error")
        return redirect(url_for("dashboard"))

    try:
        emp_id = next_employee_id(row.department)
        user = User(
            employee_id=emp_id, name=row.name, gender=row.gender,
            department=row.department, email=row.email, username=row.username,
            password_hash=row.password_hash, role="user", is_active=True
        )
        db.session.add(user)
        db.session.delete(row)
        db.session.commit()
        log_action(user.username, "ADMIN_CREATE",
                   f"Admin {session.get('username','admin')} created user after verified email: {user.name} | {user.department} | EMP#{emp_id}")
        session.pop("admin_pending_email", None)
        flash(f"✅ Email verified! User ‘{user.username}’ ({emp_id}) created successfully.", "success")
    except IntegrityError:
        db.session.rollback()
        flash("Account could not be created because the username/email is already in use.", "error")
    return redirect(url_for("dashboard"))


@app.route("/admin/users/resend-otp", methods=["POST"])
@admin_required
def resend_admin_user_otp():
    email = request.form.get("email", "").strip().lower()
    pending = session.get("admin_pending_email", "").strip().lower()
    if not email or email != pending:
        flash("No matching pending admin user verification was found.", "error")
        return redirect(url_for("dashboard"))

    row = EmailVerification.query.filter(
        db.func.lower(EmailVerification.email) == email.casefold()
    ).first()
    if not row:
        session.pop("admin_pending_email", None)
        flash("Verification session expired. Please create the user again.", "error")
        return redirect(url_for("dashboard"))
    try:
        create_email_otp(row.email, row.name, row.gender, row.department, row.username, row.password_hash)
        flash("📧 A new verification code has been sent.", "success")
    except ValueError as exc:
        db.session.rollback()
        flash(str(exc), "error")
    except Exception:
        db.session.rollback()
        flash("We could not resend the verification email. Please try again later.", "error")
    return redirect(url_for("dashboard"))


@app.route("/admin/users/<int:user_id>/edit", methods=["POST"])
@admin_required
def edit_user(user_id):
    user       = User.query.get_or_404(user_id)
    if user.role == "admin":
        flash("Admin username and password are managed only in Vercel Environment Variables.", "error")
        return redirect(url_for("dashboard"))
    name       = request.form.get("name",       "").strip()
    gender     = request.form.get("gender",     "").strip()
    department = request.form.get("department", "").strip()
    email      = request.form.get("email",      "").strip().lower()
    username   = request.form.get("username",   "").strip()
    password   = request.form.get("password",   "")
    confirm    = request.form.get("confirm",    "")

    def err(msg):
        flash(msg, "error")
        return redirect(url_for("dashboard"))

    if not all([name, gender, department, email, username]):
        return err("Required fields are missing.")
    if gender not in VALID_GENDERS:
        return err("Invalid gender.")
    if department not in VALID_DEPARTMENTS:
        return err("Invalid department.")
    if not re.match(r'^[a-zA-Z0-9_.\-]+$', username):
        return err("Invalid username characters.")

    try:
        user.name       = name
        user.gender     = gender
        user.department = department
        user.email      = email
        user.username   = username
        if password:
            if password != confirm:
                return err("Passwords do not match.")
            ok, pw_err = validate_password(password)
            if not ok:
                return err(pw_err)
            user.password_hash = generate_password_hash(password)
        db.session.commit()
        log_action(session.get("username", "admin"), "ADMIN_EDIT",
                   f"Edited user {username} | {department}")
        flash(f"✅ User ‘{username}’ updated.", "success")
    except IntegrityError:
        db.session.rollback()
        flash("Username or email already exists.", "error")
    return redirect(url_for("dashboard"))


@app.route("/admin/users/<int:user_id>/role", methods=["POST"])
@admin_required
def change_user_role(user_id):
    """Allow an administrator to promote/demote non-admin accounts only."""
    user = db.session.get(User, user_id)
    if not user:
        flash("User not found.", "error")
        return redirect(url_for("dashboard"))
    if user.role == "admin":
        flash("The environment-managed admin role cannot be changed here.", "error")
        return redirect(url_for("dashboard"))
    new_role = request.form.get("role", "").strip().lower()
    if new_role not in {"user", "librarian"}:
        flash("Choose a valid role.", "error")
        return redirect(url_for("dashboard"))
    old_role = user.role
    user.role = new_role
    db.session.commit()
    log_action(session.get("username", "admin"), "ADMIN_ROLE_CHANGE",
               f"Changed role for {user.username}: {old_role} -> {new_role}")
    flash(f"{user.username} is now {'Librarian' if new_role == 'librarian' else 'User'}.", "success")
    return redirect(url_for("dashboard"))


@app.route("/admin/users/<int:user_id>/delete", methods=["POST"])
@admin_required
def delete_user(user_id):
    user = User.query.get_or_404(user_id)
    if user.role == "admin":
        flash("The environment-managed admin account cannot be deleted.", "error")
        return redirect(url_for("dashboard"))
    if user.id == session["user_id"]:
        flash("You cannot delete your own account.", "error")
        return redirect(url_for("dashboard"))
    uname = user.username
    db.session.delete(user)
    db.session.commit()
    log_action(session.get("username", "admin"), "ADMIN_DELETE", f"Deleted user {uname}")
    flash(f"✅ User ‘{uname}’ deleted.", "success")
    return redirect(url_for("dashboard"))


@app.route("/admin/users/<int:user_id>/toggle-status", methods=["POST"])
@admin_required
def toggle_user_status(user_id):
    user = User.query.get_or_404(user_id)
    if user.role == "admin":
        flash("The environment-managed admin account cannot be deactivated.", "error")
        return redirect(url_for("dashboard"))
    if user.id == session["user_id"]:
        flash("You cannot deactivate your own account.", "error")
        return redirect(url_for("dashboard"))
    user.is_active = not user.is_active
    db.session.commit()
    status = "activated" if user.is_active else "deactivated"
    log_action(session.get("username", "admin"), "ADMIN_STATUS", f"{status.title()} user {user.username}")
    flash(f"✅ User ‘{user.username}’ {status}.", "success")
    return redirect(url_for("dashboard"))


@app.route("/admin/users/<int:user_id>/unlock", methods=["POST"])
@admin_required
def unlock_user(user_id):
    user = User.query.get_or_404(user_id)
    user.unlock()
    db.session.commit()
    log_action(session.get("username", "admin"), "ADMIN_UNLOCK", f"Unlocked user {user.username}")
    flash(f"✅ User ‘{user.username}’ has been unlocked.", "success")
    return redirect(url_for("dashboard"))


@app.route("/export/users")
@admin_required
def export_users():
    users = User.query.order_by(User.created_at.desc()).all()
    output = io.StringIO()
    writer = csv.writer(output)

    def csv_safe(value):
        value = "" if value is None else str(value)
        if value.startswith(("=", "+", "-", "@", "\t", "\r")):
            return "'" + value
        return value
    writer.writerow([
        "Employee ID", "Name", "Gender", "Department",
        "Email", "Username", "Role", "Active", "Created At"
    ])
    for u in users:
        writer.writerow([
            csv_safe(u.employee_id), csv_safe(u.name), csv_safe(u.gender), csv_safe(u.department),
            csv_safe(u.email), csv_safe(u.username), csv_safe(u.role),
            "Yes" if u.is_active else "No",
            u.created_at.strftime("%Y-%m-%d %H:%M") if u.created_at else "",
        ])
    resp = Response(
        output.getvalue(),
        mimetype="text/csv",
        headers={
            "Content-Disposition":
                f'attachment; filename="users_{date.today().isoformat()}.csv"'
        },
    )
    return resp


# ══════════════════════════ INIT & RUN ══════════════════════════

def _columns_for(table_name):
    inspector = inspect(db.engine)
    if not inspector.has_table(table_name):
        return set()
    return {column["name"] for column in inspector.get_columns(table_name)}


def _add_missing_columns(table_name, definitions):
    """Add only absent columns. Definitions are hard-coded internal SQL, never user input."""
    columns = _columns_for(table_name)
    for column_name, sql_definition in definitions.items():
        if column_name not in columns:
            effective_definition = sql_definition
            sqlite_timestamp_default = (
                db.engine.dialect.name == "sqlite" and "DEFAULT CURRENT_TIMESTAMP" in sql_definition.upper()
            )
            # SQLite disallows non-constant defaults on ALTER TABLE ADD COLUMN.
            if sqlite_timestamp_default:
                effective_definition = "TIMESTAMP NULL"
            add_if_missing = " IF NOT EXISTS" if db.engine.dialect.name == "postgresql" else ""
            db.session.execute(text(
                f'ALTER TABLE "{table_name}" ADD COLUMN{add_if_missing} "{column_name}" {effective_definition}'
            ))
            if sqlite_timestamp_default:
                db.session.execute(text(
                    f'UPDATE "{table_name}" SET "{column_name}" = CURRENT_TIMESTAMP WHERE "{column_name}" IS NULL'
                ))
            db.session.commit()
            app.logger.warning("Schema migration added %s.%s", table_name, column_name)
            columns.add(column_name)
    return columns


def _ensure_unique_index(table_name, column_name):
    """Create a unique index only when existing data is already unique."""
    index_name = f"uq_{table_name}_{column_name}_v20"
    inspector = inspect(db.engine)
    for idx in inspector.get_indexes(table_name):
        if idx.get("unique") and idx.get("column_names") == [column_name]:
            return
    for constraint in inspector.get_unique_constraints(table_name):
        if constraint.get("column_names") == [column_name]:
            return
    duplicates = db.session.execute(text(
        f'SELECT "{column_name}", COUNT(*) FROM "{table_name}" '
        f'WHERE "{column_name}" IS NOT NULL GROUP BY "{column_name}" HAVING COUNT(*) > 1 LIMIT 1'
    )).first()
    if duplicates:
        raise RuntimeError(
            f"Cannot safely migrate {table_name}.{column_name}: duplicate values exist. "
            "Resolve duplicates before deployment; existing records were not deleted."
        )
    db.session.execute(text(
        f'CREATE UNIQUE INDEX IF NOT EXISTS "{index_name}" ON "{table_name}" ("{column_name}")'
    ))
    db.session.commit()


def _ensure_casefold_unique_index(table_name, column_name):
    """Protect case-insensitive lookups from legacy duplicates without deleting records."""
    index_name = f"uq_{table_name}_{column_name}_lower_v23"
    duplicates = db.session.execute(text(
        f'SELECT lower("{column_name}"), COUNT(*) FROM "{table_name}" '
        f'WHERE "{column_name}" IS NOT NULL GROUP BY lower("{column_name}") '
        'HAVING COUNT(*) > 1 LIMIT 1'
    )).first()
    if duplicates:
        raise RuntimeError(
            f"Cannot safely create case-insensitive uniqueness for {table_name}.{column_name}: "
            "case variants of the same value already exist. No rows were deleted; back up the database "
            "and reconcile the duplicate accounts before retrying."
        )
    db.session.execute(text(
        f'CREATE UNIQUE INDEX IF NOT EXISTS "{index_name}" ON "{table_name}" (lower("{column_name}"))'
    ))
    db.session.commit()


def _migrate_existing_schema():
    """Additively reconcile the known legacy Neon user schema without dropping rows."""
    user_cols = _columns_for("user")
    # The current Neon table uses user_id as its primary key. Map that physical
    # column to the ORM attribute User.id; keep compatibility with older `id` schemas.
    user_pk_col = "user_id" if "user_id" in user_cols and "id" not in user_cols else "id"
    critical = {user_pk_col, "name", "gender", "email", "username", "password_hash"}
    missing_critical = sorted(critical - user_cols)
    if missing_critical:
        raise RuntimeError(
            "Existing user table is missing required columns: " + ", ".join(missing_critical) +
            ". No records were deleted. Back up the database and verify the legacy schema before retrying."
        )

    user_defs = {
        "employee_id": "VARCHAR(20)",
        # Older accounts did not have a department. A neutral value preserves access.
        "department": "VARCHAR(80) NOT NULL DEFAULT 'Unassigned'",
        "role": "VARCHAR(20) NOT NULL DEFAULT 'user'",
        "is_active": "BOOLEAN NOT NULL DEFAULT TRUE",
        "failed_attempts": "INTEGER NOT NULL DEFAULT 0",
        "locked_until": "TIMESTAMP NULL",
        "last_login": "TIMESTAMP NULL",
        "created_at": "TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP",
    }
    user_cols = _add_missing_columns("user", user_defs)
    # Existing rows receive deterministic IDs only when employee_id is absent.
    rows = db.session.execute(text(
        f'SELECT "{user_pk_col}" FROM "user" '
        'WHERE "employee_id" IS NULL OR "employee_id" = \'\' '
        f'ORDER BY "{user_pk_col}"'
    )).fetchall()
    used_ids = {str(value) for (value,) in db.session.execute(
        text('SELECT "employee_id" FROM "user" WHERE "employee_id" IS NOT NULL AND "employee_id" != \'\'')
    ).fetchall()}
    for (uid,) in rows:
        base = f"MIG-{uid}"
        candidate = base[:20]
        suffix = 1
        while candidate in used_ids:
            tail = f"-{suffix}"
            candidate = base[:20-len(tail)] + tail
            suffix += 1
        db.session.execute(text(
            f'UPDATE "user" SET "employee_id" = :eid WHERE "{user_pk_col}" = :uid'
        ), {"eid": candidate, "uid": uid})
        used_ids.add(candidate)
    db.session.execute(text("UPDATE \"user\" SET \"role\" = 'user' WHERE \"role\" IS NULL OR \"role\" = ''"))
    db.session.execute(text('UPDATE "user" SET "is_active" = TRUE WHERE "is_active" IS NULL'))
    db.session.execute(text('UPDATE "user" SET "failed_attempts" = 0 WHERE "failed_attempts" IS NULL'))
    db.session.execute(text('UPDATE "user" SET "created_at" = CURRENT_TIMESTAMP WHERE "created_at" IS NULL'))
    db.session.commit()
    for col in ("username", "email", "employee_id"):
        _ensure_unique_index("user", col)
    _ensure_casefold_unique_index("user", "username")
    _ensure_casefold_unique_index("user", "email")

    # Repair legacy book tables additively. Older production databases may predate
    # several Book model fields, not just category. Add every non-primary-key model
    # column that can safely be introduced without deleting or rewriting book rows.
    # Required title/author/id columns are not fabricated here because their values
    # cannot be reconstructed safely; existing catalog schemas are expected to have them.
    if inspect(db.engine).has_table("book"):
        _add_missing_columns("book", {
            "category": "VARCHAR(80) NOT NULL DEFAULT 'General'",
            "isbn": "VARCHAR(32) NULL",
            "publisher": "VARCHAR(140) NULL",
            "publication_year": "INTEGER NULL",
            "total_copies": "INTEGER NOT NULL DEFAULT 1",
            "available_copies": "INTEGER NOT NULL DEFAULT 1",
            "description": "TEXT NULL",
            "cover_data": "TEXT NULL",
            "created_by": "VARCHAR(80) NOT NULL DEFAULT 'system'",
            "created_at": "TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP",
        })
        # Older deployments may have a legacy `price` column marked NOT NULL.
        # This app's current Book form/model does not collect a price, so that
        # leftover constraint blocks inserts. Keep the column and existing values,
        # but allow NULL for new rows instead of deleting or rewriting book data.
        book_cols_after_migration = _columns_for("book")
        if "price" in book_cols_after_migration and db.engine.dialect.name == "postgresql":
            db.session.execute(text('ALTER TABLE "book" ALTER COLUMN "price" DROP NOT NULL'))
            db.session.commit()
            app.logger.info("Schema migration made legacy book.price nullable")
        # Legacy production tables can also retain a required `stock` column that
        # is no longer part of the current Book model/form. Without a database
        # default, new ORM inserts omit this legacy column and PostgreSQL rejects
        # them with NotNullViolation. Preserve the column and its existing values,
        # while defaulting it for new catalog rows.
        if "stock" in book_cols_after_migration and db.engine.dialect.name == "postgresql":
            db.session.execute(text('ALTER TABLE "book" ALTER COLUMN "stock" SET DEFAULT 1'))
            db.session.execute(text('UPDATE "book" SET "stock" = 1 WHERE "stock" IS NULL'))
            db.session.commit()
            app.logger.info("Schema migration set a safe default for legacy book.stock")

    # OTP/reset tables can contain in-progress user workflows. Never delete rows during
    # automatic schema repair: if a legacy partial table has malformed records, fail safely
    # and ask for a backup/manual reconciliation instead of silently discarding data.
    if inspect(db.engine).has_table("email_verification"):
        ev_defs = {
            "email": "VARCHAR(160)", "username": "VARCHAR(80)", "name": "VARCHAR(120)",
            "gender": "VARCHAR(30)", "department": "VARCHAR(80)",
            "password_hash": "VARCHAR(255)", "otp_hash": "VARCHAR(255)",
            "expires_at": "TIMESTAMP NULL", "last_sent_at": "TIMESTAMP NULL",
            "attempts": "INTEGER NOT NULL DEFAULT 0", "created_at": "TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP",
        }
        _add_missing_columns("email_verification", ev_defs)
        malformed = db.session.execute(text(
            'SELECT "id" FROM "email_verification" WHERE "email" IS NULL OR "username" IS NULL '
            'OR "name" IS NULL OR "gender" IS NULL OR "department" IS NULL '
            'OR "password_hash" IS NULL OR "otp_hash" IS NULL OR "expires_at" IS NULL '
            'OR "last_sent_at" IS NULL LIMIT 1'
        )).first()
        if malformed:
            raise RuntimeError(
                "The email_verification table contains incomplete legacy rows. "
                "No rows were deleted; back up the database and reconcile those pending OTP rows manually."
            )
        _ensure_unique_index("email_verification", "email")
        _ensure_casefold_unique_index("email_verification", "email")

    if inspect(db.engine).has_table("password_reset"):
        pr_defs = {
            "user_id": "INTEGER", "email": "VARCHAR(160)", "otp_hash": "VARCHAR(255)",
            "expires_at": "TIMESTAMP NULL", "last_sent_at": "TIMESTAMP NULL",
            "attempts": "INTEGER NOT NULL DEFAULT 0", "verified_at": "TIMESTAMP NULL",
            "created_at": "TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP",
        }
        _add_missing_columns("password_reset", pr_defs)
        malformed = db.session.execute(text(
            'SELECT "id" FROM "password_reset" WHERE "user_id" IS NULL OR "email" IS NULL '
            'OR "otp_hash" IS NULL OR "expires_at" IS NULL OR "last_sent_at" IS NULL LIMIT 1'
        )).first()
        if malformed:
            raise RuntimeError(
                "The password_reset table contains incomplete legacy rows. "
                "No rows were deleted; back up the database and reconcile those pending reset rows manually."
            )

    if inspect(db.engine).has_table("audit_log"):
        audit_defs = {
            "username": "VARCHAR(80) NOT NULL DEFAULT 'system'",
            "action": "VARCHAR(80) NOT NULL DEFAULT 'LEGACY'",
            "ip_address": "VARCHAR(45) NULL", "details": "VARCHAR(500) NULL",
            "timestamp": "TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP",
        }
        _add_missing_columns("audit_log", audit_defs)


def _init_db():
    """Create missing tables, add compatible columns, and sync the environment admin."""
    db.create_all()
    _migrate_existing_schema()

    # Do not rewrite existing employee IDs during startup. New IDs are generated by
    # next_employee_id(); legacy IDs remain valid and untouched to preserve user data.

    admin_username = os.environ.get("ADMIN_USERNAME", "").strip()
    admin_password = os.environ.get("ADMIN_PASSWORD", "").strip()
    configured_admin_email = os.environ.get("ADMIN_EMAIL", "").strip()
    admin_email = configured_admin_email or (admin_username + "@system.local")
    if not admin_username or not admin_password:
        if os.environ.get("VERCEL"):
            raise RuntimeError(
                "ADMIN_USERNAME and ADMIN_PASSWORD are required on Vercel. "
                "Set both in Project Settings → Environment Variables, then redeploy."
            )
        app.logger.warning("ADMIN_USERNAME / ADMIN_PASSWORD are not configured; admin bootstrap skipped")
        return
    ok, pw_err = validate_password(admin_password)
    if not ok:
        raise RuntimeError("ADMIN_PASSWORD is too weak: " + pw_err)

    admin = User.query.filter(db.func.lower(User.username) == admin_username.casefold()).first()
    if admin is None:
        if os.environ.get("VERCEL") and not configured_admin_email:
            raise RuntimeError("ADMIN_EMAIL is required on Vercel when creating the environment-managed admin. Set it to a real, verified mailbox.")
        email_owner = User.query.filter(db.func.lower(User.email) == admin_email.casefold()).first()
        if email_owner and email_owner.username.casefold() != admin_username.casefold():
            raise RuntimeError("ADMIN_EMAIL is already assigned to another user. Set a unique admin email or reconcile the account without deleting records.")
        admin = User(
            employee_id=next_employee_id("Administration"), name="System Administrator",
            gender="Prefer not to say", department="Administration", email=admin_email,
            username=admin_username, password_hash=generate_password_hash(admin_password),
            role="admin", is_active=True,
        )
        db.session.add(admin)
        try:
            db.session.commit()
            app.logger.info("Environment-managed admin account created")
        except IntegrityError:
            db.session.rollback()
            admin = User.query.filter(db.func.lower(User.username) == admin_username.casefold()).first()
            if admin is None:
                raise
    else:
        changed = False
        if admin.role != "admin":
            admin.role = "admin"
            changed = True
        if not admin.is_active:
            admin.is_active = True
            changed = True
        if configured_admin_email and admin.email.casefold() != admin_email.casefold():
            email_owner = User.query.filter(
                db.func.lower(User.email) == admin_email.casefold(), User.id != admin.id
            ).first()
            if email_owner:
                raise RuntimeError("ADMIN_EMAIL is already assigned to another user. Set a unique admin email or reconcile the account without deleting records.")
            admin.email = admin_email
            changed = True
        if not check_password_hash(admin.password_hash, admin_password):
            admin.password_hash = generate_password_hash(admin_password)
            changed = True
        if changed:
            db.session.commit()
            app.logger.info("Environment-managed admin account synchronized")


# Vercel imports the Flask app while creating the function.
# Do NOT connect/migrate the database during module import: a transient
# database/network problem must not prevent Vercel from importing the app.
_db_ready = False
_db_init_error = None
_db_init_failed_at = 0.0
_db_init_lock = threading.Lock()
_DB_INIT_RETRY_DELAY_SECONDS = 5.0


def _ensure_db_ready():
    """Initialize once per warm worker, serializing concurrent first requests.

    A short failure cooldown prevents a burst of requests from each retrying
    create_all/schema migrations against an already-saturated Neon connection.
    """
    global _db_ready, _db_init_error, _db_init_failed_at
    if not _database_configured and os.environ.get("VERCEL"):
        _db_init_error = "DATABASE_URL is missing"
        return False
    if _db_ready:
        return True
    if _db_init_error and (time.monotonic() - _db_init_failed_at) < _DB_INIT_RETRY_DELAY_SECONDS:
        return False

    with _db_init_lock:
        # Another request may have finished initialization while we waited.
        if _db_ready:
            return True
        if _db_init_error and (time.monotonic() - _db_init_failed_at) < _DB_INIT_RETRY_DELAY_SECONDS:
            return False
        try:
            with app.app_context():
                _init_db()
            _db_ready = True
            _db_init_error = None
            _db_init_failed_at = 0.0
            return True
        except Exception as exc:
            # Roll back while an application context is active so the cleanup
            # cannot mask the original database error.
            try:
                with app.app_context():
                    db.session.rollback()
            except Exception:
                app.logger.warning("Database session rollback also failed", exc_info=True)
            _db_init_error = str(exc)
            _db_init_failed_at = time.monotonic()
            app.logger.exception("Database initialization failed")
            return False


@app.before_request
def _initialize_database_before_request():
    # Keep the landing page and assets available even during a database outage.
    # These routes must not run schema creation/migrations. The landing page
    # calls /public-stats immediately, so initializing the whole DB from that
    # endpoint caused concurrent cold starts to compete for Neon connections.
    # /public-stats handles query errors itself and returns a safe 503 fallback.
    if request.path in {
        "/", "/style.css", "/ui_advanced.js", "/favicon.ico",
        "/public-stats", "/auth-status",
    } or request.endpoint == "static":
        return None
    if not _ensure_db_ready():
        # Keep API/health responses machine-readable, but show a useful branded
        # diagnostic page to people using the login/registration UI.
        payload = {
            "status": "error",
            "message": "Database is not ready. Check DATABASE_URL and Vercel Function Logs.",
        }
        if (request.path in {"/health", "/healthz", "/public-stats", "/auth-status"}
                or request.accept_mimetypes.best == "application/json"):
            return jsonify(payload), 503
        return render_template(
            "error.html", code=503,
            message="The database is not ready, so sign-in and account actions are temporarily unavailable. "
                    "In Vercel, verify DATABASE_URL points to your existing Neon database, then check Function Logs. "
                    "No database records were intentionally deleted by this request."
        ), 503


if __name__ == '__main__':
    app.run(
        host=os.environ.get('HOST', '0.0.0.0'),
        port=int(os.environ.get('PORT', 5000)),
        debug=os.environ.get('FLASK_DEBUG', '0') == '1',
    )

import os
import csv
import io
import secrets
import re
import json
import html
import urllib.request
import urllib.error
from datetime import date, timedelta, datetime, timezone
from functools import wraps
from flask import (
    Flask, render_template, request, redirect, url_for,
    session, flash, make_response, Response, jsonify, send_from_directory
)
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy import inspect, text
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
app.config["SQLALCHEMY_DATABASE_URI"] = database_url
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

if database_url.startswith(("postgresql://", "postgresql+psycopg2://")):
    is_serverless = bool(os.environ.get("VERCEL"))
    app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {
        "pool_pre_ping": True,
        "pool_recycle": 240,
        "pool_timeout": 10 if is_serverless else 30,
        # Each Vercel instance should hold as few DB connections as possible.
        "pool_size": 1 if is_serverless else 5,
        "max_overflow": 0 if is_serverless else 5,
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
    id              = db.Column(db.Integer, primary_key=True)
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


def send_brevo_otp(email, otp, name, purpose="verification"):
    """Send a Brevo OTP email for registration verification or password reset."""
    api_key = os.environ.get("BREVO_API_KEY", "").strip()
    sender_email = os.environ.get("BREVO_SENDER_EMAIL", "").strip()
    sender_name = os.environ.get("BREVO_SENDER_NAME", "Management System").strip() or "Management System"
    if not api_key or not sender_email:
        raise RuntimeError("Brevo email service is not configured.")

    raw_name = (name or "there").strip()[:120]
    safe_name = html.escape(raw_name, quote=True)
    is_reset = purpose == "password_reset"
    title = "🔐 Reset your password" if is_reset else "🔐 Verify your email"
    intro = ("Use this one-time code to reset your Management System password:"
             if is_reset else
             "Use this one-time verification code to finish creating your Management System account:")
    subject = ("Your Management System password reset code"
               if is_reset else
               "Your Management System verification code")
    plain = (f"Your Management System password reset code is {otp}. It expires in {OTP_EXPIRY_MINUTES} minutes."
             if is_reset else
             f"Your Management System verification code is {otp}. It expires in {OTP_EXPIRY_MINUTES} minutes.")
    html = f"""<!doctype html><html><body style=\"font-family:Arial,sans-serif;background:#f6f7fb;padding:24px\">
      <div style=\"max-width:520px;margin:auto;background:white;border-radius:18px;padding:30px;box-shadow:0 8px 30px rgba(0,0,0,.08)\">
      <h2 style=\"margin-top:0\">{title}</h2>
      <p>Hi {safe_name},</p><p>{intro}</p>
      <div style=\"font-size:32px;font-weight:800;letter-spacing:10px;text-align:center;padding:18px;background:#f1efff;border-radius:14px\">{otp}</div>
      <p style=\"color:#667085\">This code expires in {OTP_EXPIRY_MINUTES} minutes. If you did not request this, you can ignore this email.</p>
      </div></body></html>"""
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
        raise RuntimeError(f"Brevo email send failed ({exc.code}). {detail}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError("Brevo email service is temporarily unavailable.") from exc


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
        send_brevo_otp(user.email, otp, user.name, purpose="password_reset")
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
        send_brevo_otp(email, otp, name)
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
    public_paths = {"/", "/style.css", "/ui_advanced.js", "/favicon.ico"}
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
    return send_from_directory(os.path.join(app.root_path, "public"), "style.css", mimetype="text/css")


@app.route("/ui_advanced.js")
def public_script():
    return send_from_directory(os.path.join(app.root_path, "public"), "ui_advanced.js", mimetype="application/javascript")


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

    # Successful login: reset lockout and persist activity timestamp.
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
        return render_template("register.html", otp_sent=otp_sent, pending_email=pending_email)

    name       = request.form.get("name", "").strip()
    gender     = request.form.get("gender", "").strip()
    department = request.form.get("department", "").strip()
    email      = request.form.get("email", "").strip().lower()
    username   = request.form.get("username", "").strip()
    password   = request.form.get("password", "")
    confirm    = request.form.get("confirm", "")

    def err(msg):
        flash(msg, "error")
        return render_template("register.html", otp_sent=False, pending_email=""), 400

    if not all([name, gender, department, email, username, password, confirm]):
        return err("All fields are required.")
    if not _brevo_configured():
        return err("Email verification is not configured yet. Please contact the administrator.")
    if gender not in VALID_GENDERS:
        return err("Invalid gender selected.")
    if department not in VALID_DEPARTMENTS:
        return err("Invalid department selected.")
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
        return render_template("register.html", otp_sent=True, pending_email=email)
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
        flash("✅ Email verified! Account created successfully. You can now sign in.", "success")
        return redirect(url_for("login_page"))
    except IntegrityError:
        db.session.rollback()
        flash("Account could not be created. Please try again.", "error")
        return redirect(url_for("register"))


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
                               current_username=current_user)

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
        username = default_username
        reset_sent = bool(session.get("password_reset_user_id"))
        reset_verified = bool(session.get("password_reset_verified"))
        masked = session.get("password_reset_masked_email", "")
        return render_template("forgot_password.html", username=username, reset_sent=reset_sent,
                               reset_verified=reset_verified, masked_email=masked)

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
    if not _brevo_configured():
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
    department = request.form.get("department", "").strip()
    email      = request.form.get("email",      "").strip().lower()
    username   = request.form.get("username",   "").strip()
    password   = request.form.get("password",   "")
    confirm    = request.form.get("confirm",    "")

    def err(msg):
        flash(msg, "error")
        return redirect(url_for("dashboard"))

    if not all([name, gender, department, email, username, password, confirm]):
        return err("All fields are required.")
    if not _brevo_configured():
        return err("Email verification is not configured. Add the Brevo environment variables first.")
    if gender not in VALID_GENDERS:
        return err("Invalid gender.")
    if department not in VALID_DEPARTMENTS:
        return err("Invalid department.")
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
    """Repair additive schema drift without dropping/recreating existing user data."""
    user_cols = _columns_for("user")
    critical = {"id", "name", "gender", "department", "email", "username", "password_hash"}
    missing_critical = sorted(critical - user_cols)
    if missing_critical:
        raise RuntimeError(
            "Existing user table is missing essential columns: " + ", ".join(missing_critical) +
            ". Automatic recovery is unsafe because it could make existing accounts inaccessible. "
            "Back up the database and reconcile this legacy schema before retrying."
        )

    user_defs = {
        "employee_id": "VARCHAR(20)",
        "role": "VARCHAR(20) NOT NULL DEFAULT 'user'",
        "is_active": "BOOLEAN NOT NULL DEFAULT TRUE",
        "failed_attempts": "INTEGER NOT NULL DEFAULT 0",
        "locked_until": "TIMESTAMP NULL",
        "last_login": "TIMESTAMP NULL",
        "created_at": "TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP",
    }
    user_cols = _add_missing_columns("user", user_defs)
    # Existing rows get a deterministic, non-sensitive migration ID only when missing.
    rows = db.session.execute(text(
        "SELECT \"id\" FROM \"user\" WHERE \"employee_id\" IS NULL OR \"employee_id\" = '' ORDER BY \"id\""
    )).fetchall()
    used_ids = {str(value) for (value,) in db.session.execute(
        text("SELECT \"employee_id\" FROM \"user\" WHERE \"employee_id\" IS NOT NULL AND \"employee_id\" != ''")
    ).fetchall()}
    for (uid,) in rows:
        base = f"MIG-{uid}"
        candidate = base[:20]
        suffix = 1
        while candidate in used_ids:
            tail = f"-{suffix}"
            candidate = base[:20-len(tail)] + tail
            suffix += 1
        db.session.execute(text('UPDATE "user" SET "employee_id" = :eid WHERE "id" = :uid'),
                           {"eid": candidate, "uid": uid})
        used_ids.add(candidate)
    # Normalize nullable fields from older schemas before the ORM uses them.
    db.session.execute(text("UPDATE \"user\" SET \"role\" = 'user' WHERE \"role\" IS NULL OR \"role\" = ''"))
    db.session.execute(text('UPDATE "user" SET "is_active" = TRUE WHERE "is_active" IS NULL'))
    db.session.execute(text('UPDATE "user" SET "failed_attempts" = 0 WHERE "failed_attempts" IS NULL'))
    db.session.execute(text('UPDATE "user" SET "created_at" = CURRENT_TIMESTAMP WHERE "created_at" IS NULL'))
    db.session.commit()
    for col in ("username", "email", "employee_id"):
        _ensure_unique_index("user", col)
    _ensure_casefold_unique_index("user", "username")
    _ensure_casefold_unique_index("user", "email")

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


def _ensure_db_ready():
    global _db_ready, _db_init_error
    if not _database_configured and os.environ.get("VERCEL"):
        _db_init_error = "DATABASE_URL is missing"
        return False
    if _db_ready:
        return True
    try:
        with app.app_context():
            _init_db()
        _db_ready = True
        _db_init_error = None
        return True
    except Exception as exc:
        db.session.rollback()
        _db_init_error = str(exc)
        app.logger.exception("Database initialization failed")
        return False


@app.before_request
def _initialize_database_before_request():
    # Keep the landing page and assets available even during a database outage.
    if request.path in {"/", "/style.css", "/ui_advanced.js", "/favicon.ico"} or request.endpoint == "static":
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

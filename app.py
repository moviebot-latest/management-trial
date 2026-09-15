import os
import csv
import io
import secrets
import re
from datetime import date, timedelta, datetime, timezone
from functools import wraps
from flask import (
    Flask, render_template, request, redirect, url_for,
    session, flash, make_response, Response, jsonify
)
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy import inspect, text
from werkzeug.security import generate_password_hash, check_password_hash

app = Flask(__name__)

# ── Secret Key ───────────────────────────────────────────────────────
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY")
if not app.config["SECRET_KEY"]:
    raise RuntimeError("SECRET_KEY environment variable is required.")

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
database_url = os.environ.get("DATABASE_URL", "sqlite:///management.db")
if database_url.startswith("postgres://"):
    database_url = database_url.replace("postgres://", "postgresql://", 1)
app.config["SQLALCHEMY_DATABASE_URI"] = database_url
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

if database_url.startswith(("postgresql://", "postgresql+psycopg2://")):
    app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {
        "pool_pre_ping": True, "pool_recycle": 300,
        "pool_timeout": 30, "pool_size": 5, "max_overflow": 5,
        "connect_args": {
            "connect_timeout": 10, "keepalives": 1,
            "keepalives_idle": 30, "keepalives_interval": 10,
            "keepalives_count": 3,
            "sslmode": os.environ.get("PGSSLMODE", "require"),
        },
    }

db = SQLAlchemy(app)

# ════════════════════════════ CONSTANTS ════════════════════════════

MAX_FAILED_ATTEMPTS = 5
LOCKOUT_MINUTES = 15

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
    if "_csrf_token" not in session:
        session["_csrf_token"] = secrets.token_urlsafe(32)
    return session["_csrf_token"]


@app.before_request
def before_request():
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
        log_action(username, "LOGIN_FAILED", "User not found")
        flash("Invalid username or password.", "error")
        return render_template("login.html"), 401

    if user.is_locked():
        log_action(username, "LOGIN_BLOCKED",
                   f"Account locked for {user.lockout_remaining_minutes()} more min")
        flash(
            f"❌ Account locked. Try again in "
            f"{user.lockout_remaining_minutes()} minute(s).", "error"
        )
        return render_template("login.html"), 403

    if not user.is_active:
        log_action(username, "LOGIN_BLOCKED", "Account inactive")
        flash("Your account is inactive. Contact an administrator.", "error")
        return render_template("login.html"), 403

    if not check_password_hash(user.password_hash, password):
        user.record_failed_attempt()
        db.session.commit()
        log_action(username, "LOGIN_FAILED",
                   f"Wrong password (attempt {user.failed_attempts})")
        remaining = MAX_FAILED_ATTEMPTS - user.failed_attempts
        if remaining > 0:
            flash(f"Invalid username or password. {remaining} attempt(s) remaining.",
                  "error")
        else:
            flash(f"❌ Account locked for {LOCKOUT_MINUTES} minutes due to too many failed attempts.",
                  "error")
        return render_template("login.html"), 401

    # ✔ Successful login
    user.unlock()
    user.last_login = datetime.utcnow()
    db.session.commit()

    session.clear()
    session.permanent = True
    session["user_id"]   = user.id
    session["username"]  = user.username
    session["name"]      = user.name
    session["role"]      = user.role

    log_action(user.username, "LOGIN_SUCCESS", f"Role: {user.role}")
    return redirect(url_for("dashboard"))


@app.route("/register", methods=["GET", "POST"])
def register():
    if session.get("user_id"):
        return redirect(url_for("dashboard"))

    if request.method == "GET":
        return render_template("register.html")

    name       = request.form.get("name",       "").strip()
    gender     = request.form.get("gender",     "").strip()
    department = request.form.get("department", "").strip()
    email      = request.form.get("email",      "").strip().lower()
    username   = request.form.get("username",   "").strip()
    password   = request.form.get("password",   "")
    confirm    = request.form.get("confirm",    "")

    def err(msg):
        flash(msg, "error")
        return render_template("register.html"), 400

    if not all([name, gender, department, email, username, password, confirm]):
        return err("All fields are required.")
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

    try:
        emp_id = next_employee_id(department)
        user = User(
            employee_id   = emp_id,
            name          = name,
            gender        = gender,
            department    = department,
            email         = email,
            username      = username,
            password_hash = generate_password_hash(password),
        )
        db.session.add(user)
        db.session.commit()
        log_action(username, "REGISTER",
                   f"New user registered: {name} | {department} | EMP#{emp_id}")
        flash("✅ Registration successful! You can now log in.", "success")
        return redirect(url_for("login_page"))
    except IntegrityError:
        db.session.rollback()
        return err("Username or email already exists. Please choose a different one.")


@app.route("/logout")
@login_required
def logout():
    username = session.get("username", "unknown")
    log_action(username, "LOGOUT")
    session.clear()
    resp = make_response(redirect(url_for("login_page")))
    resp.set_cookie("session", "", expires=0)
    flash("✅ You have been securely logged out.", "success")
    return resp


@app.route("/auth-status")
def auth_status():
    resp = jsonify({"authenticated": bool(session.get("user_id"))})
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.route("/health")
def health():
    """Render-friendly health check that verifies the database too."""
    try:
        db.session.execute(text("SELECT 1"))
        return jsonify({"status": "ok", "database": "ok"}), 200
    except Exception as exc:
        db.session.rollback()
        return jsonify({"status": "error", "database": "unavailable", "detail": str(exc)}), 503


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

    recent_logs     = AuditLog.query.order_by(AuditLog.timestamp.desc()).limit(20).all()
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
                           all_departments=all_departments)


# ═════════════════════════ PROFILE ═══════════════════════════

@app.route("/profile")
@login_required
def profile():
    user = User.query.get_or_404(session["user_id"])
    return render_template("profile.html",
                           user=user,
                           role=session["role"],
                           valid_departments=VALID_DEPARTMENTS)


@app.route("/profile/edit", methods=["POST"])
@login_required
def edit_profile():
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

    try:
        old_dept = user.department
        user.name       = name
        user.email      = email
        user.gender     = gender
        user.department = department
        db.session.commit()
        session["name"] = name
        log_action(user.username, "PROFILE_UPDATED",
                   f"Name/email/dept updated. Dept: {old_dept} → {department}")
        flash("✅ Profile updated successfully!", "success")
    except IntegrityError:
        db.session.rollback()
        flash("That email is already in use by another account.", "error")
    return redirect(url_for("profile"))


# ════════════════════════ CHANGE PASSWORD ════════════════════════

@app.route("/change-password", methods=["GET", "POST"])
@login_required
def change_password():
    user = User.query.get_or_404(session["user_id"])
    if request.method == "GET":
        return render_template("change_password.html",
                               prefill_username=user.username)

    current  = request.form.get("current_password",  "")
    new_pw   = request.form.get("new_password",      "")
    confirm  = request.form.get("confirm_password",  "")

    def err(msg):
        flash(msg, "error")
        return render_template("change_password.html",
                               prefill_username=user.username), 400

    if not check_password_hash(user.password_hash, current):
        log_action(user.username, "PASSWORD_CHANGE_FAILED", "Wrong current password")
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
    session.clear()
    log_action(user.username, "PASSWORD_CHANGED")
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
                           valid_departments=VALID_DEPARTMENTS)


@app.route("/admin/settings/clear-logs", methods=["POST"])
@admin_required
def clear_logs():
    try:
        deleted = AuditLog.query.delete()
        db.session.commit()
        log_action(session["username"], "ADMIN_CLEAR_LOGS",
                   f"{deleted} log records permanently deleted")
        flash(f"✅ {deleted} audit log records have been permanently deleted.", "success")
    except Exception as e:
        db.session.rollback()
        flash(f"Error clearing logs: {str(e)}", "error")
    return redirect(url_for("settings"))


# ════════════════════════ AUDIT LOGS (admin) ════════════════════

@app.route("/admin/audit-logs")
@admin_required
def audit_logs():
    search        = request.args.get("q",      "").strip()
    action_filter = request.args.get("action", "").strip()
    page          = request.args.get("page",   1, type=int)

    q = AuditLog.query
    if search:
        pat = f"%{search}%"
        q = q.filter(
            db.or_(AuditLog.username.ilike(pat),
                   AuditLog.action.ilike(pat),
                   AuditLog.details.ilike(pat))
        )
    if action_filter:
        q = q.filter(AuditLog.action.ilike(f"%{action_filter}%"))

    pagination  = q.order_by(AuditLog.timestamp.desc()).paginate(
        page=page, per_page=50, error_out=False)
    total_logs  = AuditLog.query.count()

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
    if gender not in VALID_GENDERS:
        return err("Invalid gender.")
    if department not in VALID_DEPARTMENTS:
        return err("Invalid department.")
    if not re.match(r'^[a-zA-Z0-9_.\-]+$', username):
        return err("Invalid username characters.")
    if is_reserved_username(username):
        return err("That username is reserved.")
    if password != confirm:
        return err("Passwords do not match.")
    ok, pw_err = validate_password(password)
    if not ok:
        return err(pw_err)

    try:
        emp_id = next_employee_id(department)
        user = User(
            employee_id=emp_id, name=name, gender=gender,
            department=department, email=email, username=username,
            password_hash=generate_password_hash(password),
        )
        db.session.add(user)
        db.session.commit()
        log_action(session["username"], "ADMIN_CREATE_USER",
                   f"Created: {username} ({name}) in {department}")
        flash(f"✅ User ‘{username}’ ({emp_id}) created successfully.", "success")
    except IntegrityError:
        db.session.rollback()
        flash("Username or email already exists.", "error")
    return redirect(url_for("dashboard"))


@app.route("/admin/users/<int:user_id>/edit", methods=["POST"])
@admin_required
def edit_user(user_id):
    user       = User.query.get_or_404(user_id)
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
        log_action(session["username"], "ADMIN_EDIT_USER",
                   f"Edited: {username} ({name}) dept={department}")
        flash(f"✅ User ‘{username}’ updated.", "success")
    except IntegrityError:
        db.session.rollback()
        flash("Username or email already exists.", "error")
    return redirect(url_for("dashboard"))


@app.route("/admin/users/<int:user_id>/delete", methods=["POST"])
@admin_required
def delete_user(user_id):
    user = User.query.get_or_404(user_id)
    if user.id == session["user_id"]:
        flash("You cannot delete your own account.", "error")
        return redirect(url_for("dashboard"))
    uname = user.username
    db.session.delete(user)
    db.session.commit()
    log_action(session["username"], "ADMIN_DELETE_USER", f"Deleted user: {uname}")
    flash(f"✅ User ‘{uname}’ deleted.", "success")
    return redirect(url_for("dashboard"))


@app.route("/admin/users/<int:user_id>/toggle-status", methods=["POST"])
@admin_required
def toggle_user_status(user_id):
    user = User.query.get_or_404(user_id)
    if user.id == session["user_id"]:
        flash("You cannot deactivate your own account.", "error")
        return redirect(url_for("dashboard"))
    user.is_active = not user.is_active
    db.session.commit()
    status = "activated" if user.is_active else "deactivated"
    log_action(session["username"], "ADMIN_TOGGLE_STATUS",
               f"User {user.username} {status}")
    flash(f"✅ User ‘{user.username}’ {status}.", "success")
    return redirect(url_for("dashboard"))


@app.route("/admin/users/<int:user_id>/unlock", methods=["POST"])
@admin_required
def unlock_user(user_id):
    user = User.query.get_or_404(user_id)
    user.unlock()
    db.session.commit()
    log_action(session["username"], "ADMIN_UNLOCK_USER",
               f"Unlocked: {user.username}")
    flash(f"✅ User ‘{user.username}’ has been unlocked.", "success")
    return redirect(url_for("dashboard"))


@app.route("/export/users")
@admin_required
def export_users():
    users = User.query.order_by(User.created_at.desc()).all()
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "Employee ID", "Name", "Gender", "Department",
        "Email", "Username", "Role", "Active",
        "Last Login", "Created At"
    ])
    for u in users:
        writer.writerow([
            u.employee_id, u.name, u.gender, u.department,
            u.email, u.username, u.role,
            "Yes" if u.is_active else "No",
            u.last_login.strftime("%Y-%m-%d %H:%M") if u.last_login else "",
            u.created_at.strftime("%Y-%m-%d %H:%M") if u.created_at else "",
        ])
    log_action(session["username"], "ADMIN_EXPORT_USERS",
               f"{len(users)} records exported")
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

def _init_db():
    """Create/migrate tables and safely bootstrap the configured admin."""
    db.create_all()

    inspector = inspect(db.engine)
    user_columns = {c["name"] for c in inspector.get_columns("user")}
    if "role" not in user_columns:
        db.session.execute(text(
            "ALTER TABLE \"user\" ADD COLUMN role VARCHAR(20) NOT NULL DEFAULT 'user'"
        ))
        db.session.commit()
        print("[MIGRATION] Added missing user.role column.", flush=True)

    # Migrate legacy EMP### IDs once to the department/year format.
    legacy_users = User.query.filter(User.employee_id.like("EMP%")).order_by(User.id.asc()).all()
    if legacy_users:
        # Temporarily move IDs so the UNIQUE constraint cannot collide during migration.
        for u in legacy_users:
            u.employee_id = f"MIG-{u.id}"
        db.session.commit()
        used = set()
        for u in legacy_users:
            join_year = u.created_at.year if u.created_at else datetime.now().year
            code = DEPARTMENT_CODES.get(u.department, "EMP")
            yy = join_year % 100
            prefix = f"{code}-{yy:02d}-"
            n = 1
            while f"{prefix}{n:04d}" in used or User.query.filter_by(employee_id=f"{prefix}{n:04d}").first():
                n += 1
            u.employee_id = f"{prefix}{n:04d}"
            used.add(u.employee_id)
        db.session.commit()
        print(f"[MIGRATION] Converted {len(legacy_users)} legacy employee IDs.", flush=True)

    admin_username = os.environ.get("ADMIN_USERNAME", "").strip()
    admin_password = os.environ.get("ADMIN_PASSWORD", "").strip()
    admin_email = os.environ.get("ADMIN_EMAIL", "").strip() or (admin_username + "@system.local")

    if not admin_username or not admin_password:
        print("[INIT] ADMIN_USERNAME / ADMIN_PASSWORD not configured; skipping admin bootstrap.", flush=True)
        return

    ok, pw_err = validate_password(admin_password)
    if not ok:
        raise RuntimeError("ADMIN_PASSWORD is too weak: " + pw_err)

    # Login is case-insensitive, so bootstrap lookup is case-insensitive too.
    admin = User.query.filter(db.func.lower(User.username) == admin_username.casefold()).first()

    if admin is None:
        employee_id = next_employee_id("Administration")
        admin = User(
            employee_id=employee_id,
            name="System Administrator",
            gender="Prefer not to say",
            department="Administration",
            email=admin_email,
            username=admin_username,
            password_hash=generate_password_hash(admin_password),
            role="admin",
            is_active=True,
        )
        db.session.add(admin)
        try:
            db.session.commit()
            print(f"[INIT] Admin created: {admin_username} ({employee_id})", flush=True)
        except IntegrityError:
            db.session.rollback()
            # Another worker may have created the admin concurrently.
            admin = User.query.filter(db.func.lower(User.username) == admin_username.casefold()).first()
            if admin is None:
                raise
            print(f"[INIT] Admin already created by another worker: {admin.username}", flush=True)
    else:
        changed = False
        if admin.role != "admin":
            admin.role = "admin"
            changed = True
        if not admin.is_active:
            admin.is_active = True
            changed = True
        if admin.email != admin_email:
            # Only update when an explicit ADMIN_EMAIL is provided.
            if os.environ.get("ADMIN_EMAIL", "").strip():
                admin.email = admin_email
                changed = True
        # Keep the Render admin credentials authoritative.
        if not check_password_hash(admin.password_hash, admin_password):
            admin.password_hash = generate_password_hash(admin_password)
            changed = True
        if changed:
            db.session.commit()
            print(f"[INIT] Admin synchronized: {admin.username}", flush=True)


# Vercel imports the Flask app while creating the function.
# Do NOT connect/migrate the database during module import: a transient
# database/network problem must not prevent Vercel from importing the app.
_db_ready = False
_db_init_error = None


def _ensure_db_ready():
    global _db_ready, _db_init_error
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
    if not _ensure_db_ready():
        return (jsonify({
            "status": "error",
            "message": "Database is not available.",
            "detail": _db_init_error,
        }), 503)


if __name__ == '__main__':
    app.run(
        host=os.environ.get('HOST', '0.0.0.0'),
        port=int(os.environ.get('PORT', 5000)),
        debug=os.environ.get('FLASK_DEBUG', '0') == '1',
    )

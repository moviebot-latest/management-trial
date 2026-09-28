import os
import secrets
import json
import io
import re
import urllib.request
import urllib.error
from html import escape
from uuid import uuid4
from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo
from functools import wraps
from flask import Flask, render_template, request, redirect, url_for, session, flash, make_response, jsonify
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import text, or_, inspect
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import synonym
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
try:
    from pypdf import PdfReader
except ImportError:
    PdfReader = None
try:
    import fitz
except ImportError:
    fitz = None

try:
    import boto3
except ImportError:
    boto3 = None

app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY') or secrets.token_hex(32)
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SECURE=os.environ.get('FLASK_DEBUG', '0') != '1',
    SESSION_COOKIE_SAMESITE='Lax',
    PERMANENT_SESSION_LIFETIME=timedelta(minutes=30),
    MAX_CONTENT_LENGTH=100 * 1024 * 1024,
)

database_url = os.environ.get('DATABASE_URL', 'sqlite:///library.db')
if database_url.startswith('postgres://'):
    database_url = database_url.replace('postgres://', 'postgresql://', 1)
# Use psycopg (v3) for PostgreSQL on Vercel/Neon.
if database_url.startswith('postgresql+psycopg2://'):
    database_url = database_url.replace('postgresql+psycopg2://', 'postgresql+psycopg://', 1)
app.config['SQLALCHEMY_DATABASE_URI'] = database_url
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
if database_url.startswith(('postgresql://','postgresql+psycopg://')):
    app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {
        'pool_pre_ping': True, 'pool_recycle': 300, 'pool_timeout': 30,
        'pool_size': 5, 'max_overflow': 5,
        'connect_args': {'connect_timeout': 10, 'sslmode': os.environ.get('PGSSLMODE','require')}
    }

db=SQLAlchemy(app)
IST = ZoneInfo('Asia/Kolkata')

# Advanced authentication/security controls
MAX_FAILED_ATTEMPTS = 5
LOCKOUT_MINUTES = 15
OTP_EXPIRY_MINUTES = 10
OTP_RESEND_SECONDS = 60
OTP_MAX_ATTEMPTS = 5

BREVO_API_URL = 'https://api.brevo.com/v3/smtp/email'

def _email_html(title, greeting, body_html, accent='#6d32d8'):
    return f'''<!doctype html><html><body style="margin:0;background:#f4f6fb;font-family:Arial,sans-serif;color:#17233f">
    <div style="max-width:680px;margin:24px auto;padding:0 12px">
      <div style="background:linear-gradient(135deg,{accent},#435df2);color:#fff;padding:26px 24px;border-radius:22px 22px 0 0">
        <div style="font-size:12px;letter-spacing:1.6px;font-weight:800;opacity:.85">LIBRARY MANAGEMENT SYSTEM</div>
        <h1 style="margin:8px 0 0;font-size:28px">{escape(title)}</h1>
      </div>
      <div style="background:#fff;padding:26px 24px;border:1px solid #e4e7f0;border-top:0;border-radius:0 0 22px 22px">
        <p style="font-size:16px;font-weight:700">{escape(greeting)}</p>{body_html}
        <p style="margin-top:26px;color:#7b8497;font-size:12px">This is an automated message from the Library Management System. Please do not reply to this email.</p>
      </div>
    </div></body></html>'''

def send_brevo_email(to_email, subject, title, greeting, body_html, accent='#6d32d8'):
    """Send an email through Brevo. Email failures never break the library transaction."""
    api_key=os.environ.get('BREVO_API_KEY','').strip()
    sender=os.environ.get('BREVO_SENDER_EMAIL','').strip()
    sender_name=os.environ.get('BREVO_SENDER_NAME','Library Management System').strip()
    if not api_key or not sender or not to_email:
        app.logger.info('Email skipped: Brevo environment variables are not configured.')
        return False
    payload={
        'sender': {'name': sender_name, 'email': sender},
        'to': [{'email': to_email}],
        'subject': subject,
        'htmlContent': _email_html(title,greeting,body_html,accent),
        'textContent': re.sub(r'<[^>]+>',' ',body_html).replace('&nbsp;',' ').strip(),
    }
    req=urllib.request.Request(BREVO_API_URL, data=json.dumps(payload).encode('utf-8'),
        headers={'accept':'application/json','api-key':api_key,'content-type':'application/json'}, method='POST')
    try:
        with urllib.request.urlopen(req, timeout=12) as resp:
            return 200 <= resp.status < 300
    except Exception:
        app.logger.exception('Brevo email send failed for %s', to_email)
        return False

def email_congratulations(user):
    body=f'''<p>Congratulations <b>{escape(user.name)}</b>! Your library account has been created successfully.</p>
    <div style="background:#f5f3ff;border:1px solid #e2dcff;padding:16px;border-radius:16px">
      <b>Username:</b> {escape(user.username)}<br><b>Gmail:</b> {escape(user.email)}<br><b>Member ID:</b> #{user.id}
    </div>
    <p>You can now log in and browse the library collection, issue books and view your library activity.</p>'''
    return send_brevo_email(user.email,'Congratulations! Your Library account is ready','Account Created','Hello '+user.name+',',body,'#147b4a')

def email_issue_confirmation(user, book, loan):
    due=to_ist(loan.due_at).strftime('%d %b %Y, %I:%M %p')
    issued=to_ist(loan.issued_at).strftime('%d %b %Y, %I:%M %p')
    body=f'''<p>Congratulations <b>{escape(user.name)}</b>! Your book has been issued successfully.</p>
    <div style="background:#f5f3ff;border:1px solid #e2dcff;padding:16px;border-radius:16px;line-height:1.8">
      <b>Book:</b> {escape(book.title)}<br><b>Book ID:</b> {escape(book.book_id)}<br><b>Author:</b> {escape(book.author)}<br>
      <b>Issue Date:</b> {issued}<br><b>Due Date:</b> {due}<br><b>Free Period:</b> 14 days
    </div>
    <p>Please return the book on or before the due date to avoid a late fine.</p>'''
    return send_brevo_email(user.email,'Congratulations! Book issued successfully','Book Issued','Hello '+user.name+',',body,'#435df2')

def email_return_confirmation(user, book, loan, total_fine=0):
    returned=to_ist(loan.returned_at).strftime('%d %b %Y, %I:%M %p') if loan.returned_at else to_ist(utcnow_naive()).strftime('%d %b %Y, %I:%M %p')
    fine_html=f'<b>Fine:</b> ₹{total_fine:.2f}' if total_fine else '<b>Fine:</b> ₹0.00 (No fine)'
    body=f'''<p>Your book return has been recorded successfully.</p>
    <div style="background:#eefaf3;border:1px solid #cdebd9;padding:16px;border-radius:16px;line-height:1.8">
      <b>Book:</b> {escape(book.title)}<br><b>Book ID:</b> {escape(book.book_id)}<br><b>Returned:</b> {returned}<br>{fine_html}
    </div>
    <p>Thank you for using the Library Management System.</p>'''
    return send_brevo_email(user.email,'Book return confirmation','Book Returned','Hello '+user.name+',',body,'#147b4a')

def email_payment_receipt(user, payment, loan):
    paid=to_ist(payment.paid_at).strftime('%d %b %Y, %I:%M:%S %p')
    body=f'''<p>Your payment has been recorded successfully. Here is your payment slip.</p>
    <div style="background:#f7f8ff;border:1px solid #e4e7f0;padding:16px;border-radius:16px;line-height:1.8">
      <b>Payment ID:</b> {escape(payment.payment_id)}<br><b>Transaction ID:</b> {escape(payment.transaction_reference or 'N/A')}<br>
      <b>Book:</b> {escape(loan.book.title)}<br><b>Book ID:</b> {escape(loan.book.book_id)}<br>
      <b>Method:</b> {escape(payment.payment_method)}<br><b>Date:</b> {paid}<br><b>Total Paid:</b> ₹{float(payment.amount):.2f}
    </div>
    <p><b>Payment Status: PAID</b><br>Book return has also been completed.</p>'''
    return send_brevo_email(user.email,'Payment successful — your library payment slip','Payment Receipt','Hello '+user.name+',',body,'#147b4a')

def email_refund_confirmation(user, payment, loan):
    refunded=to_ist(payment.refunded_at).strftime('%d %b %Y, %I:%M:%S %p')
    body=f'''<p>Your refund has been processed successfully.</p>
    <div style="background:#fff8e9;border:1px solid #f1dfac;padding:16px;border-radius:16px;line-height:1.8">
      <b>Refund ID:</b> {escape(payment.refund_reference or 'N/A')}<br><b>Original Payment ID:</b> {escape(payment.payment_id)}<br>
      <b>Transaction ID:</b> {escape(payment.transaction_reference or 'N/A')}<br><b>Book:</b> {escape(loan.book.title)}<br>
      <b>Refund Date:</b> {refunded}<br><b>Refund Amount:</b> ₹{float(payment.amount):.2f}
    </div>
    <p><b>Refund Status: REFUNDED</b></p>'''
    return send_brevo_email(user.email,'Refund processed — Library Management System','Refund Confirmation','Hello '+user.name+',',body,'#c47b00')

def email_password_changed(user):
    body=f'''<p>Your library account password was changed successfully.</p><div style="background:#f7f8ff;padding:16px;border-radius:16px"><b>Username:</b> {escape(user.username)}<br><b>Changed:</b> {escape(to_ist(utcnow_naive()).strftime('%d %b %Y, %I:%M %p'))}</div><p>If you did not make this change, contact the library administrator immediately.</p>'''
    return send_brevo_email(user.email,'Security alert — password changed','Password Changed','Hello '+user.name+',',body,'#c23b5d')

def _brevo_configured():
    return bool(os.environ.get('BREVO_API_KEY','').strip() and os.environ.get('BREVO_SENDER_EMAIL','').strip())

def mask_email(email):
    email=(email or '').strip()
    if '@' not in email: return 'your registered email'
    local,domain=email.split('@',1)
    if len(local)<=2: masked=local[:1]+'*'*max(1,len(local)-1)
    else: masked=local[:2]+'*'*max(2,len(local)-2)
    return f'{masked}@{domain}'

def send_brevo_otp(email, otp, name, purpose='verification'):
    if not _brevo_configured(): raise RuntimeError('Email OTP service is not configured.')
    reset=purpose=='password_reset'
    subject='Your Library Management System password reset code' if reset else 'Your Library Management System verification code'
    title='Reset your password' if reset else 'Verify your email'
    intro='Use this one-time code to reset your password:' if reset else 'Use this one-time code to finish creating your library account:'
    safe=escape((name or 'there')[:120])
    html=f'''<!doctype html><html><body style="font-family:Arial;background:#f5f6fb;padding:24px"><div style="max-width:560px;margin:auto;background:white;border-radius:22px;padding:30px;box-shadow:0 15px 45px rgba(40,30,90,.12)"><div style="font-size:12px;letter-spacing:2px;font-weight:800;color:#6747df">LIBRARY MANAGEMENT SYSTEM</div><h2>{title}</h2><p>Hello {safe},</p><p>{intro}</p><div style="font-size:34px;letter-spacing:10px;text-align:center;font-weight:900;background:#f0edff;padding:20px;border-radius:16px">{otp}</div><p style="color:#687085">This code expires in {OTP_EXPIRY_MINUTES} minutes and can be used only once. If you did not request it, ignore this email.</p></div></body></html>'''
    plain=f'Your Library Management System code is {otp}. It expires in {OTP_EXPIRY_MINUTES} minutes.'
    payload=json.dumps({'sender':{'name':os.environ.get('BREVO_SENDER_NAME','Library Management System'),'email':os.environ['BREVO_SENDER_EMAIL']},'to':[{'email':email,'name':(name or '')[:120]}],'subject':subject,'htmlContent':html,'textContent':plain}).encode('utf-8')
    req=urllib.request.Request(BREVO_API_URL,data=payload,headers={'accept':'application/json','api-key':os.environ['BREVO_API_KEY'],'content-type':'application/json'},method='POST')
    with urllib.request.urlopen(req,timeout=12) as resp:
        if not 200 <= resp.status < 300: raise RuntimeError('Email service rejected the request.')

def validate_strong_password(password):
    if len(password)<8: return False,'Password must be at least 8 characters.'
    if not re.search(r'[A-Z]',password): return False,'Password must contain an uppercase letter.'
    if not re.search(r'[a-z]',password): return False,'Password must contain a lowercase letter.'
    if not re.search(r'\d',password): return False,'Password must contain a number.'
    if not re.search(r'[^A-Za-z0-9]',password): return False,'Password must contain a special character.'
    return True,''

def create_registration_otp(email,name,gender,username,password_hash):
    now=utcnow_naive(); row=EmailVerification.query.filter_by(email=email).first()
    if row and (now-row.last_sent_at).total_seconds()<OTP_RESEND_SECONDS:
        raise ValueError(f'Please wait {OTP_RESEND_SECONDS-int((now-row.last_sent_at).total_seconds())} seconds before requesting another OTP.')
    otp=f'{secrets.randbelow(1000000):06d}'
    if not row: row=EmailVerification(email=email,username=username,name=name,gender=gender,password_hash=password_hash,otp_hash=generate_password_hash(otp),expires_at=now+timedelta(minutes=OTP_EXPIRY_MINUTES),last_sent_at=now,attempts=0)
    else:
        row.username=username; row.name=name; row.gender=gender; row.password_hash=password_hash; row.otp_hash=generate_password_hash(otp); row.expires_at=now+timedelta(minutes=OTP_EXPIRY_MINUTES); row.last_sent_at=now; row.attempts=0
    db.session.add(row); db.session.commit()
    try: send_brevo_otp(email,otp,name,'verification')
    except Exception:
        db.session.delete(row); db.session.commit(); raise

def create_password_reset_otp(user):
    now=utcnow_naive(); row=PasswordReset.query.filter_by(user_id=user.id).first()
    if row and (now-row.last_sent_at).total_seconds()<OTP_RESEND_SECONDS:
        raise ValueError(f'Please wait {OTP_RESEND_SECONDS-int((now-row.last_sent_at).total_seconds())} seconds before requesting another OTP.')
    otp=f'{secrets.randbelow(1000000):06d}'
    if not row: row=PasswordReset(user_id=user.id,email=user.email,otp_hash=generate_password_hash(otp),expires_at=now+timedelta(minutes=OTP_EXPIRY_MINUTES),last_sent_at=now,attempts=0,verified_at=None)
    else:
        row.email=user.email; row.otp_hash=generate_password_hash(otp); row.expires_at=now+timedelta(minutes=OTP_EXPIRY_MINUTES); row.last_sent_at=now; row.attempts=0; row.verified_at=None
    db.session.add(row); db.session.commit()
    try: send_brevo_otp(user.email,otp,user.name,'password_reset')
    except Exception:
        db.session.delete(row); db.session.commit(); raise

def utcnow_naive():
    return datetime.utcnow().replace(microsecond=0)

def to_ist(dt):
    if not dt:
        return None
    return dt.replace(tzinfo=ZoneInfo('UTC')).astimezone(IST)


class User(db.Model):
    __tablename__='user'
    # The database now exposes the member identifier as user_id.  The id
    # synonym keeps the existing application/template code compatible.
    user_id=db.Column(db.Integer, primary_key=True)
    id=synonym('user_id')
    name=db.Column(db.String(120), nullable=False)
    gender=db.Column(db.String(30), nullable=False, default='Other')
    email=db.Column(db.String(160), unique=True, nullable=False)
    username=db.Column(db.String(80), unique=True, nullable=False)
    password_hash=db.Column(db.String(255), nullable=False)
    role=db.Column(db.String(20), nullable=False, default='member')
    is_active=db.Column(db.Boolean, nullable=False, default=True)
    failed_attempts=db.Column(db.Integer, nullable=False, default=0)
    locked_until=db.Column(db.DateTime, nullable=True)
    last_login=db.Column(db.DateTime, nullable=True)
    created_at=db.Column(db.DateTime, server_default=db.func.now(), nullable=False)

    def is_locked(self):
        return bool(self.locked_until and datetime.utcnow() < self.locked_until)

    def record_failed_attempt(self):
        self.failed_attempts=(self.failed_attempts or 0)+1
        if self.failed_attempts >= MAX_FAILED_ATTEMPTS:
            self.locked_until=datetime.utcnow()+timedelta(minutes=LOCKOUT_MINUTES)

    def unlock(self):
        self.failed_attempts=0
        self.locked_until=None

    def lockout_remaining_minutes(self):
        if not self.locked_until: return 0
        return max(0, int((self.locked_until-datetime.utcnow()).total_seconds()//60)+1)


class EmailVerification(db.Model):
    __tablename__='email_verification'
    id=db.Column(db.Integer, primary_key=True)
    email=db.Column(db.String(160), unique=True, nullable=False, index=True)
    username=db.Column(db.String(80), nullable=False)
    name=db.Column(db.String(120), nullable=False)
    gender=db.Column(db.String(30), nullable=False)
    password_hash=db.Column(db.String(255), nullable=False)
    otp_hash=db.Column(db.String(255), nullable=False)
    expires_at=db.Column(db.DateTime, nullable=False)
    last_sent_at=db.Column(db.DateTime, nullable=False)
    attempts=db.Column(db.Integer, nullable=False, default=0)
    created_at=db.Column(db.DateTime, server_default=db.func.now(), nullable=False)

class PasswordReset(db.Model):
    __tablename__='password_reset'
    id=db.Column(db.Integer, primary_key=True)
    user_id=db.Column(db.Integer, nullable=False, index=True)
    email=db.Column(db.String(160), nullable=False, index=True)
    otp_hash=db.Column(db.String(255), nullable=False)
    expires_at=db.Column(db.DateTime, nullable=False)
    last_sent_at=db.Column(db.DateTime, nullable=False)
    attempts=db.Column(db.Integer, nullable=False, default=0)
    verified_at=db.Column(db.DateTime, nullable=True)
    created_at=db.Column(db.DateTime, server_default=db.func.now(), nullable=False)

class Book(db.Model):
    __tablename__='book'
    id=db.Column(db.Integer, primary_key=True)
    book_id=db.Column(db.String(20), unique=True, nullable=False)
    title=db.Column(db.String(180), nullable=False)
    author=db.Column(db.String(140), nullable=False)
    isbn=db.Column(db.String(40), unique=True, nullable=True)
    category=db.Column(db.String(80), nullable=False, default='General')
    description=db.Column(db.Text, nullable=True)
    cover_url=db.Column(db.String(600), nullable=True)
    pdf_path=db.Column(db.String(700), nullable=True)
    publication_year=db.Column(db.Integer, nullable=True)
    total_copies=db.Column(db.Integer, nullable=False, default=1)
    available_copies=db.Column(db.Integer, nullable=False, default=1)
    created_at=db.Column(db.DateTime, server_default=db.func.now(), nullable=False)

class Loan(db.Model):
    __tablename__='loan'
    id=db.Column(db.Integer, primary_key=True)
    book_id=db.Column(db.Integer, db.ForeignKey('book.id'), nullable=False)
    user_id=db.Column(db.Integer, db.ForeignKey('user.user_id'), nullable=False)
    issued_at=db.Column(db.DateTime, nullable=False, default=utcnow_naive)
    due_at=db.Column(db.DateTime, nullable=False)
    returned_at=db.Column(db.DateTime, nullable=True)
    book=db.relationship('Book', backref=db.backref('loans', lazy=True))
    user=db.relationship('User', backref=db.backref('loans', lazy=True))


class Payment(db.Model):
    __tablename__='payment'
    id=db.Column(db.Integer, primary_key=True)
    payment_id=db.Column(db.String(80), unique=True, nullable=False)
    return_id=db.Column(db.Integer, db.ForeignKey('return_record.id'), nullable=False)
    loan_id=db.Column(db.Integer, db.ForeignKey('loan.id'), nullable=False)
    user_id=db.Column(db.Integer, db.ForeignKey('user.user_id'), nullable=False)
    book_id=db.Column(db.Integer, db.ForeignKey('book.id'), nullable=False)
    payer_name=db.Column(db.String(120), nullable=False)
    amount=db.Column(db.Numeric(10,2), nullable=False, default=0)
    payment_method=db.Column(db.String(20), nullable=False)
    payment_status=db.Column(db.String(20), nullable=False, default='paid')
    transaction_reference=db.Column(db.String(120), unique=True, nullable=True)
    card_last4=db.Column(db.String(4), nullable=True)
    cash_received_by=db.Column(db.String(80), nullable=True)
    refund_status=db.Column(db.String(20), nullable=False, default='not_refunded')
    refund_reference=db.Column(db.String(120), unique=True, nullable=True)
    refunded_at=db.Column(db.DateTime, nullable=True)
    paid_at=db.Column(db.DateTime, nullable=False)
    created_at=db.Column(db.DateTime, server_default=db.func.now(), nullable=False)
    return_record=db.relationship('ReturnRecord', backref=db.backref('payment', uselist=False))
    loan=db.relationship('Loan', backref=db.backref('payments', lazy=True))
    user=db.relationship('User', backref=db.backref('payments', lazy=True))
    book=db.relationship('Book', backref=db.backref('payments', lazy=True))

class ReturnRecord(db.Model):
    __tablename__='return_record'
    id=db.Column(db.Integer, primary_key=True)
    loan_id=db.Column(db.Integer, db.ForeignKey('loan.id'), nullable=False, unique=True)
    late_fine=db.Column(db.Numeric(10,2), nullable=False, default=0)
    admin_fine=db.Column(db.Numeric(10,2), nullable=False, default=0)
    fine_reason=db.Column(db.String(255), nullable=True)
    photo_paths=db.Column(db.Text, nullable=True)
    total_fine=db.Column(db.Numeric(10,2), nullable=False, default=0)
    payment_status=db.Column(db.String(20), nullable=False, default='not_required')
    payment_method=db.Column(db.String(20), nullable=True)
    payment_id=db.Column(db.String(80), unique=True, nullable=True)
    paid_at=db.Column(db.DateTime, nullable=True)
    returned_at=db.Column(db.DateTime, nullable=True)
    created_by=db.Column(db.String(80), nullable=True)
    loan=db.relationship('Loan', backref=db.backref('return_record', uselist=False))

    @property
    def photos(self):
        try:
            return json.loads(self.photo_paths or '[]') or []
        except Exception:
            return []

# Neon Object Storage (S3-compatible). The code accepts the custom Vercel
# variables below and also Neon's standard AWS_* variables produced by
# `neon env pull --service object-storage`.
STORAGE_ENDPOINT = (os.environ.get('NEON_STORAGE_ENDPOINT')
                    or os.environ.get('AWS_ENDPOINT_URL_S3')
                    or os.environ.get('AWS_S3_ENDPOINT'))
STORAGE_ACCESS_KEY = os.environ.get('NEON_STORAGE_ACCESS_KEY') or os.environ.get('AWS_ACCESS_KEY_ID')
STORAGE_SECRET_KEY = os.environ.get('NEON_STORAGE_SECRET_KEY') or os.environ.get('AWS_SECRET_ACCESS_KEY')
STORAGE_REGION = os.environ.get('NEON_STORAGE_REGION') or os.environ.get('AWS_REGION') or 'us-east-2'
STORAGE_BUCKET = os.environ.get('NEON_STORAGE_BUCKET') or os.environ.get('AWS_S3_BUCKET') or 'library-uploads'

_s3_client = None
def storage_client():
    global _s3_client
    if _s3_client is not None:
        return _s3_client
    if boto3 is None:
        raise RuntimeError('boto3 is not installed. Add boto3 to requirements.txt.')
    if not STORAGE_ENDPOINT or not STORAGE_ACCESS_KEY or not STORAGE_SECRET_KEY:
        raise RuntimeError('Neon Object Storage is not configured. Set NEON_STORAGE_ENDPOINT, NEON_STORAGE_ACCESS_KEY and NEON_STORAGE_SECRET_KEY in Vercel.')
    _s3_client = boto3.client(
        's3',
        endpoint_url=STORAGE_ENDPOINT,
        aws_access_key_id=STORAGE_ACCESS_KEY,
        aws_secret_access_key=STORAGE_SECRET_KEY,
        region_name=STORAGE_REGION,
    )
    return _s3_client

def storage_upload(file_obj, object_key, content_type=None):
    extra = {'ContentType': content_type} if content_type else {}
    storage_client().upload_fileobj(file_obj, STORAGE_BUCKET, object_key, ExtraArgs=extra)
    return object_key

def storage_upload_bytes(data, object_key, content_type=None):
    return storage_upload(io.BytesIO(data), object_key, content_type)

def _pdf_text_and_metadata(file_bytes):
    if PdfReader is None:
        raise RuntimeError('PDF parser is not installed. Add pypdf to requirements.txt.')
    reader = PdfReader(io.BytesIO(file_bytes))
    meta = reader.metadata or {}
    chunks = []
    for page in reader.pages[:5]:
        try: txt = page.extract_text() or ''
        except Exception: txt = ''
        if txt: chunks.append(txt)
    return reader, meta, '\n'.join(chunks)

def analyze_book_pdf(file_bytes, filename='book.pdf'):
    """Extract metadata from one PDF. If it looks like a numbered multi-book catalog,
    return separate entries so the upload form can create separate Book records."""
    reader, meta, text = _pdf_text_and_metadata(file_bytes)
    clean = re.sub(r'\s+', ' ', text).strip()

    # Bulk catalog detection: entries such as "1. Python Programming", "2. Data Structures".
    # This specifically prevents a 30-book catalog PDF from being saved as one book.
    bulk = parse_bulk_catalog(file_bytes)
    if len(bulk) >= 2:
        return {'bulk': True, 'count': len(bulk), 'books': bulk, 'title': '', 'author': '',
                'isbn': '', 'publication_year': None, 'category': 'General',
                'description': f'{len(bulk)} books detected. Review the list and save all as separate books.'}

    title = str(meta.get('/Title') or '').strip()
    author = str(meta.get('/Author') or '').strip()
    isbn = None
    m = re.search(r'(?i)\b(?:ISBN(?:-1[03])?\s*[:#-]?\s*)?((?:97[89][ -]?)?\d[\d -]{8,16}\d)\b', clean)
    if m: isbn = re.sub(r'[^0-9Xx]', '', m.group(1))
    years = re.findall(r'\b(19\d{2}|20\d{2})\b', clean[:12000])
    year = int(years[0]) if years else None
    lines=[re.sub(r'\s+',' ',x).strip(' -–—|') for x in text.splitlines() if x.strip()]
    if not title:
        for line in lines[:40]:
            if 4 <= len(line) <= 180 and not re.search(r'(?i)^(isbn|copyright|contents|chapter|www\.|http)', line):
                title=line; break
    if not author:
        for line in lines[:60]:
            m=re.search(r'(?i)\bby\s*[:.-]?\s*(.+)$', line)
            if m and 2 <= len(m.group(1)) <= 120: author=m.group(1).strip(); break
    if not author and len(lines)>1 and re.search(r'(?i)(author|written by)',lines[1]):
        author=re.sub(r'(?i)^(author|written by)\s*[:.-]?\s*','',lines[1]).strip()
    low=clean.lower(); category='General'
    for key,cat in [('python','Programming'),('java','Programming'),('programming','Programming'),('database','DBMS'),('sql','DBMS'),('network','Networking'),('operating system','Operating Systems'),('artificial intelligence','Artificial Intelligence'),('machine learning','Machine Learning'),('cyber','Cyber Security'),('security','Security'),('cloud','Cloud Computing'),('mathematics','Mathematics'),('communication','Communication'),('management','Management'),('energy','Energy')]:
        if key in low: category=cat; break
    return {'bulk':False,'title':title[:180],'author':author[:140],'isbn':isbn[:40] if isbn else '','publication_year':year,'category':category,'description':clean[:500]}

def parse_bulk_catalog(file_bytes):
    """Parse numbered book entries from a catalog PDF and capture page/clip/image info.
    Designed for PDFs containing multiple book cards/entries per page."""
    if fitz is None:
        return []
    doc=fitz.open(stream=file_bytes,filetype='pdf')
    entries=[]
    try:
        for page_no in range(doc.page_count):
            page=doc.load_page(page_no)
            blocks=[]
            for b in page.get_text('blocks'):
                txt=(b[4] or '').strip()
                m=re.match(r'^(\d+)\.\s+(.+)$', txt.replace('\n',' ').strip())
                if m:
                    blocks.append({'num':int(m.group(1)),'title':m.group(2).strip(),'rect':fitz.Rect(b[0],b[1],b[2],b[3])})
            if not blocks:
                continue
            blocks.sort(key=lambda x:x['rect'].y0)
            page_images=[]
            for im in page.get_images(full=True):
                try:
                    rects=page.get_image_rects(im[0])
                    if rects: page_images.append((rects[0],im[0]))
                except Exception:
                    pass
            page_images.sort(key=lambda x:x[0].y0)
            page_h=page.rect.height
            for idx,head in enumerate(blocks):
                y0=max(0, head['rect'].y0-8)
                y1=(blocks[idx+1]['rect'].y0-8) if idx+1<len(blocks) else page_h-18
                clip=fitz.Rect(0,y0,page.rect.width,min(page_h,y1))
                # Pull text only from this book's region.
                region_text=page.get_text('text',clip=clip) or ''
                lines=[re.sub(r'\s+',' ',x).strip(' -–—|') for x in region_text.splitlines() if x.strip()]
                title=head['title'][:180]
                author=''
                category='General'
                isbn=''
                year=None
                for line in lines:
                    if re.match(r'(?i)^author$',line):
                        continue
                    if re.match(r'(?i)^category$',line):
                        continue
                    if re.match(r'(?i)^book id$',line) or re.match(r'(?i)^cover photo$',line) or re.match(r'(?i)^pdf$',line):
                        continue
                # Label/value pairs in common catalog layouts.
                for label in ('Author','Category','ISBN','Publication Year','Year','Title'):
                    pat=re.compile(r'(?is)'+re.escape(label)+r'\s*[:\-]?\s*([^\n]+)')
                    mm=pat.search(region_text)
                    if mm:
                        val=re.sub(r'\s+',' ',mm.group(1)).strip(' :|-')
                        if label=='Author': author=val[:140]
                        elif label=='Category': category=val[:80]
                        elif label=='ISBN': isbn=re.sub(r'[^0-9Xx]','',val)[:40]
                        elif label in ('Publication Year','Year'):
                            ym=re.search(r'(19\d{2}|20\d{2})',val)
                            if ym: year=int(ym.group(1))
                # If labels were split onto their own lines, use the next line as value.
                for i,line in enumerate(lines[:-1]):
                    nxt=lines[i+1]
                    if line.lower()=='author' and not author: author=nxt[:140]
                    elif line.lower()=='category' and category=='General': category=nxt[:80]
                    elif line.lower()=='isbn' and not isbn: isbn=re.sub(r'[^0-9Xx]','',nxt)[:40]
                # Extract an embedded image whose center is inside this book region.
                image_bytes=None
                for rect,xref in page_images:
                    if rect.y0 >= clip.y0 and rect.y1 <= clip.y1:
                        try:
                            image_bytes=doc.extract_image(xref)['image']
                            break
                        except Exception:
                            pass
                has_catalog_marker=bool(re.search(r'(?i)\b(Book ID|Cover Photo|Ready for website upload|PDF)\b', region_text))
                entries.append({'source_number':head['num'],'title':title,'author':author,'isbn':isbn,
                                'publication_year':year,'category':category or 'General',
                                'description':re.sub(r'\s+',' ',region_text).strip()[:1000],
                                'page':page_no,'clip':[clip.x0,clip.y0,clip.x1,clip.y1],
                                'image_bytes':image_bytes.hex() if image_bytes else '',
                                'catalog_marker':has_catalog_marker})
        entries.sort(key=lambda x:x['source_number'])
        # Only call it a bulk catalog when the numbering is meaningful and the
        # regions actually look like book-card records, not ordinary chapter numbers.
        if len(entries)>=2 and sum(1 for x in entries if x['catalog_marker']) >= max(2, int(len(entries)*0.7)) and [x['source_number'] for x in entries] == list(range(entries[0]['source_number'], entries[0]['source_number']+len(entries))):
            return entries
        return []
    finally:
        doc.close()

def make_bulk_book_pdf(file_bytes, page_no, clip):
    if fitz is None: return None
    src=fitz.open(stream=file_bytes,filetype='pdf')
    out=fitz.open()
    try:
        page=src.load_page(page_no)
        r=fitz.Rect(*clip)
        new=out.new_page(width=r.width,height=r.height)
        new.show_pdf_page(new.rect,src,page_no,clip=r)
        return out.tobytes()
    finally:
        src.close(); out.close()

def extract_pdf_cover(file_bytes):
    if fitz is None: return None
    doc=fitz.open(stream=file_bytes,filetype='pdf')
    try:
        if not doc.page_count: return None
        pix=doc.load_page(0).get_pixmap(matrix=fitz.Matrix(1.5,1.5),alpha=False)
        return pix.tobytes('png')
    finally: doc.close()

def _allowed_file_ext(filename, allowed):
    return bool(filename and '.' in filename and filename.rsplit('.',1)[1].lower() in allowed)

def storage_delete(object_key):
    if not object_key or object_key.startswith('/static/'):
        return
    try:
        storage_client().delete_object(Bucket=STORAGE_BUCKET, Key=object_key)
    except Exception:
        app.logger.warning('Could not delete old storage object %s', object_key, exc_info=True)

def storage_url(object_key):
    if not object_key:
        return ''
    # Backward compatibility for old local uploads.
    if object_key.startswith('/') or object_key.startswith('http://') or object_key.startswith('https://'):
        return object_key
    try:
        return storage_client().generate_presigned_url(
            'get_object', Params={'Bucket': STORAGE_BUCKET, 'Key': object_key}, ExpiresIn=3600
        )
    except Exception:
        app.logger.warning('Could not create signed storage URL for %s', object_key, exc_info=True)
        return ''

ALLOWED_PHOTO_EXTENSIONS={'jpg','jpeg','png','webp'}


def db_retry(fn):
    try:
        return fn()
    except OperationalError:
        db.session.rollback(); db.engine.dispose(); return fn()


def csrf_token():
    token=session.get('_csrf_token')
    if not token:
        token=secrets.token_urlsafe(32); session['_csrf_token']=token
    return token

@app.context_processor
def inject(): return {'csrf_token': csrf_token(), 'to_ist': to_ist, 'photo_url': storage_url}

@app.before_request
def protect_post():
    if request.method=='POST':
        sent=request.form.get('_csrf_token',''); expected=session.get('_csrf_token','')
        if not expected or not sent or not secrets.compare_digest(sent,expected): return 'Invalid or missing CSRF token.',400

@app.after_request
def security_headers(r):
    r.headers['X-Content-Type-Options']='nosniff'; r.headers['X-Frame-Options']='DENY'
    if os.environ.get('FLASK_DEBUG','0') != '1': r.headers['Strict-Transport-Security']='max-age=31536000; includeSubDomains'
    r.headers['Referrer-Policy']='strict-origin-when-cross-origin'
    r.headers['Cache-Control']='no-store, no-cache, must-revalidate, max-age=0'; r.headers['Pragma']='no-cache'
    r.headers['Permissions-Policy']='camera=(), microphone=(), geolocation=()'
    r.headers['Content-Security-Policy']="default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; img-src 'self' data: https:; font-src 'self' data:; connect-src 'self'; frame-ancestors 'self'; base-uri 'self'; form-action 'self'"
    return r

def is_reserved_username(username):
    admin=os.environ.get('ADMIN_USERNAME','admin').strip().casefold()
    return username.strip().casefold() in {admin,'admin'}


def _valid_username_format(username):
    return bool(re.fullmatch(r'[A-Za-z0-9._-]{3,80}', username or ''))


def _email_exists(email):
    return User.query.filter(db.func.lower(User.email) == (email or '').strip().casefold()).first() is not None


def _username_exists(username):
    return User.query.filter(db.func.lower(User.username) == (username or '').strip().casefold()).first() is not None

def is_logged(): return bool(session.get('user_id'))
def role(): return session.get('role','')
def is_admin(): return role()=='admin'
def is_librarian(): return role() in {'admin','librarian'}
def is_staff(): return role() in {'admin','librarian'}

def login_required(fn):
    @wraps(fn)
    def wrapper(*a,**kw):
        if not is_logged(): return redirect(url_for('index'))
        return fn(*a,**kw)
    return wrapper

def staff_required(fn):
    @wraps(fn)
    def wrapper(*a,**kw):
        if not is_staff():
            flash('Librarian or admin access required.','error'); return redirect(url_for('dashboard'))
        return fn(*a,**kw)
    return wrapper

def admin_required(fn):
    @wraps(fn)
    def wrapper(*a,**kw):
        if not is_admin():
            flash('Admin access required.','error'); return redirect(url_for('dashboard'))
        return fn(*a,**kw)
    return wrapper

def next_code(model, field, prefix):
    rows=model.query.order_by(model.id.desc()).limit(1000).all()
    nums=[]
    for x in rows:
        v=getattr(x,field,'') or ''
        try: nums.append(int(v.replace(prefix,'')))
        except: pass
    n=max(nums,default=0)+1
    return f'{prefix}{n:04d}'

def migrate_user_schema():
    """Rename the public user primary-key column to user_id and remove the old employee_id.
    Existing numeric IDs are preserved; PostgreSQL automatically updates FK dependencies
    when a referenced column is renamed.
    """
    if not database_url.startswith(('postgresql://','postgresql+psycopg://')):
        return
    try:
        cols={r[0] for r in db.session.execute(text(
            """SELECT column_name FROM information_schema.columns
               WHERE table_schema='public' AND table_name='user'"""
        )).fetchall()}
        if not cols:
            return
        if 'id' in cols and 'user_id' not in cols:
            db.session.execute(text('ALTER TABLE "user" RENAME COLUMN id TO user_id'))
            cols.remove('id'); cols.add('user_id')
        if 'employee_id' in cols:
            db.session.execute(text('ALTER TABLE "user" DROP COLUMN employee_id'))
            cols.remove('employee_id')
        if 'department' in cols:
            db.session.execute(text('ALTER TABLE "user" DROP COLUMN department'))
            cols.remove('department')
        db.session.commit()
    except Exception:
        db.session.rollback()
        raise


def migrate_audit_log_schema():
    """Create/upgrade audit_log without assuming a particular old audit schema."""
    if not database_url.startswith(('postgresql://','postgresql+psycopg://')):
        return
    try:
        exists=db.session.execute(text("SELECT to_regclass('public.audit_log')")).scalar()
        if not exists:
            db.session.execute(text("""
                CREATE TABLE audit_log (
                    id BIGSERIAL PRIMARY KEY,
                    actor_user_id INTEGER NULL,
                    actor_name VARCHAR(120) NOT NULL DEFAULT 'System',
                    action VARCHAR(80) NOT NULL,
                    entity_type VARCHAR(80) NULL,
                    entity_id VARCHAR(80) NULL,
                    details TEXT NULL,
                    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
            """))
        else:
            cols={r[0] for r in db.session.execute(text(
                """SELECT column_name FROM information_schema.columns
                   WHERE table_schema='public' AND table_name='audit_log'"""
            )).fetchall()}
            additions={
                'actor_user_id':'INTEGER',
                'actor_name':"VARCHAR(120) DEFAULT 'System'",
                'action':"VARCHAR(80) DEFAULT 'system'",
                'entity_type':'VARCHAR(80)',
                'entity_id':'VARCHAR(80)',
                'details':'TEXT',
                'created_at':'TIMESTAMP DEFAULT CURRENT_TIMESTAMP',
            }
            for col,typ in additions.items():
                if col not in cols:
                    db.session.execute(text(f'ALTER TABLE audit_log ADD COLUMN {col} {typ}'))
        db.session.commit()
    except Exception:
        db.session.rollback()
        raise


def write_audit(action, entity_type=None, entity_id=None, details=None):
    """Write an activity row without allowing audit failures to break the main action."""
    try:
        if database_url.startswith(('postgresql://','postgresql+psycopg://')):
            cols={r[0] for r in db.session.execute(text(
                """SELECT column_name FROM information_schema.columns
                   WHERE table_schema='public' AND table_name='audit_log'"""
            )).fetchall()}
            if not cols:
                return
            values={
                'actor_user_id': session.get('user_id') if isinstance(session.get('user_id'), int) else None,
                'actor_name': session.get('name') or session.get('username') or 'System',
                'action': action,
                'entity_type': entity_type,
                'entity_id': str(entity_id) if entity_id is not None else None,
                'details': details,
                'created_at': datetime.utcnow(),
            }
            use=[c for c in values if c in cols]
            if 'action' not in use:
                # Existing audit table may use a different legacy action column.
                for legacy in ('event','activity','activity_type','event_type','log_type'):
                    if legacy in cols:
                        values[legacy]=action; use.append(legacy); break
            if not use:
                return
            names=', '.join('"'+c+'"' for c in use)
            params=', '.join(':'+c for c in use)
            db.session.execute(text(f'INSERT INTO audit_log ({names}) VALUES ({params})'), {c:values[c] for c in use})
            db.session.commit()
    except Exception:
        db.session.rollback()
        app.logger.exception('Audit log write failed for %s', action)


def migrate_auth_schema():
    if not database_url.startswith(('postgresql://','postgresql+psycopg://')): return
    try:
        cols={r[0] for r in db.session.execute(text('SELECT column_name FROM information_schema.columns WHERE table_schema=\'public\' AND table_name=\'user\'')).fetchall()}
        additions={'is_active':'BOOLEAN NOT NULL DEFAULT TRUE','failed_attempts':'INTEGER NOT NULL DEFAULT 0','locked_until':'TIMESTAMP NULL','last_login':'TIMESTAMP NULL'}
        for col,typ in additions.items():
            if col not in cols: db.session.execute(text(f'ALTER TABLE "user" ADD COLUMN {col} {typ}'))
        db.session.commit()
    except Exception:
        db.session.rollback(); raise

def migrate_existing_db():
    # Existing deployments may already have a user table. Add only the new role
    # column if it is missing; no existing accounts are deleted.
    try:
        if database_url.startswith(('postgresql://','postgresql+psycopg://')):
            db.session.execute(text("ALTER TABLE \"user\" ADD COLUMN IF NOT EXISTS role VARCHAR(20) NOT NULL DEFAULT 'member'"))
        else:
            cols=[r[1] for r in db.session.execute(text('PRAGMA table_info(user)')).fetchall()]
            if 'role' not in cols:
                db.session.execute(text("ALTER TABLE user ADD COLUMN role VARCHAR(20) NOT NULL DEFAULT 'member'"))
        db.session.commit()
    except Exception:
        db.session.rollback()


def migrate_book_schema():
    # Upgrade the older PostgreSQL book schema in-place.
    # Old DB: book.book_code was the primary key.
    # Current ORM: book.id is the integer PK and book.book_id is the public code.
    if not database_url.startswith(('postgresql://','postgresql+psycopg://')):
        return

    try:
        book_cols = {r[0]: r[1] for r in db.session.execute(text(
            """SELECT column_name, data_type
               FROM information_schema.columns
               WHERE table_schema='public' AND table_name='book'"""
        )).fetchall()}

        if not book_cols:
            return

        # Rename the old public book code column to the name expected by the ORM.
        if 'book_code' in book_cols and 'book_id' not in book_cols:
            db.session.execute(text('ALTER TABLE book RENAME COLUMN book_code TO book_id'))
            book_cols['book_id'] = book_cols.pop('book_code')

        # IMPORTANT: remove every foreign key that references book before changing
        # book's primary-key index. Older databases may have issue_book, loan, etc.
        db.session.execute(text("""
            DO $$
            DECLARE r record;
            BEGIN
                IF to_regclass('public.book') IS NOT NULL THEN
                    FOR r IN
                        SELECT conname, conrelid::regclass AS child_table
                        FROM pg_constraint
                        WHERE confrelid='public.book'::regclass AND contype='f'
                    LOOP
                        EXECUTE format('ALTER TABLE %s DROP CONSTRAINT %I', r.child_table, r.conname);
                    END LOOP;
                END IF;
            END $$;
        """))

        # Add the integer ORM primary key if the old table does not have it.
        if 'id' not in book_cols:
            db.session.execute(text(
                'ALTER TABLE book ADD COLUMN id INTEGER GENERATED BY DEFAULT AS IDENTITY'
            ))
            book_cols['id'] = 'integer'

        # Older deployments can also be missing newer nullable book fields.
        # Add only missing columns; existing book data is left untouched.
        missing_book_columns = {
            'description': 'TEXT',
            'cover_url': 'VARCHAR(600)',
            'pdf_path': 'VARCHAR(700)',
            'publication_year': 'INTEGER',
            'created_at': 'TIMESTAMP DEFAULT CURRENT_TIMESTAMP',
        }
        for col_name, col_sql in missing_book_columns.items():
            if col_name not in book_cols:
                db.session.execute(text(
                    f'ALTER TABLE book ADD COLUMN {col_name} {col_sql}'
                ))
                book_cols[col_name] = 'added'

        # Convert the current app's loan references from old book codes to book.id.
        loan_cols = {r[0]: r[1] for r in db.session.execute(text(
            """SELECT column_name, data_type
               FROM information_schema.columns
               WHERE table_schema='public' AND table_name='loan'"""
        )).fetchall()}

        if 'book_id' in loan_cols and loan_cols['book_id'] in ('character varying','text','character'):
            db.session.execute(text("""
                UPDATE loan l
                SET book_id = b.id::text
                FROM book b
                WHERE l.book_id = b.book_id
            """))
            db.session.execute(text(
                'ALTER TABLE loan ALTER COLUMN book_id TYPE INTEGER USING book_id::integer'
            ))

        # Drop the old book primary key only after all dependent foreign keys are gone.
        db.session.execute(text("""
            DO $$
            DECLARE r record;
            BEGIN
                FOR r IN
                    SELECT conname
                    FROM pg_constraint
                    WHERE conrelid='public.book'::regclass AND contype='p'
                LOOP
                    EXECUTE format('ALTER TABLE book DROP CONSTRAINT %I', r.conname);
                END LOOP;
            END $$;
        """))

        db.session.execute(text("""
            DO $$
            BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM pg_constraint
                    WHERE conrelid='public.book'::regclass AND contype='p'
                ) THEN
                    ALTER TABLE book ADD CONSTRAINT book_pkey_new PRIMARY KEY (id);
                END IF;
            END $$;
        """))

        # Keep book_id unique for normal book-code lookups.
        db.session.execute(text("""
            DO $$
            BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM pg_constraint
                    WHERE conrelid='public.book'::regclass
                      AND contype='u'
                      AND conname='book_book_id_key'
                ) THEN
                    ALTER TABLE book ADD CONSTRAINT book_book_id_key UNIQUE (book_id);
                END IF;
            END $$;
        """))

        # Restore the FK used by the current ORM. Old legacy tables can remain
        # untouched; the app only needs loan.book_id -> book.id.
        db.session.execute(text("""
            DO $$
            BEGIN
                IF to_regclass('public.loan') IS NOT NULL
                   AND EXISTS (SELECT 1 FROM information_schema.columns
                               WHERE table_schema='public' AND table_name='loan'
                                 AND column_name='book_id')
                   AND NOT EXISTS (
                       SELECT 1 FROM pg_constraint
                       WHERE conrelid='public.loan'::regclass
                         AND confrelid='public.book'::regclass
                         AND contype='f'
                   ) THEN
                    ALTER TABLE loan ADD CONSTRAINT loan_book_id_fkey
                        FOREIGN KEY (book_id) REFERENCES book(id);
                END IF;
            END $$;
        """))

        db.session.commit()
    except Exception:
        db.session.rollback()
        raise


def migrate_return_schema():
    """Ensure return_record contains all fine/photo/payment fields used by the app."""
    if not database_url.startswith(('postgresql://','postgresql+psycopg://')):
        return
    try:
        exists=db.session.execute(text("SELECT to_regclass('public.return_record')")).scalar()
        if not exists:
            db.session.execute(text("""
                CREATE TABLE return_record (
                    id BIGSERIAL PRIMARY KEY,
                    loan_id INTEGER NOT NULL UNIQUE,
                    late_fine NUMERIC(10,2) NOT NULL DEFAULT 0,
                    admin_fine NUMERIC(10,2) NOT NULL DEFAULT 0,
                    fine_reason VARCHAR(255),
                    photo_paths TEXT,
                    total_fine NUMERIC(10,2) NOT NULL DEFAULT 0,
                    payment_status VARCHAR(20) NOT NULL DEFAULT 'not_required',
                    payment_method VARCHAR(20),
                    payment_id VARCHAR(80) UNIQUE,
                    paid_at TIMESTAMP,
                    returned_at TIMESTAMP,
                    created_by VARCHAR(80)
                )
            """))
        else:
            cols={r[0] for r in db.session.execute(text(
                """SELECT column_name FROM information_schema.columns
                   WHERE table_schema='public' AND table_name='return_record'"""
            )).fetchall()}
            missing={
                'late_fine':'NUMERIC(10,2) DEFAULT 0',
                'admin_fine':'NUMERIC(10,2) DEFAULT 0',
                'fine_reason':'VARCHAR(255)',
                'photo_paths':'TEXT',
                'total_fine':'NUMERIC(10,2) DEFAULT 0',
                'payment_status':"VARCHAR(20) DEFAULT 'not_required'",
                'payment_method':'VARCHAR(20)',
                'payment_id':'VARCHAR(80)',
                'paid_at':'TIMESTAMP', 'returned_at':'TIMESTAMP', 'created_by':'VARCHAR(80)'
            }
            for col,typ in missing.items():
                if col not in cols:
                    db.session.execute(text(f'ALTER TABLE return_record ADD COLUMN {col} {typ}'))
        db.session.commit()
    except Exception:
        db.session.rollback()
        raise


def migrate_payment_schema():
    """Ensure the payment table has the exact columns needed by the return/payment flow."""
    if not database_url.startswith(('postgresql://','postgresql+psycopg://')):
        return
    try:
        exists=db.session.execute(text("SELECT to_regclass('public.payment')")).scalar()
        if not exists:
            db.session.execute(text("""
                CREATE TABLE payment (
                    id BIGSERIAL PRIMARY KEY,
                    payment_id VARCHAR(80) UNIQUE NOT NULL,
                    return_id INTEGER NOT NULL,
                    loan_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    book_id INTEGER NOT NULL,
                    payer_name VARCHAR(120) NOT NULL,
                    amount NUMERIC(10,2) NOT NULL DEFAULT 0,
                    payment_method VARCHAR(20) NOT NULL DEFAULT 'UPI',
                    payment_status VARCHAR(20) NOT NULL DEFAULT 'paid',
                    transaction_reference VARCHAR(120) UNIQUE,
                    card_last4 VARCHAR(4),
                    cash_received_by VARCHAR(80),
                    refund_status VARCHAR(20) NOT NULL DEFAULT 'not_refunded',
                    refund_reference VARCHAR(120) UNIQUE,
                    refunded_at TIMESTAMP,
                    paid_at TIMESTAMP NOT NULL,
                    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
            """))
        else:
            cols={r[0] for r in db.session.execute(text(
                """SELECT column_name FROM information_schema.columns
                   WHERE table_schema='public' AND table_name='payment'"""
            )).fetchall()}
            missing={
                'payment_id':'VARCHAR(80)', 'return_id':'INTEGER', 'loan_id':'INTEGER',
                'user_id':'INTEGER', 'book_id':'INTEGER', 'payer_name':'VARCHAR(120)',
                'amount':'NUMERIC(10,2) DEFAULT 0', 'payment_method':"VARCHAR(20) DEFAULT 'UPI'",
                'payment_status':"VARCHAR(20) DEFAULT 'paid'", 'transaction_reference':'VARCHAR(120)',
                'card_last4':'VARCHAR(4)', 'cash_received_by':'VARCHAR(80)',
                'refund_status':"VARCHAR(20) DEFAULT 'not_refunded'", 'refund_reference':'VARCHAR(120)',
                'refunded_at':'TIMESTAMP', 'paid_at':'TIMESTAMP', 'created_at':'TIMESTAMP DEFAULT CURRENT_TIMESTAMP'
            }
            for col,typ in missing.items():
                if col not in cols:
                    db.session.execute(text(f'ALTER TABLE payment ADD COLUMN {col} {typ}'))
        # Ensure the three critical foreign keys exist. Do not duplicate constraints.
        constraints=db.session.execute(text("""
            SELECT conname FROM pg_constraint
            WHERE conrelid='public.payment'::regclass AND contype='f'
        """)).fetchall()
        names={r[0] for r in constraints}
        fks=[
            ('payment_return_id_fkey','return_id','return_record','id'),
            ('payment_loan_id_fkey','loan_id','loan','id'),
            ('payment_user_id_fkey','user_id','user','user_id'),
            ('payment_book_id_fkey','book_id','book','id'),
        ]
        for cname,col,table,refcol in fks:
            if cname not in names:
                # Only add when there are no orphan rows.
                orphan=db.session.execute(text(
                    f'SELECT COUNT(*) FROM payment p LEFT JOIN {table} t ON p.{col}=t.{refcol} WHERE p.{col} IS NOT NULL AND t.{refcol} IS NULL'
                )).scalar()
                if orphan==0:
                    db.session.execute(text(
                        f'ALTER TABLE payment ADD CONSTRAINT {cname} FOREIGN KEY ({col}) REFERENCES {table}({refcol})'
                    ))
        db.session.commit()
    except Exception:
        db.session.rollback()
        raise

@app.get('/api/check-email')
def check_email_availability():
    email=request.args.get('email','').strip().lower()
    if not email:
        return jsonify(available=False, message='Enter your email address.')
    if not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+', email):
        return jsonify(available=False, message='Enter a valid email address.')
    try:
        exists=_email_exists(email)
        return jsonify(available=not exists, message=('Email is already registered. Please use a different email.' if exists else 'Email is available.'))
    except Exception:
        db.session.rollback()
        return jsonify(available=False, message='Could not check email right now. Try again.'), 500


@app.get('/api/check-username')
def check_username_availability():
    username=request.args.get('username','').strip()
    if not username:
        return jsonify(available=False, message='Create a username.')
    if not _valid_username_format(username):
        return jsonify(available=False, message='Use 3-80 letters, numbers, dot, underscore or hyphen.')
    if is_reserved_username(username):
        return jsonify(available=False, message='This username is reserved. Please choose another username.')
    try:
        exists=_username_exists(username)
        return jsonify(available=not exists, message=('Username is already taken. Try a different username.' if exists else 'Username is available.'))
    except Exception:
        db.session.rollback()
        return jsonify(available=False, message='Could not check username right now. Try again.'), 500


@app.route('/')
def index():
    return redirect(url_for('dashboard')) if is_logged() else render_template('login.html')

@app.route('/register',methods=['GET','POST'])
def register():
    if request.method=='GET':
        return render_template('register.html', otp_sent=bool(session.get('registration_email')), pending_email=session.get('registration_masked_email',''))
    f=request.form
    name=f.get('name','').strip(); gender=f.get('gender','').strip(); email=f.get('email','').strip().lower(); username=f.get('username','').strip(); password=f.get('password',''); confirm=f.get('confirm','')
    if not all([name,gender,email,username,password,confirm]): flash('Please fill in all fields.','error'); return redirect(url_for('register'))
    if not re.fullmatch(r'[A-Za-z0-9._-]{3,80}',username) or is_reserved_username(username): flash('Please choose a valid, non-reserved username.','error'); return redirect(url_for('register'))
    if not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+',email): flash('Please enter a valid email address.','error'); return redirect(url_for('register'))
    if gender not in {'Male','Female','Other','Prefer not to say'}: flash('Please select a valid gender.','error'); return redirect(url_for('register'))
    if password!=confirm: flash('Passwords do not match.','error'); return redirect(url_for('register'))
    ok,pwerr=validate_strong_password(password)
    if not ok: flash(pwerr,'error'); return redirect(url_for('register'))
    try:
        if _username_exists(username): flash('Username already exists.','error'); return redirect(url_for('register'))
        if _email_exists(email): flash('Email already exists.','error'); return redirect(url_for('register'))
        create_registration_otp(email,name,gender,username,generate_password_hash(password))
        session['registration_email']=email; session['registration_masked_email']=mask_email(email)
        session['registration_username']=username
        flash(f'📧 OTP sent to {mask_email(email)}. It expires in {OTP_EXPIRY_MINUTES} minutes.','success')
        write_audit('registration_otp_requested','registration',email,f'OTP requested for username {username}')
        return redirect(url_for('register'))
    except ValueError as exc:
        db.session.rollback(); flash(str(exc),'error')
    except Exception:
        db.session.rollback(); flash('Verification email could not be sent. Please try again later.','error')
    return redirect(url_for('register'))

@app.post('/register/verify-otp')
def verify_registration_otp():
    email=session.get('registration_email'); otp=request.form.get('otp','').strip()
    row=EmailVerification.query.filter_by(email=email).first() if email else None
    if not row or not re.fullmatch(r'\d{6}',otp): flash('Enter the 6-digit OTP sent to your email.','error'); return redirect(url_for('register'))
    now=utcnow_naive()
    if row.expires_at < now: db.session.delete(row); db.session.commit(); flash('OTP expired. Please request a new code.','error'); return redirect(url_for('register'))
    if row.attempts >= OTP_MAX_ATTEMPTS: flash('Too many incorrect attempts. Please resend a new OTP.','error'); return redirect(url_for('register'))
    if not check_password_hash(row.otp_hash,otp):
        row.attempts+=1; db.session.commit(); flash(f'Invalid OTP. {max(0,OTP_MAX_ATTEMPTS-row.attempts)} attempt(s) remaining.','error'); return redirect(url_for('register'))
    if _username_exists(row.username) or _email_exists(row.email):
        db.session.delete(row); db.session.commit(); session.pop('registration_email',None); flash('Account details are no longer available. Please start registration again.','error'); return redirect(url_for('register'))
    try:
        u=User(name=row.name,gender=row.gender,email=row.email,username=row.username,password_hash=row.password_hash,role='member')
        db.session.add(u); db.session.delete(row); db.session.commit()
        write_audit('registration','user',u.id,f'Email verified and member created: {u.name}')
        email_congratulations(u)
        admin_email=os.environ.get('ADMIN_EMAIL','').strip()
        if admin_email: send_brevo_email(admin_email,'New library member registration','New Member Registered','Hello Admin,',f'<p>A new member has completed email verification.</p><p><b>Name:</b> {escape(u.name)}<br><b>Username:</b> {escape(u.username)}<br><b>Email:</b> {escape(u.email)}<br><b>Member ID:</b> #{u.id}</p>','#435df2')
        for k in ('registration_email','registration_masked_email','registration_username'): session.pop(k,None)
        flash('✅ Email verified. Library account created successfully.','success'); return redirect(url_for('index'))
    except Exception:
        db.session.rollback(); flash('Account could not be created. Please try again.','error'); return redirect(url_for('register'))

@app.post('/register/resend-otp')
def resend_registration_otp():
    email=session.get('registration_email')
    row=EmailVerification.query.filter_by(email=email).first() if email else None
    if not row: flash('Registration session expired. Please start again.','error'); return redirect(url_for('register'))
    try:
        create_registration_otp(row.email,row.name,row.gender,row.username,row.password_hash)
        flash(f'📧 A new OTP was sent to {mask_email(row.email)}.','success')
    except ValueError as exc: db.session.rollback(); flash(str(exc),'error')
    except Exception: db.session.rollback(); flash('Could not resend OTP right now.','error')
    return redirect(url_for('register'))

@app.post('/login')
def login():
    username=request.form.get('username','').strip(); password=request.form.get('password','')
    au=os.environ.get('ADMIN_USERNAME','admin'); ap=os.environ.get('ADMIN_PASSWORD','')
    if au and ap and username.casefold()==au.casefold() and secrets.compare_digest(password,ap):
        session.clear(); session.permanent=True; session.update(user_id='admin',role='admin',username=au,name=os.environ.get('ADMIN_NAME','Admin')); write_audit('login','admin',None,f'Admin login: {au}'); return redirect(url_for('dashboard'))
    try: u=db_retry(lambda: User.query.filter(db.func.lower(User.username)==username.casefold()).first())
    except OperationalError: flash('Database connection was temporarily unavailable.','error'); return redirect(url_for('index'))
    if not u or not u.is_active:
        flash('Invalid username or password.','error'); return redirect(url_for('index'))
    if u.is_locked():
        flash(f'Account temporarily locked. Try again in {u.lockout_remaining_minutes()} minute(s).','error'); return redirect(url_for('index'))
    if check_password_hash(u.password_hash,password):
        u.unlock(); u.last_login=datetime.utcnow(); db.session.commit(); session.clear(); session.permanent=True; session.update(user_id=u.id,role=u.role or 'member',username=u.username,name=u.name); write_audit('login','user',u.id,f'User login: {u.username}'); return redirect(url_for('dashboard'))
    u.record_failed_attempt(); db.session.commit(); write_audit('login_failed','user',u.id,f'Failed login attempt {u.failed_attempts}')
    if u.is_locked(): flash(f'Too many failed attempts. Account locked for {LOCKOUT_MINUTES} minutes.','error')
    else: flash('Invalid username or password.','error')
    return redirect(url_for('index'))

@app.route('/logout')
def logout():
    session.clear(); return redirect(url_for('index'),303)

@app.route('/dashboard')
@login_required
def dashboard():
    try:
        books=db_retry(lambda: Book.query.order_by(Book.id.desc()).all())
        members=db_retry(lambda: User.query.filter(User.role.in_(['member','librarian'])).order_by(User.id.desc()).all()) if is_staff() else []
        active_loans=db_retry(lambda: Loan.query.filter_by(user_id=session.get('user_id')).filter(Loan.returned_at.is_(None)).order_by(Loan.due_at.asc()).all()) if not is_staff() else []
        overdue=db_retry(lambda: Loan.query.filter(Loan.returned_at.is_(None),Loan.due_at < utcnow_naive()).count()) if is_staff() else sum(1 for x in active_loans if x.due_at < utcnow_naive())
        total_copies=sum(b.total_copies for b in books); available=sum(b.available_copies for b in books)
        all_loans=db_retry(lambda: Loan.query.filter(Loan.returned_at.is_(None)).count())
        # Staff see every issue/return record; members see their own history so
        # an admin-added fine and its evidence remain visible in the member account.
        loan_records=db_retry(lambda: Loan.query.order_by(Loan.issued_at.desc()).all()) if is_staff() else db_retry(lambda: Loan.query.filter_by(user_id=session.get('user_id')).order_by(Loan.issued_at.desc()).all())
    except OperationalError:
        db.session.rollback(); flash('Database connection was temporarily unavailable.','error'); return redirect(url_for('index'))
    current_user = User.query.get(session['user_id']) if isinstance(session.get('user_id'), int) else None
    late_fee_per_day = max(0, float(os.environ.get('LATE_FEE_PER_DAY', '10')))
    return render_template('dashboard.html',books=books,members=members,loans=loan_records,role=role(),current_name=session.get('name','User'),current_username=session.get('username',''),current_email=(current_user.email if current_user else ''),total_books=len(books),total_copies=total_copies,available_copies=available,issued=all_loans,overdue=overdue,active_loans=active_loans,now=utcnow_naive(),late_fee_per_day=late_fee_per_day)

@app.post('/books/create')
@staff_required
def create_book():
    f=request.form; pdf=request.files.get('pdf_file'); cover_file=request.files.get('cover_file')
    title=f.get('title','').strip(); author=f.get('author','').strip(); isbn=f.get('isbn','').strip() or None
    category=f.get('category','General').strip() or 'General'; desc=f.get('description','').strip(); cover=f.get('cover_url','').strip() or None
    publication_year=f.get('publication_year','').strip(); pdf_bytes=None
    if pdf and pdf.filename:
        if not _allowed_file_ext(pdf.filename,{'pdf'}): flash('Only PDF files are allowed.','error'); return redirect(url_for('dashboard')+'#screen-addbook')
        try:
            pdf_bytes=pdf.read()
            if len(pdf_bytes)>50*1024*1024: raise ValueError('PDF must be 50 MB or smaller.')
            d=analyze_book_pdf(pdf_bytes,pdf.filename); title=title or d['title']; author=author or d['author']; isbn=isbn or d['isbn'] or None
            if category=='General': category=d['category']
            desc=desc or d['description']; publication_year=publication_year or (str(d['publication_year']) if d['publication_year'] else '')
        except Exception as e: flash(f'PDF analysis failed: {e}','error'); return redirect(url_for('dashboard')+'#screen-addbook')

    # Bulk catalog mode: one numbered catalog PDF can create many separate Book rows.
    # Each detected entry receives its own unique Book ID, cropped one-book PDF, and
    # extracted cover image when the source PDF contains one.
    if pdf_bytes:
        bulk_entries=parse_bulk_catalog(pdf_bytes)
        if len(bulk_entries)>=2:
            uploaded=[]; created=0
            try:
                for item in bulk_entries:
                    bt=(item.get('title') or '').strip()
                    ba=(item.get('author') or '').strip() or 'Unknown Author'
                    bi=(item.get('isbn') or '').strip() or None
                    bc=(item.get('category') or 'General').strip() or 'General'
                    by=item.get('publication_year')
                    bd=(item.get('description') or '').strip()[:1000]
                    # Do not let a duplicate ISBN abort the entire import. If the
                    # exact title/author already exists, keep the existing record.
                    existing=Book.query.filter(db.func.lower(Book.title)==bt.lower(), db.func.lower(Book.author)==ba.lower()).first() if bt else None
                    if existing:
                        continue
                    if bi and Book.query.filter_by(isbn=bi).first():
                        bi=None
                    code=next_code(Book,'book_id','BK')
                    one_pdf=make_bulk_book_pdf(pdf_bytes,int(item['page']),item['clip'])
                    pdf_key=None; cover_key=None
                    if one_pdf:
                        pdf_key=storage_upload_bytes(one_pdf,f'books/{code}/{uuid4().hex}.pdf','application/pdf'); uploaded.append(pdf_key)
                    img_hex=item.get('image_bytes') or ''
                    if img_hex:
                        raw=bytes.fromhex(img_hex)
                        cover_key=storage_upload_bytes(raw,f'books/{code}/{uuid4().hex}.jpg','image/jpeg'); uploaded.append(cover_key)
                    elif one_pdf:
                        cb=extract_pdf_cover(one_pdf)
                        if cb:
                            cover_key=storage_upload_bytes(cb,f'books/{code}/{uuid4().hex}.png','image/png'); uploaded.append(cover_key)
                    b=Book(book_id=code,title=bt or f'Imported Book {code}',author=ba,isbn=bi,category=bc,description=bd,cover_url=cover_key,pdf_path=pdf_key,publication_year=by,total_copies=1,available_copies=1)
                    db.session.add(b); db.session.flush(); created+=1
                    write_audit('book_created','book',b.id,f'Bulk book added: {b.title} ({b.book_id})')
                db.session.commit()
                flash(f'Bulk PDF processed: {created} separate books created with unique Book IDs.','success')
            except Exception as e:
                db.session.rollback()
                for key in uploaded: storage_delete(key)
                app.logger.exception('Bulk book import failed'); flash(f'Bulk PDF import failed: {e}','error')
            return redirect(url_for('dashboard')+'#screen-books')

    try: copies=max(1,int(f.get('copies','1')))
    except: copies=1
    try: pub_year=int(publication_year) if publication_year else None
    except: pub_year=None
    if not title or not author: flash('Book title and author are required. Upload a PDF or enter them manually.','error'); return redirect(url_for('dashboard')+'#screen-addbook')
    if isbn and Book.query.filter_by(isbn=isbn).first(): flash('ISBN already exists.','error'); return redirect(url_for('dashboard')+'#screen-addbook')
    code=next_code(Book,'book_id','BK'); uploaded=[]
    try:
        pdf_key=None
        if pdf_bytes:
            pdf_key=storage_upload_bytes(pdf_bytes,f'books/{code}/{uuid4().hex}.pdf','application/pdf'); uploaded.append(pdf_key)

        # If the user selected a real cover photo, always prefer that upload.
        # Only fall back to the first PDF page when no cover photo was supplied.
        if cover_file and cover_file.filename:
            if not _allowed_file_ext(cover_file.filename,ALLOWED_PHOTO_EXTENSIONS):
                raise ValueError('Cover photo must be JPG, JPEG, PNG or WEBP.')
            ext=cover_file.filename.rsplit('.',1)[1].lower()
            cover=storage_upload(cover_file.stream,f'books/{code}/{uuid4().hex}.{ext}',cover_file.mimetype)
            uploaded.append(cover)
        elif pdf_bytes:
            cb=extract_pdf_cover(pdf_bytes)
            if cb:
                cover=storage_upload_bytes(cb,f'books/{code}/{uuid4().hex}.png','image/png')
                uploaded.append(cover)
        b=Book(book_id=code,title=title,author=author,isbn=isbn,category=category,description=desc,cover_url=cover,pdf_path=pdf_key,publication_year=pub_year,total_copies=copies,available_copies=copies)
        db.session.add(b); db.session.commit(); write_audit('book_created','book',b.id,f'Book added: {b.title} ({b.book_id})')
        flash('Book added successfully. PDF details were auto-filled where available.','success')
    except Exception as e:
        db.session.rollback()
        for key in uploaded: storage_delete(key)
        app.logger.exception('Book create/upload failed'); flash(f'Book could not be added: {e}','error')
    return redirect(url_for('dashboard')+'#screen-books')

@app.post('/books/analyze-pdf')
@staff_required
def analyze_pdf_route():
    pdf=request.files.get('pdf_file')
    if not pdf or not pdf.filename: return {'ok':False,'error':'Select a PDF first.'},400
    if not _allowed_file_ext(pdf.filename,{'pdf'}): return {'ok':False,'error':'Only PDF files are allowed.'},400
    try:
        data=pdf.read()
        if len(data)>50*1024*1024: return {'ok':False,'error':'PDF must be 50 MB or smaller.'},400
        return {'ok':True,'data':analyze_book_pdf(data,pdf.filename)}
    except Exception as e: return {'ok':False,'error':str(e)},400

@app.get('/books/<int:book_id>/pdf')
@login_required
def view_book_pdf(book_id):
    b=db.get_or_404(Book,book_id)
    if not b.pdf_path: flash('No PDF is attached to this book.','error'); return redirect(url_for('dashboard')+'#screen-books')
    target=storage_url(b.pdf_path)
    if not target: flash('Book PDF storage is unavailable.','error'); return redirect(url_for('dashboard')+'#screen-books')
    return redirect(target)

@app.get('/books/<int:book_id>/edit')
@staff_required
def edit_book_page(book_id):
    book = db.get_or_404(Book, book_id)
    return render_template('edit_book.html', book=book)

@app.post('/books/<int:book_id>/edit')
@staff_required
def edit_book(book_id):
    b=db.get_or_404(Book,book_id); f=request.form; title=f.get('title','').strip(); author=f.get('author','').strip(); isbn=f.get('isbn','').strip() or None
    try: new_total=max(1,int(f.get('copies','1')))
    except: new_total=b.total_copies
    if isbn and Book.query.filter(Book.isbn==isbn,Book.id!=book_id).first(): flash('ISBN already exists.','error'); return redirect(url_for('dashboard'))
    issued=b.total_copies-b.available_copies; b.title=title or b.title; b.author=author or b.author; b.isbn=isbn; b.category=f.get('category','General').strip() or 'General'; b.description=f.get('description','').strip(); b.cover_url=f.get('cover_url','').strip() or b.cover_url
    try: b.publication_year=int(f.get('publication_year','')) if f.get('publication_year','').strip() else None
    except: pass
    if new_total<issued: flash(f'Copies cannot be less than currently issued copies ({issued}).','error'); return redirect(url_for('dashboard'))
    b.total_copies=new_total; b.available_copies=new_total-issued
    db.session.commit(); flash('Book updated successfully.','success'); return redirect(url_for('dashboard'))

@app.post('/books/<int:book_id>/delete')
@staff_required
def delete_book(book_id):
    b=db.get_or_404(Book,book_id)
    if Loan.query.filter_by(book_id=book_id).first(): flash('This book has issue history and cannot be deleted.','error'); return redirect(url_for('dashboard'))
    db.session.delete(b); db.session.commit(); flash('Book deleted.','success'); return redirect(url_for('dashboard'))

@app.get('/books/<int:book_id>/issue/confirm')
@login_required
def issue_confirm(book_id):
    # Member-facing confirmation page: no JavaScript is required to open it.
    # This avoids mobile inline-click issues and keeps all issue details server-side.
    if is_staff():
        flash('Use the Issue & Return panel to issue a book to a selected member.', 'error')
        return redirect(url_for('dashboard'))
    u = User.query.get(session.get('user_id'))
    b = Book.query.get_or_404(book_id)
    if not u:
        session.clear()
        return redirect(url_for('index'))
    if b.available_copies <= 0:
        flash('No available copy for this book.', 'error')
        return redirect(url_for('dashboard'))
    existing = Loan.query.filter_by(book_id=b.id, user_id=u.id, returned_at=None).first()
    if existing:
        flash('This member already has an active copy of this book.', 'error')
        return redirect(url_for('dashboard'))
    active = Loan.query.filter_by(user_id=u.id, returned_at=None).count()
    if active >= 5:
        flash('Maximum 5 active books allowed.', 'error')
        return redirect(url_for('dashboard'))
    issue_at = utcnow_naive().replace(microsecond=0)
    due_at = issue_at + timedelta(days=14)
    return render_template('issue_confirm.html', user=u, book=b, issue_at=issue_at, due_at=due_at)

@app.post('/books/<int:book_id>/issue')
@login_required
def issue_book(book_id):
    if is_admin() or is_librarian():
        username=request.form.get('username','').strip(); u=User.query.filter_by(username=username).first() if username else None
        if not u: flash('Select a valid member username.','error'); return redirect(url_for('dashboard'))
    else: u=User.query.get(session['user_id'])
    b=Book.query.get_or_404(book_id)
    if b.available_copies<=0: flash('No available copy for this book.','error'); return redirect(url_for('dashboard'))
    existing=Loan.query.filter_by(book_id=b.id,user_id=u.id,returned_at=None).first()
    if existing: flash('This member already has an active copy of this book.','error'); return redirect(url_for('dashboard'))
    active=Loan.query.filter_by(user_id=u.id,returned_at=None).count()
    if active>=5 and not is_staff(): flash('Maximum 5 active books allowed.','error'); return redirect(url_for('dashboard'))
    l=Loan(book_id=b.id,user_id=u.id,due_at=utcnow_naive().replace(microsecond=0)); l.due_at += timedelta(days=14)
    b.available_copies-=1; db.session.add(l); db.session.commit()
    write_audit('book_issued','loan',l.id,f'Book issued to {u.name}: {b.title}')
    email_issue_confirmation(u,b,l)
    flash(f'Book issued to {u.name}. Confirmation email sent if email service is configured.','success'); return redirect(url_for('dashboard'))

@app.route('/admin/additional-fine', methods=['GET', 'POST'])
@admin_required
def additional_fine():
    active_loans = (Loan.query.filter(Loan.returned_at.is_(None))
                    .order_by(Loan.issued_at.desc()).all())
    if request.method == 'GET':
        return render_template('additional_fine.html', active_loans=active_loans)

    try:
        loan_id = int(request.form.get('loan_id', '0'))
    except ValueError:
        loan_id = 0
    loan = Loan.query.get(loan_id) if loan_id else None
    if not loan or loan.returned_at:
        flash('Please select a valid active issued book.', 'error')
        return redirect(url_for('additional_fine'))

    raw = request.form.get('admin_fine', '').strip()
    try:
        admin_fine = round(float(raw), 2)
    except ValueError:
        admin_fine = -1
    if admin_fine <= 0:
        flash('Please enter an additional fine amount greater than ₹0.', 'error')
        return redirect(url_for('additional_fine'))

    reason = request.form.get('fine_reason', '').strip()
    other_reason = request.form.get('other_reason', '').strip()
    if reason == 'Other':
        if not other_reason:
            flash('Please enter the Other reason.', 'error')
            return redirect(url_for('additional_fine'))
        reason = other_reason
    if not reason:
        flash('Please select a reason for the additional fine.', 'error')
        return redirect(url_for('additional_fine'))

    files = [f for f in request.files.getlist('book_photos') if f and f.filename]
    if len(files) > 5:
        flash('You can upload a maximum of 5 book photos.', 'error')
        return redirect(url_for('additional_fine'))
    for f in files:
        ext = secure_filename(f.filename).rsplit('.', 1)[-1].lower() if '.' in f.filename else ''
        if ext not in ALLOWED_PHOTO_EXTENSIONS:
            flash('Only JPG, JPEG, PNG or WEBP photos are allowed.', 'error')
            return redirect(url_for('additional_fine'))

    record = ReturnRecord.query.filter_by(loan_id=loan.id).first()
    old_paths = []
    if record and record.photo_paths:
        try:
            old_paths = json.loads(record.photo_paths) or []
        except Exception:
            old_paths = []

    paths = old_paths
    if files:
        paths = []
        for f in files:
            ext = secure_filename(f.filename).rsplit('.', 1)[-1].lower()
            name = f'returns/{loan.id}/{uuid4().hex}.{ext}'
            storage_upload(f.stream, name, f.mimetype or f'image/{ext}')
            paths.append(name)
        for old in old_paths:
            if old and not old.startswith('/') and not old.startswith('http://') and not old.startswith('https://'):
                storage_delete(old)

    if record:
        record.admin_fine = admin_fine
        record.fine_reason = reason
        record.photo_paths = json.dumps(paths)
        record.total_fine = round(float(record.late_fine or 0) + admin_fine, 2)
        if record.payment_status == 'paid':
            record.payment_status = 'pending'
            record.payment_method = None
            record.payment_id = None
            record.paid_at = None
        record.returned_at = None
        record.created_by = str(session.get('username', 'admin'))
    else:
        record = ReturnRecord(
            loan_id=loan.id, late_fine=0, admin_fine=admin_fine,
            fine_reason=reason, photo_paths=json.dumps(paths),
            total_fine=admin_fine, payment_status='pending',
            created_by=str(session.get('username', 'admin'))
        )
        db.session.add(record)
    db.session.commit()
    write_audit('additional_fine', 'return_record', record.id, f'Additional fine ₹{admin_fine:.2f} for {loan.book.title}')
    send_brevo_email(loan.user.email,'Additional library fine added','Additional Fine Notice','Hello '+loan.user.name+',',f'<p>An additional fine has been added to your library issue.</p><div style=\"background:#fff8e9;border:1px solid #f1dfb9;padding:16px;border-radius:16px;line-height:1.8\"><b>Book:</b> {escape(loan.book.title)}<br><b>Book ID:</b> {escape(loan.book.book_id)}<br><b>Additional Fine:</b> ₹{admin_fine:.2f}<br><b>Reason:</b> {escape(reason)}<br><b>Total Fine:</b> ₹{float(record.total_fine):.2f}</div><p>Please check your account and complete the payment before returning the book.</p>','#c47b00')
    flash(f'Additional fine of ₹{admin_fine:.2f} added to {loan.user.name} — {loan.book.title}. Fine email sent if configured.', 'success')
    return redirect(url_for('additional_fine'))

def _return_calculation(loan, returned_at):
    late_seconds=max(0,int((returned_at-loan.due_at).total_seconds()))
    late_days=(late_seconds+86399)//86400 if late_seconds else 0
    rate=max(0,float(os.environ.get('LATE_FEE_PER_DAY','10')))
    late_fine=late_days*rate
    return late_days, rate, late_fine

def _existing_admin_fine(loan):
    record=ReturnRecord.query.filter_by(loan_id=loan.id).first()
    if not record:
        return 0.0, None, []
    try:
        paths=json.loads(record.photo_paths or '[]') or []
    except Exception:
        paths=[]
    return float(record.admin_fine or 0), record.fine_reason, paths

def _save_return_record(loan, returned_at, late_fine, admin_fine, reason, files=None, payment_status='not_required', payment_method=None, payment_details=None):
    record=ReturnRecord.query.filter_by(loan_id=loan.id).first()
    existing_paths=[]
    if record and record.photo_paths:
        try:
            existing_paths=json.loads(record.photo_paths) or []
        except Exception:
            existing_paths=[]

    paths=existing_paths
    if files:
        paths=[]
        for f in files:
            ext=secure_filename(f.filename).rsplit('.',1)[-1].lower()
            name=f'returns/{loan.id}/{uuid4().hex}.{ext}'
            storage_upload(f.stream,name,f.mimetype or f'image/{ext}')
            paths.append(name)
        for old in existing_paths:
            if old and not old.startswith('/') and not old.startswith('http://') and not old.startswith('https://'):
                storage_delete(old)

    total=round(late_fine+admin_fine,2)
    payment_id=None
    paid_at=None
    payment_obj=None
    if payment_status=='paid':
        payment_id=f'PAY-{datetime.utcnow().strftime("%Y%m%d%H%M%S")}-{loan.id}-{uuid4().hex[:6].upper()}'
        paid_at=returned_at

    if record:
        record.late_fine=late_fine
        record.admin_fine=admin_fine
        record.fine_reason=reason
        record.photo_paths=json.dumps(paths)
        record.total_fine=total
        record.payment_status=payment_status
        record.payment_method=payment_method
        record.payment_id=payment_id
        record.paid_at=paid_at
        record.returned_at=returned_at
        record.created_by=record.created_by or str(session.get('username','admin'))
    else:
        record=ReturnRecord(loan_id=loan.id,late_fine=late_fine,admin_fine=admin_fine,
            fine_reason=reason,photo_paths=json.dumps(paths),total_fine=total,
            payment_status=payment_status,payment_method=payment_method,payment_id=payment_id,
            paid_at=paid_at,returned_at=returned_at,created_by=str(session.get('username','admin')))
        db.session.add(record)

    if payment_status=='paid':
        # The ReturnRecord must have a real primary key before it is used as
        # payment.return_id (the FK is NOT NULL).
        db.session.flush()
        details=payment_details or {}
        transaction_reference=f'TXN-{uuid4().hex[:12].upper()}'
        payment_obj=Payment(
            payment_id=payment_id, return_id=record.id, loan_id=loan.id,
            user_id=loan.user_id, book_id=loan.book_id, payer_name=loan.user.name,
            amount=total, payment_method=payment_method or 'UPI', payment_status='paid',
            transaction_reference=transaction_reference,
            card_last4=(details.get('card_last4') or '')[-4:] or None,
            cash_received_by=details.get('cash_received_by') or None, paid_at=paid_at
        )
        db.session.add(payment_obj)
        db.session.flush()
        record.payment_id=payment_id
    loan.returned_at=returned_at
    loan.book.available_copies=min(loan.book.total_copies,loan.book.available_copies+1)
    db.session.commit()
    write_audit('payment' if payment_status=='paid' else 'return', 'payment' if payment_status=='paid' else 'return_record', payment_id or record.id, f'Returned {loan.book.title}; total fine ₹{total:.2f}' + (f'; payment {payment_id}' if payment_id else ''))
    return total, payment_id

@app.get('/loans/<int:loan_id>/return/confirm')
@login_required
def return_confirm(loan_id):
    l=Loan.query.get_or_404(loan_id)
    if not is_staff() and l.user_id != session.get('user_id'):
        flash('You can only return your own book.', 'error')
        return redirect(url_for('dashboard'))
    if l.returned_at:
        flash('This book has already been returned.', 'error')
        return redirect(url_for('dashboard'))
    now=utcnow_naive()
    late_days,rate,late_fine=_return_calculation(l,now)
    admin_fine,reason,photos=_existing_admin_fine(l)
    total=round(late_fine+admin_fine,2)
    return render_template('return_confirm.html',loan=l,now=now,late_days=late_days,
        fine=late_fine,rate=rate,admin_fine=admin_fine,admin_reason=reason,admin_photos=photos,total_fine=total)

@app.post('/loans/<int:loan_id>/return')
@login_required
def return_book(loan_id):
    l=Loan.query.get_or_404(loan_id)
    if not is_staff() and l.user_id!=session.get('user_id'):
        flash('You can only return your own book.','error'); return redirect(url_for('dashboard'))
    if l.returned_at:
        flash('This book has already been returned.','error'); return redirect(url_for('dashboard'))
    returned_at=utcnow_naive().replace(microsecond=0)
    late_days,rate,late_fine=_return_calculation(l,returned_at)
    admin_fine,reason,photos=_existing_admin_fine(l)
    total=round(late_fine+admin_fine,2)
    if total>0:
        return redirect(url_for('payment_page',loan_id=loan_id))
    total,_=_save_return_record(l,returned_at,late_fine,admin_fine,reason,[], 'not_required',None)
    email_return_confirmation(l.user,l.book,l,0)
    flash('Book returned successfully. No fine. Return confirmation email sent if configured.','success')
    return redirect(url_for('dashboard'))

@app.get('/loans/<int:loan_id>/payment')
@login_required
def payment_page(loan_id):
    l=Loan.query.get_or_404(loan_id)
    if not is_staff() and l.user_id!=session.get('user_id'):
        flash('You can only pay for your own book.','error'); return redirect(url_for('dashboard'))
    if l.returned_at:
        flash('This book has already been returned.','error'); return redirect(url_for('dashboard'))
    now=utcnow_naive().replace(microsecond=0)
    late_days,rate,late_fine=_return_calculation(l,now)
    admin_fine,reason,photos=_existing_admin_fine(l)
    total=round(late_fine+admin_fine,2)
    if total<=0:
        return redirect(url_for('return_confirm',loan_id=loan_id))
    return render_template('payment.html',loan=l,total_fine=total,late_fine=late_fine,admin_fine=admin_fine,admin_reason=reason,admin_photos=photos)

@app.post('/loans/<int:loan_id>/payment')
@login_required
def pay_and_return(loan_id):
    l=Loan.query.get_or_404(loan_id)
    if not is_staff() and l.user_id!=session.get('user_id'):
        flash('You can only return your own book.', 'error'); return redirect(url_for('dashboard'))
    if l.returned_at:
        flash('This book has already been returned.', 'error'); return redirect(url_for('dashboard'))
    returned_at=utcnow_naive().replace(microsecond=0)
    late_days,rate,late_fine=_return_calculation(l,returned_at)
    admin_fine,reason,photos=_existing_admin_fine(l)
    total=round(late_fine+admin_fine,2)
    if total<=0:
        _save_return_record(l,returned_at,late_fine,admin_fine,reason,[], 'not_required',None)
        flash('Book returned successfully. No fine.', 'success')
        return redirect(url_for('dashboard'))
    method=request.form.get('payment_method','UPI').strip().upper()
    if method not in {'UPI','CARD','CASH'}: method='UPI'
    try:
        payment_details={
            'card_last4': request.form.get('card_last4','').strip(),
            'cash_received_by': request.form.get('cash_received_by','').strip(),
        }
        total,payment_id=_save_return_record(l,returned_at,late_fine,admin_fine,reason,[], 'paid',method,payment_details)
        if not payment_id:
            raise RuntimeError('Payment record was not created.')
    except Exception:
        db.session.rollback()
        app.logger.exception('Payment/return failed for loan %s', loan_id)
        flash('Payment could not be completed. No book status or stock was changed. Please try again.', 'error')
        return redirect(url_for('payment_page', loan_id=loan_id))
    payment=Payment.query.filter_by(payment_id=payment_id).first()
    if payment:
        email_payment_receipt(l.user,payment,l)
        email_return_confirmation(l.user,l.book,l,total)
    return redirect(url_for('payment_receipt',payment_id=payment_id))

@app.post('/admin/payments/<payment_id>/refund')
@admin_required
def refund_payment(payment_id):
    payment=Payment.query.filter_by(payment_id=payment_id).first_or_404()
    if payment.payment_status != 'paid':
        flash('Only a paid transaction can be refunded.','error')
        return redirect(url_for('payment_receipt',payment_id=payment_id))
    if payment.refund_status == 'refunded':
        flash('This payment has already been refunded.','error')
        return redirect(url_for('payment_receipt',payment_id=payment_id))
    payment.refund_status='refunded'
    payment.refund_reference=f'REF-{datetime.utcnow().strftime("%Y%m%d%H%M%S")}-{uuid4().hex[:8].upper()}'
    payment.refunded_at=utcnow_naive()
    payment.payment_status='refunded'
    db.session.commit()
    email_refund_confirmation(payment.user,payment,payment.loan)
    write_audit('refund','payment',payment.payment_id,f'Refund processed for ₹{float(payment.amount):.2f}; {payment.refund_reference}')
    flash('Refund recorded successfully. Refund confirmation email sent if configured.','success')
    return redirect(url_for('payment_receipt',payment_id=payment_id))

# Backward-compatible endpoint for older deployed templates.
@app.post('/loans/<int:loan_id>/pay-demo-and-return')
@login_required
def pay_demo_and_return(loan_id):
    return pay_and_return(loan_id)

@app.get('/payments/<payment_id>/receipt')
@login_required
def payment_receipt(payment_id):
    payment=Payment.query.filter_by(payment_id=payment_id).first_or_404()
    record=payment.return_record
    l=record.loan
    if not is_staff() and l.user_id!=session.get('user_id'):
        flash('You can only view your own payment receipt.','error'); return redirect(url_for('dashboard'))
    return render_template('payment_receipt.html',record=record,payment=payment,loan=l,photos=record.photos)

@app.post('/admin/members/<int:user_id>/role')
@admin_required
def change_role(user_id):
    u=User.query.get_or_404(user_id); new_role=request.form.get('role','member')
    if new_role not in {'member','librarian'}: new_role='member'
    u.role=new_role; db.session.commit(); flash(f'{u.name} is now {new_role}.','success'); return redirect(url_for('dashboard'))

@app.post('/admin/members/<int:user_id>/delete')
@admin_required
def delete_member(user_id):
    u=User.query.get_or_404(user_id)
    if Loan.query.filter_by(user_id=user_id,returned_at=None).first(): flash('Member has active issued books. Return them before deleting.','error'); return redirect(url_for('dashboard'))
    Loan.query.filter_by(user_id=user_id).delete(synchronize_session=False); db.session.delete(u); db.session.commit(); flash('Member removed.','success'); return redirect(url_for('dashboard'))

@app.route('/forgot-password',methods=['GET','POST'])
def forgot_password():
    if request.method=='GET':
        return render_template('forgot_password.html',username=session.get('password_reset_username',''),reset_sent=bool(session.get('password_reset_user_id')),reset_verified=bool(session.get('password_reset_verified')),masked_email=session.get('password_reset_masked_email',''))
    username=request.form.get('username','').strip()
    user=User.query.filter(db.func.lower(User.username)==username.casefold()).first() if username else None
    if not user or not user.is_active:
        flash('If the account exists, a password reset code will be sent to its registered email.','success'); return redirect(url_for('forgot_password'))
    if not _brevo_configured(): flash('Password reset email is not configured. Please contact the administrator.','error'); return redirect(url_for('forgot_password'))
    try:
        create_password_reset_otp(user); session['password_reset_user_id']=user.id; session['password_reset_username']=user.username; session['password_reset_masked_email']=mask_email(user.email); session['password_reset_verified']=False; flash(f'📧 OTP sent to {mask_email(user.email)}. It expires in {OTP_EXPIRY_MINUTES} minutes.','success'); write_audit('password_reset_request','user',user.id,'Password reset OTP requested')
    except ValueError as exc: db.session.rollback(); flash(str(exc),'error')
    except Exception: db.session.rollback(); flash('We could not send the reset email right now.','error')
    return redirect(url_for('forgot_password'))

@app.post('/forgot-password/resend')
def resend_forgot_password_otp():
    user_id=session.get('password_reset_user_id'); user=User.query.get(user_id) if user_id else None
    if not user: flash('Reset session expired. Please start again.','error'); return redirect(url_for('forgot_password'))
    try: create_password_reset_otp(user); session['password_reset_verified']=False; flash(f'📧 A new OTP was sent to {mask_email(user.email)}.','success')
    except ValueError as exc: db.session.rollback(); flash(str(exc),'error')
    except Exception: db.session.rollback(); flash('Could not resend OTP right now.','error')
    return redirect(url_for('forgot_password'))

@app.post('/forgot-password/verify')
def verify_forgot_password_otp():
    uid=session.get('password_reset_user_id'); otp=request.form.get('otp','').strip(); row=PasswordReset.query.filter_by(user_id=uid).first() if uid else None
    if not row or not re.fullmatch(r'\d{6}',otp): flash('Enter the 6-digit OTP sent to your registered email.','error'); return redirect(url_for('forgot_password'))
    now=utcnow_naive()
    if row.expires_at < now: flash('OTP expired. Please request a new code.','error'); return redirect(url_for('forgot_password'))
    if row.attempts >= OTP_MAX_ATTEMPTS: flash('Too many incorrect attempts. Please resend a new OTP.','error'); return redirect(url_for('forgot_password'))
    if not check_password_hash(row.otp_hash,otp): row.attempts+=1; db.session.commit(); flash('Invalid OTP.','error'); return redirect(url_for('forgot_password'))
    row.verified_at=now; db.session.commit(); session['password_reset_verified']=True; flash('✅ OTP verified. Set your new password below.','success'); return redirect(url_for('forgot_password'))

@app.post('/forgot-password/reset')
def reset_password_after_otp():
    uid=session.get('password_reset_user_id'); user=User.query.get(uid) if uid else None; row=PasswordReset.query.filter_by(user_id=uid).first() if uid else None
    if not user or not row or not session.get('password_reset_verified') or not row.verified_at: flash('Please verify the OTP first.','error'); return redirect(url_for('forgot_password'))
    if row.expires_at < utcnow_naive(): flash('OTP session expired. Please start again.','error'); return redirect(url_for('forgot_password'))
    new_pw=request.form.get('new_password',''); confirm=request.form.get('confirm_password','')
    if new_pw!=confirm: flash('New passwords do not match.','error'); return redirect(url_for('forgot_password'))
    ok,err=validate_strong_password(new_pw)
    if not ok: flash(err,'error'); return redirect(url_for('forgot_password'))
    if check_password_hash(user.password_hash,new_pw): flash('New password must be different from the current password.','error'); return redirect(url_for('forgot_password'))
    user.password_hash=generate_password_hash(new_pw); user.unlock(); db.session.delete(row); db.session.commit(); write_audit('password_reset','user',user.id,'Password reset completed using email OTP'); email_password_changed(user)
    for k in ('password_reset_user_id','password_reset_username','password_reset_masked_email','password_reset_verified'): session.pop(k,None)
    flash('✅ Password reset successfully. Please login with your new password.','success'); return redirect(url_for('index'))

@app.route('/change-password',methods=['GET','POST'])
@login_required
def change_password():
    if request.method=='GET': return render_template('change_password.html')
    old=request.form.get('current_password',''); new=request.form.get('new_password',''); confirm=request.form.get('confirm_password','')
    if not all([old,new,confirm]): flash('Please fill in all fields.','error'); return redirect(url_for('change_password'))
    if new!=confirm: flash('New passwords do not match.','error'); return redirect(url_for('change_password'))
    ok,pwerr=validate_strong_password(new)
    if not ok: flash(pwerr,'error'); return redirect(url_for('change_password'))
    if is_admin(): flash('Admin password is managed in Vercel Environment Variables.','error'); return redirect(url_for('change_password'))
    u=User.query.get_or_404(session['user_id'])
    if not check_password_hash(u.password_hash,old): flash('Current password is incorrect.','error'); return redirect(url_for('change_password'))
    u.password_hash=generate_password_hash(new); db.session.commit(); email_password_changed(u); session.clear(); flash('Password changed successfully. Please login again.','success'); return redirect(url_for('index'))

@app.get('/profile')
@login_required
def profile():
    if session.get('user_id')=='admin':
        user=None
        return render_template('profile.html',user=user,role='admin',admin_username=session.get('username','admin'))
    user=User.query.get_or_404(session['user_id'])
    locked=user.is_locked()
    security_score=100 if user.is_active and not locked else 60
    return render_template('profile.html',user=user,role=role(),admin_username='',security_score=security_score)

@app.get('/admin/audit-logs')
@admin_required
def admin_audit_logs():
    try:
        rows=db.session.execute(text('SELECT actor_name, action, entity_type, entity_id, details, created_at FROM audit_log ORDER BY created_at DESC LIMIT 200')).mappings().all()
    except Exception:
        db.session.rollback(); rows=[]
    return render_template('audit_logs.html', logs=rows)

@app.get('/admin/analytics')
@admin_required
def admin_analytics():
    try:
        total_members=User.query.filter(User.role.in_(['member','librarian'])).count()
        total_books=Book.query.count(); active_loans=Loan.query.filter(Loan.returned_at.is_(None)).count(); overdue=Loan.query.filter(Loan.returned_at.is_(None),Loan.due_at < utcnow_naive()).count()
        paid=Payment.query.filter(Payment.payment_status.in_(['paid','refunded'])).count(); refunded=Payment.query.filter_by(payment_status='refunded').count()
        revenue=float(db.session.query(db.func.coalesce(db.func.sum(Payment.amount),0)).filter(Payment.payment_status.in_(['paid','refunded'])).scalar() or 0)
    except Exception:
        db.session.rollback(); total_members=total_books=active_loans=overdue=paid=refunded=0; revenue=0
    return render_template('analytics.html',total_members=total_members,total_books=total_books,active_loans=active_loans,overdue=overdue,paid=paid,refunded=refunded,revenue=revenue)

@app.get('/auth-status')
def auth_status(): return {'authenticated':is_logged()}

with app.app_context():
    migrate_existing_db(); migrate_book_schema(); migrate_user_schema(); migrate_auth_schema(); db.create_all(); migrate_return_schema(); migrate_payment_schema(); migrate_audit_log_schema()
    # Seed a small demo collection only when there are no books at all.
    if Book.query.count()==0:
        db.session.add_all([
            Book(book_id='BK0001',title='Python Programming',author='Library Collection',isbn=None,category='Programming',description='A starter programming book.',cover_url=None,total_copies=5,available_copies=5),
            Book(book_id='BK0002',title='Database Systems',author='Library Collection',isbn=None,category='Database',description='Database concepts and SQL.',cover_url=None,total_copies=3,available_copies=3)
        ]); db.session.commit()

if __name__=='__main__': app.run(debug=True)

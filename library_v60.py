"""Additive Library Management System V60 modules.

Loaded by app.py and passed the existing Flask-SQLAlchemy application/models.
This extension deliberately avoids destructive migrations and filesystem uploads.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import io
import json
import os
import re
import smtplib
import urllib.parse
import uuid
from datetime import datetime, timedelta, date
from email.message import EmailMessage
from functools import wraps

from flask import (abort, flash, jsonify, make_response, redirect, render_template,
                   request, send_file, session, url_for)
from sqlalchemy import and_, or_, text, Index
from sqlalchemy.exc import IntegrityError


def register_library_v60(app, db, User, Book, BookLoan, AuditLog,
                         login_required, staff_required, admin_required, log_action):
    """Register tables, helpers and routes. Called once while importing app.py."""

    class LibrarySetting(db.Model):
        __tablename__ = "library_setting"
        key = db.Column(db.String(80), primary_key=True)
        value = db.Column(db.String(500), nullable=False)
        updated_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
        updated_by = db.Column(db.String(80), nullable=False, default="system")

    class BookRequest(db.Model):
        __tablename__ = "book_request"
        id = db.Column(db.Integer, primary_key=True)
        user_id = db.Column(db.Integer, db.ForeignKey("user.user_id"), nullable=False, index=True)
        book_id = db.Column(db.Integer, db.ForeignKey("book.id"), nullable=False, index=True)
        status = db.Column(db.String(20), nullable=False, default="pending", index=True)
        requested_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow, index=True)
        reviewed_at = db.Column(db.DateTime, nullable=True)
        reviewed_by = db.Column(db.String(80), nullable=True)
        review_note = db.Column(db.String(500), nullable=True)
        member = db.relationship(User, foreign_keys=[user_id])
        book = db.relationship(Book, foreign_keys=[book_id])
        __table_args__ = (
            Index(
                "uq_book_request_pending_member_book", "user_id", "book_id",
                unique=True,
                postgresql_where=text("status = 'pending'"),
                sqlite_where=text("status = 'pending'"),
            ),
        )

    class Fine(db.Model):
        __tablename__ = "library_fine"
        id = db.Column(db.Integer, primary_key=True)
        user_id = db.Column(db.Integer, db.ForeignKey("user.user_id"), nullable=False, index=True)
        loan_id = db.Column(db.Integer, db.ForeignKey("book_loan.id"), nullable=True, index=True)
        book_id = db.Column(db.Integer, db.ForeignKey("book.id"), nullable=True, index=True)
        kind = db.Column(db.String(24), nullable=False, default="manual")
        amount = db.Column(db.Float, nullable=False, default=0)
        paid_amount = db.Column(db.Float, nullable=False, default=0)
        status = db.Column(db.String(20), nullable=False, default="open", index=True)
        reason = db.Column(db.String(500), nullable=False, default="")
        created_by = db.Column(db.String(80), nullable=False, default="system")
        created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow, index=True)
        updated_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)
        waived_at = db.Column(db.DateTime, nullable=True)
        waived_by = db.Column(db.String(80), nullable=True)
        waived_reason = db.Column(db.String(500), nullable=True)
        member = db.relationship(User, foreign_keys=[user_id])
        book = db.relationship(Book, foreign_keys=[book_id])
        loan = db.relationship(BookLoan, foreign_keys=[loan_id])

        @property
        def balance(self):
            if self.status in {"paid", "waived"}:
                return 0.0
            return max(0.0, round((self.amount or 0) - (self.paid_amount or 0), 2))

    class DamageRecord(db.Model):
        __tablename__ = "book_damage_record"
        id = db.Column(db.Integer, primary_key=True)
        loan_id = db.Column(db.Integer, db.ForeignKey("book_loan.id"), nullable=False, index=True)
        book_id = db.Column(db.Integer, db.ForeignKey("book.id"), nullable=False, index=True)
        user_id = db.Column(db.Integer, db.ForeignKey("user.user_id"), nullable=False, index=True)
        condition = db.Column(db.String(20), nullable=False)
        damage_type = db.Column(db.String(40), nullable=False, default="none")
        description = db.Column(db.String(1000), nullable=True)
        fine_amount = db.Column(db.Float, nullable=False, default=0)
        photo_data = db.Column(db.Text, nullable=True)
        inspected_by = db.Column(db.String(80), nullable=False)
        created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
        member = db.relationship(User, foreign_keys=[user_id])
        book = db.relationship(Book, foreign_keys=[book_id])

    class FinePayment(db.Model):
        __tablename__ = "library_fine_payment"
        id = db.Column(db.Integer, primary_key=True)
        fine_id = db.Column(db.Integer, db.ForeignKey("library_fine.id"), nullable=False, index=True)
        user_id = db.Column(db.Integer, db.ForeignKey("user.user_id"), nullable=False, index=True)
        amount = db.Column(db.Float, nullable=False)
        method = db.Column(db.String(20), nullable=False, default="upi")
        transaction_id = db.Column(db.String(100), nullable=False, unique=True, index=True)
        proof_data = db.Column(db.Text, nullable=True)
        status = db.Column(db.String(20), nullable=False, default="pending", index=True)
        submitted_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
        reviewed_at = db.Column(db.DateTime, nullable=True)
        reviewed_by = db.Column(db.String(80), nullable=True)
        review_note = db.Column(db.String(500), nullable=True)
        member = db.relationship(User, foreign_keys=[user_id])
        fine = db.relationship(Fine, foreign_keys=[fine_id])

    class FineDispute(db.Model):
        __tablename__ = "library_fine_dispute"
        id = db.Column(db.Integer, primary_key=True)
        fine_id = db.Column(db.Integer, db.ForeignKey("library_fine.id"), nullable=False, index=True)
        user_id = db.Column(db.Integer, db.ForeignKey("user.user_id"), nullable=False, index=True)
        reason = db.Column(db.String(1000), nullable=False)
        status = db.Column(db.String(20), nullable=False, default="pending", index=True)
        created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
        reviewed_at = db.Column(db.DateTime, nullable=True)
        reviewed_by = db.Column(db.String(80), nullable=True)
        response = db.Column(db.String(1000), nullable=True)
        member = db.relationship(User, foreign_keys=[user_id])
        fine = db.relationship(Fine, foreign_keys=[fine_id])

    class LibraryEmailLog(db.Model):
        __tablename__ = "library_email_log"
        id = db.Column(db.Integer, primary_key=True)
        event_key = db.Column(db.String(180), nullable=False, unique=True, index=True)
        recipient = db.Column(db.String(160), nullable=False)
        event_type = db.Column(db.String(40), nullable=False)
        sent_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
        subject = db.Column(db.String(180), nullable=False, default="Library notification")
        status = db.Column(db.String(20), nullable=False, default="sent")
        error = db.Column(db.String(500), nullable=True)

    class BackupRecord(db.Model):
        __tablename__ = "library_backup_record"
        id = db.Column(db.Integer, primary_key=True)
        file_name = db.Column(db.String(180), nullable=False)
        checksum = db.Column(db.String(64), nullable=False)
        storage_uri = db.Column(db.String(500), nullable=True)
        size_bytes = db.Column(db.Integer, nullable=False, default=0)
        status = db.Column(db.String(20), nullable=False, default="created")
        created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
        created_by = db.Column(db.String(80), nullable=False, default="system")

    # Export model handles to app.py globals for app.py migrations/helpers and tests.
    app.extensions["library_v60_models"] = {
        "LibrarySetting": LibrarySetting, "BookRequest": BookRequest, "Fine": Fine,
        "DamageRecord": DamageRecord, "FinePayment": FinePayment,
        "FineDispute": FineDispute, "LibraryEmailLog": LibraryEmailLog,
        "BackupRecord": BackupRecord,
    }

    VALID_DAMAGE = {"none", "cover_damaged", "torn_pages", "water_damage", "other", "lost"}
    VALID_CONDITIONS = {"good", "damaged", "lost"}

    def setting(key, default=""):
        row = db.session.get(LibrarySetting, key)
        return row.value if row else default

    def set_setting(key, value):
        row = db.session.get(LibrarySetting, key)
        if row is None:
            row = LibrarySetting(key=key, value=str(value), updated_by=session.get("username", "system"))
            db.session.add(row)
        else:
            row.value = str(value)
            row.updated_by = session.get("username", "system")
            row.updated_at = datetime.utcnow()

    def librarian_fine_allowed():
        return session.get("role") == "admin" or setting("librarian_fine_permission", "0") == "1"

    def fine_rate():
        try:
            return max(0.0, min(10000.0, float(setting("fine_per_day", "10"))))
        except (TypeError, ValueError):
            return 10.0

    def grace_days():
        try:
            return max(0, min(30, int(setting("fine_grace_days", "0"))))
        except (TypeError, ValueError):
            return 0

    def fine_balance(user_id):
        rows = Fine.query.filter_by(user_id=user_id).filter(Fine.status == "open").all()
        return round(sum(f.balance for f in rows), 2)

    def safe_email(to, subject, body, event_key=None, event_type="library"):
        """Send transactional SMTP mail and independently record sent/skipped/failed."""
        to = (to or "").strip()
        subject = (subject or "Library notification")[:180]
        log_key = event_key or f"{event_type}:{uuid.uuid4().hex}"
        existing = LibraryEmailLog.query.filter_by(event_key=log_key).first()
        if existing and existing.status == "sent":
            return True

        def record(status, error=None):
            try:
                values = {"event_key": log_key, "recipient": to[:160], "event_type": (event_type or "library")[:40],
                          "sent_at": datetime.utcnow(), "subject": subject, "status": status,
                          "error": (str(error)[:500] if error else None)}
                with db.engine.begin() as connection:
                    if existing:
                        connection.execute(LibraryEmailLog.__table__.update().where(
                            LibraryEmailLog.event_key == log_key).values(**{k: v for k, v in values.items() if k != "event_key"}))
                    else:
                        connection.execute(LibraryEmailLog.__table__.insert().values(**values))
            except IntegrityError:
                pass  # Another worker recorded the same idempotency key.
            except Exception:
                app.logger.exception("Could not record library email event")

        host = os.environ.get("SMTP_HOST", "").strip()
        smtp_user = os.environ.get("SMTP_USER", "").strip()
        smtp_pass = os.environ.get("SMTP_PASSWORD", "").strip()
        sender = (os.environ.get("SMTP_FROM", "").strip() or smtp_user)
        if not (to and host and smtp_user and smtp_pass and sender):
            app.logger.info("Library email skipped: SMTP is not configured")
            record("skipped", "SMTP sender/host/credentials are not configured")
            return False

        msg = EmailMessage()
        msg["From"] = sender
        msg["To"] = to
        msg["Subject"] = subject
        msg.set_content(body or "")
        try:
            port = int(os.environ.get("SMTP_PORT", "587"))
            if port == 465:
                with smtplib.SMTP_SSL(host, port, timeout=15) as server:
                    server.login(smtp_user, smtp_pass)
                    server.send_message(msg)
            else:
                with smtplib.SMTP(host, port, timeout=15) as server:
                    server.ehlo()
                    server.starttls()
                    server.ehlo()
                    server.login(smtp_user, smtp_pass)
                    server.send_message(msg)
            record("sent")
            return True
        except Exception as exc:
            app.logger.exception("Library notification email failed")
            record("failed", exc)
            return False

    def notify_member(member, subject, body, event_key=None, event_type="library"):
        if member:
            safe_email(member.email, subject, body, event_key=event_key, event_type=event_type)

    def book_code(book):
        return f"BK-{book.id:06d}"

    def get_due_fine(loan, at=None):
        if not loan.due_at:
            return 0.0, 0
        at = at or datetime.utcnow()
        days_late = max(0, (at.date() - loan.due_at.date()).days - grace_days())
        return round(days_late * fine_rate(), 2), days_late

    def add_fine(user_id, loan_id, book_id, kind, amount, reason, created_by):
        amount = round(float(amount or 0), 2)
        if amount <= 0:
            return None
        # Idempotency for automated late fines: one cumulative late fine per loan.
        # The daily cron may refresh it while overdue; return inspection recalculates
        # the final amount. Existing payments are never erased by a recalculation.
        if kind == "late" and loan_id:
            existing = Fine.query.filter_by(loan_id=loan_id, kind="late").first()
            if existing:
                if existing.status == "open":
                    existing.amount = max(round(existing.paid_amount or 0, 2), amount)
                    existing.updated_at = datetime.utcnow()
                return existing
        fine = Fine(user_id=user_id, loan_id=loan_id, book_id=book_id, kind=kind,
                    amount=amount, paid_amount=0, status="open", reason=(reason or "")[:500],
                    created_by=(created_by or "system")[:80])
        db.session.add(fine)
        db.session.flush()
        member = db.session.get(User, user_id)
        book = db.session.get(Book, book_id) if book_id else None
        if member:
            notify_member(member, f"Library fine added — ₹{amount:.2f}",
                          f"A fine of ₹{amount:.2f} has been added.\nBook: {book.title if book else 'Library'}\nReason: {reason}\nOutstanding balance: ₹{fine_balance(user_id):.2f}",
                          event_type="fine")
        return fine

    def create_issue(member, book, issuer):
        if not member or not book or not member.is_active or not book.is_active:
            raise ValueError("Member or book is inactive or unavailable.")
        # Serialize issue operations for one member on PostgreSQL. This prevents two
        # simultaneous requests from both passing the five-active-loan check.
        locked_member = User.query.filter_by(id=member.id).with_for_update().first()
        if not locked_member or not locked_member.is_active:
            raise ValueError("Member account is inactive or unavailable.")
        if (book.available_copies or 0) < 1:
            raise ValueError("This title is sold out. Submit a request for the Admin.")
        if BookLoan.query.filter_by(user_id=member.id, status="issued").count() >= 5:
            raise ValueError("A member may keep a maximum of 5 active books.")
        if BookLoan.query.filter_by(user_id=member.id, book_id=book.id, status="issued").first():
            raise ValueError("This member already has this book issued.")
        changed = Book.query.filter(Book.id == book.id, Book.available_copies > 0).update(
            {Book.available_copies: Book.available_copies - 1}, synchronize_session=False)
        if changed != 1:
            raise ValueError("The last available copy was just issued. Refresh and try again.")
        now = datetime.utcnow()
        loan = BookLoan(user_id=member.id, book_id=book.id, status="issued", issued_at=now,
                        due_at=now + timedelta(days=14), issued_by=(issuer or "system")[:80])
        db.session.add(loan)
        db.session.flush()
        return loan

    def notify_issued(loan):
        """Send issue confirmation only after the caller commits the loan."""
        if not loan:
            return
        member = db.session.get(User, loan.user_id)
        book = db.session.get(Book, loan.book_id)
        if member and book:
            notify_member(member, f"Book issued: {book.title}",
                          f"Hello {member.name},\n\nYour book has been issued.\nBook: {book.title}\nBook ID: {book_code(book)}\nIssue date: {loan.issued_at:%d %b %Y}\nDue date: {loan.due_at:%d %b %Y}\n\nPlease return it on or before the due date.",
                          event_key=f"issue:{loan.id}", event_type="issue")

    def queue_availability_emails(book):
        pending = BookRequest.query.filter_by(book_id=book.id, status="pending").all()
        for item in pending:
            member = db.session.get(User, item.user_id)
            if member:
                notify_member(member, f"Book available: {book.title}",
                              f"The book {book.title} ({book_code(book)}) is now available. Please sign in and check your request.",
                              event_key=f"available:{item.id}", event_type="availability")

    def return_loan(loan, condition="good", damage_type="none", description="", damage_fine=0, photo_data=None):
        if not loan or loan.status != "issued":
            raise ValueError("Active book loan not found.")
        if condition not in VALID_CONDITIONS or damage_type not in VALID_DAMAGE:
            raise ValueError("Select a valid inspection condition and damage type.")
        book = db.session.get(Book, loan.book_id)
        member = db.session.get(User, loan.user_id)
        now = datetime.utcnow()
        late_amount, late_days = get_due_fine(loan, now)
        late_fine = add_fine(loan.user_id, loan.id, loan.book_id, "late", late_amount,
                             f"Returned {late_days} day(s) late at ₹{fine_rate():.2f}/day", session.get("username", "system"))
        loan.status = "returned"
        loan.returned_at = now
        if book:
            if condition == "good":
                book.available_copies = min(book.total_copies or 0, (book.available_copies or 0) + 1)
            elif condition in {"damaged", "lost"}:
                # A damaged/lost physical copy is removed from the active circulating
                # total so a later catalogue edit cannot silently put it back in stock.
                book.total_copies = max(0, (book.total_copies or 0) - 1)
            # Damaged/lost copies remain in the separate inspection history.
        record = DamageRecord(loan_id=loan.id, book_id=loan.book_id, user_id=loan.user_id,
                              condition=condition, damage_type=damage_type,
                              description=(description or "")[:1000], fine_amount=max(0, float(damage_fine or 0)),
                              photo_data=photo_data, inspected_by=session.get("username", "staff"))
        db.session.add(record)
        if condition != "good" and damage_fine:
            add_fine(loan.user_id, loan.id, loan.book_id, "damage", damage_fine,
                     f"{damage_type.replace('_', ' ').title()}: {description or 'Damage/loss inspection'}",
                     session.get("username", "staff"))
        db.session.commit()
        log_action(session.get("username", "staff"), "BOOK_RETURN_INSPECTED",
                   f"Loan {loan.id}; condition={condition}; damage={damage_type}; late fine=₹{late_amount:.2f}")
        if member and book:
            notify_member(member, f"Book returned: {book.title}",
                          f"Your return was recorded.\nBook: {book.title}\nCondition: {condition}\nLate fine added: ₹{late_amount:.2f}\nCurrent fine balance: ₹{fine_balance(member.id):.2f}",
                          event_key=f"return:{loan.id}", event_type="return")
        if book and book.available_copies > 0:
            queue_availability_emails(book)
        return late_amount

    def staff_route(f):
        @wraps(f)
        def wrapped(*args, **kwargs):
            if not session.get("user_id"):
                flash("Please sign in.", "error")
                return redirect(url_for("login_page"))
            if session.get("role") not in {"admin", "librarian"}:
                abort(403)
            return f(*args, **kwargs)
        return wrapped

    def book_detail_or_404(book_id):
        return db.session.get(Book, book_id) or abort(404)

    @app.route("/book/<int:book_id>")
    @login_required
    def book_detail(book_id):
        book = book_detail_or_404(book_id)
        active_loan = BookLoan.query.filter_by(user_id=session["user_id"], book_id=book.id, status="issued").first()
        pending_request = BookRequest.query.filter_by(user_id=session["user_id"], book_id=book.id, status="pending").first()
        return render_template("book_detail_v60.html", book=book, book_code=book_code(book),
                               active_loan=active_loan, pending_request=pending_request,
                               role=session.get("role"))

    @app.route("/books/<int:book_id>/qr.png")
    @login_required
    def book_qr(book_id):
        book = book_detail_or_404(book_id)
        try:
            import qrcode
        except ImportError:
            return "QR generation package is not installed. Install qrcode[pil].", 503
        target = url_for("book_detail", book_id=book.id, _external=True)
        qr = qrcode.QRCode(version=None, error_correction=qrcode.constants.ERROR_CORRECT_M, box_size=8, border=3)
        qr.add_data(target)
        qr.make(fit=True)
        image = qr.make_image(fill_color="black", back_color="white")
        stream = io.BytesIO()
        image.save(stream, format="PNG")
        stream.seek(0)
        return send_file(stream, mimetype="image/png", download_name=f"{book_code(book)}-QR.png")

    @app.route("/books/<int:book_id>/barcode.png")
    @login_required
    def book_barcode(book_id):
        book = book_detail_or_404(book_id)
        try:
            import barcode
            from barcode.writer import ImageWriter
        except ImportError:
            return "Barcode generation package is not installed. Install python-barcode[images].", 503
        code = barcode.get("code128", book_code(book), writer=ImageWriter())
        stream = io.BytesIO()
        code.write(stream, options={"write_text": True, "module_height": 12, "quiet_zone": 3})
        stream.seek(0)
        return send_file(stream, mimetype="image/png", download_name=f"{book_code(book)}-barcode.png")

    @app.route("/library/scanner")
    @login_required
    def library_scanner():
        return render_template("library_scanner_v60.html")

    @app.route("/books/<int:book_id>/request", methods=["POST"])
    @login_required
    def request_sold_out_book(book_id):
        member = db.session.get(User, session["user_id"])
        book = book_detail_or_404(book_id)
        if not member or not member.is_active or not book.is_active:
            abort(403)
        if BookLoan.query.filter_by(user_id=member.id, book_id=book.id, status="issued").first():
            flash("You already have this book issued.", "info")
            return redirect(url_for("book_detail", book_id=book.id))
        if book.available_copies > 0:
            flash("This book is available now. You can issue it directly.", "info")
            return redirect(url_for("book_detail", book_id=book.id))
        existing = BookRequest.query.filter_by(user_id=member.id, book_id=book.id, status="pending").first()
        if existing:
            flash("You already have a pending request for this book.", "info")
            return redirect(url_for("my_book_requests"))
        item = BookRequest(user_id=member.id, book_id=book.id)
        db.session.add(item)
        try:
            db.session.commit()
        except IntegrityError:
            db.session.rollback()
            flash("You already have a pending request for this book.", "info")
            return redirect(url_for("my_book_requests"))
        log_action(member.username, "BOOK_AVAILABILITY_REQUESTED", f"{book.title} ({book_code(book)}) request #{item.id}")
        notify_member(member, f"Request received: {book.title}",
                      f"Your request for {book.title} ({book_code(book)}) was received on {item.requested_at:%d %b %Y %H:%M}.",
                      event_key=f"request:{item.id}", event_type="request")
        admin_email = os.environ.get("ADMIN_EMAIL", "").strip()
        if admin_email:
            safe_email(admin_email, f"Sold-out book request: {book.title}",
                       f"Member: {member.name} ({member.employee_id})\nBook: {book.title} ({book_code(book)})\nRequest ID: {item.id}",
                       event_key=f"admin-request:{item.id}", event_type="admin_alert")
        flash("Your request was sent to the Admin.", "success")
        return redirect(url_for("my_book_requests"))

    @app.route("/my-requests")
    @login_required
    def my_book_requests():
        rows = (BookRequest.query.filter_by(user_id=session["user_id"])
                .order_by(BookRequest.requested_at.desc()).limit(100).all())
        return render_template("my_requests_v60.html", rows=rows)

    @app.route("/book-requests/<int:request_id>/cancel", methods=["POST"])
    @login_required
    def cancel_book_request(request_id):
        item = BookRequest.query.filter_by(id=request_id, user_id=session["user_id"], status="pending").first_or_404()
        item.status = "cancelled"
        item.reviewed_at = datetime.utcnow()
        item.reviewed_by = session.get("username", "user")
        db.session.commit()
        log_action(session.get("username"), "BOOK_REQUEST_CANCELLED", f"Request #{item.id}")
        flash("Pending request cancelled.", "success")
        return redirect(url_for("my_book_requests"))

    @app.route("/staff/users/<int:user_id>/issue-book", methods=["POST"])
    @staff_route
    def staff_issue_book(user_id):
        member = db.session.get(User, user_id)
        book = db.session.get(Book, request.form.get("book_id", type=int))
        try:
            loan = create_issue(member, book, session.get("username", "librarian"))
            db.session.commit()
            notify_issued(loan)
        except ValueError as exc:
            db.session.rollback()
            flash(str(exc), "error")
            return redirect(url_for("dashboard"))
        log_action(session.get("username"), "STAFF_BOOK_ISSUED", f"Loan #{loan.id}; {book.title} to {member.username}")
        flash(f"{book.title} issued to {member.name}.", "success")
        return redirect(url_for("dashboard"))

    @app.route("/admin/book-requests")
    @staff_route
    def admin_book_requests():
        status = request.args.get("status", "pending")
        q = BookRequest.query
        if status in {"pending", "approved", "rejected", "cancelled"}:
            q = q.filter_by(status=status)
        rows = q.order_by(BookRequest.requested_at.desc()).limit(300).all()
        return render_template("admin_requests_v60.html", rows=rows, status=status)

    @app.route("/admin/book-requests/<int:request_id>/review", methods=["POST"])
    @staff_route
    def review_book_request(request_id):
        item = BookRequest.query.get_or_404(request_id)
        member = db.session.get(User, item.user_id)
        book = db.session.get(Book, item.book_id)
        action = request.form.get("action", "").lower()
        if item.status != "pending":
            flash("This request has already been reviewed.", "error")
            return redirect(url_for("admin_book_requests"))
        note = request.form.get("note", "").strip()[:500]
        if action == "approve":
            try:
                issued_loan = create_issue(member, book, session.get("username", "librarian"))
            except ValueError as exc:
                db.session.rollback()
                flash(str(exc), "error")
                return redirect(url_for("admin_book_requests"))
            item.status = "approved"
            item.reviewed_at = datetime.utcnow()
            item.reviewed_by = session.get("username")
            item.review_note = note
            db.session.commit()
            notify_issued(issued_loan)
            log_action(session.get("username"), "BOOK_REQUEST_APPROVED", f"Request #{item.id}; member={member.username}; book={book.title}")
            flash("Request approved and book issued.", "success")
        elif action == "reject":
            item.status = "rejected"
            item.reviewed_at = datetime.utcnow()
            item.reviewed_by = session.get("username")
            item.review_note = note or "Request rejected by library staff."
            db.session.commit()
            if member and book:
                notify_member(member, f"Book request update: {book.title}",
                              f"Your request for {book.title} ({book_code(book)}) was rejected.\nReason: {item.review_note}",
                              event_key=f"rejected:{item.id}", event_type="request")
            log_action(session.get("username"), "BOOK_REQUEST_REJECTED", f"Request #{item.id}; {note}")
            flash("Request rejected and member notified.", "success")
        else:
            flash("Choose approve or reject.", "error")
        return redirect(url_for("admin_book_requests"))

    @app.route("/admin/book-loans/<int:loan_id>/inspect-return", methods=["POST"])
    @staff_route
    def inspect_return(loan_id):
        loan = db.session.get(BookLoan, loan_id)
        if not loan or loan.status != "issued":
            flash("Active loan not found.", "error")
            return redirect(url_for("dashboard"))
        condition = request.form.get("condition", "good")
        damage_type = request.form.get("damage_type", "none")
        desc = request.form.get("description", "").strip()
        try:
            damage_fine = float(request.form.get("damage_fine", "0") or 0)
            if damage_fine > 0 and not librarian_fine_allowed():
                raise ValueError("Admin has not granted the Librarian permission to add damage fines.")
            if damage_fine < 0 or damage_fine > 1000000:
                raise ValueError
        except ValueError:
            flash("Enter a valid damage fine.", "error")
            return redirect(url_for("dashboard"))
        photo_data = None
        photo = request.files.get("damage_photo")
        if photo and photo.filename:
            raw = photo.read(500_001)
            if len(raw) > 500_000 or (photo.mimetype or "").lower() not in {"image/jpeg", "image/png", "image/webp"}:
                flash("Damage photo must be JPG, PNG or WebP under 500 KB.", "error")
                return redirect(url_for("dashboard"))
            photo_data = f"data:{photo.mimetype};base64," + base64.b64encode(raw).decode("ascii")
        try:
            late = return_loan(loan, condition, damage_type, desc, damage_fine, photo_data)
        except ValueError as exc:
            db.session.rollback()
            flash(str(exc), "error")
            return redirect(url_for("dashboard"))
        flash(f"Return inspected. Automatic late fine: ₹{late:.2f}.", "success")
        return redirect(url_for("admin_fines"))

    @app.route("/library/upi-qr/<int:fine_id>.png")
    @login_required
    def fine_upi_qr(fine_id):
        fine = Fine.query.filter_by(id=fine_id, user_id=session["user_id"]).first_or_404()
        upi_id = setting("upi_id", os.environ.get("LIBRARY_UPI_ID", ""))
        upi_name = setting("upi_name", os.environ.get("LIBRARY_UPI_NAME", "Library"))
        if not upi_id:
            return "UPI ID is not configured. Contact the library admin.", 404
        try:
            requested_amount = round(float(request.args.get("amount", fine.balance)), 2)
        except (TypeError, ValueError):
            return "Invalid UPI payment amount.", 400
        if requested_amount <= 0 or requested_amount > fine.balance:
            return "UPI payment amount exceeds the outstanding fine balance.", 400
        params = urllib.parse.urlencode({"pa": upi_id, "pn": upi_name, "am": f"{requested_amount:.2f}",
                                         "cu": "INR", "tn": f"Library fine {fine.id}"})
        uri = "upi://pay?" + params
        import qrcode
        qr = qrcode.QRCode(box_size=8, border=3)
        qr.add_data(uri)
        qr.make(fit=True)
        image = qr.make_image(fill_color="black", back_color="white")
        stream = io.BytesIO()
        image.save(stream, format="PNG")
        stream.seek(0)
        return send_file(stream, mimetype="image/png", download_name=f"fine-{fine.id}-upi.png")

    @app.route("/my-fines")
    @login_required
    def my_fines():
        fines = Fine.query.filter_by(user_id=session["user_id"]).order_by(Fine.created_at.desc()).all()
        payments = FinePayment.query.filter_by(user_id=session["user_id"]).order_by(FinePayment.submitted_at.desc()).limit(100).all()
        disputes = FineDispute.query.filter_by(user_id=session["user_id"]).order_by(FineDispute.created_at.desc()).limit(100).all()
        damages = DamageRecord.query.filter_by(user_id=session["user_id"]).order_by(DamageRecord.created_at.desc()).limit(100).all()
        upi_id = setting("upi_id", os.environ.get("LIBRARY_UPI_ID", ""))
        upi_name = setting("upi_name", os.environ.get("LIBRARY_UPI_NAME", "Library"))
        return render_template("my_fines_v60.html", fines=fines, payments=payments,
                               disputes=disputes, damages=damages, upi_id=upi_id, upi_name=upi_name)

    @app.route("/fines/<int:fine_id>/pay", methods=["POST"])
    @login_required
    def submit_fine_payment(fine_id):
        fine = Fine.query.filter_by(id=fine_id, user_id=session["user_id"]).first_or_404()
        if not setting("upi_id", os.environ.get("LIBRARY_UPI_ID", "")):
            flash("UPI ID has not been configured by the Admin yet.", "error")
            return redirect(url_for("my_fines"))
        if fine.status != "open" or fine.balance <= 0:
            flash("This fine has no remaining balance.", "error")
            return redirect(url_for("my_fines"))
        try:
            amount = round(float(request.form.get("amount", "0")), 2)
            if amount <= 0 or amount > fine.balance:
                raise ValueError
        except ValueError:
            flash(f"Enter an amount from ₹0.01 to ₹{fine.balance:.2f}.", "error")
            return redirect(url_for("my_fines"))
        txn = request.form.get("transaction_id", "").strip()
        if not re.fullmatch(r"[A-Za-z0-9@._-]{6,100}", txn):
            flash("Enter a valid UPI transaction/reference ID (6–100 characters).", "error")
            return redirect(url_for("my_fines"))
        proof_data = None
        proof = request.files.get("proof")
        if proof and proof.filename:
            raw = proof.read(500_001)
            if len(raw) > 500_000 or (proof.mimetype or "").lower() not in {"image/jpeg", "image/png", "image/webp"}:
                flash("Payment screenshot must be JPG, PNG or WebP under 500 KB.", "error")
                return redirect(url_for("my_fines"))
            proof_data = f"data:{proof.mimetype};base64," + base64.b64encode(raw).decode("ascii")
        payment = FinePayment(fine_id=fine.id, user_id=fine.user_id, amount=amount,
                              transaction_id=txn, proof_data=proof_data, status="pending")
        db.session.add(payment)
        try:
            db.session.commit()
        except IntegrityError:
            db.session.rollback()
            flash("That transaction ID has already been submitted.", "error")
            return redirect(url_for("my_fines"))
        log_action(session.get("username"), "FINE_PAYMENT_SUBMITTED", f"Fine #{fine.id}; ₹{amount:.2f}; UPI ref {txn}")
        admin_email = os.environ.get("ADMIN_EMAIL", "").strip()
        if admin_email:
            safe_email(admin_email, "Library UPI payment needs verification",
                       f"Member: {session.get('name')}\nFine ID: {fine.id}\nAmount: ₹{amount:.2f}\nTransaction ID: {txn}",
                       event_key=f"admin-payment:{payment.id}", event_type="admin_alert")
        flash("Payment submitted. It will be counted only after Admin verification.", "success")
        return redirect(url_for("my_fines"))

    @app.route("/admin/payments")
    @admin_required
    def admin_payments():
        status = request.args.get("status", "pending")
        q = FinePayment.query
        if status in {"pending", "approved", "rejected"}:
            q = q.filter_by(status=status)
        rows = q.order_by(FinePayment.submitted_at.desc()).limit(300).all()
        return render_template("admin_payments_v60.html", rows=rows, status=status)

    @app.route("/admin/payments/<int:payment_id>/review", methods=["POST"])
    @admin_required
    def review_payment(payment_id):
        payment = FinePayment.query.get_or_404(payment_id)
        if payment.status != "pending":
            flash("This payment has already been reviewed.", "error")
            return redirect(url_for("admin_payments"))
        action = request.form.get("action", "")
        note = request.form.get("note", "").strip()[:500]
        fine = db.session.get(Fine, payment.fine_id)
        member = db.session.get(User, payment.user_id)
        if not fine:
            flash("Associated fine no longer exists; payment was not changed.", "error")
            return redirect(url_for("admin_payments"))
        payment.reviewed_at = datetime.utcnow()
        payment.reviewed_by = session.get("username")
        payment.review_note = note
        if action == "approve":
            if payment.amount > fine.balance + 0.001:
                flash("Payment exceeds the remaining balance. Reconcile before approval.", "error")
                return redirect(url_for("admin_payments"))
            payment.status = "approved"
            fine.paid_amount = round((fine.paid_amount or 0) + payment.amount, 2)
            if fine.balance <= 0.01:
                fine.status = "paid"
            db.session.commit()
            log_action(session.get("username"), "FINE_PAYMENT_APPROVED", f"Payment #{payment.id}; ₹{payment.amount:.2f}; transaction={payment.transaction_id}")
            if member:
                receipt_url = url_for("payment_receipt_pdf", payment_id=payment.id, _external=True)
                notify_member(member, "Library payment verified",
                              f"Your UPI payment of ₹{payment.amount:.2f} was verified. Transaction ID: {payment.transaction_id}\nPDF receipt: {receipt_url}",
                              event_key=f"payment-approved:{payment.id}", event_type="payment")
            flash("Payment verified. Fine balance updated.", "success")
        elif action == "reject":
            payment.status = "rejected"
            db.session.commit()
            log_action(session.get("username"), "FINE_PAYMENT_REJECTED", f"Payment #{payment.id}; {note}")
            if member:
                notify_member(member, "Library payment needs attention",
                              f"Your UPI payment submission was not approved.\nTransaction ID: {payment.transaction_id}\nReason: {note or 'Please contact the library administrator.'}",
                              event_key=f"payment-rejected:{payment.id}", event_type="payment")
            flash("Payment rejected; fine balance remains unchanged.", "success")
        else:
            flash("Choose approve or reject.", "error")
        return redirect(url_for("admin_payments"))

    @app.route("/payments/<int:payment_id>/receipt.pdf")
    @login_required
    def payment_receipt_pdf(payment_id):
        payment = FinePayment.query.get_or_404(payment_id)
        if payment.user_id != session["user_id"] and session.get("role") != "admin":
            abort(403)
        if payment.status != "approved":
            abort(404)
        try:
            from reportlab.lib.pagesizes import A4
            from reportlab.pdfgen import canvas
        except ImportError:
            return "PDF support is not installed. Install reportlab.", 503
        buffer = io.BytesIO()
        c = canvas.Canvas(buffer, pagesize=A4)
        c.setTitle(f"Payment Receipt {payment.id}")
        c.setFont("Helvetica-Bold", 20)
        c.drawString(55, 790, "LIBRARY PAYMENT RECEIPT")
        c.setFont("Helvetica", 11)
        fine = db.session.get(Fine, payment.fine_id)
        member = db.session.get(User, payment.user_id)
        lines = [f"Receipt No: LMS-{payment.id:07d}", f"Date: {(payment.reviewed_at or payment.submitted_at):%d %b %Y %H:%M}",
                 f"Member: {member.name if member else payment.user_id}", f"Library ID: {member.employee_id if member else '—'}",
                 f"Fine ID: {payment.fine_id}", f"Amount received: INR {payment.amount:.2f}",
                 f"UPI transaction ID: {payment.transaction_id}", "Status: VERIFIED / PAID",
                 f"Fine remaining balance: INR {fine.balance if fine else 0:.2f}", "Developed by Aman Kumar"]
        y = 745
        for line in lines:
            c.drawString(60, y, line[:110])
            y -= 28
        c.line(55, y - 5, 540, y - 5)
        c.setFont("Helvetica-Oblique", 9)
        c.drawString(60, y - 28, "This is a computer-generated receipt for a verified library payment.")
        c.save()
        buffer.seek(0)
        return send_file(buffer, mimetype="application/pdf", as_attachment=True,
                         download_name=f"LMS-receipt-{payment.id}.pdf")

    @app.route("/fines/<int:fine_id>/dispute", methods=["POST"])
    @login_required
    def dispute_fine(fine_id):
        fine = Fine.query.filter_by(id=fine_id, user_id=session["user_id"], status="open").first_or_404()
        reason = request.form.get("reason", "").strip()
        if len(reason) < 10:
            flash("Please explain your dispute in at least 10 characters.", "error")
            return redirect(url_for("my_fines"))
        existing = FineDispute.query.filter_by(fine_id=fine.id, status="pending").first()
        if existing:
            flash("A dispute is already pending for this fine.", "info")
            return redirect(url_for("my_fines"))
        dispute = FineDispute(fine_id=fine.id, user_id=fine.user_id, reason=reason[:1000])
        db.session.add(dispute)
        db.session.commit()
        log_action(session.get("username"), "FINE_DISPUTED", f"Fine #{fine.id}; dispute #{dispute.id}")
        admin_email = os.environ.get("ADMIN_EMAIL", "").strip()
        member = db.session.get(User, session["user_id"])
        if admin_email:
            safe_email(admin_email, "Library fine dispute submitted", f"Member: {member.name if member else session.get('username')}\nFine ID: {fine.id}\nDispute: {reason}", event_key=f"admin-dispute:{dispute.id}", event_type="admin_alert")
        flash("Fine dispute submitted to Admin.", "success")
        return redirect(url_for("my_fines"))

    @app.route("/admin/fines")
    @staff_route
    def admin_fines():
        fines = Fine.query.order_by(Fine.created_at.desc()).limit(500).all()
        damages = DamageRecord.query.order_by(DamageRecord.created_at.desc()).limit(100).all()
        disputes = FineDispute.query.order_by(FineDispute.created_at.desc()).limit(100).all()
        return render_template("admin_fines_v60.html", fines=fines, damages=damages, disputes=disputes,
                               members=User.query.filter_by(is_active=True).order_by(User.name.asc()).limit(1000).all(),
                               can_manage_fines=librarian_fine_allowed())

    @app.route("/admin/fines/add", methods=["POST"])
    @staff_route
    def add_manual_fine():
        if not librarian_fine_allowed():
            abort(403)
        member = db.session.get(User, request.form.get("user_id", type=int))
        try:
            amount = round(float(request.form.get("amount", "0")), 2)
            if not member or amount <= 0 or amount > 1000000:
                raise ValueError
        except (ValueError, TypeError):
            flash("Select a member and enter a valid fine amount.", "error")
            return redirect(url_for("admin_fines"))
        reason = request.form.get("reason", "").strip()
        if len(reason) < 3:
            flash("A reason is required for every additional fine.", "error")
            return redirect(url_for("admin_fines"))
        fine = add_fine(member.id, None, None, "manual", amount, reason, session.get("username"))
        db.session.commit()
        log_action(session.get("username"), "MANUAL_FINE_ADDED", f"Fine #{fine.id}; member={member.username}; ₹{amount:.2f}; {reason}")
        flash("Additional fine added and member notified.", "success")
        return redirect(url_for("admin_fines"))

    @app.route("/admin/fines/<int:fine_id>/adjust", methods=["POST"])
    @staff_route
    def adjust_fine(fine_id):
        if not librarian_fine_allowed():
            abort(403)
        fine = Fine.query.get_or_404(fine_id)
        try:
            amount = round(float(request.form.get("amount", "0")), 2)
            if amount < fine.paid_amount or amount > 1000000:
                raise ValueError
        except ValueError:
            flash("New fine total cannot be below the amount already paid.", "error")
            return redirect(url_for("admin_fines"))
        reason = request.form.get("reason", "").strip()
        if len(reason) < 3:
            flash("A reason is required for every adjustment.", "error")
            return redirect(url_for("admin_fines"))
        old = fine.amount
        fine.amount = amount
        fine.status = "paid" if fine.balance <= 0.01 else "open"
        db.session.commit()
        log_action(session.get("username"), "FINE_ADJUSTED", f"Fine #{fine.id}; ₹{old:.2f} -> ₹{amount:.2f}; {reason}")
        member = db.session.get(User, fine.user_id)
        if member:
            notify_member(member, "Library fine updated", f"Fine #{fine.id} was updated from ₹{old:.2f} to ₹{amount:.2f}. Reason: {reason}. Current balance: ₹{fine.balance:.2f}.", event_key=f"fine-adjusted:{fine.id}:{fine.updated_at.isoformat() if getattr(fine, 'updated_at', None) else datetime.utcnow().isoformat()}", event_type="fine")
        flash("Fine amount updated. Audit history recorded.", "success")
        return redirect(url_for("admin_fines"))

    @app.route("/admin/fines/<int:fine_id>/waive", methods=["POST"])
    @staff_route
    def waive_fine(fine_id):
        if not librarian_fine_allowed():
            abort(403)
        fine = Fine.query.get_or_404(fine_id)
        reason = request.form.get("reason", "").strip()
        if not reason:
            flash("Enter the reason for waiving a fine.", "error")
            return redirect(url_for("admin_fines"))
        fine.status = "waived"
        fine.waived_at = datetime.utcnow()
        fine.waived_by = session.get("username")
        fine.waived_reason = reason[:500]
        db.session.commit()
        log_action(session.get("username"), "FINE_WAIVED", f"Fine #{fine.id}; {reason}")
        member = db.session.get(User, fine.user_id)
        if member:
            notify_member(member, "Library fine waived", f"Fine #{fine.id} was waived. Reason: {reason}",
                          event_key=f"fine-waived:{fine.id}:{fine.waived_at.isoformat()}", event_type="fine")
        flash("Fine waived and audit history recorded.", "success")
        return redirect(url_for("admin_fines"))

    @app.route("/admin/disputes/<int:dispute_id>/review", methods=["POST"])
    @staff_route
    def review_dispute(dispute_id):
        if not librarian_fine_allowed():
            abort(403)
        dispute = FineDispute.query.get_or_404(dispute_id)
        action = request.form.get("action")
        response = request.form.get("response", "").strip()[:1000]
        if dispute.status != "pending":
            flash("This dispute has already been reviewed.", "error")
            return redirect(url_for("admin_fines"))
        if action not in {"upheld", "rejected"}:
            flash("Select uphold or reject.", "error")
            return redirect(url_for("admin_fines"))
        dispute.status = action
        dispute.reviewed_at = datetime.utcnow()
        dispute.reviewed_by = session.get("username")
        dispute.response = response
        if action == "upheld":
            fine = db.session.get(Fine, dispute.fine_id)
            if fine and fine.status == "open":
                fine.status = "waived"
                fine.waived_at = datetime.utcnow()
                fine.waived_by = session.get("username")
                fine.waived_reason = f"Dispute upheld: {response}"[:500]
        db.session.commit()
        log_action(session.get("username"), "FINE_DISPUTE_REVIEWED", f"Dispute #{dispute.id}: {action}; {response}")
        member = db.session.get(User, dispute.user_id)
        if member:
            notify_member(member, "Library fine dispute reviewed", f"Your dispute for Fine #{dispute.fine_id} was reviewed: {action}.\n{response}", event_key=f"dispute-reviewed:{dispute.id}", event_type="dispute")
        flash("Dispute review saved.", "success")
        return redirect(url_for("admin_fines"))

    @app.route("/library/members")
    @staff_route
    def library_members():
        q = request.args.get("q", "").strip()
        members_query = User.query.filter(User.role != "admin")
        if q:
            pattern = f"%{q}%"
            members_query = members_query.filter(or_(User.name.ilike(pattern), User.username.ilike(pattern),
                                                      User.email.ilike(pattern), User.employee_id.ilike(pattern)))
        members = members_query.order_by(User.name.asc()).limit(100).all()
        books = Book.query.filter(Book.is_active.is_(True), Book.available_copies > 0).order_by(Book.title.asc()).limit(500).all()
        loans = {}
        for member in members:
            loans[member.id] = BookLoan.query.filter_by(user_id=member.id, status="issued").order_by(BookLoan.due_at.asc()).all()
        return render_template("library_members_v60.html", members=members, books=books, loans=loans, q=q,
                               can_manage_fines=librarian_fine_allowed())

    @app.route("/admin/library/settings", methods=["GET", "POST"])
    @admin_required
    def library_settings():
        if request.method == "POST":
            try:
                per_day = float(request.form.get("fine_per_day", "10"))
                grace = int(request.form.get("fine_grace_days", "0"))
                if not 0 <= per_day <= 10000 or not 0 <= grace <= 30:
                    raise ValueError
            except ValueError:
                flash("Use a fine rate from ₹0 to ₹10,000 and grace days from 0 to 30.", "error")
                return redirect(url_for("library_settings"))
            upi = request.form.get("upi_id", "").strip()
            if upi and not re.fullmatch(r"[A-Za-z0-9._-]{2,100}@[A-Za-z]{2,30}", upi):
                flash("Enter a valid UPI ID, e.g. library@upi.", "error")
                return redirect(url_for("library_settings"))
            set_setting("fine_per_day", per_day)
            set_setting("fine_grace_days", grace)
            set_setting("upi_id", upi)
            set_setting("upi_name", request.form.get("upi_name", "Library").strip()[:80] or "Library")
            set_setting("librarian_fine_permission", "1" if request.form.get("librarian_fine_permission") == "yes" else "0")
            db.session.commit()
            log_action(session.get("username"), "LIBRARY_SETTINGS_UPDATED", f"Fine/day ₹{per_day}; grace {grace}; UPI configured={bool(upi)}")
            flash("Library settings saved.", "success")
            return redirect(url_for("library_settings"))
        return render_template("library_settings_v60.html", fine_per_day=fine_rate(), grace_days=grace_days(),
                               upi_id=setting("upi_id", os.environ.get("LIBRARY_UPI_ID", "")),
                               upi_name=setting("upi_name", os.environ.get("LIBRARY_UPI_NAME", "Library")),
                               librarian_fine_permission=setting("librarian_fine_permission", "0") == "1")

    @app.route("/admin/library/reports")
    @admin_required
    def library_reports():
        total_books = Book.query.count()
        total_copies = db.session.query(db.func.coalesce(db.func.sum(Book.total_copies), 0)).scalar() or 0
        available = db.session.query(db.func.coalesce(db.func.sum(Book.available_copies), 0)).scalar() or 0
        issued = BookLoan.query.filter_by(status="issued").count()
        overdue = BookLoan.query.filter(BookLoan.status == "issued", BookLoan.due_at < datetime.utcnow()).count()
        pending_requests = BookRequest.query.filter_by(status="pending").count()
        open_fines = Fine.query.filter_by(status="open").all()
        pending_payments = FinePayment.query.filter_by(status="pending").count()
        collection = db.session.query(db.func.coalesce(db.func.sum(FinePayment.amount), 0)).filter_by(status="approved").scalar() or 0
        month_start = datetime.utcnow().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        monthly_collection = db.session.query(db.func.coalesce(db.func.sum(FinePayment.amount), 0)).filter(
            FinePayment.status == "approved", FinePayment.reviewed_at >= month_start).scalar() or 0
        fine_due = sum(row.balance for row in open_fines)
        return render_template("library_reports_v60.html", total_books=total_books, total_copies=total_copies,
                               available=available, issued=issued, overdue=overdue,
                               pending_requests=pending_requests, pending_payments=pending_payments,
                               fine_due=fine_due, collection=collection, monthly_collection=monthly_collection)

    def snapshot_database():
        """Portable full logical snapshot; all rows included, JSON-safe values."""
        payload = {"format": "lms-v60-backup", "created_at": datetime.utcnow().isoformat() + "Z", "tables": {}}
        from sqlalchemy import inspect as sa_inspect
        inspector = sa_inspect(db.engine)
        for table_name in inspector.get_table_names():
            if table_name.startswith("sqlite_"):
                continue
            table = db.metadata.tables.get(table_name)
            if table is None:
                continue
            rows = db.session.execute(table.select()).mappings().all()
            converted = []
            for row in rows:
                record = {}
                for key, value in row.items():
                    if isinstance(value, (datetime, date)):
                        record[key] = value.isoformat()
                    elif isinstance(value, bytes):
                        record[key] = {"__bytes_b64__": base64.b64encode(value).decode("ascii")}
                    else:
                        record[key] = value
                converted.append(record)
            payload["tables"][table_name] = converted
        return payload

    @app.route("/admin/library/backup.json")
    @admin_required
    def download_library_backup():
        payload = snapshot_database()
        raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str).encode("utf-8")
        checksum = hashlib.sha256(raw).hexdigest()
        name = f"lms-backup-{datetime.utcnow():%Y%m%d-%H%M%S}.json"
        db.session.add(BackupRecord(file_name=name, checksum=checksum, size_bytes=len(raw), status="downloaded",
                                    created_by=session.get("username", "admin")))
        db.session.commit()
        log_action(session.get("username"), "DATABASE_BACKUP_DOWNLOADED", f"{name}; sha256={checksum}")
        response = make_response(raw)
        response.headers["Content-Type"] = "application/json; charset=utf-8"
        response.headers["Content-Disposition"] = f'attachment; filename="{name}"'
        response.headers["X-Backup-SHA256"] = checksum
        return response

    @app.route("/admin/library/restore", methods=["POST"])
    @admin_required
    def restore_library_backup():
        if request.form.get("confirm_backup") != "yes" or request.form.get("confirm_restore", "").strip() != "RESTORE":
            flash("For safety, confirm that you have downloaded a fresh backup and type RESTORE.", "error")
            return redirect(url_for("library_settings"))
        uploaded = request.files.get("backup_file")
        if not uploaded:
            flash("Select a backup JSON file.", "error")
            return redirect(url_for("library_settings"))
        raw = uploaded.read(25_000_001)
        if len(raw) > 25_000_000:
            flash("Backup file is larger than the 25 MB restore limit.", "error")
            return redirect(url_for("library_settings"))
        try:
            payload = json.loads(raw.decode("utf-8"))
            if payload.get("format") != "lms-v60-backup" or not isinstance(payload.get("tables"), dict):
                raise ValueError
        except Exception:
            flash("Invalid LMS backup file.", "error")
            return redirect(url_for("library_settings"))
        # Merge-only restore: inserts rows with non-conflicting primary keys. Never deletes or overwrites.
        inserted = skipped = 0
        ordered = [t.name for t in db.metadata.sorted_tables]
        try:
            for table_name in ordered:
                rows = payload["tables"].get(table_name, [])
                table = db.metadata.tables.get(table_name)
                if table is None or not isinstance(rows, list):
                    continue
                pk_cols = [c.name for c in table.primary_key.columns]
                for row in rows:
                    if not isinstance(row, dict) or any(k not in table.c for k in row):
                        skipped += 1
                        continue
                    if pk_cols and all(k in row for k in pk_cols):
                        exists = db.session.execute(table.select().where(and_(*[table.c[k] == row[k] for k in pk_cols]))).first()
                        if exists:
                            skipped += 1
                            continue
                    converted = dict(row)
                    for column_name, column in table.c.items():
                        value = converted.get(column_name)
                        if value is None:
                            continue
                        try:
                            from sqlalchemy import DateTime as SADateTime, Date as SADate, LargeBinary
                            if isinstance(column.type, SADateTime) and isinstance(value, str):
                                converted[column_name] = datetime.fromisoformat(value.replace("Z", "+00:00")).replace(tzinfo=None)
                            elif isinstance(column.type, SADate) and isinstance(value, str):
                                converted[column_name] = date.fromisoformat(value[:10])
                            elif isinstance(column.type, LargeBinary) and isinstance(value, dict) and "__bytes_b64__" in value:
                                converted[column_name] = base64.b64decode(value["__bytes_b64__"])
                        except Exception:
                            pass
                    try:
                        with db.session.begin_nested():
                            db.session.execute(table.insert().values(**converted))
                        inserted += 1
                    except Exception:
                        skipped += 1
            db.session.commit()
            if db.engine.dialect.name == "postgresql":
                # Advance serial/identity sequences after restored explicit primary keys.
                with db.engine.begin() as connection:
                    for table_name in ordered:
                        table = db.metadata.tables.get(table_name)
                        if table is None or len(table.primary_key.columns) != 1:
                            continue
                        pk = next(iter(table.primary_key.columns))
                        if not isinstance(pk.type, db.Integer):
                            continue
                        sequence = connection.execute(
                            text("SELECT pg_get_serial_sequence(:table_name, :column_name)"),
                            {"table_name": table_name, "column_name": pk.name},
                        ).scalar()
                        if not sequence:
                            continue
                        maximum = connection.execute(
                            table.select().with_only_columns(db.func.max(pk))
                        ).scalar()
                        connection.execute(
                            text("SELECT setval(CAST(:sequence AS regclass), :value, :called)"),
                            {"sequence": sequence, "value": int(maximum or 1), "called": maximum is not None},
                        )
        except Exception:
            db.session.rollback()
            app.logger.exception("Library database merge restore failed")
            flash("Restore failed. Existing data was not intentionally deleted. Review server logs.", "error")
            return redirect(url_for("library_settings"))
        log_action(session.get("username"), "DATABASE_BACKUP_RESTORED", f"Inserted {inserted}; skipped {skipped}; merge-only")
        flash(f"Merge restore complete. Inserted {inserted} records; skipped {skipped} conflicts. Existing records were not overwritten or deleted.", "success")
        return redirect(url_for("library_settings"))

    def cron_authorized():
        secret = os.environ.get("CRON_SECRET", "").strip()
        supplied = request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
        return bool(secret and len(secret) >= 24 and supplied and hmac.compare_digest(secret, supplied))

    @app.route("/api/cron/library-reminders", methods=["GET", "POST"])
    def library_reminders_cron():
        if not cron_authorized():
            abort(403)
        now = datetime.utcnow()
        due_target = now.date() + timedelta(days=2)
        sent_due = sent_overdue = 0
        active = BookLoan.query.filter_by(status="issued").all()
        for loan in active:
            member = db.session.get(User, loan.user_id)
            book = db.session.get(Book, loan.book_id)
            if not member or not book or not loan.due_at:
                continue
            if loan.due_at.date() == due_target:
                key = f"due-reminder:{loan.id}:{due_target.isoformat()}"
                if safe_email(member.email, f"Book due in 2 days: {book.title}",
                              f"Reminder: {book.title} ({book_code(book)}) is due on {loan.due_at:%d %b %Y}.", key, "due_reminder"):
                    sent_due += 1
            elif loan.due_at.date() < now.date():
                amount, days = get_due_fine(loan, now)
                if amount > 0:
                    add_fine(loan.user_id, loan.id, loan.book_id, "late", amount,
                             f"Overdue by {days} day(s) at ₹{fine_rate():.2f}/day", "system-cron")
                key = f"overdue-reminder:{loan.id}:{now.date().isoformat()}"
                if safe_email(member.email, f"Overdue book: {book.title}",
                              f"Book: {book.title}\nBook ID: {book_code(book)}\nIssue date: {loan.issued_at:%d %b %Y}\nDue date: {loan.due_at:%d %b %Y}\nDays late (after grace): {days}\nIndicative late fine: ₹{amount:.2f}\nFine is posted when the book is returned.", key, "overdue_reminder"):
                    sent_overdue += 1
        overdue_count = BookLoan.query.filter(BookLoan.status == "issued", BookLoan.due_at < now).count()
        admin_email = os.environ.get("ADMIN_EMAIL", "").strip()
        if admin_email and overdue_count:
            safe_email(admin_email, "Library overdue-loan alert",
                       f"There are currently {overdue_count} overdue issued loans. Please review the Library Reports / Member Lookup dashboard.",
                       event_key=f"admin-overdue-summary:{now.date().isoformat()}", event_type="admin_alert")
        db.session.commit()
        return jsonify({"status": "ok", "due_reminders": sent_due, "overdue_reminders": sent_overdue, "overdue_loans": overdue_count})

    @app.route("/api/cron/library-backup", methods=["GET", "POST"])
    def library_backup_cron():
        if not cron_authorized():
            abort(403)
        bucket = os.environ.get("BACKUP_S3_BUCKET", "").strip()
        if not bucket:
            return jsonify({"status": "not_configured", "message": "Set BACKUP_S3_BUCKET and AWS credentials for durable automatic backups."}), 503
        try:
            import boto3
        except ImportError:
            return jsonify({"status": "not_configured", "message": "Install boto3 to enable S3-compatible backups."}), 503
        payload = snapshot_database()
        raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str).encode("utf-8")
        checksum = hashlib.sha256(raw).hexdigest()
        name = f"library-backups/lms-{datetime.utcnow():%Y/%m/%d}/lms-{datetime.utcnow():%Y%m%d-%H%M%S}.json"
        try:
            client_options = {"region_name": os.environ.get("AWS_REGION", "ap-south-1")}
            backup_endpoint = os.environ.get("BACKUP_S3_ENDPOINT", "").strip()
            if backup_endpoint:
                client_options["endpoint_url"] = backup_endpoint
            client = boto3.client("s3", **client_options)
            client.put_object(Bucket=bucket, Key=name, Body=raw, ContentType="application/json", ServerSideEncryption="AES256")
            db.session.add(BackupRecord(file_name=name, checksum=checksum, storage_uri=f"s3://{bucket}/{name}",
                                        size_bytes=len(raw), status="uploaded", created_by="cron"))
            db.session.commit()
            log_action("system", "AUTOMATIC_DATABASE_BACKUP", f"s3://{bucket}/{name}; sha256={checksum}")
            return jsonify({"status": "ok", "storage_uri": f"s3://{bucket}/{name}", "sha256": checksum, "size_bytes": len(raw)})
        except Exception:
            db.session.rollback()
            app.logger.exception("Automatic LMS S3 backup failed")
            return jsonify({"status": "error", "message": "Backup upload failed. Review server logs."}), 500

    @app.route("/admin/library/management")
    @staff_route
    def library_management():
        pending_requests = BookRequest.query.filter_by(status="pending").count()
        pending_payments = FinePayment.query.filter_by(status="pending").count()
        overdue = BookLoan.query.filter(BookLoan.status == "issued", BookLoan.due_at < datetime.utcnow()).count()
        return render_template("library_management_v60.html", pending_requests=pending_requests,
                               pending_payments=pending_payments, overdue=overdue,
                               fine_rate=fine_rate(), fine_due=sum(f.balance for f in Fine.query.filter_by(status="open").all()))

    # ── Compatibility endpoints for archived V51 templates ────────────
    # These aliases intentionally route legacy commerce-style pages into the
    # current library circulation and manual-UPI workflows. They do not fake sales,
    # payments, or refunds.
    @app.route("/library/legacy/books")
    @login_required
    def books():
        return redirect(url_for("books_catalog", **request.args))

    @app.route("/library/legacy/cart")
    @login_required
    def cart():
        flash("The old shopping cart is not used for library borrowing. Browse and issue books directly.", "info")
        return redirect(url_for("books_catalog"))

    @app.route("/library/legacy/cart/add/<int:book_id>", methods=["GET", "POST"])
    @login_required
    def add_to_cart(book_id):
        flash("Books are issued directly from the catalog; review this book before issuing.", "info")
        return redirect(url_for("book_detail", book_id=book_id))

    @app.route("/library/legacy/checkout", methods=["GET", "POST"])
    @login_required
    def checkout():
        return redirect(url_for("my_books"))

    @app.route("/library/legacy/cart/update", methods=["POST"])
    @login_required
    def update_cart():
        return redirect(url_for("my_books"))

    @app.route("/library/legacy/demo-payment/<int:order_id>")
    @login_required
    def demo_payment(order_id):
        flash("Demo sales payments are disabled. Library fines use Manual UPI with Admin verification.", "info")
        return redirect(url_for("my_fines"))

    @app.route("/library/legacy/demo-payment/simulate", methods=["POST"])
    @login_required
    def simulate_demo_payment():
        flash("A demo button cannot mark a payment successful. Submit a real UPI reference for Admin verification.", "warning")
        return redirect(url_for("my_fines"))

    @app.route("/library/legacy/issue/<int:book_id>", methods=["GET", "POST"])
    @login_required
    def issue_book(book_id):
        if request.method == "GET":
            return redirect(url_for("book_detail", book_id=book_id))
        member = db.session.get(User, session["user_id"])
        book = db.session.get(Book, book_id)
        try:
            loan = create_issue(member, book, member.username if member else "user")
            db.session.commit()
            notify_issued(loan)
        except ValueError as exc:
            db.session.rollback()
            flash(str(exc), "error")
            return redirect(url_for("book_detail", book_id=book_id))
        log_action(member.username, "BOOK_SELF_ISSUED", f"{book.title}; loan #{loan.id}")
        flash(f"{book.title} issued. Due {loan.due_at:%d %b %Y}.", "success")
        return redirect(url_for("my_books"))

    @app.route("/library/legacy/return-confirm/<int:loan_id>")
    @login_required
    def return_confirm(loan_id):
        loan = BookLoan.query.filter_by(id=loan_id, user_id=session["user_id"], status="issued").first_or_404()
        flash("Please take the book to the librarian for return inspection. Any late fine will be calculated at return.", "info")
        return redirect(url_for("my_books"))

    @app.route("/library/legacy/return/<int:loan_id>", methods=["POST"])
    @login_required
    def return_book(loan_id):
        loan = BookLoan.query.filter_by(id=loan_id, user_id=session["user_id"], status="issued").first_or_404()
        flash("A Librarian or Admin must inspect and record the physical return.", "info")
        return redirect(url_for("my_books"))

    @app.route("/library/legacy/payment/<int:loan_id>")
    @login_required
    def payment_page(loan_id):
        return redirect(url_for("my_fines"))

    @app.route("/library/legacy/refund/<int:payment_id>", methods=["POST"])
    @admin_required
    def refund_payment(payment_id):
        flash("Manual UPI refunds must be processed with the UPI provider and recorded outside this app. No false refund status was created.", "warning")
        return redirect(url_for("admin_payments"))

    @app.route("/books/<int:book_id>/pdf")
    @staff_route
    def view_book_pdf(book_id):
        book = book_detail_or_404(book_id)
        flash("This catalog record does not have a stored PDF attachment.", "info")
        return redirect(url_for("book_detail", book_id=book.id))

    @app.route("/admin/email-logs")
    @admin_required
    def email_logs():
        logs = LibraryEmailLog.query.order_by(LibraryEmailLog.sent_at.desc()).limit(200).all()
        provider = "SMTP" if (os.environ.get("SMTP_HOST") and os.environ.get("SMTP_USER") and os.environ.get("SMTP_PASSWORD")) else ("Brevo (OTP/campaign only)" if os.environ.get("BREVO_API_KEY") else "")
        return render_template("email_logs.html", logs=logs, provider=provider,
                               admin_copy=os.environ.get("ADMIN_EMAIL", ""),
                               to_ist=lambda value: value + timedelta(hours=5, minutes=30))

    @app.route("/admin/email-test", methods=["POST"])
    @admin_required
    def email_test():
        to = request.form.get("to", "").strip()
        if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", to):
            flash("Enter a valid recipient email address.", "error")
        elif safe_email(to, "Library Management System test email", "Your SMTP test email was delivered.", event_type="test"):
            flash("Test email sent.", "success")
        else:
            flash("Test email could not be sent. Check SMTP settings and server logs.", "error")
        return redirect(url_for("email_logs"))

    @app.route("/admin/email-send-reminders", methods=["POST"])
    @admin_required
    def email_send_reminders():
        now = datetime.utcnow()
        target = now.date() + timedelta(days=2)
        due_count = overdue_count = 0
        for loan in BookLoan.query.filter_by(status="issued").all():
            member = db.session.get(User, loan.user_id)
            book = db.session.get(Book, loan.book_id)
            if not member or not book or not loan.due_at:
                continue
            if loan.due_at.date() == target:
                sent = safe_email(member.email, f"Book due in 2 days: {book.title}",
                                  f"{book.title} ({book_code(book)}) is due on {loan.due_at:%d %b %Y}.",
                                  f"due-reminder:{loan.id}:{target.isoformat()}", "due_reminder")
                due_count += int(sent)
            elif loan.due_at.date() < now.date():
                amount, days = get_due_fine(loan, now)
                if amount > 0:
                    add_fine(loan.user_id, loan.id, loan.book_id, "late", amount,
                             f"Overdue by {days} day(s) at ₹{fine_rate():.2f}/day", "admin-reminder")
                sent = safe_email(member.email, f"Overdue book: {book.title}",
                                  f"Book: {book.title}; ID: {book_code(book)}; issue: {loan.issued_at:%d %b %Y}; due: {loan.due_at:%d %b %Y}; late days after grace: {days}; indicative fine: ₹{amount:.2f}.",
                                  f"overdue-reminder:{loan.id}:{now.date().isoformat()}", "overdue_reminder")
                overdue_count += int(sent)
        db.session.commit()
        flash(f"Reminder run complete. Due-soon emails: {due_count}; overdue emails: {overdue_count}.", "success")
        return redirect(url_for("email_logs"))

    # Helper functions are made callable by existing app.py routes.
    app.extensions["library_v60_helpers"] = {
        "safe_email": safe_email, "notify_member": notify_member,
        "create_issue": create_issue, "notify_issued": notify_issued, "return_loan": return_loan,
        "queue_availability_emails": queue_availability_emails,
        "book_code": book_code, "fine_rate": fine_rate,
        "librarian_fine_allowed": librarian_fine_allowed,
    }

    # Default setting rows are created safely on first schema initialization / first request.
    @app.before_request
    def _seed_library_settings():
        if request.path in {"/", "/style.css", "/ui_advanced.js", "/favicon.ico"} or request.endpoint == "static":
            return None
        # db.create_all runs in the existing app's DB initializer. Seed once per warm worker.
        if app.extensions.get("library_v60_settings_seeded"):
            return None
        try:
            if db.session.get(LibrarySetting, "fine_per_day") is None:
                db.session.add(LibrarySetting(key="fine_per_day", value="10", updated_by="system"))
            if db.session.get(LibrarySetting, "fine_grace_days") is None:
                db.session.add(LibrarySetting(key="fine_grace_days", value="0", updated_by="system"))
            if db.session.get(LibrarySetting, "librarian_fine_permission") is None:
                db.session.add(LibrarySetting(key="librarian_fine_permission", value="0", updated_by="system"))
            db.session.commit()
            app.extensions["library_v60_settings_seeded"] = True
            if not app.extensions.get("library_v60_loan_unique_index_checked"):
                try:
                    duplicate = db.session.execute(text(
                        "SELECT user_id, book_id FROM book_loan WHERE status = 'issued' "
                        "GROUP BY user_id, book_id HAVING COUNT(*) > 1 LIMIT 1"
                    )).first()
                    if duplicate:
                        app.logger.warning(
                            "Active duplicate member/book loans exist; the protective unique index was not created. "
                            "Review loan history before reconciling duplicates."
                        )
                    else:
                        active_loan_index = Index(
                            "uq_active_loan_member_book", BookLoan.user_id, BookLoan.book_id,
                            unique=True,
                            postgresql_where=text("status = 'issued'"),
                            sqlite_where=text("status = 'issued'"),
                        )
                        active_loan_index.create(bind=db.engine, checkfirst=True)
                    app.extensions["library_v60_loan_unique_index_checked"] = True
                except Exception:
                    db.session.rollback()
                    app.logger.warning("Active-loan unique index could not be verified/created; application checks remain enabled.", exc_info=True)
                    app.extensions["library_v60_loan_unique_index_checked"] = True
        except Exception:
            db.session.rollback()
            # The app's own DB readiness guard will return a 503 if schema is unavailable.
            return None

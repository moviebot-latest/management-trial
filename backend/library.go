package backend

import (
	"context"
	"database/sql"
	"fmt"
	"math"
	"net/http"
	"os"
	"strconv"
	"strings"
	"time"
)

// Schema creation is additive only: it never drops or renames existing data.
func (a *App) ensureLibrarySchema(ctx context.Context) error {
	statements := []string{
		`CREATE TABLE IF NOT EXISTS book (id BIGSERIAL PRIMARY KEY, book_id VARCHAR(20) UNIQUE NOT NULL, title VARCHAR(180) NOT NULL, author VARCHAR(140) NOT NULL, isbn VARCHAR(40) UNIQUE, category VARCHAR(80) NOT NULL DEFAULT 'General', description TEXT, cover_url VARCHAR(600), pdf_path VARCHAR(700), publication_year INTEGER, total_copies INTEGER NOT NULL DEFAULT 1 CHECK (total_copies >= 0), available_copies INTEGER NOT NULL DEFAULT 1 CHECK (available_copies >= 0), created_at TIMESTAMP NOT NULL DEFAULT NOW())`,
		`CREATE TABLE IF NOT EXISTS loan (id BIGSERIAL PRIMARY KEY, book_id BIGINT NOT NULL REFERENCES book(id), user_id BIGINT NOT NULL REFERENCES "user"(user_id), issued_at TIMESTAMP NOT NULL DEFAULT NOW(), due_at TIMESTAMP NOT NULL, returned_at TIMESTAMP)`,
		`CREATE TABLE IF NOT EXISTS return_record (id BIGSERIAL PRIMARY KEY, loan_id BIGINT UNIQUE NOT NULL REFERENCES loan(id), late_fine NUMERIC(10,2) NOT NULL DEFAULT 0, admin_fine NUMERIC(10,2) NOT NULL DEFAULT 0, fine_reason VARCHAR(255), photo_paths TEXT, total_fine NUMERIC(10,2) NOT NULL DEFAULT 0, payment_status VARCHAR(20) NOT NULL DEFAULT 'not_required', payment_method VARCHAR(20), payment_id VARCHAR(80) UNIQUE, paid_at TIMESTAMP, returned_at TIMESTAMP, created_by VARCHAR(120))`,
		`CREATE TABLE IF NOT EXISTS payment (id BIGSERIAL PRIMARY KEY, payment_id VARCHAR(80) UNIQUE NOT NULL, return_id BIGINT NOT NULL REFERENCES return_record(id), loan_id BIGINT NOT NULL REFERENCES loan(id), user_id BIGINT NOT NULL REFERENCES "user"(user_id), book_id BIGINT NOT NULL REFERENCES book(id), payer_name VARCHAR(120) NOT NULL, amount NUMERIC(10,2) NOT NULL DEFAULT 0, payment_method VARCHAR(20) NOT NULL, payment_status VARCHAR(20) NOT NULL DEFAULT 'recorded_demo', transaction_reference VARCHAR(120) UNIQUE, card_last4 VARCHAR(4), cash_received_by VARCHAR(80), paid_at TIMESTAMP NOT NULL, created_at TIMESTAMP NOT NULL DEFAULT NOW())`,
		`CREATE INDEX IF NOT EXISTS idx_book_title_author ON book (lower(title), lower(author))`,
		`CREATE INDEX IF NOT EXISTS idx_loan_user_returned ON loan (user_id, returned_at)`,
		`CREATE INDEX IF NOT EXISTS idx_payment_created_at ON payment (created_at DESC)`,
	}
	for _, q := range statements {
		if _, err := a.db.ExecContext(ctx, q); err != nil {
			return err
		}
	}
	return nil
}

type bookInput struct {
	BookCode        string `json:"book_id"`
	Title           string `json:"title"`
	Author          string `json:"author"`
	ISBN            string `json:"isbn"`
	Category        string `json:"category"`
	Description     string `json:"description"`
	CoverURL        string `json:"cover_url"`
	PDFPath         string `json:"pdf_path"`
	PublicationYear int    `json:"publication_year"`
	TotalCopies     int    `json:"total_copies"`
}

func (a *App) listBooks(w http.ResponseWriter, r *http.Request) {
	if _, ok := a.require(w, r, false); !ok {
		return
	}
	q := strings.TrimSpace(r.URL.Query().Get("q"))
	rows, err := a.db.QueryContext(r.Context(), `SELECT b.id,b.book_id,b.title,b.author,COALESCE(b.isbn,''),b.category,COALESCE(b.description,''),COALESCE(b.cover_url,''),COALESCE(b.pdf_path,''),COALESCE(b.publication_year,0),b.total_copies,b.available_copies,b.created_at FROM book b WHERE ($1='' OR b.title ILIKE '%'||$1||'%' OR b.author ILIKE '%'||$1||'%' OR b.book_id ILIKE '%'||$1||'%' OR COALESCE(b.isbn,'') ILIKE '%'||$1||'%') ORDER BY b.created_at DESC LIMIT 500`, q)
	if err != nil {
		fail(w, 503, "Could not load books. Check that the library database schema is compatible.")
		return
	}
	defer rows.Close()
	items := []body{}
	for rows.Next() {
		var id, year, total, available int
		var code, title, author, isbn, category, desc, cover, pdf string
		var created time.Time
		if rows.Scan(&id, &code, &title, &author, &isbn, &category, &desc, &cover, &pdf, &year, &total, &available, &created) == nil {
			items = append(items, body{"id": id, "book_id": code, "title": title, "author": author, "isbn": isbn, "category": category, "description": desc, "cover_url": cover, "pdf_path": pdf, "publication_year": year, "total_copies": total, "available_copies": available, "created_at": created})
		}
	}
	jsonOut(w, 200, body{"books": items})
}
func (a *App) createBook(w http.ResponseWriter, r *http.Request) {
	s, ok := a.require(w, r, false)
	if !ok {
		return
	}
	if s.Role != "admin" && s.Role != "librarian" {
		fail(w, 403, "Librarian or administrator access required.")
		return
	}
	var b bookInput
	if readBody(r, &b) != nil {
		fail(w, 400, "Invalid book details.")
		return
	}
	b.Title = strings.TrimSpace(b.Title)
	b.Author = strings.TrimSpace(b.Author)
	b.BookCode = strings.TrimSpace(b.BookCode)
	b.Category = strings.TrimSpace(b.Category)
	if b.Title == "" || b.Author == "" {
		fail(w, 400, "Title and author are required.")
		return
	}
	if b.TotalCopies < 1 {
		b.TotalCopies = 1
	}
	if b.Category == "" {
		b.Category = "General"
	}
	if b.BookCode == "" {
		b.BookCode = fmt.Sprintf("BK-%s", strings.ToUpper(randomToken(6)))
	}
	var id int64
	err := a.db.QueryRowContext(r.Context(), `INSERT INTO book(book_id,title,author,isbn,category,description,cover_url,pdf_path,publication_year,total_copies,available_copies) VALUES($1,$2,$3,NULLIF($4,''),$5,NULLIF($6,''),NULLIF($7,''),NULLIF($8,''),NULLIF($9,0),$10,$10) RETURNING id`, b.BookCode, b.Title, b.Author, strings.TrimSpace(b.ISBN), b.Category, b.Description, b.CoverURL, b.PDFPath, b.PublicationYear, b.TotalCopies).Scan(&id)
	if err != nil {
		fail(w, 409, "Book could not be saved. Check duplicate book code or ISBN.")
		return
	}
	_ = a.audit(r, s.Username, "LIBRARY_BOOK_CREATE", fmt.Sprintf("book_id=%d code=%s title=%s", id, b.BookCode, truncate(b.Title, 100)))
	jsonOut(w, 201, body{"ok": true, "id": id, "book_id": b.BookCode})
}
func (a *App) updateBook(w http.ResponseWriter, r *http.Request) {
	s, ok := a.require(w, r, false)
	if !ok {
		return
	}
	if s.Role != "admin" && s.Role != "librarian" {
		fail(w, 403, "Librarian or administrator access required.")
		return
	}
	id, e := strconv.ParseInt(r.PathValue("id"), 10, 64)
	if e != nil {
		fail(w, 400, "Invalid book ID.")
		return
	}
	var b bookInput
	if readBody(r, &b) != nil {
		fail(w, 400, "Invalid book details.")
		return
	}
	if strings.TrimSpace(b.Title) == "" || strings.TrimSpace(b.Author) == "" || b.TotalCopies < 1 {
		fail(w, 400, "Title, author and at least one copy are required.")
		return
	}
	var activeLoans int
	if err := a.db.QueryRowContext(r.Context(), `SELECT COUNT(*) FROM loan WHERE book_id=$1 AND returned_at IS NULL`, id).Scan(&activeLoans); err != nil {
		fail(w, 503, "Could not validate active loans.")
		return
	}
	if b.TotalCopies < activeLoans {
		fail(w, 409, "Total copies cannot be lower than the number of active loans.")
		return
	}
	res, err := a.db.ExecContext(r.Context(), `UPDATE book SET book_id=$1,title=$2,author=$3,isbn=NULLIF($4,''),category=$5,description=NULLIF($6,''),cover_url=NULLIF($7,''),pdf_path=NULLIF($8,''),publication_year=NULLIF($9,0),available_copies=GREATEST(0,$10-(total_copies-available_copies)),total_copies=$10 WHERE id=$11`, strings.TrimSpace(b.BookCode), strings.TrimSpace(b.Title), strings.TrimSpace(b.Author), strings.TrimSpace(b.ISBN), defaultString(b.Category, "General"), b.Description, b.CoverURL, b.PDFPath, b.PublicationYear, b.TotalCopies, id)
	if err != nil {
		fail(w, 409, "Book update failed. Check duplicate code/ISBN and copy counts.")
		return
	}
	n, _ := res.RowsAffected()
	if n == 0 {
		fail(w, 404, "Book not found.")
		return
	}
	_ = a.audit(r, s.Username, "LIBRARY_BOOK_UPDATE", fmt.Sprintf("book_id=%d", id))
	jsonOut(w, 200, body{"ok": true})
}
func (a *App) deleteBook(w http.ResponseWriter, r *http.Request) {
	s, ok := a.require(w, r, false)
	if !ok {
		return
	}
	if s.Role != "admin" && s.Role != "librarian" {
		fail(w, 403, "Librarian or administrator access required.")
		return
	}
	id, e := strconv.ParseInt(r.PathValue("id"), 10, 64)
	if e != nil {
		fail(w, 400, "Invalid book ID.")
		return
	}
	var loans int
	if a.db.QueryRowContext(r.Context(), `SELECT COUNT(*) FROM loan WHERE book_id=$1`, id).Scan(&loans) != nil {
		fail(w, 503, "Could not check book history.")
		return
	}
	if loans > 0 {
		fail(w, 409, "This book has loan history and cannot be deleted. Keep the record for audit integrity.")
		return
	}
	res, err := a.db.ExecContext(r.Context(), `DELETE FROM book WHERE id=$1`, id)
	if err != nil {
		fail(w, 409, "Book could not be deleted.")
		return
	}
	n, _ := res.RowsAffected()
	if n == 0 {
		fail(w, 404, "Book not found.")
		return
	}
	_ = a.audit(r, s.Username, "LIBRARY_BOOK_DELETE", fmt.Sprintf("book_id=%d", id))
	jsonOut(w, 200, body{"ok": true})
}
func defaultString(s, d string) string {
	if strings.TrimSpace(s) == "" {
		return d
	}
	return strings.TrimSpace(s)
}

type loanInput struct {
	BookID int64 `json:"book_id"`
	UserID int64 `json:"user_id"`
	Days   int   `json:"days"`
}

func (a *App) listLoans(w http.ResponseWriter, r *http.Request) {
	s, ok := a.require(w, r, false)
	if !ok {
		return
	}
	rows, err := a.db.QueryContext(r.Context(), `SELECT l.id,l.book_id,b.title,b.book_id,l.user_id,u.name,u.username,l.issued_at,l.due_at,l.returned_at,COALESCE(rr.total_fine,0),COALESCE(rr.payment_status,'not_required') FROM loan l JOIN book b ON b.id=l.book_id JOIN "user" u ON u.user_id=l.user_id LEFT JOIN return_record rr ON rr.loan_id=l.id WHERE ($1 OR l.user_id=$2) ORDER BY l.issued_at DESC LIMIT 500`, s.Role == "admin" || s.Role == "librarian", s.UserID)
	if err != nil {
		fail(w, 503, "Could not load issue/return records.")
		return
	}
	defer rows.Close()
	items := []body{}
	for rows.Next() {
		var id, bid, uid int64
		var title, code, name, username, status string
		var issued, due time.Time
		var returned sql.NullTime
		var fine float64
		if rows.Scan(&id, &bid, &title, &code, &uid, &name, &username, &issued, &due, &returned, &fine, &status) == nil {
			var ret any = nil
			if returned.Valid {
				ret = returned.Time
			}
			items = append(items, body{"id": id, "book_id": bid, "book_title": title, "book_code": code, "user_id": uid, "user_name": name, "username": username, "issued_at": issued, "due_at": due, "returned_at": ret, "fine": fine, "payment_status": status})
		}
	}
	jsonOut(w, 200, body{"loans": items})
}
func (a *App) issueLoan(w http.ResponseWriter, r *http.Request) {
	s, ok := a.require(w, r, false)
	if !ok {
		return
	}
	var b loanInput
	if readBody(r, &b) != nil || b.BookID < 1 {
		fail(w, 400, "Choose a valid book.")
		return
	}
	uid := int64(s.UserID)
	if s.Role == "admin" || s.Role == "librarian" {
		if b.UserID > 0 {
			uid = b.UserID
		}
	} else if b.UserID > 0 && b.UserID != int64(s.UserID) {
		fail(w, 403, "You can only issue a book to your own account.")
		return
	}
	days := b.Days
	if days < 1 || days > 90 {
		days = 14
	}
	tx, err := a.db.BeginTx(r.Context(), nil)
	if err != nil {
		fail(w, 503, "Could not start issue transaction.")
		return
	}
	defer tx.Rollback()
	var available int
	if err = tx.QueryRowContext(r.Context(), `SELECT available_copies FROM book WHERE id=$1 FOR UPDATE`, b.BookID).Scan(&available); err != nil {
		fail(w, 404, "Book not found.")
		return
	}
	if available < 1 {
		fail(w, 409, "No copies are currently available.")
		return
	}
	var exists bool
	_ = tx.QueryRowContext(r.Context(), `SELECT EXISTS(SELECT 1 FROM loan WHERE book_id=$1 AND user_id=$2 AND returned_at IS NULL)`, b.BookID, uid).Scan(&exists)
	if exists {
		fail(w, 409, "This member already has an active loan for this book.")
		return
	}
	var loanID int64
	err = tx.QueryRowContext(r.Context(), `INSERT INTO loan(book_id,user_id,issued_at,due_at) VALUES($1,$2,NOW(),NOW()+($3 * INTERVAL '1 day')) RETURNING id`, b.BookID, uid, days).Scan(&loanID)
	if err == nil {
		_, err = tx.ExecContext(r.Context(), `UPDATE book SET available_copies=available_copies-1 WHERE id=$1`, b.BookID)
	}
	if err == nil {
		err = tx.Commit()
	}
	if err != nil {
		fail(w, 503, "Book issue failed; stock was not changed.")
		return
	}
	_ = a.audit(r, s.Username, "LIBRARY_BOOK_ISSUE", fmt.Sprintf("loan_id=%d book_id=%d user_id=%d due_in_days=%d", loanID, b.BookID, uid, days))
	jsonOut(w, 201, body{"ok": true, "loan_id": loanID, "due_in_days": days})
}
func (a *App) addFine(w http.ResponseWriter, r *http.Request) {
	s, ok := a.require(w, r, false)
	if !ok {
		return
	}
	if s.Role != "admin" && s.Role != "librarian" {
		fail(w, 403, "Librarian or administrator access required.")
		return
	}
	id, e := strconv.ParseInt(r.PathValue("id"), 10, 64)
	if e != nil {
		fail(w, 400, "Invalid loan ID.")
		return
	}
	var b struct {
		Amount float64 `json:"amount"`
		Reason string  `json:"reason"`
	}
	if readBody(r, &b) != nil || b.Amount < 0 || b.Amount > 100000 {
		fail(w, 400, "Enter a valid fine amount.")
		return
	}
	b.Reason = truncate(strings.TrimSpace(b.Reason), 255)
	tx, err := a.db.BeginTx(r.Context(), nil)
	if err != nil {
		fail(w, 503, "Could not start fine update.")
		return
	}
	defer tx.Rollback()
	var returned sql.NullTime
	var late, admin float64
	err = tx.QueryRowContext(r.Context(), `SELECT l.returned_at,COALESCE(rr.late_fine,0),COALESCE(rr.admin_fine,0) FROM loan l LEFT JOIN return_record rr ON rr.loan_id=l.id WHERE l.id=$1 FOR UPDATE OF l`, id).Scan(&returned, &late, &admin)
	if err != nil {
		fail(w, 404, "Loan not found.")
		return
	}
	if returned.Valid {
		fail(w, 409, "Fine cannot be changed after return has been finalized.")
		return
	}
	_, err = tx.ExecContext(r.Context(), `INSERT INTO return_record(loan_id,late_fine,admin_fine,fine_reason,total_fine,payment_status,created_by) VALUES($1,0,$2,NULLIF($3,''),$2,'unpaid',$4) ON CONFLICT(loan_id) DO UPDATE SET admin_fine=EXCLUDED.admin_fine,fine_reason=EXCLUDED.fine_reason,total_fine=return_record.late_fine+EXCLUDED.admin_fine,payment_status=CASE WHEN return_record.payment_status='paid' THEN 'paid' ELSE 'unpaid' END`, id, b.Amount, b.Reason, s.Username)
	if err != nil {
		fail(w, 503, "Fine could not be saved.")
		return
	}
	if err = tx.Commit(); err != nil {
		fail(w, 503, "Fine could not be saved.")
		return
	}
	_ = a.audit(r, s.Username, "LIBRARY_FINE_ADD", fmt.Sprintf("loan_id=%d amount=%.2f reason=%s", id, b.Amount, b.Reason))
	jsonOut(w, 200, body{"ok": true})
}
func (a *App) returnLoan(w http.ResponseWriter, r *http.Request) {
	s, ok := a.require(w, r, false)
	if !ok {
		return
	}
	id, e := strconv.ParseInt(r.PathValue("id"), 10, 64)
	if e != nil {
		fail(w, 400, "Invalid loan ID.")
		return
	}
	var b struct {
		PaymentMethod  string `json:"payment_method"`
		CashReceivedBy string `json:"cash_received_by"`
	}
	if r.ContentLength > 0 && readBody(r, &b) != nil {
		fail(w, 400, "Invalid return details.")
		return
	}
	tx, err := a.db.BeginTx(r.Context(), nil)
	if err != nil {
		fail(w, 503, "Could not start return transaction.")
		return
	}
	defer tx.Rollback()
	var uid, bid int64
	var name, title string
	var due time.Time
	var returned sql.NullTime
	var late, admin float64
	var reason sql.NullString
	var recordID sql.NullInt64
	err = tx.QueryRowContext(r.Context(), `SELECT l.user_id,l.book_id,u.name,b.title,l.due_at,l.returned_at,COALESCE(rr.late_fine,0),COALESCE(rr.admin_fine,0),rr.fine_reason,rr.id FROM loan l JOIN "user" u ON u.user_id=l.user_id JOIN book b ON b.id=l.book_id LEFT JOIN return_record rr ON rr.loan_id=l.id WHERE l.id=$1 FOR UPDATE OF l`, id).Scan(&uid, &bid, &name, &title, &due, &returned, &late, &admin, &reason, &recordID)
	if err != nil {
		fail(w, 404, "Loan not found.")
		return
	}
	if s.Role != "admin" && s.Role != "librarian" && uid != int64(s.UserID) {
		fail(w, 403, "You can only return your own book.")
		return
	}
	if returned.Valid {
		fail(w, 409, "This book has already been returned.")
		return
	}
	now := time.Now().UTC().Truncate(time.Second)
	lateDays := 0
	if now.After(due) {
		lateDays = int(math.Ceil(now.Sub(due).Hours() / 24))
	}
	rate := 10.0
	if raw := strings.TrimSpace(os.Getenv("LATE_FEE_PER_DAY")); raw != "" {
		if parsed, parseErr := strconv.ParseFloat(raw, 64); parseErr == nil && parsed >= 0 {
			rate = parsed
		}
	}
	late = math.Round(float64(lateDays)*rate*100) / 100
	total := math.Round((late+admin)*100) / 100
	method := strings.ToUpper(strings.TrimSpace(b.PaymentMethod))
	if method != "UPI" && method != "CARD" && method != "CASH" {
		method = "UPI"
	}
	paymentID := ""
	var rid int64
	if recordID.Valid {
		rid = recordID.Int64
		_, err = tx.ExecContext(r.Context(), `UPDATE return_record SET late_fine=$1,total_fine=$2,returned_at=$3,payment_status=$4,payment_method=CASE WHEN $4='recorded_demo' THEN $5 ELSE payment_method END,paid_at=CASE WHEN $4='recorded_demo' THEN $3 ELSE paid_at END WHERE id=$6`, late, total, now, func() string {
			if total > 0 {
				return "recorded_demo"
			}
			return "not_required"
		}(), method, rid)
	} else {
		err = tx.QueryRowContext(r.Context(), `INSERT INTO return_record(loan_id,late_fine,admin_fine,fine_reason,total_fine,payment_status,payment_method,paid_at,returned_at,created_by) VALUES($1,$2,$3,$4,$5,$6,CASE WHEN $6='recorded_demo' THEN $7 ELSE NULL END,CASE WHEN $6='recorded_demo' THEN $8 ELSE NULL END,$8,$9) RETURNING id`, id, late, admin, reason.String, total, func() string {
			if total > 0 {
				return "recorded_demo"
			}
			return "not_required"
		}(), method, now, s.Username).Scan(&rid)
	}
	if err != nil {
		fail(w, 503, "Return record could not be saved; book stock was not changed.")
		return
	}
	if total > 0 {
		paymentID = fmt.Sprintf("PAY-%d-%s", now.Unix(), strings.ToUpper(randomToken(6)))
		ref := fmt.Sprintf("DEMO-%s", strings.ToUpper(randomToken(12)))
		_, err = tx.ExecContext(r.Context(), `INSERT INTO payment(payment_id,return_id,loan_id,user_id,book_id,payer_name,amount,payment_method,payment_status,transaction_reference,cash_received_by,paid_at) VALUES($1,$2,$3,$4,$5,$6,$7,$8,'recorded_demo',$9,NULLIF($10,''),$11)`, paymentID, rid, id, uid, bid, name, total, method, ref, b.CashReceivedBy, now)
		if err != nil {
			fail(w, 503, "Payment record could not be saved; book stock was not changed.")
			return
		}
		_, err = tx.ExecContext(r.Context(), `UPDATE return_record SET payment_id=$1 WHERE id=$2`, paymentID, rid)
		if err != nil {
			fail(w, 503, "Payment reference could not be saved.")
			return
		}
	}
	_, err = tx.ExecContext(r.Context(), `UPDATE loan SET returned_at=$1 WHERE id=$2`, now, id)
	if err == nil {
		_, err = tx.ExecContext(r.Context(), `UPDATE book SET available_copies=LEAST(total_copies,available_copies+1) WHERE id=$1`, bid)
	}
	if err != nil {
		fail(w, 503, "Return could not be completed; transaction rolled back.")
		return
	}
	if err = tx.Commit(); err != nil {
		fail(w, 503, "Return transaction failed.")
		return
	}
	_ = a.audit(r, s.Username, "LIBRARY_BOOK_RETURN", fmt.Sprintf("loan_id=%d book=%s fine=%.2f payment_id=%s", id, title, total, paymentID))
	jsonOut(w, 200, body{"ok": true, "loan_id": id, "late_days": lateDays, "late_fine": late, "admin_fine": admin, "total_fine": total, "payment_id": paymentID, "payment_status": statusFor(total)})
}
func statusFor(total float64) string {
	if total > 0 {
		return "recorded_demo"
	}
	return "not_required"
}
func (a *App) listPayments(w http.ResponseWriter, r *http.Request) {
	s, ok := a.require(w, r, false)
	if !ok {
		return
	}
	rows, err := a.db.QueryContext(r.Context(), `SELECT p.id,p.payment_id,p.loan_id,p.user_id,p.book_id,p.payer_name,p.amount,p.payment_method,p.payment_status,COALESCE(p.transaction_reference,''),p.paid_at,b.title FROM payment p JOIN book b ON b.id=p.book_id WHERE ($1 OR p.user_id=$2) ORDER BY p.created_at DESC LIMIT 500`, s.Role == "admin" || s.Role == "librarian", s.UserID)
	if err != nil {
		fail(w, 503, "Could not load payment records.")
		return
	}
	defer rows.Close()
	items := []body{}
	for rows.Next() {
		var id, loanID, userID, bookID int64
		var pid, name, method, status, ref, title string
		var amount float64
		var paid time.Time
		if rows.Scan(&id, &pid, &loanID, &userID, &bookID, &name, &amount, &method, &status, &ref, &paid, &title) == nil {
			items = append(items, body{"id": id, "payment_id": pid, "loan_id": loanID, "user_id": userID, "book_id": bookID, "payer_name": name, "amount": amount, "payment_method": method, "payment_status": status, "transaction_reference": ref, "paid_at": paid, "book_title": title})
		}
	}
	jsonOut(w, 200, body{"payments": items})
}

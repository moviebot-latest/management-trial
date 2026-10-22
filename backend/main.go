package backend

import (
	"context"
	"crypto/hmac"
	"crypto/rand"
	"crypto/sha256"
	"crypto/tls"
	"database/sql"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"net"
	"net/http"
	"net/mail"
	"net/smtp"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"time"

	_ "github.com/jackc/pgx/v5/stdlib"
	"golang.org/x/crypto/scrypt"
)

type App struct {
	db     *sql.DB
	secret []byte
}
type User struct {
	ID           int          `json:"id"`
	EmployeeID   string       `json:"employee_id"`
	Name         string       `json:"name"`
	Gender       string       `json:"gender"`
	Department   string       `json:"department"`
	Email        string       `json:"email"`
	Username     string       `json:"username"`
	Role         string       `json:"role"`
	Active       bool         `json:"is_active"`
	Failed       int          `json:"failed_attempts"`
	LockedUntil  sql.NullTime `json:"-"`
	PasswordHash string       `json:"-"`
}
type Session struct {
	UserID   int    `json:"uid"`
	Username string `json:"username"`
	Name     string `json:"name"`
	Role     string `json:"role"`
	Expires  int64  `json:"exp"`
	CSRF     string `json:"csrf"`
}
type body map[string]any

func NewHandler() (http.Handler, error) {
	dsn := strings.TrimSpace(os.Getenv("DATABASE_URL"))
	if dsn == "" {
		return nil, errors.New("DATABASE_URL is required; refusing to use an in-memory or local fallback")
	}
	if strings.HasPrefix(dsn, "postgres://") {
		dsn = "postgresql://" + strings.TrimPrefix(dsn, "postgres://")
	}
	if strings.HasPrefix(dsn, "postgresql+psycopg://") {
		dsn = "postgresql://" + strings.TrimPrefix(dsn, "postgresql+psycopg://")
	}
	if strings.HasPrefix(dsn, "postgresql+psycopg2://") {
		dsn = "postgresql://" + strings.TrimPrefix(dsn, "postgresql+psycopg2://")
	}
	db, err := sql.Open("pgx", dsn)
	if err != nil {
		return nil, err
	}
	db.SetMaxOpenConns(envInt("DB_MAX_OPEN_CONNS", 5))
	db.SetMaxIdleConns(envInt("DB_MAX_IDLE_CONNS", 2))
	db.SetConnMaxLifetime(4 * time.Minute)
	db.SetConnMaxIdleTime(time.Minute)
	ctx, cancel := context.WithTimeout(context.Background(), 8*time.Second)
	defer cancel()
	if err := db.PingContext(ctx); err != nil {
		db.Close()
		return nil, fmt.Errorf("database connection failed: %w", err)
	}
	secret := strings.TrimSpace(os.Getenv("SESSION_SECRET"))
	if secret == "" {
		secret = strings.TrimSpace(os.Getenv("SECRET_KEY"))
	}
	if len(secret) < 32 {
		db.Close()
		return nil, errors.New("SESSION_SECRET (or SECRET_KEY) must contain at least 32 characters")
	}
	a := &App{db: db, secret: []byte(secret)}
	if err := a.ensureLibrarySchema(ctx); err != nil {
		db.Close()
		return nil, fmt.Errorf("library schema check failed: %w", err)
	}
	mux := http.NewServeMux()
	mux.HandleFunc("GET /api/health", a.health)
	mux.HandleFunc("GET /healthz", a.health)
	mux.HandleFunc("GET /api/auth/status", a.authStatus)
	mux.HandleFunc("POST /api/auth/login", a.login)
	mux.HandleFunc("POST /api/auth/logout", a.logout)
	mux.HandleFunc("POST /api/auth/register", a.register)
	mux.HandleFunc("POST /api/auth/register/verify-otp", a.verifyRegistration)
	mux.HandleFunc("POST /api/auth/register/resend-otp", a.resendRegistrationOTP)
	mux.HandleFunc("POST /api/auth/forgot-password/resend", a.resendResetOTP)
	mux.HandleFunc("POST /api/auth/forgot-password", a.forgotPassword)
	mux.HandleFunc("POST /api/auth/forgot-password/verify", a.verifyResetOTP)
	mux.HandleFunc("POST /api/auth/forgot-password/reset", a.resetPassword)
	mux.HandleFunc("GET /api/admin/users", a.adminUsers)
	mux.HandleFunc("PATCH /api/admin/users/{id}/status", a.toggleUser)
	mux.HandleFunc("POST /api/admin/users/{id}/unlock", a.unlockUser)
	mux.HandleFunc("DELETE /api/admin/users/{id}", a.deleteUser)
	mux.HandleFunc("GET /api/admin/email-campaigns", a.adminCampaigns)
	mux.HandleFunc("POST /api/admin/email-campaigns", a.createCampaign)
	mux.HandleFunc("POST /api/admin/email-campaigns/{id}/process", a.processCampaign)
	mux.HandleFunc("GET /api/admin/audit-logs", a.auditLogs)
	mux.HandleFunc("GET /api/admin/analytics", a.analytics)
	mux.HandleFunc("GET /api/library/books", a.listBooks)
	mux.HandleFunc("POST /api/library/books", a.createBook)
	mux.HandleFunc("PATCH /api/library/books/{id}", a.updateBook)
	mux.HandleFunc("DELETE /api/library/books/{id}", a.deleteBook)
	mux.HandleFunc("GET /api/library/loans", a.listLoans)
	mux.HandleFunc("POST /api/library/loans", a.issueLoan)
	mux.HandleFunc("POST /api/library/loans/{id}/return", a.returnLoan)
	mux.HandleFunc("POST /api/library/loans/{id}/fine", a.addFine)
	mux.HandleFunc("GET /api/library/payments", a.listPayments)
	mux.Handle("/", spaStatic(envOr("FRONTEND_DIST", "frontend/dist")))
	return a.middleware(mux), nil
}

func spaStatic(dist string) http.Handler {
	files := http.FileServer(http.Dir(dist))
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		rel := strings.TrimPrefix(filepath.Clean("/"+r.URL.Path), string(os.PathSeparator))
		if rel == "" || rel == "." {
			rel = "index.html"
		}
		root, _ := filepath.Abs(dist)
		path, _ := filepath.Abs(filepath.Join(dist, rel))
		if strings.HasPrefix(path, root+string(os.PathSeparator)) {
			if info, err := os.Stat(path); err == nil && !info.IsDir() {
				files.ServeHTTP(w, r)
				return
			}
		}
		http.ServeFile(w, r, filepath.Join(dist, "index.html"))
	})
}

func envOr(k, d string) string {
	if v := strings.TrimSpace(os.Getenv(k)); v != "" {
		return v
	}
	return d
}
func envInt(k string, d int) int {
	n, e := strconv.Atoi(os.Getenv(k))
	if e != nil || n < 1 {
		return d
	}
	return n
}
func (a *App) middleware(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("X-Content-Type-Options", "nosniff")
		w.Header().Set("X-Frame-Options", "DENY")
		w.Header().Set("Referrer-Policy", "strict-origin-when-cross-origin")
		w.Header().Set("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
		w.Header().Set("Cache-Control", "no-store")
		if r.Method == http.MethodOptions {
			w.WriteHeader(http.StatusNoContent)
			return
		}
		next.ServeHTTP(w, r)
	})
}
func jsonOut(w http.ResponseWriter, code int, v any) {
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.WriteHeader(code)
	_ = json.NewEncoder(w).Encode(v)
}
func fail(w http.ResponseWriter, code int, msg string) { jsonOut(w, code, body{"message": msg}) }
func readBody(r *http.Request, v any) error {
	r.Body = http.MaxBytesReader(nil, r.Body, 2<<20)
	d := json.NewDecoder(r.Body)
	d.DisallowUnknownFields()
	return d.Decode(v)
}
func (a *App) health(w http.ResponseWriter, r *http.Request) {
	ctx, c := context.WithTimeout(r.Context(), 2*time.Second)
	defer c()
	if e := a.db.PingContext(ctx); e != nil {
		fail(w, 503, "Database unavailable")
		return
	}
	jsonOut(w, 200, body{"ok": true, "service": "management-system-go", "database": "connected", "time": time.Now().UTC().Format(time.RFC3339)})
}
func (a *App) newSession(w http.ResponseWriter, s Session) {
	s.Expires = time.Now().Add(30 * time.Minute).Unix()
	raw, _ := json.Marshal(s)
	enc := base64.RawURLEncoding.EncodeToString(raw)
	mac := hmac.New(sha256.New, a.secret)
	mac.Write([]byte(enc))
	value := enc + "." + base64.RawURLEncoding.EncodeToString(mac.Sum(nil))
	http.SetCookie(w, &http.Cookie{Name: "ms_session", Value: value, Path: "/", HttpOnly: true, Secure: envOr("APP_ENV", "production") == "production", SameSite: http.SameSiteLaxMode, MaxAge: 1800})
	http.SetCookie(w, &http.Cookie{Name: "ms_csrf", Value: s.CSRF, Path: "/", HttpOnly: false, Secure: envOr("APP_ENV", "production") == "production", SameSite: http.SameSiteLaxMode, MaxAge: 1800})
}
func (a *App) session(r *http.Request) (Session, bool) {
	c, e := r.Cookie("ms_session")
	if e != nil {
		return Session{}, false
	}
	p := strings.Split(c.Value, ".")
	if len(p) != 2 {
		return Session{}, false
	}
	mac := hmac.New(sha256.New, a.secret)
	mac.Write([]byte(p[0]))
	sig, e := base64.RawURLEncoding.DecodeString(p[1])
	if e != nil || !hmac.Equal(sig, mac.Sum(nil)) {
		return Session{}, false
	}
	raw, e := base64.RawURLEncoding.DecodeString(p[0])
	if e != nil {
		return Session{}, false
	}
	var s Session
	if json.Unmarshal(raw, &s) != nil || s.UserID < 1 || s.Expires < time.Now().Unix() {
		return Session{}, false
	}
	return s, true
}
func (a *App) require(w http.ResponseWriter, r *http.Request, admin bool) (Session, bool) {
	s, ok := a.session(r)
	if !ok {
		fail(w, 401, "Please sign in again.")
		return Session{}, false
	}
	if admin && s.Role != "admin" {
		fail(w, 403, "Administrator access required.")
		return Session{}, false
	}
	if r.Method != "GET" && r.Method != "HEAD" {
		c, _ := r.Cookie("ms_csrf")
		if c == nil || c.Value == "" || r.Header.Get("X-CSRF-Token") == "" || !hmac.Equal([]byte(c.Value), []byte(r.Header.Get("X-CSRF-Token"))) || !hmac.Equal([]byte(s.CSRF), []byte(r.Header.Get("X-CSRF-Token"))) {
			fail(w, 403, "CSRF validation failed. Refresh the page and try again.")
			return Session{}, false
		}
	}
	return s, true
}
func (a *App) authStatus(w http.ResponseWriter, r *http.Request) {
	s, ok := a.session(r)
	if !ok {
		jsonOut(w, 200, body{"authenticated": false})
		return
	}
	jsonOut(w, 200, body{"authenticated": true, "user": body{"id": s.UserID, "username": s.Username, "name": s.Name, "role": s.Role}})
}
func (a *App) logout(w http.ResponseWriter, r *http.Request) {
	http.SetCookie(w, &http.Cookie{Name: "ms_session", Value: "", Path: "/", MaxAge: -1, HttpOnly: true, Secure: envOr("APP_ENV", "production") == "production", SameSite: http.SameSiteLaxMode})
	http.SetCookie(w, &http.Cookie{Name: "ms_csrf", Value: "", Path: "/", MaxAge: -1, Secure: envOr("APP_ENV", "production") == "production", SameSite: http.SameSiteLaxMode})
	jsonOut(w, 200, body{"ok": true})
}
func scanUser(row *sql.Row) (User, error) {
	var u User
	err := row.Scan(&u.ID, &u.EmployeeID, &u.Name, &u.Gender, &u.Department, &u.Email, &u.Username, &u.PasswordHash, &u.Role, &u.Active, &u.Failed, &u.LockedUntil)
	return u, err
}
func (a *App) login(w http.ResponseWriter, r *http.Request) {
	var b struct {
		Username string `json:"username"`
		Password string `json:"password"`
	}
	if readBody(r, &b) != nil || strings.TrimSpace(b.Username) == "" || b.Password == "" {
		fail(w, 400, "Username and password are required.")
		return
	}
	ctx, c := context.WithTimeout(r.Context(), 5*time.Second)
	defer c()
	u, e := scanUser(a.db.QueryRowContext(ctx, `SELECT user_id,employee_id,name,gender,department,email,username,password_hash,role,is_active,failed_attempts,locked_until FROM "user" WHERE lower(username)=lower($1)`, strings.TrimSpace(b.Username)))
	if e != nil {
		_ = a.audit(r, b.Username, "LOGIN_FAILED", "Unknown username or invalid credentials")
		fail(w, 401, "Invalid username or password.")
		return
	}
	if u.LockedUntil.Valid && u.LockedUntil.Time.After(time.Now().UTC()) {
		fail(w, 403, "Account temporarily locked. Try again later.")
		return
	}
	if !u.Active {
		fail(w, 403, "Your account is inactive. Contact an administrator.")
		return
	}
	if !checkWerkzeugHash(u.PasswordHash, b.Password) {
		_, _ = a.db.ExecContext(ctx, `UPDATE "user" SET failed_attempts=failed_attempts+1, locked_until=CASE WHEN failed_attempts+1>=5 THEN NOW()+INTERVAL '15 minutes' ELSE locked_until END WHERE user_id=$1`, u.ID)
		_ = a.audit(r, u.Username, "LOGIN_FAILED", "Invalid password")
		fail(w, 401, "Invalid username or password.")
		return
	}
	_, _ = a.db.ExecContext(ctx, `UPDATE "user" SET failed_attempts=0,locked_until=NULL,last_login=NOW() WHERE user_id=$1`, u.ID)
	csrf := randomToken(24)
	a.newSession(w, Session{UserID: u.ID, Username: u.Username, Name: u.Name, Role: u.Role, CSRF: csrf})
	_ = a.audit(r, u.Username, "LOGIN_SUCCESS", "Successful login")
	jsonOut(w, 200, body{"ok": true, "user": body{"id": u.ID, "username": u.Username, "name": u.Name, "role": u.Role}})
}
func randomToken(n int) string {
	b := make([]byte, n)
	if _, e := rand.Read(b); e != nil {
		return ""
	}
	return base64.RawURLEncoding.EncodeToString(b)
}
func randomOTP() string {
	b := make([]byte, 4)
	if _, e := rand.Read(b); e != nil {
		return "000000"
	}
	n := uint32(b[0])<<24 | uint32(b[1])<<16 | uint32(b[2])<<8 | uint32(b[3])
	return fmt.Sprintf("%06d", n%1000000)
}
func hashPassword(p string) (string, error) {
	salt := randomToken(16)
	sum, e := scrypt.Key([]byte(p), []byte(salt), 32768, 8, 1, 64)
	if e != nil {
		return "", e
	}
	return "scrypt:32768:8:1$" + salt + "$" + hex.EncodeToString(sum), nil
}
func checkWerkzeugHash(encoded, password string) bool {
	parts := strings.Split(encoded, "$")
	if len(parts) != 3 {
		return false
	}
	method, salt, want := parts[0], parts[1], parts[2]
	var got []byte
	var err error
	if strings.HasPrefix(method, "scrypt:") {
		var n, r, p int
		_, err = fmt.Sscanf(method, "scrypt:%d:%d:%d", &n, &r, &p)
		if err == nil && (n < 2 || n > 1<<20 || r < 1 || r > 32 || p < 1 || p > 16) {
			return false
		}
		if err == nil {
			got, err = scrypt.Key([]byte(password), []byte(salt), n, r, p, len(want)/2)
		}
	} else if strings.HasPrefix(method, "pbkdf2:sha256:") {
		var iter int
		_, err = fmt.Sscanf(method, "pbkdf2:sha256:%d", &iter)
		if err == nil {
			got = pbkdf2SHA256([]byte(password), []byte(salt), iter, len(want)/2)
		}
	} else {
		return false
	}
	if err != nil {
		return false
	}
	expected, e := hex.DecodeString(want)
	return e == nil && hmac.Equal(got, expected)
}
func pbkdf2SHA256(password, salt []byte, iter, keyLen int) []byte {
	if iter < 1 || iter > 5000000 || keyLen < 1 || keyLen > 128 {
		return nil
	}
	out := make([]byte, 0, keyLen)
	for i := uint32(1); len(out) < keyLen; i++ {
		mac := hmac.New(sha256.New, password)
		mac.Write(salt)
		mac.Write([]byte{byte(i >> 24), byte(i >> 16), byte(i >> 8), byte(i)})
		u := mac.Sum(nil)
		t := append([]byte(nil), u...)
		for j := 1; j < iter; j++ {
			mac = hmac.New(sha256.New, password)
			mac.Write(u)
			u = mac.Sum(nil)
			for k := range t {
				t[k] ^= u[k]
			}
		}
		out = append(out, t...)
	}
	return out[:keyLen]
}
func validEmail(s string) bool {
	a, e := mail.ParseAddress(strings.TrimSpace(s))
	return e == nil && a.Address == strings.TrimSpace(s) && strings.Contains(a.Address, ".")
}
func validPassword(p string) bool {
	if len(p) < 8 {
		return false
	}
	var up, lo, dig, sym bool
	for _, c := range p {
		switch {
		case c >= 'A' && c <= 'Z':
			up = true
		case c >= 'a' && c <= 'z':
			lo = true
		case c >= '0' && c <= '9':
			dig = true
		default:
			sym = true
		}
	}
	return up && lo && dig && sym
}
func (a *App) register(w http.ResponseWriter, r *http.Request) {
	var b struct {
		Name       string `json:"name"`
		Username   string `json:"username"`
		Email      string `json:"email"`
		Password   string `json:"password"`
		Confirm    string `json:"confirm_password"`
		Gender     string `json:"gender"`
		Department string `json:"department"`
	}
	if readBody(r, &b) != nil {
		fail(w, 400, "Invalid request body.")
		return
	}
	b.Name = strings.TrimSpace(b.Name)
	b.Username = strings.TrimSpace(b.Username)
	b.Email = strings.ToLower(strings.TrimSpace(b.Email))
	if b.Name == "" || b.Username == "" || !validEmail(b.Email) || !validPassword(b.Password) || b.Department == "" || b.Password != b.Confirm {
		fail(w, 400, "Enter valid account details. Password must include uppercase, lowercase, number and symbol (8+ characters).")
		return
	}
	if b.Gender != "Male" && b.Gender != "Female" && b.Gender != "Other" && b.Gender != "Prefer not to say" {
		fail(w, 400, "Select a valid gender.")
		return
	}
	if strings.EqualFold(b.Username, envOr("ADMIN_USERNAME", "admin")) || strings.EqualFold(b.Username, "admin") {
		fail(w, 400, "That username is reserved.")
		return
	}
	if !emailConfigured() {
		fail(w, 503, "Email verification is not configured. Set Brevo or SMTP environment variables.")
		return
	}
	ctx, c := context.WithTimeout(r.Context(), 5*time.Second)
	defer c()
	var exists bool
	e := a.db.QueryRowContext(ctx, `SELECT EXISTS(SELECT 1 FROM "user" WHERE lower(username)=lower($1) OR lower(email)=lower($2))`, b.Username, b.Email).Scan(&exists)
	if e != nil {
		fail(w, 503, "Database unavailable.")
		return
	}
	if exists {
		fail(w, 409, "That username or email is already in use.")
		return
	}
	var lastSent time.Time
	if err := a.db.QueryRowContext(ctx, `SELECT last_sent_at FROM email_verification WHERE lower(email)=lower($1)`, b.Email).Scan(&lastSent); err == nil && time.Since(lastSent) < 60*time.Second {
		fail(w, 429, "A verification code was sent recently. Please wait before requesting another.")
		return
	}
	pw, e := hashPassword(b.Password)
	if e != nil {
		fail(w, 500, "Could not secure the password.")
		return
	}
	otp := randomOTP()
	oh, e := hashPassword(otp)
	if e != nil {
		fail(w, 500, "Could not create verification code.")
		return
	}
	now := time.Now().UTC()
	_, e = a.db.ExecContext(ctx, `INSERT INTO email_verification(email,username,name,gender,department,password_hash,otp_hash,expires_at,last_sent_at,attempts,created_at) VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,0,NOW()) ON CONFLICT(email) DO UPDATE SET username=EXCLUDED.username,name=EXCLUDED.name,gender=EXCLUDED.gender,department=EXCLUDED.department,password_hash=EXCLUDED.password_hash,otp_hash=EXCLUDED.otp_hash,expires_at=EXCLUDED.expires_at,last_sent_at=EXCLUDED.last_sent_at,attempts=0,created_at=NOW()`, b.Email, b.Username, b.Name, b.Gender, b.Department, pw, oh, now.Add(10*time.Minute), now)
	if e != nil {
		fail(w, 503, "Could not create verification request.")
		return
	}
	if e = sendMail(b.Email, b.Name, "Your verification code", fmt.Sprintf("Your verification code is %s. It expires in 10 minutes. If you did not request this, ignore this email.", otp)); e != nil {
		_, _ = a.db.ExecContext(ctx, `DELETE FROM email_verification WHERE lower(email)=lower($1)`, b.Email)
		fail(w, 503, "Verification email could not be sent. Check the email provider configuration.")
		return
	}
	jsonOut(w, 202, body{"ok": true, "otp_required": true, "message": "Verification code sent to your email."})
}
func (a *App) resendRegistrationOTP(w http.ResponseWriter, r *http.Request) {
	var b struct {
		Email string `json:"email"`
	}
	if readBody(r, &b) != nil || !validEmail(b.Email) {
		fail(w, 400, "Enter a valid email address.")
		return
	}
	ctx, c := context.WithTimeout(r.Context(), 5*time.Second)
	defer c()
	var name, otpHash string
	var last time.Time
	err := a.db.QueryRowContext(ctx, `SELECT name,otp_hash,last_sent_at FROM email_verification WHERE lower(email)=lower($1)`, strings.ToLower(b.Email)).Scan(&name, &otpHash, &last)
	if err != nil {
		fail(w, 404, "Registration request not found. Start registration again.")
		return
	}
	if time.Since(last) < 60*time.Second {
		fail(w, 429, fmt.Sprintf("Please wait %d seconds before requesting another code.", int(60-time.Since(last).Seconds())))
		return
	}
	otp := randomOTP()
	h, err := hashPassword(otp)
	if err != nil {
		fail(w, 500, "Could not create code.")
		return
	}
	_, err = a.db.ExecContext(ctx, `UPDATE email_verification SET otp_hash=$1,expires_at=NOW()+INTERVAL '10 minutes',last_sent_at=NOW(),attempts=0 WHERE lower(email)=lower($2)`, h, strings.ToLower(b.Email))
	if err != nil {
		fail(w, 503, "Could not refresh verification code.")
		return
	}
	if err = sendMail(strings.ToLower(b.Email), name, "Your verification code", fmt.Sprintf("Your verification code is %s. It expires in 10 minutes.", otp)); err != nil {
		fail(w, 503, "Verification email could not be sent.")
		return
	}
	jsonOut(w, 200, body{"ok": true, "message": "A new verification code has been sent."})
}
func (a *App) resendResetOTP(w http.ResponseWriter, r *http.Request) {
	var b struct {
		Email string `json:"email"`
	}
	if readBody(r, &b) != nil || !validEmail(b.Email) {
		fail(w, 400, "Enter a valid email address.")
		return
	}
	ctx, c := context.WithTimeout(r.Context(), 5*time.Second)
	defer c()
	var id int
	var name, role string
	err := a.db.QueryRowContext(ctx, `SELECT user_id,name,role FROM "user" WHERE lower(email)=lower($1) AND is_active=TRUE`, strings.ToLower(b.Email)).Scan(&id, &name, &role)
	if err != nil || role == "admin" {
		jsonOut(w, 202, body{"ok": true, "message": "If the account exists, reset instructions will be sent."})
		return
	}
	var last time.Time
	err = a.db.QueryRowContext(ctx, `SELECT last_sent_at FROM password_reset WHERE user_id=$1 ORDER BY created_at DESC LIMIT 1`, id).Scan(&last)
	if err == nil && time.Since(last) < 60*time.Second {
		fail(w, 429, "Please wait before requesting another code.")
		return
	}
	otp := randomOTP()
	h, err := hashPassword(otp)
	if err != nil {
		fail(w, 500, "Could not create code.")
		return
	}
	_, err = a.db.ExecContext(ctx, `INSERT INTO password_reset(user_id,email,otp_hash,expires_at,last_sent_at,attempts,created_at) VALUES($1,$2,$3,NOW()+INTERVAL '10 minutes',NOW(),0,NOW())`, id, strings.ToLower(b.Email), h)
	if err != nil {
		fail(w, 503, "Could not start password reset.")
		return
	}
	if err = sendMail(strings.ToLower(b.Email), name, "Password reset verification code", fmt.Sprintf("Your password reset code is %s. It expires in 10 minutes.", otp)); err != nil {
		fail(w, 503, "Reset email could not be sent.")
		return
	}
	jsonOut(w, 202, body{"ok": true, "message": "If the account exists, reset instructions will be sent."})
}

func (a *App) verifyRegistration(w http.ResponseWriter, r *http.Request) {
	var b struct {
		Email string `json:"email"`
		OTP   string `json:"otp"`
	}
	if readBody(r, &b) != nil || !validEmail(b.Email) || len(b.OTP) != 6 {
		fail(w, 400, "Enter email and 6-digit OTP.")
		return
	}
	ctx, c := context.WithTimeout(r.Context(), 5*time.Second)
	defer c()
	var username, name, gender, dept, pw, oh string
	var exp time.Time
	var attempts int
	e := a.db.QueryRowContext(ctx, `SELECT username,name,gender,department,password_hash,otp_hash,expires_at,attempts FROM email_verification WHERE lower(email)=lower($1)`, strings.ToLower(b.Email)).Scan(&username, &name, &gender, &dept, &pw, &oh, &exp, &attempts)
	if e != nil {
		fail(w, 400, "Verification request not found. Request a new code.")
		return
	}
	if time.Now().After(exp) || attempts >= 5 {
		_, _ = a.db.ExecContext(ctx, `DELETE FROM email_verification WHERE lower(email)=lower($1)`, b.Email)
		fail(w, 400, "Code expired or attempts exceeded. Request a new code.")
		return
	}
	if !checkWerkzeugHash(oh, b.OTP) {
		_, _ = a.db.ExecContext(ctx, `UPDATE email_verification SET attempts=attempts+1 WHERE lower(email)=lower($1)`, b.Email)
		fail(w, 400, "Invalid verification code.")
		return
	}
	tx, e := a.db.BeginTx(ctx, nil)
	if e != nil {
		fail(w, 503, "Database unavailable.")
		return
	}
	defer tx.Rollback()
	var dup bool
	if e = tx.QueryRowContext(ctx, `SELECT EXISTS(SELECT 1 FROM "user" WHERE lower(username)=lower($1) OR lower(email)=lower($2))`, username, b.Email).Scan(&dup); e != nil || dup {
		fail(w, 409, "Username or email is already registered.")
		return
	}
	code := departmentCode(dept)
	year := time.Now().Year() % 100
	var seq int
	e = tx.QueryRowContext(ctx, `SELECT COALESCE(MAX(CAST(split_part(employee_id,'-',3) AS INTEGER)),0)+1 FROM "user" WHERE employee_id LIKE $1`, fmt.Sprintf("%s-%02d-%%", code, year)).Scan(&seq)
	if e != nil {
		fail(w, 503, "Could not generate employee ID.")
		return
	}
	emp := fmt.Sprintf("%s-%02d-%04d", code, year, seq)
	_, e = tx.ExecContext(ctx, `INSERT INTO "user"(employee_id,name,gender,department,email,username,password_hash,role,is_active,failed_attempts) VALUES($1,$2,$3,$4,$5,$6,$7,'user',TRUE,0)`, emp, name, gender, dept, strings.ToLower(b.Email), username, pw)
	if e != nil {
		fail(w, 409, "Account could not be created. Check for duplicate details.")
		return
	}
	_, _ = tx.ExecContext(ctx, `DELETE FROM email_verification WHERE lower(email)=lower($1)`, b.Email)
	if e = tx.Commit(); e != nil {
		fail(w, 503, "Account could not be saved.")
		return
	}
	_ = a.audit(r, username, "REGISTER", "Verified email; employee ID "+emp)
	jsonOut(w, 201, body{"ok": true, "message": "Account created successfully.", "employee_id": emp})
}
func departmentCode(d string) string {
	m := map[string]string{"Administration": "ADM", "Human Resources": "HR", "Finance": "FIN", "Sales": "SAL", "IT / Engineering": "ITE", "Operations": "OPS", "Marketing": "MKT", "Legal": "LGL", "Customer Support": "CUS", "Research & Development": "RND"}
	if c := m[d]; c != "" {
		return c
	}
	return "EMP"
}
func (a *App) forgotPassword(w http.ResponseWriter, r *http.Request) {
	var b struct {
		Email string `json:"email"`
	}
	if readBody(r, &b) != nil || !validEmail(b.Email) {
		fail(w, 400, "Enter a valid email address.")
		return
	}
	ctx, c := context.WithTimeout(r.Context(), 5*time.Second)
	defer c()
	var id int
	var name, username, role string
	e := a.db.QueryRowContext(ctx, `SELECT user_id,name,username,role FROM "user" WHERE lower(email)=lower($1) AND is_active=TRUE`, strings.ToLower(b.Email)).Scan(&id, &name, &username, &role)
	if e != nil {
		jsonOut(w, 202, body{"ok": true, "message": "If the account exists, reset instructions will be sent."})
		return
	}
	if role == "admin" {
		jsonOut(w, 202, body{"ok": true, "message": "Admin password is managed by deployment environment settings."})
		return
	}
	var lastSent time.Time
	if err := a.db.QueryRowContext(ctx, `SELECT last_sent_at FROM password_reset WHERE user_id=$1 ORDER BY created_at DESC LIMIT 1`, id).Scan(&lastSent); err == nil && time.Since(lastSent) < 60*time.Second {
		jsonOut(w, 202, body{"ok": true, "message": "If the account exists, reset instructions will be sent."})
		return
	}
	otp := randomOTP()
	oh, e := hashPassword(otp)
	if e != nil {
		fail(w, 500, "Could not create reset code.")
		return
	}
	_, e = a.db.ExecContext(ctx, `INSERT INTO password_reset(user_id,email,otp_hash,expires_at,last_sent_at,attempts,created_at) VALUES($1,$2,$3,NOW()+INTERVAL '10 minutes',NOW(),0,NOW())`, id, strings.ToLower(b.Email), oh)
	if e != nil {
		fail(w, 503, "Could not start password reset.")
		return
	}
	if e = sendMail(strings.ToLower(b.Email), name, "Password reset verification code", fmt.Sprintf("Your password reset code is %s. It expires in 10 minutes.", otp)); e != nil {
		fail(w, 503, "Reset email could not be sent.")
		return
	}
	jsonOut(w, 202, body{"ok": true, "message": "If the account exists, reset instructions will be sent."})
}
func (a *App) verifyResetOTP(w http.ResponseWriter, r *http.Request) {
	var b struct {
		Email string `json:"email"`
		OTP   string `json:"otp"`
	}
	if readBody(r, &b) != nil || !validEmail(b.Email) || len(b.OTP) != 6 {
		fail(w, 400, "Enter email and 6-digit OTP.")
		return
	}
	ctx, c := context.WithTimeout(r.Context(), 5*time.Second)
	defer c()
	var id int
	var oh string
	var exp time.Time
	var attempts int
	e := a.db.QueryRowContext(ctx, `SELECT user_id,otp_hash,expires_at,attempts FROM password_reset WHERE lower(email)=lower($1) ORDER BY created_at DESC LIMIT 1`, strings.ToLower(b.Email)).Scan(&id, &oh, &exp, &attempts)
	if e != nil || time.Now().After(exp) || attempts >= 5 {
		fail(w, 400, "Reset code expired or unavailable.")
		return
	}
	if !checkWerkzeugHash(oh, b.OTP) {
		_, _ = a.db.ExecContext(ctx, `UPDATE password_reset SET attempts=attempts+1 WHERE user_id=$1 AND lower(email)=lower($2)`, id, b.Email)
		fail(w, 400, "Invalid reset code.")
		return
	}
	_, _ = a.db.ExecContext(ctx, `UPDATE password_reset SET verified_at=NOW() WHERE user_id=$1 AND lower(email)=lower($2)`, id, b.Email)
	jsonOut(w, 200, body{"ok": true, "verified": true, "message": "Code verified. You may now set a new password."})
}
func (a *App) resetPassword(w http.ResponseWriter, r *http.Request) {
	var b struct {
		Email    string `json:"email"`
		Password string `json:"password"`
		Confirm  string `json:"confirm_password"`
	}
	if readBody(r, &b) != nil || !validEmail(b.Email) || b.Password != b.Confirm || !validPassword(b.Password) {
		fail(w, 400, "Provide matching strong passwords.")
		return
	}
	ctx, c := context.WithTimeout(r.Context(), 5*time.Second)
	defer c()
	var id int
	var verified sql.NullTime
	var exp time.Time
	e := a.db.QueryRowContext(ctx, `SELECT user_id,verified_at,expires_at FROM password_reset WHERE lower(email)=lower($1) ORDER BY created_at DESC LIMIT 1`, strings.ToLower(b.Email)).Scan(&id, &verified, &exp)
	if e != nil || !verified.Valid || time.Now().After(exp) {
		fail(w, 403, "Verify a valid reset code first.")
		return
	}
	var role string
	_ = a.db.QueryRowContext(ctx, `SELECT role FROM "user" WHERE user_id=$1`, id).Scan(&role)
	if role == "admin" {
		fail(w, 403, "Admin password is managed in deployment environment settings.")
		return
	}
	pw, e := hashPassword(b.Password)
	if e != nil {
		fail(w, 500, "Could not secure password.")
		return
	}
	_, e = a.db.ExecContext(ctx, `UPDATE "user" SET password_hash=$1,failed_attempts=0,locked_until=NULL WHERE user_id=$2`, pw, id)
	if e != nil {
		fail(w, 503, "Password could not be updated.")
		return
	}
	_, _ = a.db.ExecContext(ctx, `DELETE FROM password_reset WHERE user_id=$1`, id)
	_ = a.audit(r, b.Email, "PASSWORD_RESET", "Password reset completed after OTP verification")
	jsonOut(w, 200, body{"ok": true, "message": "Password reset successfully. Please sign in."})
}
func (a *App) adminUsers(w http.ResponseWriter, r *http.Request) {
	if _, ok := a.require(w, r, true); !ok {
		return
	}
	ctx, c := context.WithTimeout(r.Context(), 5*time.Second)
	defer c()
	rows, e := a.db.QueryContext(ctx, `SELECT user_id,employee_id,name,gender,department,email,username,role,is_active,failed_attempts,created_at FROM "user" ORDER BY user_id DESC LIMIT 1000`)
	if e != nil {
		fail(w, 503, "Could not load users.")
		return
	}
	defer rows.Close()
	users := []body{}
	for rows.Next() {
		var id, failed int
		var emp, name, gender, dept, email, username, role string
		var active bool
		var created time.Time
		if rows.Scan(&id, &emp, &name, &gender, &dept, &email, &username, &role, &active, &failed, &created) == nil {
			users = append(users, body{"id": id, "employee_id": emp, "name": name, "gender": gender, "department": dept, "email": email, "username": username, "role": role, "is_active": active, "failed_attempts": failed, "created_at": created})
		}
	}
	jsonOut(w, 200, body{"users": users, "count": len(users)})
}
func (a *App) toggleUser(w http.ResponseWriter, r *http.Request) {
	s, ok := a.require(w, r, true)
	if !ok {
		return
	}
	id, e := strconv.Atoi(r.PathValue("id"))
	if e != nil || id < 1 {
		fail(w, 400, "Invalid user ID.")
		return
	}
	var b struct {
		Active bool `json:"is_active"`
	}
	if readBody(r, &b) != nil {
		fail(w, 400, "Invalid request.")
		return
	}
	res, e := a.db.ExecContext(r.Context(), `UPDATE "user" SET is_active=$1 WHERE user_id=$2 AND role<>'admin'`, b.Active, id)
	if e != nil {
		fail(w, 503, "Could not update user.")
		return
	}
	n, _ := res.RowsAffected()
	if n == 0 {
		fail(w, 404, "User not found or protected admin account.")
		return
	}
	_ = a.audit(r, s.Username, "USER_STATUS_CHANGED", fmt.Sprintf("user_id=%d active=%t", id, b.Active))
	jsonOut(w, 200, body{"ok": true})
}
func (a *App) unlockUser(w http.ResponseWriter, r *http.Request) {
	s, ok := a.require(w, r, true)
	if !ok {
		return
	}
	id, e := strconv.Atoi(r.PathValue("id"))
	if e != nil || id < 1 {
		fail(w, 400, "Invalid user ID.")
		return
	}
	res, e := a.db.ExecContext(r.Context(), `UPDATE "user" SET failed_attempts=0,locked_until=NULL WHERE user_id=$1 AND role<>'admin'`, id)
	if e != nil {
		fail(w, 503, "Could not unlock user.")
		return
	}
	n, _ := res.RowsAffected()
	if n == 0 {
		fail(w, 404, "User not found or protected admin account.")
		return
	}
	_ = a.audit(r, s.Username, "USER_UNLOCKED", fmt.Sprintf("user_id=%d", id))
	jsonOut(w, 200, body{"ok": true})
}
func (a *App) deleteUser(w http.ResponseWriter, r *http.Request) {
	s, ok := a.require(w, r, true)
	if !ok {
		return
	}
	id, e := strconv.Atoi(r.PathValue("id"))
	if e != nil || id < 1 {
		fail(w, 400, "Invalid user ID.")
		return
	}
	res, e := a.db.ExecContext(r.Context(), `DELETE FROM "user" WHERE user_id=$1 AND role<>'admin'`, id)
	if e != nil {
		fail(w, 503, "Could not delete user; related records may still exist.")
		return
	}
	n, _ := res.RowsAffected()
	if n == 0 {
		fail(w, 404, "User not found or protected admin account.")
		return
	}
	_ = a.audit(r, s.Username, "USER_DELETED", fmt.Sprintf("user_id=%d", id))
	jsonOut(w, 200, body{"ok": true})
}
func (a *App) adminCampaigns(w http.ResponseWriter, r *http.Request) {
	if _, ok := a.require(w, r, true); !ok {
		return
	}
	ctx, c := context.WithTimeout(r.Context(), 5*time.Second)
	defer c()
	rows, e := a.db.QueryContext(ctx, `SELECT id,subject,status,total_recipients,sent_count,failed_count,created_at FROM email_campaign ORDER BY created_at DESC LIMIT 500`)
	if e != nil {
		fail(w, 503, "Could not load campaigns.")
		return
	}
	defer rows.Close()
	items := []body{}
	for rows.Next() {
		var id, total, sent, failed int
		var subject, status string
		var created time.Time
		if rows.Scan(&id, &subject, &status, &total, &sent, &failed, &created) == nil {
			items = append(items, body{"id": id, "subject": subject, "status": status, "total_recipients": total, "sent_count": sent, "failed_count": failed, "created_at": created.Format(time.RFC3339)})
		}
	}
	jsonOut(w, 200, body{"campaigns": items, "count": len(items)})
}
func (a *App) createCampaign(w http.ResponseWriter, r *http.Request) {
	s, ok := a.require(w, r, true)
	if !ok {
		return
	}
	var b struct {
		Subject         string `json:"subject"`
		Message         string `json:"message"`
		RecipientMode   string `json:"recipient_mode"`
		SelectedUserIDs []int  `json:"selected_user_ids"`
		SingleUserID    int    `json:"single_user_id"`
		Schedule        string `json:"scheduled_at"`
	}
	if readBody(r, &b) != nil {
		fail(w, 400, "Invalid request body.")
		return
	}
	b.Subject = strings.TrimSpace(b.Subject)
	b.Message = strings.TrimSpace(b.Message)
	if b.Subject == "" || len(b.Subject) > 200 || strings.ContainsAny(b.Subject, "\r\n") || b.Message == "" || len(b.Message) > 12000 {
		fail(w, 400, "Enter a subject (max 200 chars) and message (max 12,000 chars).")
		return
	}
	mode := b.RecipientMode
	if mode == "" {
		mode = "all"
	}
	if mode != "all" && mode != "active" && mode != "selected" && mode != "single" {
		fail(w, 400, "Invalid recipient group.")
		return
	}
	ctx, c := context.WithTimeout(r.Context(), 8*time.Second)
	defer c()
	rows, e := a.db.QueryContext(ctx, `SELECT user_id,email,name FROM "user" WHERE email IS NOT NULL AND ($1='all' OR ($1='active' AND is_active=TRUE) OR ($1 IN ('selected','single') AND user_id=ANY($2))) ORDER BY user_id LIMIT 5000`, mode, idsForMode(mode, b.SelectedUserIDs, b.SingleUserID))
	if e != nil {
		fail(w, 503, "Could not resolve recipients.")
		return
	}
	type recipient struct {
		id          int
		email, name string
	}
	recips := []recipient{}
	seen := map[string]bool{}
	for rows.Next() {
		var x recipient
		if rows.Scan(&x.id, &x.email, &x.name) == nil && validEmail(x.email) && !strings.HasSuffix(strings.ToLower(x.email), ".local") && !seen[strings.ToLower(x.email)] {
			seen[strings.ToLower(x.email)] = true
			recips = append(recips, x)
		}
	}
	rows.Close()
	if len(recips) == 0 {
		fail(w, 400, "No valid recipients found. For selected users, pass selected_user_ids.")
		return
	}
	if mode == "single" && len(recips) != 1 {
		fail(w, 400, "Select exactly one recipient.")
		return
	}
	if len(recips) > envInt("EMAIL_CAMPAIGN_MAX_RECIPIENTS", 5000) {
		fail(w, 400, "Recipient safety cap exceeded.")
		return
	}
	tx, e := a.db.BeginTx(ctx, nil)
	if e != nil {
		fail(w, 503, "Could not save campaign.")
		return
	}
	defer tx.Rollback()
	status := "queued"
	var schedule any = nil
	if b.Schedule != "" {
		t, pe := time.Parse(time.RFC3339, b.Schedule)
		if pe != nil || !t.After(time.Now()) {
			fail(w, 400, "Schedule must be a future RFC3339 date/time.")
			return
		}
		schedule = t.UTC()
		status = "scheduled"
	}
	var id int
	e = tx.QueryRowContext(ctx, `INSERT INTO email_campaign(submission_key,subject,message,template,recipient_mode,status,created_by,scheduled_at,total_recipients,created_at,test_only) VALUES($1,$2,$3,'custom',$4,$5,$6,$7,$8,NOW(),FALSE) RETURNING id`, randomToken(24), b.Subject, b.Message, mode, status, s.Username, schedule, len(recips)).Scan(&id)
	if e != nil {
		fail(w, 503, "Campaign could not be saved.")
		return
	}
	for _, x := range recips {
		if _, e = tx.ExecContext(ctx, `INSERT INTO email_campaign_recipient(campaign_id,user_id,email,recipient_name,status,created_at) VALUES($1,$2,$3,$4,'pending',NOW()) ON CONFLICT(campaign_id,email) DO NOTHING`, id, x.id, x.email, x.name); e != nil {
			fail(w, 503, "Campaign recipients could not be saved.")
			return
		}
	}
	if e = tx.Commit(); e != nil {
		fail(w, 503, "Campaign could not be saved.")
		return
	}
	_ = a.audit(r, s.Username, "ADMIN_EMAIL_CAMPAIGN_CREATE", fmt.Sprintf("campaign_id=%d mode=%s recipients=%d", id, mode, len(recips)))
	jsonOut(w, 201, body{"ok": true, "campaign_id": id, "status": status, "total_recipients": len(recips), "message": "Campaign saved. Delivery requires processing by an authorized admin."})
}
func idsForMode(mode string, ids []int, single int) []int {
	if mode == "single" {
		if single > 0 {
			return []int{single}
		}
		return []int{}
	}
	return ids
}
func (a *App) processCampaign(w http.ResponseWriter, r *http.Request) {
	s, ok := a.require(w, r, true)
	if !ok {
		return
	}
	id, e := strconv.Atoi(r.PathValue("id"))
	if e != nil || id < 1 {
		fail(w, 400, "Invalid campaign ID.")
		return
	}
	ctx, c := context.WithTimeout(r.Context(), 10*time.Second)
	defer c()
	var status, subject, message string
	var scheduled sql.NullTime
	e = a.db.QueryRowContext(ctx, `SELECT status,subject,message,scheduled_at FROM email_campaign WHERE id=$1`, id).Scan(&status, &subject, &message, &scheduled)
	if e != nil {
		fail(w, 404, "Campaign not found.")
		return
	}
	if status == "cancelled" || status == "sent" {
		fail(w, 409, "This campaign cannot be processed in its current status.")
		return
	}
	if scheduled.Valid && scheduled.Time.After(time.Now()) {
		fail(w, 409, "Campaign is scheduled for a future time.")
		return
	}
	rows, e := a.db.QueryContext(ctx, `SELECT id,email,recipient_name FROM email_campaign_recipient WHERE campaign_id=$1 AND status IN ('pending','failed') ORDER BY id LIMIT 25`, id)
	if e != nil {
		fail(w, 503, "Could not load campaign recipients.")
		return
	}
	type rec struct {
		id          int
		email, name string
	}
	list := []rec{}
	for rows.Next() {
		var x rec
		if rows.Scan(&x.id, &x.email, &x.name) == nil {
			list = append(list, x)
		}
	}
	rows.Close()
	if len(list) == 0 {
		jsonOut(w, 200, body{"ok": true, "processed": 0, "status": "sent", "message": "No pending recipients."})
		return
	}
	sent, failed := 0, 0
	for _, x := range list {
		if er := sendMail(x.email, x.name, subject, message); er != nil {
			failed++
			_, _ = a.db.ExecContext(ctx, `UPDATE email_campaign_recipient SET status='failed',error=$1 WHERE id=$2`, truncate(er.Error(), 450), x.id)
		} else {
			sent++
			_, _ = a.db.ExecContext(ctx, `UPDATE email_campaign_recipient SET status='sent',sent_at=NOW(),error=NULL WHERE id=$1`, x.id)
		}
	}
	var pending int
	_ = a.db.QueryRowContext(ctx, `SELECT COUNT(*) FROM email_campaign_recipient WHERE campaign_id=$1 AND status='pending'`, id).Scan(&pending)
	newStatus := "sent"
	if pending > 0 {
		newStatus = "queued"
	} else if failed > 0 {
		newStatus = "partial"
	}
	_, _ = a.db.ExecContext(ctx, `UPDATE email_campaign SET status=$1,sent_count=(SELECT COUNT(*) FROM email_campaign_recipient WHERE campaign_id=$2 AND status='sent'),failed_count=(SELECT COUNT(*) FROM email_campaign_recipient WHERE campaign_id=$2 AND status='failed'),sent_at=CASE WHEN $1 IN ('sent','partial') THEN NOW() ELSE sent_at END WHERE id=$2`, newStatus, id)
	_ = a.audit(r, s.Username, "ADMIN_EMAIL_CAMPAIGN_PROCESS", fmt.Sprintf("campaign_id=%d batch_sent=%d batch_failed=%d", id, sent, failed))
	jsonOut(w, 200, body{"ok": true, "processed": len(list), "sent": sent, "failed": failed, "status": newStatus, "pending": pending})
}
func (a *App) audit(r *http.Request, user, action, details string) error {
	ip := strings.Split(r.Header.Get("X-Forwarded-For"), ",")[0]
	if ip == "" {
		ip = r.RemoteAddr
	}
	if len(details) > 500 {
		details = details[:500]
	}
	_, e := a.db.ExecContext(r.Context(), `INSERT INTO audit_log(username,action,ip_address,details,timestamp) VALUES($1,$2,$3,$4,NOW())`, truncate(user, 80), truncate(action, 80), truncate(ip, 45), details)
	return e
}
func truncate(s string, n int) string {
	if len(s) > n {
		return s[:n]
	}
	return s
}
func (a *App) auditLogs(w http.ResponseWriter, r *http.Request) {
	if _, ok := a.require(w, r, true); !ok {
		return
	}
	rows, e := a.db.QueryContext(r.Context(), `SELECT id,username,action,COALESCE(ip_address,''),COALESCE(details,''),timestamp FROM audit_log ORDER BY timestamp DESC LIMIT 300`)
	if e != nil {
		fail(w, 503, "Could not load audit log.")
		return
	}
	defer rows.Close()
	items := []body{}
	for rows.Next() {
		var id int
		var u, act, ip, details string
		var ts time.Time
		if rows.Scan(&id, &u, &act, &ip, &details, &ts) == nil {
			items = append(items, body{"id": id, "username": u, "action": act, "ip_address": ip, "details": details, "timestamp": ts})
		}
	}
	jsonOut(w, 200, body{"logs": items})
}
func (a *App) analytics(w http.ResponseWriter, r *http.Request) {
	if _, ok := a.require(w, r, true); !ok {
		return
	}
	var users, active, campaigns, sent int
	e := a.db.QueryRowContext(r.Context(), `SELECT COUNT(*),COUNT(*) FILTER(WHERE is_active) FROM "user"`).Scan(&users, &active)
	if e != nil {
		fail(w, 503, "Could not load analytics.")
		return
	}
	_ = a.db.QueryRowContext(r.Context(), `SELECT COUNT(*),COALESCE(SUM(sent_count),0) FROM email_campaign`).Scan(&campaigns, &sent)
	jsonOut(w, 200, body{"users": users, "active_users": active, "campaigns": campaigns, "sent_recipients": sent})
}
func emailConfigured() bool {
	return (strings.TrimSpace(os.Getenv("BREVO_API_KEY")) != "" && strings.TrimSpace(os.Getenv("BREVO_SENDER_EMAIL")) != "") || (strings.TrimSpace(os.Getenv("SMTP_HOST")) != "" && strings.TrimSpace(os.Getenv("SMTP_USER")) != "" && strings.TrimSpace(os.Getenv("SMTP_PASSWORD")) != "")
}
func sendMail(to, name, subject, plain string) error {
	apiKey := strings.TrimSpace(os.Getenv("BREVO_API_KEY"))
	from := strings.TrimSpace(os.Getenv("BREVO_SENDER_EMAIL"))
	senderName := envOr("BREVO_SENDER_NAME", "Management System")
	if apiKey != "" && from != "" {
		payload := map[string]any{"sender": map[string]string{"name": senderName, "email": from}, "to": []map[string]string{{"email": to, "name": name}}, "subject": subject, "textContent": plain}
		raw, _ := json.Marshal(payload)
		req, _ := http.NewRequest(http.MethodPost, "https://api.brevo.com/v3/smtp/email", strings.NewReader(string(raw)))
		req.Header.Set("api-key", apiKey)
		req.Header.Set("Content-Type", "application/json")
		client := &http.Client{Timeout: 10 * time.Second}
		resp, e := client.Do(req)
		if e == nil {
			defer resp.Body.Close()
			if resp.StatusCode >= 200 && resp.StatusCode < 300 {
				return nil
			}
			e = fmt.Errorf("Brevo returned HTTP %d", resp.StatusCode)
		}
		if strings.TrimSpace(os.Getenv("SMTP_HOST")) == "" {
			return e
		}
	}
	host := strings.TrimSpace(os.Getenv("SMTP_HOST"))
	user := strings.TrimSpace(os.Getenv("SMTP_USER"))
	pass := os.Getenv("SMTP_PASSWORD")
	port := envOr("SMTP_PORT", "587")
	sender := envOr("SMTP_FROM", user)
	if host == "" || user == "" || pass == "" {
		return errors.New("email provider not configured or delivery failed")
	}
	return sendSMTP(host, port, user, pass, sender, to, subject, plain)
}
func sendSMTP(host, port, user, password, from, to, subject, plain string) error {
	addr := net.JoinHostPort(host, port)
	auth := smtp.PlainAuth("", user, password, host)
	safeSubject := strings.ReplaceAll(strings.ReplaceAll(subject, "\r", ""), "\n", "")
	msg := []byte("From: " + from + "\r\nTo: " + to + "\r\nSubject: " + safeSubject + "\r\nMIME-Version: 1.0\r\nContent-Type: text/plain; charset=UTF-8\r\n\r\n" + plain + "\r\n")
	if port != "465" {
		return smtp.SendMail(addr, auth, from, []string{to}, msg)
	}
	conn, err := tls.DialWithDialer(&net.Dialer{Timeout: 10 * time.Second}, "tcp", addr, &tls.Config{ServerName: host, MinVersion: tls.VersionTLS12})
	if err != nil {
		return err
	}
	defer conn.Close()
	client, err := smtp.NewClient(conn, host)
	if err != nil {
		return err
	}
	defer client.Close()
	if err = client.Auth(auth); err != nil {
		return err
	}
	if err = client.Mail(from); err != nil {
		return err
	}
	if err = client.Rcpt(to); err != nil {
		return err
	}
	writer, err := client.Data()
	if err != nil {
		return err
	}
	if _, err = writer.Write(msg); err != nil {
		_ = writer.Close()
		return err
	}
	if err = writer.Close(); err != nil {
		return err
	}
	return client.Quit()
}

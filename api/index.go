package handler

import (
	"encoding/json"
	"net/http"
	"strings"
	"sync"

	"management-system-go/backend"
)

var handlerMu sync.Mutex
var apiHandler http.Handler

// Handler is the single Vercel Go Function. vercel.json forwards /api/* here
// while preserving the original API path in the private __path query value.
func Handler(w http.ResponseWriter, r *http.Request) {
	q := r.URL.Query()
	originalPath := q.Get("__path")
	q.Del("__path")
	r.URL.RawQuery = q.Encode()
	if originalPath != "" {
		originalPath = "/api/" + strings.TrimLeft(originalPath, "/")
		if strings.Contains(originalPath, "..") {
			http.Error(w, "Invalid request path", http.StatusBadRequest)
			return
		}
		r.URL.Path = originalPath
	}
	handlerMu.Lock()
	if apiHandler == nil {
		h, err := backend.NewHandler()
		if err != nil {
			handlerMu.Unlock()
			w.Header().Set("Content-Type", "application/json; charset=utf-8")
			w.WriteHeader(http.StatusServiceUnavailable)
			_ = json.NewEncoder(w).Encode(map[string]string{
				"message": "API initialization failed. Verify DATABASE_URL, SESSION_SECRET, and Neon database connectivity in Vercel Environment Variables and Runtime Logs.",
				"code":    "API_INITIALIZATION_FAILED",
			})
			return
		}
		apiHandler = h
	}
	h := apiHandler
	handlerMu.Unlock()
	h.ServeHTTP(w, r)
}

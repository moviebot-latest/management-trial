package handler

import (
	"net/http"
	"strings"
	"sync"

	"management-system-go/backend"
)

var handlerOnce sync.Once
var apiHandler http.Handler
var initErr error

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
	handlerOnce.Do(func() { apiHandler, initErr = backend.NewHandler() })
	if initErr != nil {
		http.Error(w, "Service temporarily unavailable. Check server configuration and database connectivity.", http.StatusServiceUnavailable)
		return
	}
	apiHandler.ServeHTTP(w, r)
}

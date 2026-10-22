package main

import (
	"log"
	"net/http"
	"os"
	"time"

	"management-system-go/backend"
)

func main() {
	h, err := backend.NewHandler()
	if err != nil {
		log.Fatal(err)
	}
	port := os.Getenv("PORT")
	if port == "" {
		port = "8080"
	}
	s := &http.Server{Addr: ":" + port, Handler: h, ReadHeaderTimeout: 5 * time.Second, IdleTimeout: 60 * time.Second, WriteTimeout: 30 * time.Second}
	log.Printf("Library Management API listening on :%s", port)
	log.Fatal(s.ListenAndServe())
}

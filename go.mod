module management-system-go

go 1.23

require (
  management-system-go/backend v0.0.0
  github.com/jackc/pgx/v5 v5.7.2
  golang.org/x/crypto v0.31.0
)

replace management-system-go/backend => ./backend

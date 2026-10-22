FROM node:22-alpine AS frontend-build
WORKDIR /src/frontend
COPY frontend/package.json ./package.json
RUN npm install --no-audit --no-fund
COPY frontend/ ./
RUN npm run build

FROM golang:1.23-alpine AS backend-build
WORKDIR /src
COPY go.mod ./go.mod
COPY backend/go.mod ./backend/go.mod
RUN go mod download
COPY backend/ ./backend/
COPY cmd/ ./cmd/
RUN CGO_ENABLED=0 GOOS=linux go build -trimpath -ldflags="-s -w" -o /out/management-server ./cmd/server

FROM alpine:3.21
RUN apk add --no-cache ca-certificates tzdata
WORKDIR /app
COPY --from=backend-build /out/management-server ./management-server
COPY --from=frontend-build /src/frontend/dist ./frontend/dist
ENV PORT=10000
ENV FRONTEND_DIST=/app/frontend/dist
ENV APP_ENV=production
EXPOSE 10000
ENTRYPOINT ["/app/management-server"]

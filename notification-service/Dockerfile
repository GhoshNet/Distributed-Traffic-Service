# ── Build stage ──────────────────────────────────────────────
FROM golang:1.23-alpine AS builder

WORKDIR /app

COPY notification-service/go.mod ./
COPY notification-service/*.go ./
RUN go mod tidy && CGO_ENABLED=0 GOOS=linux go build -o notification-service .

# ── Runtime stage ─────────────────────────────────────────────
FROM alpine:3.20

RUN apk --no-cache add ca-certificates

WORKDIR /app
COPY --from=builder /app/notification-service .

EXPOSE 8000

CMD ["./notification-service"]

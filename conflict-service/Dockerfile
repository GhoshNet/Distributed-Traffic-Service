FROM golang:1.23-alpine AS builder

WORKDIR /app

COPY conflict-service/go.mod conflict-service/go.sum ./
RUN go mod download

COPY conflict-service/*.go ./
RUN CGO_ENABLED=0 GOOS=linux go build -o conflict-service .

FROM alpine:3.20

RUN apk add --no-cache ca-certificates tzdata

WORKDIR /app
COPY --from=builder /app/conflict-service .

EXPOSE 8000

CMD ["./conflict-service"]

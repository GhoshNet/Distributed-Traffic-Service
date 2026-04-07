FROM golang:1.23-alpine AS builder

WORKDIR /app

COPY analytics-service/go.mod ./
COPY analytics-service/*.go ./
RUN go mod tidy && CGO_ENABLED=0 GOOS=linux go build -o analytics-service .

FROM alpine:3.20

RUN apk add --no-cache ca-certificates tzdata

WORKDIR /app
COPY --from=builder /app/analytics-service .

EXPOSE 8000

CMD ["./analytics-service"]

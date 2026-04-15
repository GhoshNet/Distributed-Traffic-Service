package main

import (
	"os"
	"strings"
)

type Config struct {
	DatabaseURL      string
	RabbitMQURL      string
	JWTSecret        string
	ServiceName      string
	Port             string
	PeerConflictURLs []string
}

func loadConfig() Config {
	var peers []string
	if raw := os.Getenv("PEER_CONFLICT_URLS"); raw != "" {
		for _, u := range strings.Split(raw, ",") {
			if u = strings.TrimSpace(u); u != "" {
				peers = append(peers, u)
			}
		}
	}
	return Config{
		DatabaseURL:      getEnv("DATABASE_URL", "postgresql://conflicts_user:conflicts_pass@localhost:5435/conflicts_db"),
		RabbitMQURL:      getEnv("RABBITMQ_URL", "amqp://journey_admin:journey_pass@rabbitmq:5672/journey_vhost"),
		JWTSecret:        getEnv("JWT_SECRET", "super-secret-jwt-key-change-in-production"),
		ServiceName:      getEnv("SERVICE_NAME", "conflict-service"),
		Port:             getEnv("PORT", "8000"),
		PeerConflictURLs: peers,
	}
}

func getEnv(key, def string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return def
}

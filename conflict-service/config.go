package main

import (
	"os"
	"strings"
)

type Config struct {
	DatabaseURL  string
	RabbitMQURL  string
	RedisURL     string
	JWTSecret    string
	ServiceName  string
	Port         string
	RegionID     string
	RegionName   string
	OwnedRoutes  []string // parsed from REGION_OWNED_ROUTES env var (comma-separated)
}

func loadConfig() Config {
	ownedRoutesRaw := getEnv("REGION_OWNED_ROUTES", "")
	var ownedRoutes []string
	if ownedRoutesRaw != "" {
		for _, r := range strings.Split(ownedRoutesRaw, ",") {
			r = strings.TrimSpace(r)
			if r != "" {
				ownedRoutes = append(ownedRoutes, r)
			}
		}
	}

	return Config{
		DatabaseURL: getEnv("DATABASE_URL", "postgresql://conflicts_user:conflicts_pass@localhost:5435/conflicts_db"),
		RabbitMQURL: getEnv("RABBITMQ_URL", "amqp://journey_admin:journey_pass@rabbitmq:5672/journey_vhost"),
		RedisURL:    getEnv("REGISTRY_REDIS_URL", getEnv("REDIS_URL", "redis://redis:6379/0")),
		JWTSecret:   getEnv("JWT_SECRET", "super-secret-jwt-key-change-in-production"),
		ServiceName: getEnv("SERVICE_NAME", "conflict-service"),
		Port:        getEnv("PORT", "8000"),
		RegionID:    getEnv("REGION_ID", "IE"),
		RegionName:  getEnv("REGION_NAME", "Republic of Ireland"),
		OwnedRoutes: ownedRoutes,
	}
}

func getEnv(key, def string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return def
}

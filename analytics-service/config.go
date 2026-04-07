package main

import "os"

type Config struct {
	DatabaseURL string
	RabbitMQURL string
	RedisURL    string
	JWTSecret   string
	ServiceName string
	Port        string
}

func loadConfig() Config {
	return Config{
		DatabaseURL: getEnv("DATABASE_URL", "postgresql://analytics_user:analytics_pass@localhost:5432/analytics_db"),
		RabbitMQURL: getEnv("RABBITMQ_URL", "amqp://journey_admin:journey_pass@rabbitmq:5672/journey_vhost"),
		RedisURL:    getEnv("REDIS_URL", "redis://redis:6379/5"),
		JWTSecret:   getEnv("JWT_SECRET", "super-secret-jwt-key-change-in-production"),
		ServiceName: getEnv("SERVICE_NAME", "analytics-service"),
		Port:        getEnv("PORT", "8000"),
	}
}

func getEnv(key, def string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return def
}

package main

import "os"

type Config struct {
	DatabaseURL string
	RabbitMQURL string
	JWTSecret   string
	ServiceName string
	Port        string
}

func loadConfig() Config {
	return Config{
		DatabaseURL: getEnv("DATABASE_URL", "postgresql://conflicts_user:conflicts_pass@localhost:5435/conflicts_db"),
		RabbitMQURL: getEnv("RABBITMQ_URL", "amqp://journey_admin:journey_pass@rabbitmq:5672/journey_vhost"),
		JWTSecret:   getEnv("JWT_SECRET", "super-secret-jwt-key-change-in-production"),
		ServiceName: getEnv("SERVICE_NAME", "conflict-service"),
		Port:        getEnv("PORT", "8000"),
	}
}

func getEnv(key, def string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return def
}

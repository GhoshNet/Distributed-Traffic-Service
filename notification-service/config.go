package main

import "os"

type Config struct {
	RedisURL    string
	RabbitMQURL string
	JWTSecret   string
	ServiceName string
	Port        string
}

func loadConfig() Config {
	return Config{
		RedisURL:    getEnv("REDIS_URL", "redis://redis:6379/3"),
		RabbitMQURL: getEnv("RABBITMQ_URL", "amqp://journey_admin:journey_pass@rabbitmq:5672/journey_vhost"),
		JWTSecret:   getEnv("JWT_SECRET", "super-secret-jwt-key-change-in-production"),
		ServiceName: getEnv("SERVICE_NAME", "notification-service"),
		Port:        getEnv("PORT", "8000"),
	}
}

func getEnv(key, def string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return def
}

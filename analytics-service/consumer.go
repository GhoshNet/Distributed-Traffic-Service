package main

import (
	"context"
	"encoding/json"
	"fmt"
	"log"
	"time"

	amqp "github.com/rabbitmq/amqp091-go"
	"github.com/google/uuid"
	"github.com/redis/go-redis/v9"
)

const (
	eventsExchange = "journey_events"
	dlxExchange    = "journey_events_dlx"
	analyticsQueue = "analytics_events"
)

var redisClient *redis.Client

func initRedis(redisURL string) error {
	opts, err := redis.ParseURL(redisURL)
	if err != nil {
		return fmt.Errorf("invalid Redis URL: %w", err)
	}
	redisClient = redis.NewClient(opts)
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	return redisClient.Ping(ctx).Err()
}

func startConsumer(rabbitURL string) error {
	var conn *amqp.Connection
	var err error

	for attempt := 1; attempt <= 10; attempt++ {
		conn, err = amqp.Dial(rabbitURL)
		if err == nil {
			break
		}
		log.Printf("RabbitMQ connection attempt %d/10 failed: %v", attempt, err)
		if attempt < 10 {
			time.Sleep(3 * time.Second)
		}
	}
	if err != nil {
		return fmt.Errorf("failed to connect to RabbitMQ after 10 attempts: %w", err)
	}

	if err := setupAnalyticsChannel(conn); err != nil {
		conn.Close()
		return err
	}

	// Reconnect on connection drop
	go func() {
		errCh := conn.NotifyClose(make(chan *amqp.Error, 1))
		<-errCh
		log.Println("RabbitMQ connection lost, reconnecting...")
		for {
			time.Sleep(3 * time.Second)
			if err := startConsumer(rabbitURL); err != nil {
				log.Printf("Reconnect failed: %v", err)
			} else {
				log.Println("Reconnected to RabbitMQ")
				return
			}
		}
	}()

	return nil
}

func setupAnalyticsChannel(conn *amqp.Connection) error {
	ch, err := conn.Channel()
	if err != nil {
		return err
	}
	if err := ch.Qos(10, 0, false); err != nil {
		return err
	}

	// Dead-letter exchange
	if err := ch.ExchangeDeclare(dlxExchange, "fanout", true, false, false, false, nil); err != nil {
		return err
	}
	dlq, err := ch.QueueDeclare("dead_letter_queue", true, false, false, false, nil)
	if err != nil {
		return err
	}
	if err := ch.QueueBind(dlq.Name, "", dlxExchange, false, nil); err != nil {
		return err
	}

	// Main topic exchange
	if err := ch.ExchangeDeclare(eventsExchange, "topic", true, false, false, false, nil); err != nil {
		return err
	}

	q, err := ch.QueueDeclare(analyticsQueue, true, false, false, false, amqp.Table{
		"x-dead-letter-exchange": dlxExchange,
		"x-message-ttl":          int32(86400000), // 24h TTL
	})
	if err != nil {
		return err
	}

	// Subscribe to all journey.* and user.* events
	for _, key := range []string{"journey.*", "user.*"} {
		if err := ch.QueueBind(q.Name, key, eventsExchange, false, nil); err != nil {
			return err
		}
		log.Printf("Queue '%s' bound to routing key '%s'", q.Name, key)
	}

	msgs, err := ch.Consume(q.Name, "", false, false, false, false, nil)
	if err != nil {
		return err
	}

	log.Println("Analytics consumer started")
	go func() {
		for msg := range msgs {
			if err := handleEvent(msg.Body, msg.RoutingKey); err != nil {
				log.Printf("Error processing message: %v", err)
				msg.Nack(false, false)
			} else {
				msg.Ack(false)
			}
		}
		log.Println("RabbitMQ channel closed")
	}()

	return nil
}

func handleEvent(body []byte, routingKey string) error {
	log.Printf("Analytics received event: %s", routingKey)

	var data map[string]any
	if err := json.Unmarshal(body, &data); err != nil {
		return fmt.Errorf("unmarshal event: %w", err)
	}

	getString := func(key string) string {
		if v, ok := data[key].(string); ok {
			return v
		}
		return ""
	}

	metaJSON, _ := json.Marshal(data)

	if err := insertEvent(
		context.Background(),
		uuid.New().String(),
		routingKey,
		getString("journey_id"),
		getString("user_id"),
		getString("origin"),
		getString("destination"),
		string(metaJSON),
	); err != nil {
		log.Printf("Failed to insert event to DB: %v", err)
	}

	// Update Redis daily counters (best-effort)
	if redisClient != nil {
		today := time.Now().UTC().Format("2006-01-02")
		key := "analytics:daily:" + today
		ctx := context.Background()
		pipe := redisClient.Pipeline()
		pipe.HIncrBy(ctx, key, "total_events", 1)
		pipe.HIncrBy(ctx, key, routingKey, 1)
		pipe.Expire(ctx, key, 48*time.Hour)
		if _, err := pipe.Exec(ctx); err != nil {
			log.Printf("Failed to update Redis counters: %v", err)
		}
	}

	return nil
}

// GetSystemStats returns a mix of real-time Redis counters and DB aggregates.
func GetSystemStats(ctx context.Context) map[string]any {
	stats := map[string]any{
		"total_events_today": 0,
		"confirmed_today":    0,
		"rejected_today":     0,
		"cancelled_today":    0,
		"total_events_all_time": 0,
		"events_last_hour":   0,
	}

	if redisClient != nil {
		today := time.Now().UTC().Format("2006-01-02")
		key := "analytics:daily:" + today
		data, err := redisClient.HGetAll(ctx, key).Result()
		if err == nil {
			stats["total_events_today"] = atoi(data["total_events"])
			stats["confirmed_today"]    = atoi(data["journey.confirmed"])
			stats["rejected_today"]     = atoi(data["journey.rejected"])
			stats["cancelled_today"]    = atoi(data["journey.cancelled"])
		}
	}

	if total, err := getTotalEvents(ctx); err == nil {
		stats["total_events_all_time"] = total
	}
	if lastHour, err := getEventsLastHour(ctx); err == nil {
		stats["events_last_hour"] = lastHour
	}

	return stats
}

func atoi(s string) int64 {
	var n int64
	fmt.Sscanf(s, "%d", &n)
	return n
}

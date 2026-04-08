package main

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"log"
	"strings"
	"sync"
	"time"

	amqp "github.com/rabbitmq/amqp091-go"

	"github.com/gorilla/websocket"
)

const (
	eventsExchange    = "journey_events"
	dlxExchange       = "journey_events_dlx"
	notificationQueue = "notification_events"
)

var routingKeys = []string{
	"journey.confirmed",
	"journey.rejected",
	"journey.cancelled",
	"journey.started",
	"journey.completed",
}

type eventTemplate struct {
	title    string
	template string
}

var eventMessages = map[string]eventTemplate{
	"journey.confirmed": {
		title:    "Journey Confirmed \u2705",
		template: "Your journey from {origin} to {destination} at {departure_time} has been confirmed.",
	},
	"journey.rejected": {
		title:    "Journey Rejected \u274c",
		template: "Your journey from {origin} to {destination} was rejected. Reason: {rejection_reason}",
	},
	"journey.cancelled": {
		title:    "Journey Cancelled \U0001f6ab",
		template: "Your journey from {origin} to {destination} at {departure_time} has been cancelled.",
	},
	"journey.started": {
		title:    "Journey Started \U0001f697",
		template: "Your journey from {origin} to {destination} has started. Drive safely!",
	},
	"journey.completed": {
		title:    "Journey Completed \U0001f3c1",
		template: "Your journey from {origin} to {destination} is complete.",
	},
}

// WebSocket registry — keyed by user_id.
var (
	wsMu    sync.RWMutex
	wsConns = make(map[string][]*websocket.Conn)
)

func registerWS(userID string, conn *websocket.Conn) {
	wsMu.Lock()
	defer wsMu.Unlock()
	wsConns[userID] = append(wsConns[userID], conn)
	log.Printf("WebSocket registered for user %s (total: %d)", userID, len(wsConns[userID]))
}

func unregisterWS(userID string, conn *websocket.Conn) {
	wsMu.Lock()
	defer wsMu.Unlock()
	conns := wsConns[userID]
	filtered := conns[:0]
	for _, c := range conns {
		if c != conn {
			filtered = append(filtered, c)
		}
	}
	if len(filtered) == 0 {
		delete(wsConns, userID)
	} else {
		wsConns[userID] = filtered
	}
}

func pushToWS(userID string, notification map[string]interface{}) {
	wsMu.Lock()
	defer wsMu.Unlock()

	conns := wsConns[userID]
	if len(conns) == 0 {
		return
	}

	data, err := json.Marshal(notification)
	if err != nil {
		return
	}

	var dead []*websocket.Conn
	for _, c := range conns {
		if err := c.WriteMessage(websocket.TextMessage, data); err != nil {
			dead = append(dead, c)
		}
	}

	if len(dead) > 0 {
		deadSet := make(map[*websocket.Conn]bool, len(dead))
		for _, d := range dead {
			deadSet[d] = true
		}
		filtered := conns[:0]
		for _, c := range conns {
			if !deadSet[c] {
				filtered = append(filtered, c)
			}
		}
		if len(filtered) == 0 {
			delete(wsConns, userID)
		} else {
			wsConns[userID] = filtered
		}
	}
}

func strField(m map[string]interface{}, key, def string) string {
	if v, ok := m[key].(string); ok && v != "" {
		return v
	}
	return def
}

func notifDedupeKey(msg amqp.Delivery) string {
	if msg.MessageId != "" {
		return "notif:processed:" + msg.MessageId
	}
	h := sha256.Sum256(msg.Body)
	return "notif:processed:" + hex.EncodeToString(h[:])
}

func notifIsDuplicate(msg amqp.Delivery) bool {
	if rdb == nil {
		return false
	}
	key := notifDedupeKey(msg)
	exists, err := rdb.Exists(context.Background(), key).Result()
	if err != nil {
		log.Printf("Redis dedup check error: %v", err)
		return false
	}
	return exists > 0
}

func notifMarkProcessed(msg amqp.Delivery) {
	if rdb == nil {
		return
	}
	key := notifDedupeKey(msg)
	if err := rdb.Set(context.Background(), key, 1, 24*time.Hour).Err(); err != nil {
		log.Printf("Redis dedup mark error: %v", err)
	}
}

func handleEvent(body []byte, routingKey string) error {
	var data map[string]interface{}
	if err := json.Unmarshal(body, &data); err != nil {
		return err
	}

	userID := strField(data, "user_id", "")
	if userID == "" {
		log.Printf("Event %s has no user_id, skipping", routingKey)
		return nil
	}

	tmpl, ok := eventMessages[routingKey]
	if !ok {
		log.Printf("No notification template for %s", routingKey)
		return nil
	}

	msg := tmpl.template
	msg = strings.ReplaceAll(msg, "{origin}", strField(data, "origin", "Unknown"))
	msg = strings.ReplaceAll(msg, "{destination}", strField(data, "destination", "Unknown"))
	msg = strings.ReplaceAll(msg, "{departure_time}", strField(data, "departure_time", "Unknown"))
	msg = strings.ReplaceAll(msg, "{rejection_reason}", strField(data, "rejection_reason", "N/A"))

	notification := map[string]interface{}{
		"event_type": routingKey,
		"title":      tmpl.title,
		"message":    msg,
		"journey_id": data["journey_id"],
		"timestamp":  time.Now().UTC().Format(time.RFC3339),
	}

	log.Printf("📧 NOTIFICATION for user %s: %s — %s", userID, tmpl.title, msg)
	storeNotification(userID, notification)
	pushToWS(userID, notification)
	return nil
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

	if err := setupChannel(conn); err != nil {
		conn.Close()
		return err
	}

	// Auto-reconnect on connection loss.
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

func setupChannel(conn *amqp.Connection) error {
	ch, err := conn.Channel()
	if err != nil {
		return err
	}
	if err := ch.Qos(10, 0, false); err != nil {
		return err
	}

	// Dead-letter exchange + queue.
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

	// Main events exchange.
	if err := ch.ExchangeDeclare(eventsExchange, "topic", true, false, false, false, nil); err != nil {
		return err
	}

	// Notification queue with DLX and 24h message TTL.
	q, err := ch.QueueDeclare(notificationQueue, true, false, false, false, amqp.Table{
		"x-dead-letter-exchange": dlxExchange,
		"x-message-ttl":          int32(86400000),
	})
	if err != nil {
		return err
	}

	for _, key := range routingKeys {
		if err := ch.QueueBind(q.Name, key, eventsExchange, false, nil); err != nil {
			return err
		}
		log.Printf("Queue '%s' bound to routing_key '%s'", q.Name, key)
	}

	msgs, err := ch.Consume(q.Name, "", false, false, false, false, nil)
	if err != nil {
		return err
	}

	log.Println("Notification consumer started")
	go func() {
		for msg := range msgs {
			if notifIsDuplicate(msg) {
				log.Printf("Skipping duplicate notification event (msgID=%s)", msg.MessageId)
				msg.Ack(false)
				continue
			}
			if err := handleEvent(msg.Body, msg.RoutingKey); err != nil {
				log.Printf("Error processing message: %v", err)
				msg.Nack(false, false) // route to DLQ
			} else {
				notifMarkProcessed(msg)
				msg.Ack(false)
			}
		}
		log.Println("RabbitMQ channel closed")
	}()

	return nil
}

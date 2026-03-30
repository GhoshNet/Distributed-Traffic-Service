package main

import (
	"context"
	"encoding/json"
	"fmt"
	"log"
	"time"

	amqp "github.com/rabbitmq/amqp091-go"
)

const (
	eventsExchange  = "journey_events"
	dlxExchange     = "journey_events_dlx"
	conflictQueue   = "conflict_cancellation_events"
	cancelRoutingKey = "journey.cancelled"
)

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

	// Main events exchange
	if err := ch.ExchangeDeclare(eventsExchange, "topic", true, false, false, false, nil); err != nil {
		return err
	}

	q, err := ch.QueueDeclare(conflictQueue, true, false, false, false, amqp.Table{
		"x-dead-letter-exchange": dlxExchange,
		"x-message-ttl":          int32(86400000),
	})
	if err != nil {
		return err
	}

	if err := ch.QueueBind(q.Name, cancelRoutingKey, eventsExchange, false, nil); err != nil {
		return err
	}
	log.Printf("Queue '%s' bound to routing_key '%s'", q.Name, cancelRoutingKey)

	msgs, err := ch.Consume(q.Name, "", false, false, false, false, nil)
	if err != nil {
		return err
	}

	log.Println("Conflict service consumer started")
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
	log.Printf("Received event: %s", routingKey)

	if routingKey != cancelRoutingKey {
		return nil
	}

	var data map[string]interface{}
	if err := json.Unmarshal(body, &data); err != nil {
		return err
	}

	journeyID, _ := data["journey_id"].(string)
	if journeyID == "" {
		log.Printf("journey.cancelled event missing journey_id")
		return nil
	}

	if err := cancelBookingSlot(context.Background(), journeyID); err != nil {
		return fmt.Errorf("cancel booking slot %s: %w", journeyID, err)
	}
	log.Printf("Processed cancellation for journey %s", journeyID)
	return nil
}

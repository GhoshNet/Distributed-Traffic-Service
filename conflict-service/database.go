package main

import (
	"context"
	"fmt"
	"log"
	"time"

	"github.com/jackc/pgx/v5/pgxpool"
)

var db *pgxpool.Pool

func initDB(databaseURL string) error {
	var err error
	for attempt := 1; attempt <= 10; attempt++ {
		db, err = pgxpool.New(context.Background(), databaseURL)
		if err == nil {
			if pingErr := db.Ping(context.Background()); pingErr == nil {
				break
			} else {
				err = pingErr
			}
		}
		log.Printf("DB connection attempt %d/10 failed: %v", attempt, err)
		if attempt < 10 {
			time.Sleep(3 * time.Second)
		}
	}
	if err != nil {
		return fmt.Errorf("failed to connect to database after 10 attempts: %w", err)
	}

	return createTables()
}

func createTables() error {
	_, err := db.Exec(context.Background(), `
		CREATE TABLE IF NOT EXISTS booked_slots (
			id                   VARCHAR(36) PRIMARY KEY,
			journey_id           VARCHAR(36) NOT NULL,
			user_id              VARCHAR(36) NOT NULL,
			vehicle_registration VARCHAR(20) NOT NULL,
			departure_time       TIMESTAMP   NOT NULL,
			arrival_time         TIMESTAMP   NOT NULL,
			origin_lat           DOUBLE PRECISION NOT NULL,
			origin_lng           DOUBLE PRECISION NOT NULL,
			destination_lat      DOUBLE PRECISION NOT NULL,
			destination_lng      DOUBLE PRECISION NOT NULL,
			is_active            BOOLEAN DEFAULT TRUE,
			created_at           TIMESTAMP DEFAULT NOW()
		);

		CREATE INDEX IF NOT EXISTS idx_slot_user_time
			ON booked_slots (user_id, departure_time, arrival_time);
		CREATE INDEX IF NOT EXISTS idx_slot_vehicle_time
			ON booked_slots (vehicle_registration, departure_time, arrival_time);
		CREATE INDEX IF NOT EXISTS idx_slot_journey
			ON booked_slots (journey_id);

		CREATE TABLE IF NOT EXISTS road_segment_capacity (
			id               VARCHAR(36) PRIMARY KEY,
			grid_lat         DOUBLE PRECISION NOT NULL,
			grid_lng         DOUBLE PRECISION NOT NULL,
			time_slot_start  TIMESTAMP NOT NULL,
			time_slot_end    TIMESTAMP NOT NULL,
			current_bookings INT DEFAULT 0,
			max_capacity     INT DEFAULT 100
		);

		CREATE UNIQUE INDEX IF NOT EXISTS idx_grid_time_unique
			ON road_segment_capacity (grid_lat, grid_lng, time_slot_start);
	`)
	return err
}

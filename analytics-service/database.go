package main

import (
	"context"
	"fmt"
	"log"
	"time"

	"github.com/google/uuid"
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
	// Two-step: create tables, then idempotently ensure unique constraint on hourly_stats.hour
	_, err := db.Exec(context.Background(), `
		CREATE TABLE IF NOT EXISTS event_logs (
			id            VARCHAR(36) PRIMARY KEY,
			event_type    VARCHAR(50)  NOT NULL,
			journey_id    VARCHAR(36),
			user_id       VARCHAR(36),
			origin        VARCHAR(500),
			destination   VARCHAR(500),
			region        VARCHAR(100),
			metadata_json TEXT,
			created_at    TIMESTAMP NOT NULL DEFAULT NOW()
		);

		CREATE INDEX IF NOT EXISTS idx_event_type    ON event_logs (event_type);
		CREATE INDEX IF NOT EXISTS idx_event_journey ON event_logs (journey_id);
		CREATE INDEX IF NOT EXISTS idx_event_user    ON event_logs (user_id);
		CREATE INDEX IF NOT EXISTS idx_event_created ON event_logs (created_at);
		CREATE INDEX IF NOT EXISTS idx_event_type_date ON event_logs (event_type, created_at);

		CREATE TABLE IF NOT EXISTS hourly_stats (
			id                   VARCHAR(36) PRIMARY KEY,
			hour                 TIMESTAMP NOT NULL,
			total_bookings       INT DEFAULT 0,
			confirmed            INT DEFAULT 0,
			rejected             INT DEFAULT 0,
			cancelled            INT DEFAULT 0,
			avg_duration_minutes DOUBLE PRECISION,
			region               VARCHAR(100)
		);

		CREATE INDEX IF NOT EXISTS idx_hourly_hour   ON hourly_stats (hour);
		CREATE INDEX IF NOT EXISTS idx_hourly_region ON hourly_stats (hour, region);
	`)
	if err != nil {
		return err
	}
	// Add unique constraint on hour if it doesn't exist (handles pre-existing tables)
	_, err = db.Exec(context.Background(), `
		CREATE UNIQUE INDEX IF NOT EXISTS idx_hourly_hour_unique ON hourly_stats (hour);
	`)
	return err
}

// EventLog mirrors the event_logs DB row used by query results.
type EventLog struct {
	ID          string
	EventType   string
	JourneyID   string
	UserID      string
	Origin      string
	Destination string
	CreatedAt   time.Time
}

func insertEvent(ctx context.Context, id, eventType, journeyID, userID, origin, destination, metadataJSON string) error {
	_, err := db.Exec(ctx, `
		INSERT INTO event_logs (id, event_type, journey_id, user_id, origin, destination, metadata_json)
		VALUES ($1, $2, $3, $4, $5, $6, $7)
	`, id, eventType, journeyID, userID, origin, destination, metadataJSON)
	return err
}

func getTotalEvents(ctx context.Context) (int64, error) {
	var count int64
	err := db.QueryRow(ctx, "SELECT COUNT(*) FROM event_logs").Scan(&count)
	return count, err
}

func getEventsLastHour(ctx context.Context) (int64, error) {
	var count int64
	oneHourAgo := time.Now().UTC().Add(-time.Hour)
	err := db.QueryRow(ctx,
		"SELECT COUNT(*) FROM event_logs WHERE created_at >= $1", oneHourAgo,
	).Scan(&count)
	return count, err
}

// runHourlyRollup aggregates event_logs into hourly_stats once per hour.
// It fills in the previous completed hour on each tick so no event is missed.
func runHourlyRollup() {
	// Run immediately on startup to catch any missed hours, then every hour.
	rollupHour(time.Now().UTC().Truncate(time.Hour).Add(-time.Hour))

	ticker := time.NewTicker(time.Hour)
	defer ticker.Stop()
	for t := range ticker.C {
		hour := t.UTC().Truncate(time.Hour).Add(-time.Hour)
		rollupHour(hour)
	}
}

func rollupHour(hour time.Time) {
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()

	hourEnd := hour.Add(time.Hour)

	var total, confirmed, rejected, cancelled int
	err := db.QueryRow(ctx, `
		SELECT
			COUNT(*),
			COUNT(*) FILTER (WHERE event_type = 'journey.confirmed'),
			COUNT(*) FILTER (WHERE event_type = 'journey.rejected'),
			COUNT(*) FILTER (WHERE event_type = 'journey.cancelled')
		FROM event_logs
		WHERE created_at >= $1 AND created_at < $2
	`, hour, hourEnd).Scan(&total, &confirmed, &rejected, &cancelled)
	if err != nil {
		log.Printf("Hourly rollup query failed for %s: %v", hour.Format(time.RFC3339), err)
		return
	}
	if total == 0 {
		return // nothing to record
	}

	_, err = db.Exec(ctx, `
		INSERT INTO hourly_stats (id, hour, total_bookings, confirmed, rejected, cancelled)
		VALUES ($1, $2, $3, $4, $5, $6)
		ON CONFLICT (hour) DO UPDATE SET
			total_bookings = EXCLUDED.total_bookings,
			confirmed      = EXCLUDED.confirmed,
			rejected       = EXCLUDED.rejected,
			cancelled      = EXCLUDED.cancelled
	`, uuid.New().String(), hour, total, confirmed, rejected, cancelled)
	if err != nil {
		log.Printf("Hourly rollup insert failed for %s: %v", hour.Format(time.RFC3339), err)
		return
	}
	log.Printf("Hourly rollup: %s — total=%d confirmed=%d rejected=%d cancelled=%d",
		hour.Format("2006-01-02 15:00"), total, confirmed, rejected, cancelled)
}

func queryEventHistory(ctx context.Context, eventType string, limit, offset int) ([]EventLog, error) {
	var (
		query string
		args  []any
	)
	if eventType != "" {
		query = `SELECT id, event_type,
		                COALESCE(journey_id, ''), COALESCE(user_id, ''),
		                COALESCE(origin, ''), COALESCE(destination, ''),
		                created_at
		         FROM event_logs WHERE event_type = $1
		         ORDER BY created_at DESC LIMIT $2 OFFSET $3`
		args = []any{eventType, limit, offset}
	} else {
		query = `SELECT id, event_type,
		                COALESCE(journey_id, ''), COALESCE(user_id, ''),
		                COALESCE(origin, ''), COALESCE(destination, ''),
		                created_at
		         FROM event_logs ORDER BY created_at DESC LIMIT $1 OFFSET $2`
		args = []any{limit, offset}
	}

	rows, err := db.Query(ctx, query, args...)
	if err != nil {
		return nil, err
	}
	defer rows.Close()

	var events []EventLog
	for rows.Next() {
		var e EventLog
		if err := rows.Scan(&e.ID, &e.EventType, &e.JourneyID, &e.UserID, &e.Origin, &e.Destination, &e.CreatedAt); err != nil {
			return nil, err
		}
		events = append(events, e)
	}
	return events, rows.Err()
}

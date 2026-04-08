package main

import (
	"context"
	"fmt"
	"log"
	"time"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"
)

var db *pgxpool.Pool

// ownedRoutes is set from config on startup. If non-empty, only routes in this
// map are seeded into the predefined_routes table.
var ownedRoutes map[string]bool

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

	if err := createTables(); err != nil {
		return err
	}
	return seedRoutes(context.Background())
}

func createTables() error {
	_, err := db.Exec(context.Background(), `
		-- Predefined road routes with real waypoints (Irish national roads)
		CREATE TABLE IF NOT EXISTS predefined_routes (
			route_id                   VARCHAR(50) PRIMARY KEY,
			name                       VARCHAR(200) NOT NULL,
			description                TEXT,
			origin_name                VARCHAR(100) NOT NULL,
			destination_name           VARCHAR(100) NOT NULL,
			origin_lat                 DOUBLE PRECISION NOT NULL,
			origin_lng                 DOUBLE PRECISION NOT NULL,
			destination_lat            DOUBLE PRECISION NOT NULL,
			destination_lng            DOUBLE PRECISION NOT NULL,
			estimated_duration_minutes INT NOT NULL
		);

		-- Ordered waypoints along each predefined route (actual road path, not straight line)
		CREATE TABLE IF NOT EXISTS route_waypoints (
			id             VARCHAR(36) PRIMARY KEY,
			route_id       VARCHAR(50) NOT NULL REFERENCES predefined_routes(route_id),
			sequence_order INT NOT NULL,
			lat            DOUBLE PRECISION NOT NULL,
			lng            DOUBLE PRECISION NOT NULL,
			location_name  VARCHAR(100)
		);

		CREATE INDEX IF NOT EXISTS idx_route_waypoints ON route_waypoints (route_id, sequence_order);

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
	if err != nil {
		return err
	}
	// Add route_id to booked_slots for correct path resolution during cancellation.
	// ALTER TABLE … ADD COLUMN IF NOT EXISTS is idempotent on Postgres 9.6+.
	_, err = db.Exec(context.Background(), `
		ALTER TABLE booked_slots ADD COLUMN IF NOT EXISTS route_id VARCHAR(50);
	`)
	if err != nil {
		return fmt.Errorf("alter booked_slots add route_id: %w", err)
	}
	_, err = db.Exec(context.Background(), `

		-- Distributed saga hold table (Phase 1 of 2-phase cross-region booking)
		CREATE TABLE IF NOT EXISTS held_bookings (
			hold_id              VARCHAR(36) PRIMARY KEY,
			journey_id           VARCHAR(36) NOT NULL,
			route_id             VARCHAR(50),
			user_id              VARCHAR(36) NOT NULL,
			vehicle_registration VARCHAR(20) NOT NULL,
			departure_time       TIMESTAMP NOT NULL,
			arrival_time         TIMESTAMP NOT NULL,
			origin_lat           DOUBLE PRECISION NOT NULL,
			origin_lng           DOUBLE PRECISION NOT NULL,
			destination_lat      DOUBLE PRECISION NOT NULL,
			destination_lng      DOUBLE PRECISION NOT NULL,
			status               VARCHAR(20) NOT NULL DEFAULT 'HELD',
			created_at           TIMESTAMP NOT NULL DEFAULT NOW(),
			expires_at           TIMESTAMP NOT NULL
		);
		CREATE INDEX IF NOT EXISTS idx_held_journey ON held_bookings (journey_id);
		CREATE INDEX IF NOT EXISTS idx_held_expires ON held_bookings (expires_at);
		CREATE INDEX IF NOT EXISTS idx_held_status  ON held_bookings (status);
	`)
	return err
}

// ─── Predefined routes ───────────────────────────────────────────────────────

// RouteInfo is returned by the GET /api/routes endpoint.
type RouteInfo struct {
	RouteID                  string     `json:"route_id"`
	Name                     string     `json:"name"`
	Description              string     `json:"description"`
	OriginName               string     `json:"origin_name"`
	DestinationName          string     `json:"destination_name"`
	OriginLat                float64    `json:"origin_lat"`
	OriginLng                float64    `json:"origin_lng"`
	DestinationLat           float64    `json:"destination_lat"`
	DestinationLng           float64    `json:"destination_lng"`
	EstimatedDurationMinutes int        `json:"estimated_duration_minutes"`
	Waypoints                []Waypoint `json:"waypoints"`
}

// Waypoint is a named lat/lng point along a predefined route.
type Waypoint struct {
	Lat  float64 `json:"lat"`
	Lng  float64 `json:"lng"`
	Name string  `json:"name"`
}

// seedRoutes inserts the predefined Irish road routes on first startup.
// ON CONFLICT DO NOTHING makes it idempotent — safe to call on every boot.
func seedRoutes(ctx context.Context) error {
	routes := []struct {
		id, name, desc, origin, dest string
		oLat, oLng, dLat, dLng      float64
		duration                     int
		waypoints                    []Waypoint
	}{
		{
			id: "dublin-galway", name: "Dublin → Galway (M6)",
			desc:   "M4/M6 motorway via Athlone — Ireland's main east-west corridor",
			origin: "Dublin", dest: "Galway",
			oLat: 53.3498, oLng: -6.2603, dLat: 53.2707, dLng: -9.0568,
			duration: 135,
			waypoints: []Waypoint{
				{53.3498, -6.2603, "Dublin"},
				{53.3636, -6.4867, "Leixlip (M4 junction)"},
				{53.4608, -7.1006, "Kinnegad"},
				{53.4239, -7.9407, "Athlone"},
				{53.3308, -8.2222, "Ballinasloe"},
				{53.2707, -9.0568, "Galway"},
			},
		},
		{
			id: "dublin-cork", name: "Dublin → Cork (M7/M8)",
			desc:   "M7 to Portlaoise, M8 south through Cashel to Cork",
			origin: "Dublin", dest: "Cork",
			oLat: 53.3498, oLng: -6.2603, dLat: 51.8985, dLng: -8.4756,
			duration: 150,
			waypoints: []Waypoint{
				{53.3498, -6.2603, "Dublin"},
				{53.1816, -6.7954, "Newbridge"},
				{53.0319, -7.2990, "Portlaoise"},
				{52.5159, -7.8879, "Cashel"},
				{51.8985, -8.4756, "Cork"},
			},
		},
		{
			id: "dublin-belfast", name: "Dublin → Belfast (M1/A1)",
			desc:   "M1 north through Drogheda and Dundalk, crossing the border at Newry",
			origin: "Dublin", dest: "Belfast",
			oLat: 53.3498, oLng: -6.2603, dLat: 54.5973, dLng: -5.9301,
			duration: 120,
			waypoints: []Waypoint{
				{53.3498, -6.2603, "Dublin"},
				{53.7179, -6.3569, "Drogheda"},
				{54.0011, -6.4011, "Dundalk"},
				{54.1751, -6.3394, "Newry (border crossing)"},
				{54.5973, -5.9301, "Belfast"},
			},
		},
		{
			id: "galway-limerick", name: "Galway → Limerick (N18)",
			desc:   "N18 south through Gort and Ennis into Limerick",
			origin: "Galway", dest: "Limerick",
			oLat: 53.2707, oLng: -9.0568, dLat: 52.6638, dLng: -8.6267,
			duration: 60,
			waypoints: []Waypoint{
				{53.2707, -9.0568, "Galway"},
				{53.0641, -8.8224, "Gort"},
				{52.8436, -8.9865, "Ennis"},
				{52.6638, -8.6267, "Limerick"},
			},
		},
		{
			id: "limerick-cork", name: "Limerick → Cork (M20)",
			desc:   "M20 south through Charleville and Mallow into Cork",
			origin: "Limerick", dest: "Cork",
			oLat: 52.6638, oLng: -8.6267, dLat: 51.8985, dLng: -8.4756,
			duration: 75,
			waypoints: []Waypoint{
				{52.6638, -8.6267, "Limerick"},
				{52.3567, -8.6817, "Charleville"},
				{52.1393, -8.6508, "Mallow"},
				{51.8985, -8.4756, "Cork"},
			},
		},
		{
			id: "dublin-limerick", name: "Dublin → Limerick (M7)",
			desc:   "M7 southwest through Newbridge and Portlaoise into Limerick",
			origin: "Dublin", dest: "Limerick",
			oLat: 53.3498, oLng: -6.2603, dLat: 52.6638, dLng: -8.6267,
			duration: 120,
			waypoints: []Waypoint{
				{53.3498, -6.2603, "Dublin"},
				{53.1816, -6.7954, "Newbridge"},
				{53.0319, -7.2990, "Portlaoise"},
				{52.8633, -8.1984, "Nenagh"},
				{52.6638, -8.6267, "Limerick"},
			},
		},
	}

	seeded := 0
	for _, r := range routes {
		// If ownedRoutes is configured (non-empty), only seed routes this region owns.
		if len(ownedRoutes) > 0 && !ownedRoutes[r.id] {
			log.Printf("Skipping route %s (not owned by this region)", r.id)
			continue
		}

		_, err := db.Exec(ctx, `
			INSERT INTO predefined_routes
				(route_id, name, description, origin_name, destination_name,
				 origin_lat, origin_lng, destination_lat, destination_lng,
				 estimated_duration_minutes)
			VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)
			ON CONFLICT (route_id) DO NOTHING
		`, r.id, r.name, r.desc, r.origin, r.dest,
			r.oLat, r.oLng, r.dLat, r.dLng, r.duration)
		if err != nil {
			return fmt.Errorf("seed route %s: %w", r.id, err)
		}

		for i, wp := range r.waypoints {
			// Deterministic ID so ON CONFLICT (id) DO NOTHING is idempotent.
			wpID := fmt.Sprintf("%s-%d", r.id, i)
			_, err := db.Exec(ctx, `
				INSERT INTO route_waypoints
					(id, route_id, sequence_order, lat, lng, location_name)
				VALUES ($1, $2, $3, $4, $5, $6)
				ON CONFLICT (id) DO NOTHING
			`, wpID, r.id, i, wp.Lat, wp.Lng, wp.Name)
			if err != nil {
				return fmt.Errorf("seed waypoint %s[%d]: %w", r.id, i, err)
			}
		}
		seeded++
	}
	log.Printf("Predefined routes seeded (%d routes)", seeded)
	return nil
}

// loadRouteWaypoints returns ordered waypoints for a route_id, or nil if not found.
func loadRouteWaypoints(ctx context.Context, routeID string) ([]Waypoint, error) {
	rows, err := db.Query(ctx, `
		SELECT lat, lng, location_name
		FROM route_waypoints
		WHERE route_id = $1
		ORDER BY sequence_order ASC
	`, routeID)
	if err != nil {
		return nil, err
	}
	defer rows.Close()

	var wps []Waypoint
	for rows.Next() {
		var wp Waypoint
		if err := rows.Scan(&wp.Lat, &wp.Lng, &wp.Name); err != nil {
			return nil, err
		}
		wps = append(wps, wp)
	}
	return wps, rows.Err()
}

// listAllRoutes returns all predefined routes with their waypoints.
func listAllRoutes(ctx context.Context) ([]RouteInfo, error) {
	rows, err := db.Query(ctx, `
		SELECT route_id, name, description, origin_name, destination_name,
		       origin_lat, origin_lng, destination_lat, destination_lng,
		       estimated_duration_minutes
		FROM predefined_routes
		ORDER BY name ASC
	`)
	if err != nil {
		return nil, err
	}
	defer rows.Close()

	var routes []RouteInfo
	for rows.Next() {
		var ri RouteInfo
		if err := rows.Scan(
			&ri.RouteID, &ri.Name, &ri.Description,
			&ri.OriginName, &ri.DestinationName,
			&ri.OriginLat, &ri.OriginLng,
			&ri.DestinationLat, &ri.DestinationLng,
			&ri.EstimatedDurationMinutes,
		); err != nil {
			return nil, err
		}
		wps, err := loadRouteWaypoints(ctx, ri.RouteID)
		if err != nil {
			return nil, err
		}
		ri.Waypoints = wps
		routes = append(routes, ri)
	}
	if routes == nil {
		routes = []RouteInfo{}
	}
	return routes, rows.Err()
}

// ─── Hold / Commit / Rollback ────────────────────────────────────────────────

// HoldRecord represents a row in the held_bookings table.
type HoldRecord struct {
	HoldID              string
	JourneyID           string
	RouteID             string
	UserID              string
	VehicleRegistration string
	DepartureTime       time.Time
	ArrivalTime         time.Time
	OriginLat           float64
	OriginLng           float64
	DestinationLat      float64
	DestinationLng      float64
	Status              string
	ExpiresAt           time.Time
}

// createHoldInTx inserts a new held_booking record within an existing transaction.
// Road capacity IS incremented (via recordBookingSlot logic) so concurrent holds
// on the same cells are rejected — the hold acts as a distributed lock.
func createHoldInTx(ctx context.Context, tx pgx.Tx, req ConflictCheckRequest, arrivalTime time.Time, holdID string, expiresAt time.Time) error {
	_, err := tx.Exec(ctx, `
		INSERT INTO held_bookings
			(hold_id, journey_id, route_id, user_id, vehicle_registration,
			 departure_time, arrival_time,
			 origin_lat, origin_lng, destination_lat, destination_lng,
			 status, expires_at)
		VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,'HELD',$12)
	`,
		holdID,
		req.JourneyID,
		req.RouteID,
		req.UserID,
		req.VehicleRegistration,
		req.DepartureTime.Time,
		arrivalTime,
		req.OriginLat, req.OriginLng,
		req.DestinationLat, req.DestinationLng,
		expiresAt,
	)
	return err
}

// getHold retrieves a hold record by hold ID.
func getHold(ctx context.Context, holdID string) (*HoldRecord, error) {
	var h HoldRecord
	err := db.QueryRow(ctx, `
		SELECT hold_id, journey_id, COALESCE(route_id,''), user_id, vehicle_registration,
		       departure_time, arrival_time,
		       origin_lat, origin_lng, destination_lat, destination_lng,
		       status, expires_at
		FROM held_bookings
		WHERE hold_id = $1
	`, holdID).Scan(
		&h.HoldID, &h.JourneyID, &h.RouteID, &h.UserID, &h.VehicleRegistration,
		&h.DepartureTime, &h.ArrivalTime,
		&h.OriginLat, &h.OriginLng, &h.DestinationLat, &h.DestinationLng,
		&h.Status, &h.ExpiresAt,
	)
	if err != nil {
		if isNoRows(err) {
			return nil, nil
		}
		return nil, err
	}
	return &h, nil
}

// commitHold promotes a HELD record to COMMITTED and inserts into booked_slots.
// The road capacity was already incremented during the hold, so no further
// capacity update is needed.
func commitHold(ctx context.Context, holdID string) error {
	tx, err := db.BeginTx(ctx, pgx.TxOptions{})
	if err != nil {
		return err
	}
	defer tx.Rollback(ctx)

	var h HoldRecord
	err = tx.QueryRow(ctx, `
		SELECT hold_id, journey_id, COALESCE(route_id,''), user_id, vehicle_registration,
		       departure_time, arrival_time,
		       origin_lat, origin_lng, destination_lat, destination_lng,
		       status, expires_at
		FROM held_bookings
		WHERE hold_id = $1
		FOR UPDATE
	`, holdID).Scan(
		&h.HoldID, &h.JourneyID, &h.RouteID, &h.UserID, &h.VehicleRegistration,
		&h.DepartureTime, &h.ArrivalTime,
		&h.OriginLat, &h.OriginLng, &h.DestinationLat, &h.DestinationLng,
		&h.Status, &h.ExpiresAt,
	)
	if err != nil {
		if isNoRows(err) {
			return ErrNotFound
		}
		return err
	}

	if h.Status != "HELD" {
		return fmt.Errorf("hold %s is in status %s, cannot commit", holdID, h.Status)
	}
	if time.Now().UTC().After(h.ExpiresAt) {
		return fmt.Errorf("hold %s has expired", holdID)
	}

	// Mark hold as committed
	_, err = tx.Exec(ctx, `UPDATE held_bookings SET status='COMMITTED' WHERE hold_id=$1`, holdID)
	if err != nil {
		return err
	}

	// Insert into booked_slots — capacity already incremented during hold
	_, err = tx.Exec(ctx, `
		INSERT INTO booked_slots
			(id, journey_id, user_id, vehicle_registration, departure_time, arrival_time,
			 origin_lat, origin_lng, destination_lat, destination_lng, is_active, route_id)
		VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,true,$11)
		ON CONFLICT DO NOTHING
	`,
		uuid.New().String(),
		h.JourneyID, h.UserID, h.VehicleRegistration,
		h.DepartureTime, h.ArrivalTime,
		h.OriginLat, h.OriginLng, h.DestinationLat, h.DestinationLng,
		h.RouteID,
	)
	if err != nil {
		return err
	}

	return tx.Commit(ctx)
}

// rollbackHold marks a hold as ROLLED_BACK and decrements road capacity,
// freeing the road cells that were reserved during the hold.
func rollbackHold(ctx context.Context, holdID string) error {
	tx, err := db.BeginTx(ctx, pgx.TxOptions{})
	if err != nil {
		return err
	}
	defer tx.Rollback(ctx)

	var h HoldRecord
	err = tx.QueryRow(ctx, `
		SELECT hold_id, COALESCE(route_id,''), origin_lat, origin_lng,
		       destination_lat, destination_lng,
		       departure_time, arrival_time, status
		FROM held_bookings
		WHERE hold_id = $1
		FOR UPDATE
	`, holdID).Scan(
		&h.HoldID, &h.RouteID, &h.OriginLat, &h.OriginLng,
		&h.DestinationLat, &h.DestinationLng,
		&h.DepartureTime, &h.ArrivalTime, &h.Status,
	)
	if err != nil {
		if isNoRows(err) {
			return ErrNotFound
		}
		return err
	}

	if h.Status == "ROLLED_BACK" || h.Status == "COMMITTED" {
		// Already rolled back or committed — idempotent
		return tx.Commit(ctx)
	}

	// Mark as rolled back
	_, err = tx.Exec(ctx, `UPDATE held_bookings SET status='ROLLED_BACK' WHERE hold_id=$1`, holdID)
	if err != nil {
		return err
	}

	// Resolve cells using the same path logic used during the hold (waypoint-aware).
	// pathGridCellsForRequest reads route waypoints from DB if route_id is set,
	// falling back to straight-line when no route is configured.
	mockReq := ConflictCheckRequest{
		RouteID:        h.RouteID,
		OriginLat:      h.OriginLat,
		OriginLng:      h.OriginLng,
		DestinationLat: h.DestinationLat,
		DestinationLng: h.DestinationLng,
	}
	cells, cellErr := pathGridCellsForRequest(ctx, mockReq)
	if cellErr != nil {
		log.Printf("rollbackHold: could not resolve cells for hold %s (falling back to straight-line): %v", holdID, cellErr)
		cells = pathGridCells(h.OriginLat, h.OriginLng, h.DestinationLat, h.DestinationLng)
	}
	for i, cell := range cells {
		t := cellTime(h.DepartureTime, h.ArrivalTime, i, len(cells))
		if err := decrementCapacity(ctx, tx, cell.lat, cell.lng, t); err != nil {
			return err
		}
	}

	return tx.Commit(ctx)
}

// expireOldHolds rolls back all HELD entries whose expires_at is in the past.
func expireOldHolds(ctx context.Context) {
	rows, err := db.Query(ctx, `
		SELECT hold_id FROM held_bookings
		WHERE status = 'HELD' AND expires_at < NOW()
	`)
	if err != nil {
		log.Printf("expireOldHolds query error: %v", err)
		return
	}
	defer rows.Close()

	var holdIDs []string
	for rows.Next() {
		var id string
		if err := rows.Scan(&id); err == nil {
			holdIDs = append(holdIDs, id)
		}
	}
	rows.Close()

	for _, id := range holdIDs {
		if err := rollbackHold(ctx, id); err != nil {
			log.Printf("expireOldHolds: rollback %s: %v", id, err)
		} else {
			log.Printf("expireOldHolds: rolled back expired hold %s", id)
		}
	}
}

// runHoldExpiry is a background goroutine that calls expireOldHolds every 10s.
func runHoldExpiry() {
	ticker := time.NewTicker(10 * time.Second)
	defer ticker.Stop()
	for range ticker.C {
		expireOldHolds(context.Background())
	}
}

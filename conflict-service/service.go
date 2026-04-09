package main

import (
	"context"
	"errors"
	"fmt"
	"math"
	"strings"
	"time"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"
)

// FlexTime accepts RFC3339 with or without timezone offset.
type FlexTime struct{ time.Time }

func (ft *FlexTime) UnmarshalJSON(b []byte) error {
	s := strings.Trim(string(b), `"`)
	formats := []string{time.RFC3339, "2006-01-02T15:04:05"}
	for _, f := range formats {
		if t, err := time.Parse(f, s); err == nil {
			ft.Time = t.UTC()
			return nil
		}
	}
	return fmt.Errorf("cannot parse %q as a time", s)
}

const (
	gridResolution       = 0.01 // ~1km in lat/lng degrees
	capacitySlotMinutes  = 30
	defaultMaxCapacity   = 1 // single-lane road: only one vehicle per cell per time slot
	journeyBufferMinutes = 5
)

type ConflictCheckRequest struct {
	JourneyID                string   `json:"journey_id"`
	UserID                   string   `json:"user_id"`
	OriginLat                float64  `json:"origin_lat"`
	OriginLng                float64  `json:"origin_lng"`
	DestinationLat           float64  `json:"destination_lat"`
	DestinationLng           float64  `json:"destination_lng"`
	DepartureTime            FlexTime `json:"departure_time"`
	EstimatedDurationMinutes int      `json:"estimated_duration_minutes"`
	VehicleRegistration      string   `json:"vehicle_registration"`
	// RouteID is optional. When set, the conflict service uses the predefined
	// real-road waypoints for that route instead of a straight-line path.
	RouteID string `json:"route_id,omitempty"`
}

type ConflictCheckResponse struct {
	JourneyID       string    `json:"journey_id"`
	IsConflict      bool      `json:"is_conflict"`
	ConflictType    *string   `json:"conflict_type,omitempty"`
	ConflictDetails *string   `json:"conflict_details,omitempty"`
	CheckedAt       time.Time `json:"checked_at"`
}

func checkConflicts(ctx context.Context, req ConflictCheckRequest) (ConflictCheckResponse, error) {
	arrivalTime := req.DepartureTime.Time.Add(time.Duration(req.EstimatedDurationMinutes) * time.Minute)
	bufferedDeparture := req.DepartureTime.Time.Add(-journeyBufferMinutes * time.Minute)
	bufferedArrival := arrivalTime.Add(journeyBufferMinutes * time.Minute)

	// Single serializable transaction for the entire check+reserve — prevents race conditions
	// where two concurrent bookings both pass the capacity check.
	tx, err := db.BeginTx(ctx, pgx.TxOptions{IsoLevel: pgx.Serializable})
	if err != nil {
		return ConflictCheckResponse{}, err
	}
	defer tx.Rollback(ctx)

	// Check 1: driver time overlap
	if conflict, err := checkDriverOverlap(ctx, tx, req.UserID, bufferedDeparture, bufferedArrival, req.JourneyID); err != nil {
		return ConflictCheckResponse{}, err
	} else if conflict != nil {
		ct := "TIME_OVERLAP"
		details := fmt.Sprintf(
			"Driver already has a journey booked from %s to %s",
			conflict.departureTime.Format(time.RFC3339),
			conflict.arrivalTime.Format(time.RFC3339),
		)
		return ConflictCheckResponse{
			JourneyID:       req.JourneyID,
			IsConflict:      true,
			ConflictType:    &ct,
			ConflictDetails: &details,
			CheckedAt:       time.Now().UTC(),
		}, nil
	}

	// Check 2: vehicle time overlap
	if conflict, err := checkVehicleOverlap(ctx, tx, req.VehicleRegistration, bufferedDeparture, bufferedArrival, req.JourneyID); err != nil {
		return ConflictCheckResponse{}, err
	} else if conflict != nil {
		ct := "TIME_OVERLAP"
		details := fmt.Sprintf(
			"Vehicle %s already has a journey booked from %s to %s",
			req.VehicleRegistration,
			conflict.departureTime.Format(time.RFC3339),
			conflict.arrivalTime.Format(time.RFC3339),
		)
		return ConflictCheckResponse{
			JourneyID:       req.JourneyID,
			IsConflict:      true,
			ConflictType:    &ct,
			ConflictDetails: &details,
			CheckedAt:       time.Now().UTC(),
		}, nil
	}

	// Check 3: road capacity along the full path (SELECT FOR UPDATE on every cell)
	if details, err := checkRoadCapacity(ctx, tx, req, arrivalTime); err != nil {
		return ConflictCheckResponse{}, err
	} else if details != "" {
		ct := "ROAD_CAPACITY"
		return ConflictCheckResponse{
			JourneyID:       req.JourneyID,
			IsConflict:      true,
			ConflictType:    &ct,
			ConflictDetails: &details,
			CheckedAt:       time.Now().UTC(),
		}, nil
	}

	// No conflict — record the slot and commit atomically
	if err := recordBookingSlot(ctx, tx, req, arrivalTime); err != nil {
		return ConflictCheckResponse{}, err
	}

	if err := tx.Commit(ctx); err != nil {
		return ConflictCheckResponse{}, fmt.Errorf("commit conflict check transaction: %w", err)
	}

	// Replicate the confirmed slot to peer nodes asynchronously (eventual consistency).
	go replicateSlotToPeers(req, arrivalTime)

	return ConflictCheckResponse{
		JourneyID:  req.JourneyID,
		IsConflict: false,
		CheckedAt:  time.Now().UTC(),
	}, nil
}

type bookedSlot struct {
	departureTime time.Time
	arrivalTime   time.Time
}

func checkDriverOverlap(ctx context.Context, tx pgx.Tx, userID string, departure, arrival time.Time, excludeJourneyID string) (*bookedSlot, error) {
	row := tx.QueryRow(ctx, `
		SELECT departure_time, arrival_time FROM booked_slots
		WHERE user_id = $1
		  AND is_active = true
		  AND journey_id != $2
		  AND departure_time < $3
		  AND arrival_time > $4
		LIMIT 1
		FOR UPDATE
	`, userID, excludeJourneyID, arrival, departure)

	var s bookedSlot
	if err := row.Scan(&s.departureTime, &s.arrivalTime); err != nil {
		if isNoRows(err) {
			return nil, nil
		}
		return nil, err
	}
	return &s, nil
}

func checkVehicleOverlap(ctx context.Context, tx pgx.Tx, vehicleReg string, departure, arrival time.Time, excludeJourneyID string) (*bookedSlot, error) {
	row := tx.QueryRow(ctx, `
		SELECT departure_time, arrival_time FROM booked_slots
		WHERE vehicle_registration = $1
		  AND is_active = true
		  AND journey_id != $2
		  AND departure_time < $3
		  AND arrival_time > $4
		LIMIT 1
		FOR UPDATE
	`, vehicleReg, excludeJourneyID, arrival, departure)

	var s bookedSlot
	if err := row.Scan(&s.departureTime, &s.arrivalTime); err != nil {
		if isNoRows(err) {
			return nil, nil
		}
		return nil, err
	}
	return &s, nil
}

// gridCell is a ~1km road segment identified by its snapped lat/lng coordinates.
type gridCell struct {
	lat float64
	lng float64
}

// pathGridCells returns every ~1km grid cell that the straight-line path from
// (originLat, originLng) to (destLat, destLng) passes through, in order.
//
// Algorithm: walk the line in steps of gridResolution. The number of steps is
// max(|ΔLat|, |ΔLng|) / gridResolution, which guarantees we never skip a cell
// even on steep diagonals. Duplicate cells (when the line hugs a cell boundary)
// are removed.
//
// Example — A(0,0) → D(0, 0.05):
//
//	cells = [(0,0.00), (0,0.01), (0,0.02), (0,0.03), (0,0.04), (0,0.05)]
//
// A booking for B(0,0.01)→C(0,0.03) would produce cells [(0,0.01),(0,0.02),(0,0.03)],
// which all overlap with the A→D booking → conflict detected.
func pathGridCells(originLat, originLng, destLat, destLng float64) []gridCell {
	deltaLat := destLat - originLat
	deltaLng := destLng - originLng

	// How many grid units does this path span in each axis?
	stepsLat := math.Abs(deltaLat) / gridResolution
	stepsLng := math.Abs(deltaLng) / gridResolution

	// Use the longer axis so we never skip a cell.
	steps := int(math.Ceil(math.Max(stepsLat, stepsLng)))
	if steps == 0 {
		steps = 1 // origin == destination: single cell
	}

	seen := make(map[gridCell]bool)
	cells := make([]gridCell, 0, steps+1)

	for i := 0; i <= steps; i++ {
		frac := float64(i) / float64(steps)
		cell := gridCell{
			lat: roundGrid(originLat + frac*deltaLat),
			lng: roundGrid(originLng + frac*deltaLng),
		}
		if !seen[cell] {
			seen[cell] = true
			cells = append(cells, cell)
		}
	}
	return cells
}

// pathGridCellsFromWaypoints builds the full set of grid cells by walking each
// consecutive pair of waypoints as a straight-line segment and combining the
// results. This gives a piecewise-linear approximation of the real road path
// instead of a single straight line from origin to destination.
//
// Example — Dublin → Galway via Athlone:
//
//	Dublin→Leixlip, Leixlip→Kinnegad, Kinnegad→Athlone, Athlone→Ballinasloe, Ballinasloe→Galway
//
// Each segment produces its own grid cells; the union is deduplicated and
// returned in route order. A booking for Athlone→Ballinasloe will share cells
// with this route and be rejected — whereas a straight Dublin→Galway line
// might miss those cells entirely depending on the angle.
func pathGridCellsFromWaypoints(waypoints []Waypoint) []gridCell {
	seen := make(map[gridCell]bool)
	var cells []gridCell
	for i := 0; i < len(waypoints)-1; i++ {
		segment := pathGridCells(
			waypoints[i].Lat, waypoints[i].Lng,
			waypoints[i+1].Lat, waypoints[i+1].Lng,
		)
		for _, cell := range segment {
			if !seen[cell] {
				seen[cell] = true
				cells = append(cells, cell)
			}
		}
	}
	return cells
}

// pathGridCellsForRequest returns the grid cells for a booking request.
// If the request carries a RouteID and that route exists in the DB, it uses
// the real road waypoints. Otherwise it falls back to straight-line.
func pathGridCellsForRequest(ctx context.Context, req ConflictCheckRequest) ([]gridCell, error) {
	if req.RouteID != "" {
		wps, err := loadRouteWaypoints(ctx, req.RouteID)
		if err != nil {
			return nil, fmt.Errorf("load route waypoints: %w", err)
		}
		if len(wps) >= 2 {
			return pathGridCellsFromWaypoints(wps), nil
		}
		// Route not found or has < 2 waypoints — fall through to straight-line
	}
	return pathGridCells(req.OriginLat, req.OriginLng, req.DestinationLat, req.DestinationLng), nil
}

// cellTime returns the interpolated timestamp at which a vehicle reaches cell i
// of n total cells, given departure and arrival times.
//
// i=0 → departure, i=n-1 → arrival, i=k → departure + k/(n-1) * duration.
// This lets the capacity check use the correct time slot for each segment.
func cellTime(departure, arrival time.Time, i, n int) time.Time {
	if n <= 1 {
		return departure
	}
	frac := float64(i) / float64(n-1)
	duration := arrival.Sub(departure)
	return departure.Add(time.Duration(float64(duration) * frac))
}

// checkRoadCapacity checks every grid cell along the straight-line path for
// capacity violations, locking each row with SELECT FOR UPDATE so that
// concurrent bookings cannot both pass the same full cell.
//
// Old behaviour: checked only origin cell (at departure) and destination cell
// (at arrival). A→D and B→C never conflicted even when B and C lay on the route.
//
// New behaviour: iterates all ~1km cells between origin and destination. Each
// cell is checked at its interpolated time. A→D will lock cells covering B and C,
// so a B→C booking that arrives during the same time window is rejected.
func checkRoadCapacity(ctx context.Context, tx pgx.Tx, req ConflictCheckRequest, arrivalTime time.Time) (string, error) {
	cells, err := pathGridCellsForRequest(ctx, req)
	if err != nil {
		return "", err
	}

	for i, cell := range cells {
		t := cellTime(req.DepartureTime.Time, arrivalTime, i, len(cells))

		// Use EXISTS + FOR UPDATE on a subquery — COUNT(*) cannot be combined with
		// FOR UPDATE in PostgreSQL (error 0A000).  We lock the matching row (if any)
		// and then check whether it was found.
		var id string
		err := tx.QueryRow(ctx, `
			SELECT id FROM road_segment_capacity
			WHERE grid_lat = $1
			  AND grid_lng = $2
			  AND time_slot_start <= $3
			  AND time_slot_end > $3
			  AND current_bookings >= max_capacity
			LIMIT 1
			FOR UPDATE
		`, cell.lat, cell.lng, t).Scan(&id)
		if err != nil && !isNoRows(err) {
			return "", err
		}
		if err == nil {
			// A row was found — the segment is at capacity
			return fmt.Sprintf(
				"Road segment (%.2f, %.2f) is fully booked at %s — segment %d of %d along route",
				cell.lat, cell.lng, t.Format("15:04 UTC"), i+1, len(cells),
			), nil
		}
	}
	return "", nil
}

// recordBookingSlot inserts the booking and increments capacity for every grid
// cell along the path (not just origin and destination).
func recordBookingSlot(ctx context.Context, tx pgx.Tx, req ConflictCheckRequest, arrivalTime time.Time) error {
	_, err := tx.Exec(ctx, `
		INSERT INTO booked_slots
			(id, journey_id, user_id, vehicle_registration, departure_time, arrival_time,
			 origin_lat, origin_lng, destination_lat, destination_lng, is_active)
		VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,true)
	`,
		uuid.New().String(),
		req.JourneyID, req.UserID, req.VehicleRegistration,
		req.DepartureTime.Time, arrivalTime,
		req.OriginLat, req.OriginLng,
		req.DestinationLat, req.DestinationLng,
	)
	if err != nil {
		return err
	}

	cells, err := pathGridCellsForRequest(ctx, req)
	if err != nil {
		return err
	}
	for i, cell := range cells {
		t := cellTime(req.DepartureTime.Time, arrivalTime, i, len(cells))
		if err := incrementCapacity(ctx, tx, cell.lat, cell.lng, t); err != nil {
			return err
		}
	}
	return nil
}

func incrementCapacity(ctx context.Context, tx pgx.Tx, lat, lng float64, t time.Time) error {
	gridLat := roundGrid(lat)
	gridLng := roundGrid(lng)

	minutes := (t.Minute() / capacitySlotMinutes) * capacitySlotMinutes
	slotStart := time.Date(t.Year(), t.Month(), t.Day(), t.Hour(), minutes, 0, 0, time.UTC)
	slotEnd := slotStart.Add(capacitySlotMinutes * time.Minute)

	_, err := tx.Exec(ctx, `
		INSERT INTO road_segment_capacity
			(id, grid_lat, grid_lng, time_slot_start, time_slot_end, current_bookings, max_capacity)
		VALUES ($1, $2, $3, $4, $5, 1, $6)
		ON CONFLICT (grid_lat, grid_lng, time_slot_start)
		DO UPDATE SET current_bookings = road_segment_capacity.current_bookings + 1
	`, uuid.New().String(), gridLat, gridLng, slotStart, slotEnd, defaultMaxCapacity)
	return err
}

var ErrNotFound = errors.New("journey not found")

// cancelBookingSlot marks the booking inactive AND decrements road capacity for
// every grid cell along the path, so those slots become available again.
//
// Previously only set is_active = false — capacity was never freed, meaning
// cancelled journeys permanently consumed road capacity.
func cancelBookingSlot(ctx context.Context, journeyID string) error {
	tx, err := db.BeginTx(ctx, pgx.TxOptions{})
	if err != nil {
		return err
	}
	defer tx.Rollback(ctx)

	// Deactivate and retrieve path endpoints in one statement.
	var originLat, originLng, destLat, destLng float64
	var departureTime, arrivalTime time.Time
	err = tx.QueryRow(ctx, `
		UPDATE booked_slots
		SET is_active = false
		WHERE journey_id = $1 AND is_active = true
		RETURNING origin_lat, origin_lng, destination_lat, destination_lng,
		          departure_time, arrival_time
	`, journeyID).Scan(
		&originLat, &originLng, &destLat, &destLng,
		&departureTime, &arrivalTime,
	)
	if err != nil {
		if isNoRows(err) {
			return ErrNotFound
		}
		return err
	}

	// Free capacity on every cell along the cancelled journey's path.
	cells := pathGridCells(originLat, originLng, destLat, destLng)
	for i, cell := range cells {
		t := cellTime(departureTime, arrivalTime, i, len(cells))
		if err := decrementCapacity(ctx, tx, cell.lat, cell.lng, t); err != nil {
			return err
		}
	}

	return tx.Commit(ctx)
}

func decrementCapacity(ctx context.Context, tx pgx.Tx, lat, lng float64, t time.Time) error {
	gridLat := roundGrid(lat)
	gridLng := roundGrid(lng)

	minutes := (t.Minute() / capacitySlotMinutes) * capacitySlotMinutes
	slotStart := time.Date(t.Year(), t.Month(), t.Day(), t.Hour(), minutes, 0, 0, time.UTC)

	_, err := tx.Exec(ctx, `
		UPDATE road_segment_capacity
		SET current_bookings = GREATEST(0, current_bookings - 1)
		WHERE grid_lat = $1 AND grid_lng = $2 AND time_slot_start = $3
	`, gridLat, gridLng, slotStart)
	return err
}

func roundGrid(v float64) float64 {
	return math.Round(v/gridResolution) * gridResolution
}

func isNoRows(err error) bool {
	return errors.Is(err, pgx.ErrNoRows)
}

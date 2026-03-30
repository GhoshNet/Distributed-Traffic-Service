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
	gridResolution       = 0.01 // ~1km in lat/lng
	capacitySlotMinutes  = 30
	defaultMaxCapacity   = 100
	journeyBufferMinutes = 5
)

type ConflictCheckRequest struct {
	JourneyID                string    `json:"journey_id"`
	UserID                   string    `json:"user_id"`
	OriginLat                float64   `json:"origin_lat"`
	OriginLng                float64   `json:"origin_lng"`
	DestinationLat           float64   `json:"destination_lat"`
	DestinationLng           float64   `json:"destination_lng"`
	DepartureTime            FlexTime  `json:"departure_time"`
	EstimatedDurationMinutes int       `json:"estimated_duration_minutes"`
	VehicleRegistration      string    `json:"vehicle_registration"`
}

type ConflictCheckResponse struct {
	JourneyID       string     `json:"journey_id"`
	IsConflict      bool       `json:"is_conflict"`
	ConflictType    *string    `json:"conflict_type,omitempty"`
	ConflictDetails *string    `json:"conflict_details,omitempty"`
	CheckedAt       time.Time  `json:"checked_at"`
}

func checkConflicts(ctx context.Context, req ConflictCheckRequest) (ConflictCheckResponse, error) {
	arrivalTime := req.DepartureTime.Time.Add(time.Duration(req.EstimatedDurationMinutes) * time.Minute)
	bufferedDeparture := req.DepartureTime.Time.Add(-journeyBufferMinutes * time.Minute)
	bufferedArrival := arrivalTime.Add(journeyBufferMinutes * time.Minute)

	// Check 1: driver time overlap
	if conflict, err := checkDriverOverlap(ctx, req.UserID, bufferedDeparture, bufferedArrival, req.JourneyID); err != nil {
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
	if conflict, err := checkVehicleOverlap(ctx, req.VehicleRegistration, bufferedDeparture, bufferedArrival, req.JourneyID); err != nil {
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

	// Check 3: road capacity
	if details, err := checkRoadCapacity(ctx, req, arrivalTime); err != nil {
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

	// No conflict — record the slot
	if err := recordBookingSlot(ctx, req, arrivalTime); err != nil {
		return ConflictCheckResponse{}, err
	}

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

func checkDriverOverlap(ctx context.Context, userID string, departure, arrival time.Time, excludeJourneyID string) (*bookedSlot, error) {
	row := db.QueryRow(ctx, `
		SELECT departure_time, arrival_time FROM booked_slots
		WHERE user_id = $1
		  AND is_active = true
		  AND journey_id != $2
		  AND departure_time < $3
		  AND arrival_time > $4
		LIMIT 1
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

func checkVehicleOverlap(ctx context.Context, vehicleReg string, departure, arrival time.Time, excludeJourneyID string) (*bookedSlot, error) {
	row := db.QueryRow(ctx, `
		SELECT departure_time, arrival_time FROM booked_slots
		WHERE vehicle_registration = $1
		  AND is_active = true
		  AND journey_id != $2
		  AND departure_time < $3
		  AND arrival_time > $4
		LIMIT 1
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

func checkRoadCapacity(ctx context.Context, req ConflictCheckRequest, arrivalTime time.Time) (string, error) {
	originGridLat := roundGrid(req.OriginLat)
	originGridLng := roundGrid(req.OriginLng)

	var count int
	err := db.QueryRow(ctx, `
		SELECT COUNT(*) FROM road_segment_capacity
		WHERE grid_lat = $1
		  AND grid_lng = $2
		  AND time_slot_start <= $3
		  AND time_slot_end > $3
		  AND current_bookings >= max_capacity
	`, originGridLat, originGridLng, req.DepartureTime.Time).Scan(&count)
	if err != nil {
		return "", err
	}
	if count > 0 {
		return fmt.Sprintf("Road capacity exceeded at origin area (%.2f, %.2f)", originGridLat, originGridLng), nil
	}

	destGridLat := roundGrid(req.DestinationLat)
	destGridLng := roundGrid(req.DestinationLng)

	err = db.QueryRow(ctx, `
		SELECT COUNT(*) FROM road_segment_capacity
		WHERE grid_lat = $1
		  AND grid_lng = $2
		  AND time_slot_start <= $3
		  AND time_slot_end > $3
		  AND current_bookings >= max_capacity
	`, destGridLat, destGridLng, arrivalTime).Scan(&count)
	if err != nil {
		return "", err
	}
	if count > 0 {
		return fmt.Sprintf("Road capacity exceeded at destination area (%.2f, %.2f)", destGridLat, destGridLng), nil
	}

	return "", nil
}

func recordBookingSlot(ctx context.Context, req ConflictCheckRequest, arrivalTime time.Time) error {
	tx, err := db.Begin(ctx)
	if err != nil {
		return err
	}
	defer tx.Rollback(ctx)

	_, err = tx.Exec(ctx, `
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

	if err := incrementCapacity(ctx, tx, req.OriginLat, req.OriginLng, req.DepartureTime.Time); err != nil {
		return err
	}
	if err := incrementCapacity(ctx, tx, req.DestinationLat, req.DestinationLng, arrivalTime); err != nil {
		return err
	}

	return tx.Commit(ctx)
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

func cancelBookingSlot(ctx context.Context, journeyID string) error {
	tag, err := db.Exec(ctx, `
		UPDATE booked_slots SET is_active = false
		WHERE journey_id = $1 AND is_active = true
	`, journeyID)
	if err != nil {
		return err
	}
	if tag.RowsAffected() == 0 {
		return ErrNotFound
	}
	return nil
}

func roundGrid(v float64) float64 {
	return math.Round(v/gridResolution) * gridResolution
}

func isNoRows(err error) bool {
	return errors.Is(err, pgx.ErrNoRows)
}

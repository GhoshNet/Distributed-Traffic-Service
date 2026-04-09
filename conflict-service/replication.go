package main

import (
	"bytes"
	"context"
	"encoding/json"
	"log"
	"net/http"
	"time"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"
)

// peerConflictURLs holds the base URLs of peer conflict-service instances.
// Populated from PEER_CONFLICT_URLS env var (comma-separated).
// Example: PEER_CONFLICT_URLS=http://192.168.1.42:8003
var peerConflictURLs []string

var replicationClient = &http.Client{Timeout: 5 * time.Second}

// ReplicateSlotRequest is the payload sent to peer nodes after a booking is confirmed.
type ReplicateSlotRequest struct {
	JourneyID           string    `json:"journey_id"`
	UserID              string    `json:"user_id"`
	VehicleRegistration string    `json:"vehicle_registration"`
	DepartureTime       time.Time `json:"departure_time"`
	ArrivalTime         time.Time `json:"arrival_time"`
	OriginLat           float64   `json:"origin_lat"`
	OriginLng           float64   `json:"origin_lng"`
	DestinationLat      float64   `json:"destination_lat"`
	DestinationLng      float64   `json:"destination_lng"`
	RouteID             string    `json:"route_id,omitempty"`
}

// replicateSlotToPeers propagates a confirmed booking slot to all registered peer
// conflict services. Runs per-peer in a goroutine (eventual consistency — does not
// block the booking response). Failures are logged but do not fail the local booking.
func replicateSlotToPeers(req ConflictCheckRequest, arrivalTime time.Time) {
	if len(peerConflictURLs) == 0 {
		return
	}
	payload := ReplicateSlotRequest{
		JourneyID:           req.JourneyID,
		UserID:              req.UserID,
		VehicleRegistration: req.VehicleRegistration,
		DepartureTime:       req.DepartureTime.Time,
		ArrivalTime:         arrivalTime,
		OriginLat:           req.OriginLat,
		OriginLng:           req.OriginLng,
		DestinationLat:      req.DestinationLat,
		DestinationLng:      req.DestinationLng,
		RouteID:             req.RouteID,
	}
	body, _ := json.Marshal(payload)
	for _, base := range peerConflictURLs {
		base := base // capture
		go func() {
			ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
			defer cancel()
			r, err := http.NewRequestWithContext(ctx, http.MethodPost,
				base+"/internal/slots/replicate", bytes.NewReader(body))
			if err != nil {
				log.Printf("[replication] build request to %s: %v", base, err)
				return
			}
			r.Header.Set("Content-Type", "application/json")
			resp, err := replicationClient.Do(r)
			if err != nil {
				log.Printf("[replication] peer %s unreachable: %v", base, err)
				return
			}
			resp.Body.Close()
			log.Printf("[replication] slot %s → %s (HTTP %d)", payload.JourneyID, base, resp.StatusCode)
		}()
	}
}

// replicateCancelToPeers tells peer nodes to deactivate the given journey's slot.
func replicateCancelToPeers(journeyID string) {
	if len(peerConflictURLs) == 0 {
		return
	}
	body, _ := json.Marshal(map[string]string{"journey_id": journeyID})
	for _, base := range peerConflictURLs {
		base := base // capture
		go func() {
			ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
			defer cancel()
			r, err := http.NewRequestWithContext(ctx, http.MethodPost,
				base+"/internal/slots/cancel", bytes.NewReader(body))
			if err != nil {
				log.Printf("[replication] build cancel request to %s: %v", base, err)
				return
			}
			r.Header.Set("Content-Type", "application/json")
			resp, err := replicationClient.Do(r)
			if err != nil {
				log.Printf("[replication] cancel peer %s unreachable: %v", base, err)
				return
			}
			resp.Body.Close()
			log.Printf("[replication] cancel %s → %s (HTTP %d)", journeyID, base, resp.StatusCode)
		}()
	}
}

// applyReplicatedSlot inserts a booking slot received from a peer into the local DB.
// Does NOT call peer replication (prevents forwarding loops).
// Idempotent: silently no-ops if the journey_id already exists locally.
func applyReplicatedSlot(ctx context.Context, r ReplicateSlotRequest) error {
	tx, err := db.BeginTx(ctx, pgx.TxOptions{})
	if err != nil {
		return err
	}
	defer tx.Rollback(ctx)

	// Idempotency: skip if this slot is already recorded locally.
	var exists bool
	if err := tx.QueryRow(ctx,
		`SELECT EXISTS(SELECT 1 FROM booked_slots WHERE journey_id = $1)`,
		r.JourneyID,
	).Scan(&exists); err != nil {
		return err
	}
	if exists {
		log.Printf("[replication] slot for journey %s already present — skipping", r.JourneyID)
		return nil
	}

	// Insert the replicated slot.
	if _, err := tx.Exec(ctx, `
		INSERT INTO booked_slots
			(id, journey_id, user_id, vehicle_registration,
			 departure_time, arrival_time,
			 origin_lat, origin_lng, destination_lat, destination_lng, is_active)
		VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,true)
	`,
		uuid.New().String(),
		r.JourneyID, r.UserID, r.VehicleRegistration,
		r.DepartureTime, r.ArrivalTime,
		r.OriginLat, r.OriginLng, r.DestinationLat, r.DestinationLng,
	); err != nil {
		return err
	}

	// Reconstruct the path and increment road-segment capacity to match the origin node.
	var cells []gridCell
	if r.RouteID != "" {
		if wps, wErr := loadRouteWaypoints(ctx, r.RouteID); wErr == nil && len(wps) >= 2 {
			cells = pathGridCellsFromWaypoints(wps)
		}
	}
	if cells == nil {
		cells = pathGridCells(r.OriginLat, r.OriginLng, r.DestinationLat, r.DestinationLng)
	}
	for i, cell := range cells {
		t := cellTime(r.DepartureTime, r.ArrivalTime, i, len(cells))
		if err := incrementCapacity(ctx, tx, cell.lat, cell.lng, t); err != nil {
			return err
		}
	}

	return tx.Commit(ctx)
}

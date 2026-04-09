package main

import (
	"bytes"
	"context"
	"encoding/json"
	"log"
	"net/http"
	"sync"
	"time"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"
)

// peerConflictURLs is the live peer list — guarded by peersMu so it can be
// extended at runtime (POST /internal/peers/register) without a restart.
var (
	peersMu          sync.RWMutex
	peerConflictURLs []string
)

// Two clients: fast one for fire-and-forget replication, slow one for
// full state-sync which may transfer many rows.
var (
	replicationClient = &http.Client{Timeout: 5 * time.Second}
	syncClient        = &http.Client{Timeout: 30 * time.Second}
)

// ReplicateSlotRequest is the payload for both real-time push replication
// and full state-sync responses.
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

// getPeers returns a safe snapshot of the current peer list.
func getPeers() []string {
	peersMu.RLock()
	defer peersMu.RUnlock()
	out := make([]string, len(peerConflictURLs))
	copy(out, peerConflictURLs)
	return out
}

// addPeer registers a peer URL at runtime (idempotent) and immediately
// triggers a catch-up sync from it.  Used by POST /internal/peers/register
// so a late-joining node can plug in without a restart.
func addPeer(peerURL string) {
	peersMu.Lock()
	for _, u := range peerConflictURLs {
		if u == peerURL {
			peersMu.Unlock()
			log.Printf("[replication] peer %s already registered", peerURL)
			return
		}
	}
	peerConflictURLs = append(peerConflictURLs, peerURL)
	peersMu.Unlock()
	log.Printf("[replication] new peer %s added — triggering catch-up sync", peerURL)
	go syncFromPeer(peerURL)
}

// ── Forward replication (push, async) ──────────────────────────────────────

// replicateSlotToPeers pushes a freshly committed slot to all peers.
// Each peer runs in its own goroutine — does not block the booking response.
func replicateSlotToPeers(req ConflictCheckRequest, arrivalTime time.Time) {
	peers := getPeers()
	if len(peers) == 0 {
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
	for _, base := range peers {
		base := base
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

// replicateCancelToPeers tells all peers to deactivate a journey slot.
func replicateCancelToPeers(journeyID string) {
	peers := getPeers()
	if len(peers) == 0 {
		return
	}
	body, _ := json.Marshal(map[string]string{"journey_id": journeyID})
	for _, base := range peers {
		base := base
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

// ── Apply incoming replication ──────────────────────────────────────────────

// applyReplicatedSlot inserts a slot received from a peer into the local DB.
// Does NOT call back to peers (loop prevention).
// Fully idempotent: no-op if journey_id already exists.
func applyReplicatedSlot(ctx context.Context, r ReplicateSlotRequest) error {
	tx, err := db.BeginTx(ctx, pgx.TxOptions{})
	if err != nil {
		return err
	}
	defer tx.Rollback(ctx)

	var exists bool
	if err := tx.QueryRow(ctx,
		`SELECT EXISTS(SELECT 1 FROM booked_slots WHERE journey_id = $1)`,
		r.JourneyID,
	).Scan(&exists); err != nil {
		return err
	}
	if exists {
		return nil // already present — idempotent no-op
	}

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

// ── State sync (pull-based catch-up) ───────────────────────────────────────

// getActiveSlots returns all active future/in-progress slots from the local DB.
// Exposed via GET /internal/slots/active so peers can pull a full state snapshot.
func getActiveSlots(ctx context.Context) ([]ReplicateSlotRequest, error) {
	rows, err := db.Query(ctx, `
		SELECT journey_id, user_id, vehicle_registration,
		       departure_time, arrival_time,
		       origin_lat, origin_lng, destination_lat, destination_lng
		FROM booked_slots
		WHERE is_active = true
		  AND arrival_time > NOW() - INTERVAL '1 hour'
		ORDER BY created_at ASC
	`)
	if err != nil {
		return nil, err
	}
	defer rows.Close()

	var slots []ReplicateSlotRequest
	for rows.Next() {
		var s ReplicateSlotRequest
		if err := rows.Scan(
			&s.JourneyID, &s.UserID, &s.VehicleRegistration,
			&s.DepartureTime, &s.ArrivalTime,
			&s.OriginLat, &s.OriginLng, &s.DestinationLat, &s.DestinationLng,
		); err != nil {
			return nil, err
		}
		slots = append(slots, s)
	}
	if slots == nil {
		slots = []ReplicateSlotRequest{}
	}
	return slots, rows.Err()
}

// syncFromPeer pulls all active slots from peerURL and applies any that are
// missing locally.  This is the catch-up mechanism for two cases:
//
//  1. Late-joining node: starts with an empty DB, pulls everything from peers.
//  2. Rejoining after downtime: missed bookings during the gap are backfilled.
func syncFromPeer(peerURL string) {
	req, err := http.NewRequest(http.MethodGet, peerURL+"/internal/slots/active", nil)
	if err != nil {
		log.Printf("[sync] build request to %s: %v", peerURL, err)
		return
	}
	resp, err := syncClient.Do(req)
	if err != nil {
		log.Printf("[sync] peer %s unreachable during catch-up: %v", peerURL, err)
		return
	}
	defer resp.Body.Close()

	var data struct {
		Slots []ReplicateSlotRequest `json:"slots"`
		Count int                    `json:"count"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&data); err != nil {
		log.Printf("[sync] decode response from %s: %v", peerURL, err)
		return
	}

	applied := 0
	for _, slot := range data.Slots {
		if err := applyReplicatedSlot(context.Background(), slot); err != nil {
			log.Printf("[sync] apply slot %s: %v", slot.JourneyID, err)
		} else {
			applied++
		}
	}
	log.Printf("[sync] catch-up from %s complete: %d/%d slots applied (rest already present)",
		peerURL, applied, len(data.Slots))
}

// startPeriodicSync re-syncs from all peers every interval.
// Handles the rejoin-after-downtime case: even if a push was missed while
// a node was down, the next periodic sync will backfill it.
func startPeriodicSync(interval time.Duration) {
	go func() {
		ticker := time.NewTicker(interval)
		defer ticker.Stop()
		for range ticker.C {
			for _, peer := range getPeers() {
				peer := peer
				go syncFromPeer(peer)
			}
		}
	}()
}

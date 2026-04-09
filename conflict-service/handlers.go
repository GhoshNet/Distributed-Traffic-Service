package main

import (
	"encoding/json"
	"errors"
	"log"
	"net/http"
	"time"

	"github.com/go-chi/chi/v5"
)


func healthHandler(w http.ResponseWriter, r *http.Request) {
	writeJSON(w, http.StatusOK, map[string]interface{}{
		"status":    "healthy",
		"service":   "conflict-service",
		"timestamp": time.Now().UTC(),
		"version":   "1.0.0",
	})
}

func checkConflictsHandler(w http.ResponseWriter, r *http.Request) {
	var req ConflictCheckRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{
			"error":   "invalid_request",
			"message": err.Error(),
		})
		return
	}

	resp, err := checkConflicts(r.Context(), req)
	if err != nil {
		log.Printf("Conflict check failed: %v", err)
		writeJSON(w, http.StatusInternalServerError, map[string]string{
			"error":   "internal_error",
			"message": err.Error(),
		})
		return
	}

	writeJSON(w, http.StatusOK, resp)
}

func cancelBookingSlotHandler(w http.ResponseWriter, r *http.Request) {
	journeyID := chi.URLParam(r, "journey_id")
	if err := cancelBookingSlot(r.Context(), journeyID); err != nil {
		if errors.Is(err, ErrNotFound) {
			writeJSON(w, http.StatusNotFound, map[string]string{
				"error":   "not_found",
				"message": "no active booking slot for journey " + journeyID,
			})
			return
		}
		log.Printf("Cancel booking slot failed: %v", err)
		writeJSON(w, http.StatusInternalServerError, map[string]string{
			"error":   "internal_error",
			"message": err.Error(),
		})
		return
	}
	// Propagate cancellation to peer nodes.
	go replicateCancelToPeers(journeyID)
	w.WriteHeader(http.StatusNoContent)
}

// activeSlotsHandler returns all currently active booking slots so that peers
// can pull a full state snapshot on startup or after rejoining.
func activeSlotsHandler(w http.ResponseWriter, r *http.Request) {
	slots, err := getActiveSlots(r.Context())
	if err != nil {
		log.Printf("[sync] getActiveSlots: %v", err)
		writeJSON(w, http.StatusInternalServerError, map[string]string{"error": err.Error()})
		return
	}
	writeJSON(w, http.StatusOK, map[string]interface{}{
		"slots": slots,
		"count": len(slots),
	})
}

// addPeerHandler registers a new peer conflict-service URL at runtime and
// immediately triggers a catch-up sync from it.
// Body: {"peer_url": "http://172.20.10.12:8003"}
func addPeerHandler(w http.ResponseWriter, r *http.Request) {
	var body struct {
		PeerURL string `json:"peer_url"`
	}
	if err := json.NewDecoder(r.Body).Decode(&body); err != nil || body.PeerURL == "" {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "peer_url required"})
		return
	}
	addPeer(body.PeerURL)
	writeJSON(w, http.StatusOK, map[string]interface{}{
		"registered": body.PeerURL,
		"peers":      getPeers(),
		"note":       "Catch-up sync started in background",
	})
}

// replicateSlotHandler receives a booking slot from a peer node and applies it
// to the local DB. Does NOT forward to other peers (loop prevention).
func replicateSlotHandler(w http.ResponseWriter, r *http.Request) {
	var req ReplicateSlotRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": err.Error()})
		return
	}
	if err := applyReplicatedSlot(r.Context(), req); err != nil {
		log.Printf("[replication] apply slot failed: %v", err)
		writeJSON(w, http.StatusInternalServerError, map[string]string{"error": err.Error()})
		return
	}
	w.WriteHeader(http.StatusNoContent)
}

// replicateCancelHandler receives a cancellation from a peer node and deactivates
// the slot locally. Does NOT forward to other peers (loop prevention).
func replicateCancelHandler(w http.ResponseWriter, r *http.Request) {
	var body struct {
		JourneyID string `json:"journey_id"`
	}
	if err := json.NewDecoder(r.Body).Decode(&body); err != nil || body.JourneyID == "" {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "journey_id required"})
		return
	}
	if err := cancelBookingSlot(r.Context(), body.JourneyID); err != nil && !errors.Is(err, ErrNotFound) {
		log.Printf("[replication] apply cancel failed: %v", err)
		writeJSON(w, http.StatusInternalServerError, map[string]string{"error": err.Error()})
		return
	}
	w.WriteHeader(http.StatusNoContent)
}

func listRoutesHandler(w http.ResponseWriter, r *http.Request) {
	routes, err := listAllRoutes(r.Context())
	if err != nil {
		log.Printf("List routes failed: %v", err)
		writeJSON(w, http.StatusInternalServerError, map[string]string{
			"error":   "internal_error",
			"message": err.Error(),
		})
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"routes": routes,
		"count":  len(routes),
	})
}

func writeJSON(w http.ResponseWriter, status int, v interface{}) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	json.NewEncoder(w).Encode(v)
}

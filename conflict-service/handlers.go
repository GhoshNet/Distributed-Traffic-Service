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
	w.WriteHeader(http.StatusNoContent)
}

func writeJSON(w http.ResponseWriter, status int, v interface{}) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	json.NewEncoder(w).Encode(v)
}

package main

import (
	"encoding/json"
	"errors"
	"log"
	"net/http"
	"time"

	"github.com/go-chi/chi/v5"
)

// regionConfig holds the loaded region configuration (set by main.go on startup).
var regionConfig Config

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

// ─── Hold / Commit / Rollback Handlers ──────────────────────────────────────

func holdHandler(w http.ResponseWriter, r *http.Request) {
	var req ConflictCheckRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{
			"error":   "invalid_request",
			"message": err.Error(),
		})
		return
	}

	resp, err := holdConflicts(r.Context(), req)
	if err != nil {
		log.Printf("Hold conflicts failed: %v", err)
		writeJSON(w, http.StatusInternalServerError, map[string]string{
			"error":   "internal_error",
			"message": err.Error(),
		})
		return
	}

	if resp.IsConflict {
		writeJSON(w, http.StatusConflict, resp)
		return
	}
	writeJSON(w, http.StatusOK, resp)
}

func commitHoldHandler(w http.ResponseWriter, r *http.Request) {
	holdID := chi.URLParam(r, "hold_id")
	if err := commitHold(r.Context(), holdID); err != nil {
		if errors.Is(err, ErrNotFound) {
			writeJSON(w, http.StatusNotFound, map[string]string{
				"error":   "not_found",
				"message": "hold not found or already expired: " + holdID,
			})
			return
		}
		log.Printf("Commit hold failed: %v", err)
		writeJSON(w, http.StatusInternalServerError, map[string]string{
			"error":   "internal_error",
			"message": err.Error(),
		})
		return
	}
	w.WriteHeader(http.StatusNoContent)
}

func rollbackHoldHandler(w http.ResponseWriter, r *http.Request) {
	holdID := chi.URLParam(r, "hold_id")
	if err := rollbackHold(r.Context(), holdID); err != nil {
		if errors.Is(err, ErrNotFound) {
			writeJSON(w, http.StatusNotFound, map[string]string{
				"error":   "not_found",
				"message": "hold not found: " + holdID,
			})
			return
		}
		log.Printf("Rollback hold failed: %v", err)
		writeJSON(w, http.StatusInternalServerError, map[string]string{
			"error":   "internal_error",
			"message": err.Error(),
		})
		return
	}
	w.WriteHeader(http.StatusNoContent)
}

// ─── Region Info Handler ─────────────────────────────────────────────────────

func regionInfoHandler(w http.ResponseWriter, r *http.Request) {
	ownedRoutesList := regionConfig.OwnedRoutes
	if len(ownedRoutesList) == 0 {
		// No filter — report all seeded routes
		routes, err := listAllRoutes(r.Context())
		if err == nil {
			for _, rt := range routes {
				ownedRoutesList = append(ownedRoutesList, rt.RouteID)
			}
		}
	}

	writeJSON(w, http.StatusOK, map[string]interface{}{
		"region_id":    regionConfig.RegionID,
		"region_name":  regionConfig.RegionName,
		"owned_routes": ownedRoutesList,
		"status":       simState.GetState().String(),
		"sim_state":    simState.StatusSnapshot(),
	})
}

// ─── Simulation Handlers ─────────────────────────────────────────────────────

func simulateDelayHandler(w http.ResponseWriter, r *http.Request) {
	var body struct {
		DelayMS int `json:"delay_ms"`
	}
	if err := json.NewDecoder(r.Body).Decode(&body); err != nil || body.DelayMS < 0 {
		writeJSON(w, http.StatusBadRequest, map[string]string{
			"error":   "invalid_request",
			"message": "delay_ms must be a non-negative integer",
		})
		return
	}
	simState.SetDelay(body.DelayMS)
	log.Printf("[simulate] Region %s entering DELAYED state (%d ms)", regionConfig.RegionID, body.DelayMS)
	writeJSON(w, http.StatusOK, map[string]interface{}{
		"state":    "DELAYED",
		"delay_ms": body.DelayMS,
	})
}

func simulateFailureHandler(w http.ResponseWriter, r *http.Request) {
	simState.SetFailed()
	log.Printf("[simulate] Region %s entering FAILED state", regionConfig.RegionID)
	writeJSON(w, http.StatusOK, map[string]string{
		"state": "FAILED",
	})
}

func simulateRecoverHandler(w http.ResponseWriter, r *http.Request) {
	simState.Recover()
	log.Printf("[simulate] Region %s recovered to NORMAL state", regionConfig.RegionID)
	writeJSON(w, http.StatusOK, map[string]string{
		"state": "NORMAL",
	})
}

func simulatePartitionHandler(w http.ResponseWriter, r *http.Request) {
	var body struct {
		TargetRegionID string `json:"target_region_id"`
	}
	if err := json.NewDecoder(r.Body).Decode(&body); err != nil || body.TargetRegionID == "" {
		writeJSON(w, http.StatusBadRequest, map[string]string{
			"error":   "invalid_request",
			"message": "target_region_id is required",
		})
		return
	}
	simState.SetPartitioned(body.TargetRegionID)
	log.Printf("[simulate] Region %s partitioned from %s", regionConfig.RegionID, body.TargetRegionID)
	writeJSON(w, http.StatusOK, map[string]interface{}{
		"state":            "PARTITIONED",
		"partitioned_from": body.TargetRegionID,
	})
}

func simulateStatusHandler(w http.ResponseWriter, r *http.Request) {
	writeJSON(w, http.StatusOK, simState.StatusSnapshot())
}

// ─── Health Handler (updated with region info) ───────────────────────────────

func healthHandlerWithRegion(w http.ResponseWriter, r *http.Request) {
	writeJSON(w, http.StatusOK, map[string]interface{}{
		"status":      "healthy",
		"service":     regionConfig.ServiceName,
		"region_id":   regionConfig.RegionID,
		"region_name": regionConfig.RegionName,
		"timestamp":   time.Now().UTC(),
		"version":     "2.0.0",
	})
}

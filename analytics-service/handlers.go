package main

import (
	"encoding/json"
	"net/http"
	"os"
	"strconv"
	"time"
)

func healthHandler(w http.ResponseWriter, r *http.Request) {
	writeJSON(w, http.StatusOK, map[string]any{
		"status":    "healthy",
		"service":   "analytics-service",
		"timestamp": time.Now().UTC(),
		"version":   "1.0.0",
	})
}

func statsHandler(w http.ResponseWriter, r *http.Request) {
	stats := GetSystemStats(r.Context())
	writeJSON(w, http.StatusOK, stats)
}

func eventsHandler(w http.ResponseWriter, r *http.Request) {
	q := r.URL.Query()
	eventType := q.Get("event_type")

	limit := 50
	if v := q.Get("limit"); v != "" {
		if n, err := strconv.Atoi(v); err == nil && n >= 1 && n <= 200 {
			limit = n
		}
	}
	offset := 0
	if v := q.Get("offset"); v != "" {
		if n, err := strconv.Atoi(v); err == nil && n >= 0 {
			offset = n
		}
	}

	events, err := queryEventHistory(r.Context(), eventType, limit, offset)
	if err != nil {
		writeJSON(w, http.StatusInternalServerError, map[string]string{
			"error":   "db_error",
			"message": err.Error(),
		})
		return
	}

	// Serialize to a flat map slice so callers get consistent field names.
	out := make([]map[string]any, 0, len(events))
	for _, e := range events {
		out = append(out, map[string]any{
			"id":          e.ID,
			"event_type":  e.EventType,
			"journey_id":  e.JourneyID,
			"user_id":     e.UserID,
			"origin":      e.Origin,
			"destination": e.Destination,
			"created_at":  e.CreatedAt.UTC().Format(time.RFC3339),
		})
	}

	writeJSON(w, http.StatusOK, map[string]any{
		"events": out,
		"count":  len(out),
	})
}

func hourlyStatsHandler(w http.ResponseWriter, r *http.Request) {
	ctx := r.Context()
	q := r.URL.Query()

	limit := 24
	if v := q.Get("limit"); v != "" {
		if n, err := strconv.Atoi(v); err == nil && n >= 1 && n <= 168 {
			limit = n
		}
	}

	rows, err := db.Query(ctx, `
		SELECT hour, total_bookings, confirmed, rejected, cancelled
		FROM hourly_stats
		ORDER BY hour DESC
		LIMIT $1
	`, limit)
	if err != nil {
		writeJSON(w, http.StatusInternalServerError, map[string]string{"error": err.Error()})
		return
	}
	defer rows.Close()

	type hourRow struct {
		Hour          string `json:"hour"`
		TotalBookings int    `json:"total_bookings"`
		Confirmed     int    `json:"confirmed"`
		Rejected      int    `json:"rejected"`
		Cancelled     int    `json:"cancelled"`
	}

	var out []hourRow
	for rows.Next() {
		var hr hourRow
		var t time.Time
		if err := rows.Scan(&t, &hr.TotalBookings, &hr.Confirmed, &hr.Rejected, &hr.Cancelled); err != nil {
			continue
		}
		hr.Hour = t.UTC().Format(time.RFC3339)
		out = append(out, hr)
	}
	if out == nil {
		out = []hourRow{}
	}

	writeJSON(w, http.StatusOK, map[string]any{
		"hourly_stats": out,
		"count":        len(out),
	})
}

func replicaLagHandler(w http.ResponseWriter, r *http.Request) {
	ctx := r.Context()

	// Query pg_stat_replication on the primary to get replication lag
	rows, err := db.Query(ctx, `
		SELECT
			application_name,
			state,
			COALESCE(write_lag::text, 'N/A')   AS write_lag,
			COALESCE(flush_lag::text, 'N/A')   AS flush_lag,
			COALESCE(replay_lag::text, 'N/A')  AS replay_lag,
			CASE WHEN pg_is_in_recovery() THEN 'replica' ELSE 'primary' END AS role
		FROM pg_stat_replication
	`)
	if err != nil {
		// Fallback: at least report whether this is primary or replica
		var inRecovery bool
		_ = db.QueryRow(ctx, "SELECT pg_is_in_recovery()").Scan(&inRecovery)
		role := "primary"
		if inRecovery {
			role = "replica"
		}
		writeJSON(w, http.StatusOK, map[string]any{
			"role":           role,
			"replicas":       []any{},
			"note":           "pg_stat_replication query failed (may be a replica node)",
		})
		return
	}
	defer rows.Close()

	type replicaRow struct {
		Name      string `json:"application_name"`
		State     string `json:"state"`
		WriteLag  string `json:"write_lag"`
		FlushLag  string `json:"flush_lag"`
		ReplayLag string `json:"replay_lag"`
		Role      string `json:"role"`
	}

	var replicas []replicaRow
	for rows.Next() {
		var r replicaRow
		if err := rows.Scan(&r.Name, &r.State, &r.WriteLag, &r.FlushLag, &r.ReplayLag, &r.Role); err != nil {
			continue
		}
		replicas = append(replicas, r)
	}
	if replicas == nil {
		replicas = []replicaRow{}
	}

	writeJSON(w, http.StatusOK, map[string]any{
		"role":     "primary",
		"replicas": replicas,
		"count":    len(replicas),
	})
}

func serviceHealthHandler(w http.ResponseWriter, r *http.Request) {
	base := os.Getenv("SERVICES_BASE_URL")

	var services map[string]string
	if base != "" {
		services = map[string]string{
			"user-service":        "http://user-service:8000/health",
			"journey-service":     "http://journey-service:8000/health",
			"conflict-service":    "http://conflict-service:8000/health",
			"notification-service": "http://notification-service:8000/health",
			"enforcement-service": "http://enforcement-service:8000/health",
			"analytics-service":   "http://localhost:8000/health",
		}
	} else {
		services = map[string]string{
			"user-service":        "http://localhost:8001/health",
			"journey-service":     "http://localhost:8002/health",
			"conflict-service":    "http://localhost:8003/health",
			"notification-service": "http://localhost:8004/health",
			"enforcement-service": "http://localhost:8005/health",
			"analytics-service":   "http://localhost:8006/health",
		}
	}

	client := &http.Client{Timeout: 5 * time.Second}
	results := make(map[string]any, len(services))
	allHealthy := true

	for name, url := range services {
		start := time.Now()
		resp, err := client.Get(url)
		elapsed := time.Since(start).Seconds() * 1000

		if err != nil {
			allHealthy = false
			results[name] = map[string]any{
				"status": "unreachable",
				"error":  err.Error(),
			}
			continue
		}
		resp.Body.Close()

		status := "healthy"
		if resp.StatusCode != http.StatusOK {
			status = "unhealthy"
			allHealthy = false
		}
		results[name] = map[string]any{
			"status":           status,
			"response_time_ms": elapsed,
		}
	}

	overall := "healthy"
	if !allHealthy {
		overall = "degraded"
	}

	writeJSON(w, http.StatusOK, map[string]any{
		"overall_status": overall,
		"services":       results,
		"checked_at":     time.Now().UTC().Format(time.RFC3339),
	})
}

func writeJSON(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	json.NewEncoder(w).Encode(v)
}

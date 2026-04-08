package main

import (
	"net/http"
	"strings"
	"sync"
	"time"
)

// SimState represents the current simulation state of this region node.
type SimState int

const (
	SimNormal     SimState = iota // NORMAL — all requests processed normally
	SimDelayed                    // DELAYED — add delay_ms before processing
	SimFailed                     // FAILED — return 503 for /api/conflicts/* endpoints
	SimPartitioned                // PARTITIONED — outbound calls to specific regions fail
)

func (s SimState) String() string {
	switch s {
	case SimNormal:
		return "NORMAL"
	case SimDelayed:
		return "DELAYED"
	case SimFailed:
		return "FAILED"
	case SimPartitioned:
		return "PARTITIONED"
	default:
		return "UNKNOWN"
	}
}

// simStatus holds the current simulation configuration.
type simStatus struct {
	mu              sync.RWMutex
	state           SimState
	delayMS         int
	partitionedFrom []string // region IDs that are "partitioned" (outbound calls blocked)
	since           time.Time
}

var simState = &simStatus{
	state: SimNormal,
	since: time.Now().UTC(),
}

// GetState returns the current simulation state (thread-safe).
func (s *simStatus) GetState() SimState {
	s.mu.RLock()
	defer s.mu.RUnlock()
	return s.state
}

// GetDelayMS returns the current delay in milliseconds (thread-safe).
func (s *simStatus) GetDelayMS() int {
	s.mu.RLock()
	defer s.mu.RUnlock()
	return s.delayMS
}

// SetDelay puts the node into DELAYED state with the given delay.
func (s *simStatus) SetDelay(ms int) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.state = SimDelayed
	s.delayMS = ms
	s.since = time.Now().UTC()
}

// SetFailed puts the node into FAILED state.
func (s *simStatus) SetFailed() {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.state = SimFailed
	s.delayMS = 0
	s.since = time.Now().UTC()
}

// SetPartitioned puts the node into PARTITIONED state.
func (s *simStatus) SetPartitioned(targetRegionID string) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.state = SimPartitioned
	s.delayMS = 0
	// Add if not already in the list
	for _, r := range s.partitionedFrom {
		if r == targetRegionID {
			s.since = time.Now().UTC()
			return
		}
	}
	s.partitionedFrom = append(s.partitionedFrom, targetRegionID)
	s.since = time.Now().UTC()
}

// Recover puts the node back into NORMAL state.
func (s *simStatus) Recover() {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.state = SimNormal
	s.delayMS = 0
	s.partitionedFrom = nil
	s.since = time.Now().UTC()
}

// StatusSnapshot returns a snapshot of the current simulation status.
func (s *simStatus) StatusSnapshot() map[string]interface{} {
	s.mu.RLock()
	defer s.mu.RUnlock()
	partitioned := make([]string, len(s.partitionedFrom))
	copy(partitioned, s.partitionedFrom)
	return map[string]interface{}{
		"state":            s.state.String(),
		"delay_ms":         s.delayMS,
		"partitioned_from": partitioned,
		"since":            s.since.Format(time.RFC3339),
	}
}

// IsPartitionedFrom reports whether outbound calls to the given region should fail.
func (s *simStatus) IsPartitionedFrom(regionID string) bool {
	s.mu.RLock()
	defer s.mu.RUnlock()
	if s.state != SimPartitioned {
		return false
	}
	for _, r := range s.partitionedFrom {
		if r == regionID {
			return true
		}
	}
	return false
}

// simulationMiddleware intercepts requests to /api/conflicts/* when the node is
// in FAILED or DELAYED state. /health and /api/simulate/* always pass through.
func simulationMiddleware(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		path := r.URL.Path

		// Always allow health and simulation control endpoints
		if path == "/health" || strings.HasPrefix(path, "/api/simulate/") || strings.HasPrefix(path, "/api/region/") {
			next.ServeHTTP(w, r)
			return
		}

		state := simState.GetState()
		switch state {
		case SimFailed:
			writeJSON(w, http.StatusServiceUnavailable, map[string]string{
				"error":   "region_failed",
				"message": "This region is in simulated FAILED state — not accepting new requests",
			})
			return
		case SimDelayed:
			delayMS := simState.GetDelayMS()
			if delayMS > 0 {
				time.Sleep(time.Duration(delayMS) * time.Millisecond)
			}
		}

		next.ServeHTTP(w, r)
	})
}

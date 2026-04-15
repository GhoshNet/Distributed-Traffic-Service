package main

// Circuit breaker for outbound peer HTTP calls.
//
// State machine (mirrors shared/circuit_breaker.py):
//
//	CLOSED   → calls pass through; 3 consecutive failures → OPEN
//	OPEN     → calls are blocked immediately (ErrCircuitOpen); after 30s → HALF-OPEN
//	HALF-OPEN → one probe call allowed; success → CLOSED, failure → OPEN again

import (
	"errors"
	"fmt"
	"log"
	"sync"
	"time"
)

type cbState int

const (
	cbClosed   cbState = iota
	cbOpen
	cbHalfOpen
)

const (
	cbFailureThreshold = 3
	cbResetTimeout     = 30 * time.Second
)

// ErrCircuitOpen is returned when a call is blocked by an open circuit.
type ErrCircuitOpen struct{ name string }

func (e ErrCircuitOpen) Error() string {
	return fmt.Sprintf("circuit breaker OPEN for %s", e.name)
}

type circuitBreaker struct {
	mu       sync.Mutex
	name     string
	state    cbState
	failures int
	openedAt time.Time
}

// Allow checks whether a call should proceed.
// Returns ErrCircuitOpen if the circuit is open and the reset timeout has not elapsed.
func (cb *circuitBreaker) Allow() error {
	cb.mu.Lock()
	defer cb.mu.Unlock()

	switch cb.state {
	case cbOpen:
		if time.Since(cb.openedAt) >= cbResetTimeout {
			cb.state = cbHalfOpen
			log.Printf("[CircuitBreaker:%s] → HALF-OPEN (probe allowed)", cb.name)
			return nil
		}
		return ErrCircuitOpen{cb.name}
	default:
		return nil
	}
}

// RecordSuccess resets the failure count and closes the circuit if it was half-open.
func (cb *circuitBreaker) RecordSuccess() {
	cb.mu.Lock()
	defer cb.mu.Unlock()
	cb.failures = 0
	if cb.state == cbHalfOpen {
		cb.state = cbClosed
		log.Printf("[CircuitBreaker:%s] → CLOSED (recovered)", cb.name)
	}
}

// RecordFailure increments the failure count and opens the circuit when the
// threshold is reached (or immediately if already half-open).
func (cb *circuitBreaker) RecordFailure(err error) {
	cb.mu.Lock()
	defer cb.mu.Unlock()
	cb.failures++
	log.Printf("[CircuitBreaker:%s] failure #%d: %v", cb.name, cb.failures, err)
	if cb.state == cbHalfOpen || cb.failures >= cbFailureThreshold {
		cb.state = cbOpen
		cb.openedAt = time.Now()
		log.Printf("[CircuitBreaker:%s] → OPEN", cb.name)
	}
}

// ── Global registry ────────────────────────────────────────────────────────────

var (
	cbRegistry   = make(map[string]*circuitBreaker)
	cbRegistryMu sync.RWMutex
)

// getPeerCB returns the circuit breaker for a given peer URL, creating it if needed.
func getPeerCB(peerURL string) *circuitBreaker {
	key := "peer:" + peerURL
	cbRegistryMu.RLock()
	cb, ok := cbRegistry[key]
	cbRegistryMu.RUnlock()
	if ok {
		return cb
	}
	cbRegistryMu.Lock()
	defer cbRegistryMu.Unlock()
	if cb, ok = cbRegistry[key]; ok {
		return cb
	}
	cb = &circuitBreaker{name: key}
	cbRegistry[key] = cb
	return cb
}

// isCircuitOpen is a convenience helper — returns true (and logs) when the error
// is an ErrCircuitOpen so call sites can skip the peer cleanly.
func isCircuitOpen(err error) bool {
	var e ErrCircuitOpen
	return errors.As(err, &e)
}

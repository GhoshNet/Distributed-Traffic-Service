package main

import (
	"io"
	"log"
	"os"
	"strings"
	"sync"
	"time"
)

// LogEntry is one structured log line stored in the ring buffer.
type LogEntry struct {
	TS      string `json:"ts"`
	Node    string `json:"node"`
	Service string `json:"service"`
	Msg     string `json:"msg"`
}

const maxLogEntries = 500

var (
	logRingMu sync.RWMutex
	logRing   []LogEntry
	logNodeID string
)

// bufWriter duplicates every write to stderr AND the ring buffer.
type bufWriter struct{ fallback io.Writer }

func (w *bufWriter) Write(p []byte) (int, error) {
	msg := strings.TrimRight(string(p), "\n\r")
	if msg != "" {
		e := LogEntry{
			TS:      time.Now().UTC().Format(time.RFC3339Nano),
			Node:    logNodeID,
			Service: "conflict-service",
			Msg:     msg,
		}
		logRingMu.Lock()
		logRing = append(logRing, e)
		if len(logRing) > maxLogEntries {
			logRing = logRing[len(logRing)-maxLogEntries:]
		}
		logRingMu.Unlock()
	}
	return w.fallback.Write(p)
}

// initLogBuffer sets the node identity and redirects log output through the buffer.
// Must be called before any log.Printf calls.
func initLogBuffer() {
	h, err := os.Hostname()
	if err != nil || h == "" {
		h = "conflict-service"
	}
	logNodeID = h
	log.SetOutput(&bufWriter{fallback: os.Stderr})
}

// getRecentLogs returns up to limit entries (oldest first, newest last).
func getRecentLogs(limit int) []LogEntry {
	logRingMu.RLock()
	defer logRingMu.RUnlock()
	n := len(logRing)
	if limit <= 0 || limit > n {
		limit = n
	}
	out := make([]LogEntry, limit)
	copy(out, logRing[n-limit:])
	return out
}

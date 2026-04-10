package main

import (
	"context"
	"log"
	"net/http"
	"os"
	"os/signal"
	"syscall"
	"time"

	"github.com/go-chi/chi/v5"
	"github.com/go-chi/chi/v5/middleware"
)

func main() {
	cfg := loadConfig()
	log.SetFlags(log.LstdFlags)
	initLogBuffer() // must be before any log.Printf — captures all subsequent log output
	log.Printf("[%s] starting up... node=%s", cfg.ServiceName, logNodeID)

	peerConflictURLs = cfg.PeerConflictURLs
	if len(peerConflictURLs) > 0 {
		log.Printf("Cross-node replication enabled — peers: %v", peerConflictURLs)
	}

	if err := initDB(cfg.DatabaseURL); err != nil {
		log.Fatalf("Failed to connect to database: %v", err)
	}
	log.Println("Database tables created/verified")

	// Startup catch-up sync: pull all active slots from every known peer.
	// Handles both fresh-start (empty DB) and rejoin-after-downtime (gap filling).
	for _, peer := range peerConflictURLs {
		peer := peer
		go func() {
			time.Sleep(3 * time.Second) // wait for own DB to be fully ready
			syncFromPeer(peer)
		}()
	}

	// Periodic re-sync every 5 minutes — fills any gap caused by missed pushes
	// while this node was temporarily unreachable.
	startPeriodicSync(5 * time.Minute)

	if err := startConsumer(cfg.RabbitMQURL); err != nil {
		log.Printf("Warning: could not connect to RabbitMQ: %v", err)
	} else {
		log.Println("Connected to RabbitMQ and started consumer")
	}

	r := chi.NewRouter()
	r.Use(middleware.Logger)
	r.Use(corsMiddleware)

	r.Get("/health", healthHandler)
	r.Get("/api/routes", listRoutesHandler)
	// Also expose under /api/conflicts/ prefix so the nginx gateway can forward it
	// without a separate upstream location block.
	r.Get("/api/conflicts/routes", listRoutesHandler)
	r.Post("/api/conflicts/check", checkConflictsHandler)
	r.Post("/api/conflicts/cancel/{journey_id}", cancelBookingSlotHandler)
	// Internal replication endpoints — called by peer conflict services only.
	r.Get("/internal/slots/active", activeSlotsHandler)      // pull full state snapshot
	r.Post("/internal/slots/replicate", replicateSlotHandler) // push a single new slot
	r.Post("/internal/slots/cancel", replicateCancelHandler)  // push a cancellation
	r.Post("/internal/peers/register", addPeerHandler)        // add peer at runtime
	r.Get("/admin/logs", logsHandler)                         // cross-node log aggregation

	srv := &http.Server{
		Addr:    ":" + cfg.Port,
		Handler: r,
	}

	quit := make(chan os.Signal, 1)
	signal.Notify(quit, syscall.SIGINT, syscall.SIGTERM)

	go func() {
		log.Printf("[%s] listening on :%s", cfg.ServiceName, cfg.Port)
		if err := srv.ListenAndServe(); err != nil && err != http.ErrServerClosed {
			log.Fatalf("Server error: %v", err)
		}
	}()

	<-quit
	log.Printf("[%s] shutting down...", cfg.ServiceName)
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	srv.Shutdown(ctx)
}

func corsMiddleware(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Access-Control-Allow-Origin", "*")
		w.Header().Set("Access-Control-Allow-Methods", "*")
		w.Header().Set("Access-Control-Allow-Headers", "*")
		w.Header().Set("Access-Control-Allow-Credentials", "true")
		if r.Method == http.MethodOptions {
			w.WriteHeader(http.StatusNoContent)
			return
		}
		next.ServeHTTP(w, r)
	})
}

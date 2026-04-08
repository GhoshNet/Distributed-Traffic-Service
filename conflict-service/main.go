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
	log.Printf("[%s] starting up... region=%s (%s)", cfg.ServiceName, cfg.RegionID, cfg.RegionName)
	if len(cfg.OwnedRoutes) > 0 {
		log.Printf("[%s] owned routes: %v", cfg.ServiceName, cfg.OwnedRoutes)
	} else {
		log.Printf("[%s] no REGION_OWNED_ROUTES set — seeding all routes", cfg.ServiceName)
	}

	// Store region config globally for handlers and DB seeding
	regionConfig = cfg

	// Set ownedRoutes package var for DB seed filtering
	if len(cfg.OwnedRoutes) > 0 {
		ownedRoutes = make(map[string]bool, len(cfg.OwnedRoutes))
		for _, r := range cfg.OwnedRoutes {
			ownedRoutes[r] = true
		}
	}

	if err := initDB(cfg.DatabaseURL); err != nil {
		log.Fatalf("Failed to connect to database: %v", err)
	}
	log.Println("Database tables created/verified")

	if err := startConsumer(cfg.RabbitMQURL); err != nil {
		log.Printf("Warning: could not connect to RabbitMQ: %v", err)
	} else {
		log.Println("Connected to RabbitMQ and started consumer")
	}

	// Start background hold expiry goroutine
	go runHoldExpiry()
	log.Println("Hold expiry background goroutine started")

	r := chi.NewRouter()
	r.Use(middleware.Logger)
	r.Use(corsMiddleware)
	r.Use(simulationMiddleware)

	r.Get("/health", healthHandlerWithRegion)
	r.Get("/api/routes", listRoutesHandler)
	r.Post("/api/conflicts/check", checkConflictsHandler)
	r.Post("/api/conflicts/cancel/{journey_id}", cancelBookingSlotHandler)

	// Hold / Commit / Rollback (distributed 2-phase saga)
	r.Post("/api/conflicts/hold", holdHandler)
	r.Post("/api/conflicts/commit/{hold_id}", commitHoldHandler)
	r.Post("/api/conflicts/rollback/{hold_id}", rollbackHoldHandler)

	// Region info
	r.Get("/api/region/info", regionInfoHandler)

	// Simulation control endpoints
	r.Post("/api/simulate/delay", simulateDelayHandler)
	r.Post("/api/simulate/failure", simulateFailureHandler)
	r.Post("/api/simulate/recover", simulateRecoverHandler)
	r.Post("/api/simulate/partition", simulatePartitionHandler)
	r.Get("/api/simulate/status", simulateStatusHandler)

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

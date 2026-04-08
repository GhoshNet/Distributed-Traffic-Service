package main

import (
	"context"
	"encoding/json"
	"fmt"
	"log"
	"strings"
	"time"

	"github.com/redis/go-redis/v9"
)

const (
	registryKeyPrefix = "region:"
	registryTTL       = 45 * time.Second
	heartbeatInterval = 15 * time.Second
)

// RegionEntry is the value stored in the Redis registry for each region.
type RegionEntry struct {
	URL         string   `json:"url"`
	OwnedRoutes []string `json:"owned_routes"`
	Status      string   `json:"status"`
	LastSeen    string   `json:"last_seen"`
}

// Registry handles Redis-based region registration, heartbeat, and peer discovery.
type Registry struct {
	client   *redis.Client
	regionID string
	entry    RegionEntry
	stopCh   chan struct{}
}

// parseRedisURL parses a redis://host:port/db URL into redis.Options.
func parseRedisURL(rawURL string) (*redis.Options, error) {
	return redis.ParseURL(rawURL)
}

// NewRegistry creates a new registry instance connected to Redis.
func NewRegistry(redisURL string, cfg Config) (*Registry, error) {
	opts, err := parseRedisURL(redisURL)
	if err != nil {
		return nil, fmt.Errorf("invalid redis URL: %w", err)
	}

	client := redis.NewClient(opts)

	// Verify connection
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	if err := client.Ping(ctx).Err(); err != nil {
		return nil, fmt.Errorf("redis ping failed: %w", err)
	}

	// Build the self-announcing URL from PORT env — inside Docker the hostname
	// is the service name, which we derive from SERVICE_NAME or fall back to
	// "conflict-service-{region_id}".
	host := cfg.ServiceName
	if host == "" {
		host = "conflict-service-" + strings.ToLower(cfg.RegionID)
	}
	selfURL := fmt.Sprintf("http://%s:%s", host, cfg.Port)

	return &Registry{
		client:   client,
		regionID: cfg.RegionID,
		entry: RegionEntry{
			URL:         selfURL,
			OwnedRoutes: cfg.OwnedRoutes,
			Status:      "NORMAL",
			LastSeen:    time.Now().UTC().Format(time.RFC3339),
		},
		stopCh: make(chan struct{}),
	}, nil
}

// Register writes this region's entry to Redis with a TTL.
func (reg *Registry) Register(ctx context.Context) error {
	reg.entry.LastSeen = time.Now().UTC().Format(time.RFC3339)
	reg.entry.Status = simState.GetState().String()

	data, err := json.Marshal(reg.entry)
	if err != nil {
		return err
	}

	key := registryKeyPrefix + reg.regionID
	return reg.client.Set(ctx, key, data, registryTTL).Err()
}

// StartHeartbeat begins a background goroutine that re-registers every 15s.
func (reg *Registry) StartHeartbeat() {
	go func() {
		ticker := time.NewTicker(heartbeatInterval)
		defer ticker.Stop()
		for {
			select {
			case <-ticker.C:
				ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
				if err := reg.Register(ctx); err != nil {
					log.Printf("[registry] heartbeat failed for region %s: %v", reg.regionID, err)
				}
				cancel()
			case <-reg.stopCh:
				return
			}
		}
	}()
}

// Stop terminates the heartbeat goroutine.
func (reg *Registry) Stop() {
	close(reg.stopCh)
}

// GetPeers returns all registered regions (including self) from Redis.
func (reg *Registry) GetPeers(ctx context.Context) ([]map[string]interface{}, error) {
	// Scan for all region:* keys
	var peers []map[string]interface{}
	iter := reg.client.Scan(ctx, 0, registryKeyPrefix+"*", 100).Iterator()
	for iter.Next(ctx) {
		key := iter.Val()
		val, err := reg.client.Get(ctx, key).Result()
		if err != nil {
			continue
		}

		var entry RegionEntry
		if err := json.Unmarshal([]byte(val), &entry); err != nil {
			continue
		}

		regionID := strings.TrimPrefix(key, registryKeyPrefix)
		peers = append(peers, map[string]interface{}{
			"region_id":    regionID,
			"url":          entry.URL,
			"owned_routes": entry.OwnedRoutes,
			"status":       entry.Status,
			"last_seen":    entry.LastSeen,
		})
	}
	if err := iter.Err(); err != nil {
		return nil, err
	}

	return peers, nil
}

// regionRegistry is the package-level registry instance, set by main.
var regionRegistry *Registry

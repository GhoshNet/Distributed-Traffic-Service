package main

import (
	"crypto/md5"
	"log"
	"os"
	"sync"
)

// Consistent-hash sharding for conflict-service routes.
//
// Each conflict-service node is assigned a shard_id of 0 (this node is always
// index 0; peers are 1..N).  A route's "home shard" is determined by
// md5(route_id) % num_nodes.
//
// Sharding here is about *write authority* (which node logs the primary check),
// NOT data isolation — all nodes still store all booked_slots for local conflict
// detection.  In a production system, the home shard would be the only writer
// and all other nodes would proxy to it; here we log the shard role so it is
// visible in the Distributed Activity Feed.

var (
	shardMu        sync.RWMutex
	knownRouteIDs  []string // populated after DB seed
)

// routeShardID returns the shard index (0..numShards-1) for a route_id.
func routeShardID(routeID string, numShards int) int {
	if numShards <= 1 {
		return 0
	}
	h := md5.Sum([]byte(routeID))
	val := int(h[0])<<8 | int(h[1])
	return val % numShards
}

// isHomeShard returns true when this node is the authoritative shard for routeID.
// node index 0 = this node; peers = indices 1..N.
func isHomeShard(routeID string) bool {
	peers := getPeers()
	numShards := 1 + len(peers)
	return routeShardID(routeID, numShards) == 0
}

// shardRole returns "PRIMARY" or "REPLICA" for logging.
func shardRole(routeID string) string {
	if isHomeShard(routeID) {
		return "PRIMARY"
	}
	return "REPLICA"
}

// registerKnownRoutes stores the known route IDs so getShardInfo can report ownership.
func registerKnownRoutes(routeIDs []string) {
	shardMu.Lock()
	defer shardMu.Unlock()
	knownRouteIDs = routeIDs
}

// ShardInfoResponse is returned by GET /internal/shard/info.
type ShardInfoResponse struct {
	NodeID      string         `json:"node_id"`
	TotalShards int            `json:"total_shards"`
	Routes      []RouteShardEntry `json:"routes"`
}

type RouteShardEntry struct {
	RouteID  string `json:"route_id"`
	ShardID  int    `json:"shard_id"`
	Role     string `json:"role"` // "PRIMARY" or "REPLICA"
}

// getShardInfo builds the full shard assignment table for this node.
func getShardInfo() ShardInfoResponse {
	peers := getPeers()
	numShards := 1 + len(peers)

	shardMu.RLock()
	routes := make([]RouteShardEntry, 0, len(knownRouteIDs))
	for _, rid := range knownRouteIDs {
		sid := routeShardID(rid, numShards)
		role := "REPLICA"
		if sid == 0 {
			role = "PRIMARY"
		}
		routes = append(routes, RouteShardEntry{RouteID: rid, ShardID: sid, Role: role})
	}
	shardMu.RUnlock()

	hostname, _ := os.Hostname()
	return ShardInfoResponse{
		NodeID:      hostname,
		TotalShards: numShards,
		Routes:      routes,
	}
}

// logShardRole logs the shard role for a conflict check — visible in Activity Feed.
func logShardRole(routeID string, journeyID string) {
	if routeID == "" {
		return
	}
	peers := getPeers()
	numShards := 1 + len(peers)
	sid := routeShardID(routeID, numShards)
	role := shardRole(routeID)
	log.Printf("[shard] conflict-check journey=%s route=%s shard=%d/%d role=%s",
		journeyID, routeID, sid, numShards, role)
}

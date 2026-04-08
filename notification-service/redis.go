package main

import (
	"context"
	"encoding/json"
	"fmt"
	"log"
	"os"
	"strings"
	"time"

	"github.com/redis/go-redis/v9"
)

var rdb *redis.Client

func initRedis(redisURL string) error {
	sentinelAddrs := os.Getenv("REDIS_SENTINEL_ADDRS")
	masterName := os.Getenv("REDIS_MASTER_NAME")
	if masterName == "" {
		masterName = "mymaster"
	}

	if sentinelAddrs != "" {
		addrs := strings.Split(sentinelAddrs, ",")
		rdb = redis.NewFailoverClient(&redis.FailoverOptions{
			MasterName:    masterName,
			SentinelAddrs: addrs,
			DB:            3,
		})
		log.Printf("Redis: using Sentinel (%d sentinels, master=%s, db=3)", len(addrs), masterName)
	} else {
		opts, err := redis.ParseURL(redisURL)
		if err != nil {
			return err
		}
		rdb = redis.NewClient(opts)
	}

	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	return rdb.Ping(ctx).Err()
}

// storeNotification pushes a notification to the user's Redis list (max 50, 7-day TTL).
func storeNotification(userID string, notification map[string]interface{}) {
	ctx := context.Background()
	key := fmt.Sprintf("notifications:%s", userID)

	data, err := json.Marshal(notification)
	if err != nil {
		log.Printf("Failed to marshal notification: %v", err)
		return
	}

	pipe := rdb.Pipeline()
	pipe.LPush(ctx, key, string(data))
	pipe.LTrim(ctx, key, 0, 49)
	pipe.Expire(ctx, key, 7*24*time.Hour)
	if _, err := pipe.Exec(ctx); err != nil {
		log.Printf("Failed to store notification in Redis: %v", err)
	}
}

// getNotifications retrieves up to limit notifications for a user from Redis.
func getNotifications(userID string, limit int) []map[string]interface{} {
	ctx := context.Background()
	key := fmt.Sprintf("notifications:%s", userID)

	raw, err := rdb.LRange(ctx, key, 0, int64(limit-1)).Result()
	if err != nil {
		log.Printf("Failed to retrieve notifications from Redis: %v", err)
		return []map[string]interface{}{}
	}

	result := make([]map[string]interface{}, 0, len(raw))
	for _, s := range raw {
		var n map[string]interface{}
		if err := json.Unmarshal([]byte(s), &n); err == nil {
			result = append(result, n)
		}
	}
	return result
}

package main

import (
	"context"
	"fmt"
	"math"
	"time"

	"github.com/jackc/pgx/v5/pgxpool"
)

const gridResolution = 0.01

func roundGrid(v float64) float64 {
	return math.Round(v/gridResolution) * gridResolution
}

func main() {
	db, err := pgxpool.New(context.Background(), "postgresql://conflicts_user:conflicts_pass@localhost:5435/conflicts_db")
	if err != nil {
		panic(err)
	}
	defer db.Close()

	lat := roundGrid(53.4608)
	lng := roundGrid(-7.1006)
	
	// t := 2026-04-08 18:21:40 UTC
	t, _ := time.Parse(time.RFC3339, "2026-04-08T18:21:40Z")

	var count int
	err = db.QueryRow(context.Background(), `
		SELECT COUNT(*) FROM road_segment_capacity
		WHERE grid_lat = $1
		  AND grid_lng = $2
		  AND time_slot_start <= $3
		  AND time_slot_end > $3
	`, lat, lng, t).Scan(&count)
	
	fmt.Printf("Float exact query: lat=%.20f lng=%.20f t=%v count=%d err=%v\n", lat, lng, t, count, err)

	// What if we do absolute diff instead
	var count2 int
	err = db.QueryRow(context.Background(), `
		SELECT COUNT(*) FROM road_segment_capacity
		WHERE ABS(grid_lat - $1) < 0.0001
		  AND ABS(grid_lng - $2) < 0.0001
		  AND time_slot_start <= $3
		  AND time_slot_end > $3
	`, lat, lng, t).Scan(&count2)
	fmt.Printf("ABS query: count=%d err=%v\n", count2, err)
}

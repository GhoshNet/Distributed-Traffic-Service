package main

import (
	"context"
	"fmt"
	"log"
	"time"

	"github.com/jackc/pgx/v5/pgxpool"
)

var db *pgxpool.Pool

func initDB(databaseURL string) error {
	var err error
	for attempt := 1; attempt <= 10; attempt++ {
		db, err = pgxpool.New(context.Background(), databaseURL)
		if err == nil {
			if pingErr := db.Ping(context.Background()); pingErr == nil {
				break
			} else {
				err = pingErr
			}
		}
		log.Printf("DB connection attempt %d/10 failed: %v", attempt, err)
		if attempt < 10 {
			time.Sleep(3 * time.Second)
		}
	}
	if err != nil {
		return fmt.Errorf("failed to connect to database after 10 attempts: %w", err)
	}

	if err := createTables(); err != nil {
		return err
	}
	return seedRoutes(context.Background())
}

func createTables() error {
	_, err := db.Exec(context.Background(), `
		-- Predefined road routes with real waypoints (Irish national roads)
		CREATE TABLE IF NOT EXISTS predefined_routes (
			route_id                   VARCHAR(50) PRIMARY KEY,
			name                       VARCHAR(200) NOT NULL,
			description                TEXT,
			origin_name                VARCHAR(100) NOT NULL,
			destination_name           VARCHAR(100) NOT NULL,
			origin_lat                 DOUBLE PRECISION NOT NULL,
			origin_lng                 DOUBLE PRECISION NOT NULL,
			destination_lat            DOUBLE PRECISION NOT NULL,
			destination_lng            DOUBLE PRECISION NOT NULL,
			estimated_duration_minutes INT NOT NULL
		);

		-- Ordered waypoints along each predefined route (actual road path, not straight line)
		CREATE TABLE IF NOT EXISTS route_waypoints (
			id             VARCHAR(36) PRIMARY KEY,
			route_id       VARCHAR(50) NOT NULL REFERENCES predefined_routes(route_id),
			sequence_order INT NOT NULL,
			lat            DOUBLE PRECISION NOT NULL,
			lng            DOUBLE PRECISION NOT NULL,
			location_name  VARCHAR(100)
		);

		CREATE INDEX IF NOT EXISTS idx_route_waypoints ON route_waypoints (route_id, sequence_order);

		CREATE TABLE IF NOT EXISTS booked_slots (
			id                   VARCHAR(36) PRIMARY KEY,
			journey_id           VARCHAR(36) NOT NULL,
			user_id              VARCHAR(36) NOT NULL,
			vehicle_registration VARCHAR(20) NOT NULL,
			departure_time       TIMESTAMP   NOT NULL,
			arrival_time         TIMESTAMP   NOT NULL,
			origin_lat           DOUBLE PRECISION NOT NULL,
			origin_lng           DOUBLE PRECISION NOT NULL,
			destination_lat      DOUBLE PRECISION NOT NULL,
			destination_lng      DOUBLE PRECISION NOT NULL,
			is_active            BOOLEAN DEFAULT TRUE,
			created_at           TIMESTAMP DEFAULT NOW()
		);

		CREATE INDEX IF NOT EXISTS idx_slot_user_time
			ON booked_slots (user_id, departure_time, arrival_time);
		CREATE INDEX IF NOT EXISTS idx_slot_vehicle_time
			ON booked_slots (vehicle_registration, departure_time, arrival_time);
		CREATE INDEX IF NOT EXISTS idx_slot_journey
			ON booked_slots (journey_id);

		CREATE TABLE IF NOT EXISTS road_segment_capacity (
			id               VARCHAR(36) PRIMARY KEY,
			grid_lat         DOUBLE PRECISION NOT NULL,
			grid_lng         DOUBLE PRECISION NOT NULL,
			time_slot_start  TIMESTAMP NOT NULL,
			time_slot_end    TIMESTAMP NOT NULL,
			current_bookings INT DEFAULT 0,
			max_capacity     INT DEFAULT 100
		);

		CREATE UNIQUE INDEX IF NOT EXISTS idx_grid_time_unique
			ON road_segment_capacity (grid_lat, grid_lng, time_slot_start);
	`)
	return err
}

// ─── Predefined routes ───────────────────────────────────────────────────────

// RouteInfo is returned by the GET /api/routes endpoint.
type RouteInfo struct {
	RouteID                  string     `json:"route_id"`
	Name                     string     `json:"name"`
	Description              string     `json:"description"`
	OriginName               string     `json:"origin_name"`
	DestinationName          string     `json:"destination_name"`
	OriginLat                float64    `json:"origin_lat"`
	OriginLng                float64    `json:"origin_lng"`
	DestinationLat           float64    `json:"destination_lat"`
	DestinationLng           float64    `json:"destination_lng"`
	EstimatedDurationMinutes int        `json:"estimated_duration_minutes"`
	Waypoints                []Waypoint `json:"waypoints"`
}

// Waypoint is a named lat/lng point along a predefined route.
type Waypoint struct {
	Lat  float64 `json:"lat"`
	Lng  float64 `json:"lng"`
	Name string  `json:"name"`
}

// seedRoutes inserts the predefined Irish road routes on first startup.
// ON CONFLICT DO NOTHING makes it idempotent — safe to call on every boot.
func seedRoutes(ctx context.Context) error {
	routes := []struct {
		id, name, desc, origin, dest string
		oLat, oLng, dLat, dLng      float64
		duration                     int
		waypoints                    []Waypoint
	}{
		{
			id: "dublin-galway", name: "Dublin → Galway (M6)",
			desc:   "M4/M6 motorway via Athlone — Ireland's main east-west corridor",
			origin: "Dublin", dest: "Galway",
			oLat: 53.3498, oLng: -6.2603, dLat: 53.2707, dLng: -9.0568,
			duration: 135,
			waypoints: []Waypoint{
				{53.3498, -6.2603, "Dublin"},
				{53.3636, -6.4867, "Leixlip (M4 junction)"},
				{53.4608, -7.1006, "Kinnegad"},
				{53.4239, -7.9407, "Athlone"},
				{53.3308, -8.2222, "Ballinasloe"},
				{53.2707, -9.0568, "Galway"},
			},
		},
		{
			id: "dublin-cork", name: "Dublin → Cork (M7/M8)",
			desc:   "M7 to Portlaoise, M8 south through Cashel to Cork",
			origin: "Dublin", dest: "Cork",
			oLat: 53.3498, oLng: -6.2603, dLat: 51.8985, dLng: -8.4756,
			duration: 150,
			waypoints: []Waypoint{
				{53.3498, -6.2603, "Dublin"},
				{53.1816, -6.7954, "Newbridge"},
				{53.0319, -7.2990, "Portlaoise"},
				{52.5159, -7.8879, "Cashel"},
				{51.8985, -8.4756, "Cork"},
			},
		},
		{
			id: "dublin-belfast", name: "Dublin → Belfast (M1/A1)",
			desc:   "M1 north through Drogheda and Dundalk, crossing the border at Newry",
			origin: "Dublin", dest: "Belfast",
			oLat: 53.3498, oLng: -6.2603, dLat: 54.5973, dLng: -5.9301,
			duration: 120,
			waypoints: []Waypoint{
				{53.3498, -6.2603, "Dublin"},
				{53.7179, -6.3569, "Drogheda"},
				{54.0011, -6.4011, "Dundalk"},
				{54.1751, -6.3394, "Newry (border crossing)"},
				{54.5973, -5.9301, "Belfast"},
			},
		},
		{
			id: "galway-limerick", name: "Galway → Limerick (N18)",
			desc:   "N18 south through Gort and Ennis into Limerick",
			origin: "Galway", dest: "Limerick",
			oLat: 53.2707, oLng: -9.0568, dLat: 52.6638, dLng: -8.6267,
			duration: 60,
			waypoints: []Waypoint{
				{53.2707, -9.0568, "Galway"},
				{53.0641, -8.8224, "Gort"},
				{52.8436, -8.9865, "Ennis"},
				{52.6638, -8.6267, "Limerick"},
			},
		},
		{
			id: "limerick-cork", name: "Limerick → Cork (M20)",
			desc:   "M20 south through Charleville and Mallow into Cork",
			origin: "Limerick", dest: "Cork",
			oLat: 52.6638, oLng: -8.6267, dLat: 51.8985, dLng: -8.4756,
			duration: 75,
			waypoints: []Waypoint{
				{52.6638, -8.6267, "Limerick"},
				{52.3567, -8.6817, "Charleville"},
				{52.1393, -8.6508, "Mallow"},
				{51.8985, -8.4756, "Cork"},
			},
		},
		{
			id: "dublin-limerick", name: "Dublin → Limerick (M7)",
			desc:   "M7 southwest through Newbridge and Portlaoise into Limerick",
			origin: "Dublin", dest: "Limerick",
			oLat: 53.3498, oLng: -6.2603, dLat: 52.6638, dLng: -8.6267,
			duration: 120,
			waypoints: []Waypoint{
				{53.3498, -6.2603, "Dublin"},
				{53.1816, -6.7954, "Newbridge"},
				{53.0319, -7.2990, "Portlaoise"},
				{52.8633, -8.1984, "Nenagh"},
				{52.6638, -8.6267, "Limerick"},
			},
		},

		// ── Europe ────────────────────────────────────────────────────────────
		{
			id: "london-paris", name: "London → Paris (Eurostar)",
			desc:   "Channel Tunnel rail link via Ashford, Folkestone, Calais and Lille",
			origin: "London", dest: "Paris",
			oLat: 51.5316, oLng: -0.1233, dLat: 48.8809, dLng: 2.3553,
			duration: 150,
			waypoints: []Waypoint{
				{51.5316, -0.1233, "London St Pancras"},
				{51.1470, 0.8762, "Ashford International"},
				{51.0786, 1.1817, "Folkestone"},
				{50.9513, 1.8587, "Calais"},
				{50.6292, 3.0573, "Lille"},
				{48.8809, 2.3553, "Paris Gare du Nord"},
			},
		},
		{
			id: "paris-amsterdam", name: "Paris → Amsterdam (Thalys)",
			desc:   "High-speed rail north through Brussels and Antwerp to Amsterdam",
			origin: "Paris", dest: "Amsterdam",
			oLat: 48.8809, oLng: 2.3553, dLat: 52.3791, dLng: 4.9003,
			duration: 210,
			waypoints: []Waypoint{
				{48.8809, 2.3553, "Paris Gare du Nord"},
				{50.8465, 4.3517, "Brussels-Midi"},
				{51.2171, 4.4210, "Antwerp Central"},
				{52.3791, 4.9003, "Amsterdam Centraal"},
			},
		},
		{
			id: "berlin-munich", name: "Berlin → Munich (A9)",
			desc:   "A9 Autobahn south through Nuremberg and Ingolstadt to Munich",
			origin: "Berlin", dest: "Munich",
			oLat: 52.5200, oLng: 13.4050, dLat: 48.1374, dLng: 11.5755,
			duration: 270,
			waypoints: []Waypoint{
				{52.5200, 13.4050, "Berlin"},
				{49.4521, 11.0767, "Nuremberg"},
				{48.7630, 11.4250, "Ingolstadt"},
				{48.1374, 11.5755, "Munich"},
			},
		},
		{
			id: "madrid-barcelona", name: "Madrid → Barcelona (A-2)",
			desc:   "A-2 northeast through Zaragoza and Lleida to Barcelona",
			origin: "Madrid", dest: "Barcelona",
			oLat: 40.4168, oLng: -3.7038, dLat: 41.3851, dLng: 2.1734,
			duration: 330,
			waypoints: []Waypoint{
				{40.4168, -3.7038, "Madrid"},
				{41.6561, -0.8773, "Zaragoza"},
				{41.6175, 0.6200, "Lleida"},
				{41.3851, 2.1734, "Barcelona"},
			},
		},
		{
			id: "rome-milan", name: "Rome → Milan (A1)",
			desc:   "A1 Autostrada del Sole north through Florence and Bologna to Milan",
			origin: "Rome", dest: "Milan",
			oLat: 41.9028, oLng: 12.4964, dLat: 45.4654, dLng: 9.1859,
			duration: 330,
			waypoints: []Waypoint{
				{41.9028, 12.4964, "Rome"},
				{43.7711, 11.2486, "Florence"},
				{44.4949, 11.3426, "Bologna"},
				{45.4654, 9.1859, "Milan"},
			},
		},

		// ── North America ─────────────────────────────────────────────────────
		{
			id: "new-york-boston", name: "New York → Boston (I-95)",
			desc:   "I-95 northeast through New Haven and Providence to Boston",
			origin: "New York", dest: "Boston",
			oLat: 40.7128, oLng: -74.0060, dLat: 42.3601, dLng: -71.0589,
			duration: 240,
			waypoints: []Waypoint{
				{40.7128, -74.0060, "New York"},
				{41.3082, -72.9279, "New Haven"},
				{41.8240, -71.4128, "Providence"},
				{42.3601, -71.0589, "Boston"},
			},
		},
		{
			id: "new-york-washington", name: "New York → Washington DC (I-95)",
			desc:   "I-95 south through Philadelphia and Baltimore to Washington DC",
			origin: "New York", dest: "Washington DC",
			oLat: 40.7128, oLng: -74.0060, dLat: 38.9072, dLng: -77.0369,
			duration: 270,
			waypoints: []Waypoint{
				{40.7128, -74.0060, "New York"},
				{39.9526, -75.1652, "Philadelphia"},
				{39.2904, -76.6122, "Baltimore"},
				{38.9072, -77.0369, "Washington DC"},
			},
		},
		{
			id: "los-angeles-san-francisco", name: "Los Angeles → San Francisco (US-101)",
			desc:   "US-101 north through Santa Barbara, San Luis Obispo and San Jose",
			origin: "Los Angeles", dest: "San Francisco",
			oLat: 34.0522, oLng: -118.2437, dLat: 37.7749, dLng: -122.4194,
			duration: 360,
			waypoints: []Waypoint{
				{34.0522, -118.2437, "Los Angeles"},
				{34.4208, -119.6982, "Santa Barbara"},
				{35.2828, -120.6596, "San Luis Obispo"},
				{37.3382, -121.8863, "San Jose"},
				{37.7749, -122.4194, "San Francisco"},
			},
		},
		{
			id: "chicago-detroit", name: "Chicago → Detroit (I-94)",
			desc:   "I-94 east through Gary, Kalamazoo and Ann Arbor to Detroit",
			origin: "Chicago", dest: "Detroit",
			oLat: 41.8781, oLng: -87.6298, dLat: 42.3314, dLng: -83.0458,
			duration: 270,
			waypoints: []Waypoint{
				{41.8781, -87.6298, "Chicago"},
				{41.5934, -87.3468, "Gary"},
				{42.2917, -85.5872, "Kalamazoo"},
				{42.2808, -83.7430, "Ann Arbor"},
				{42.3314, -83.0458, "Detroit"},
			},
		},

		// ── Asia ──────────────────────────────────────────────────────────────
		{
			id: "tokyo-osaka", name: "Tokyo → Osaka (Tokaido Shinkansen)",
			desc:   "Bullet train southwest via Yokohama, Nagoya and Kyoto to Osaka",
			origin: "Tokyo", dest: "Osaka",
			oLat: 35.6812, oLng: 139.7671, dLat: 34.6937, dLng: 135.5023,
			duration: 150,
			waypoints: []Waypoint{
				{35.6812, 139.7671, "Tokyo"},
				{35.4437, 139.6380, "Yokohama"},
				{35.1709, 136.8815, "Nagoya"},
				{35.0116, 135.7681, "Kyoto"},
				{34.6937, 135.5023, "Osaka"},
			},
		},
		{
			id: "singapore-kuala-lumpur", name: "Singapore → Kuala Lumpur (E1/E2)",
			desc:   "North S highway through Johor Bahru, Ayer Hitam and Seremban",
			origin: "Singapore", dest: "Kuala Lumpur",
			oLat: 1.3521, oLng: 103.8198, dLat: 3.1390, dLng: 101.6869,
			duration: 300,
			waypoints: []Waypoint{
				{1.3521, 103.8198, "Singapore"},
				{1.4927, 103.7414, "Johor Bahru"},
				{1.9095, 103.3246, "Ayer Hitam"},
				{2.7297, 101.9381, "Seremban"},
				{3.1390, 101.6869, "Kuala Lumpur"},
			},
		},
		{
			id: "delhi-agra", name: "Delhi → Agra (Yamuna Expressway)",
			desc:   "Yamuna Expressway south through Faridabad and Mathura to Agra",
			origin: "New Delhi", dest: "Agra",
			oLat: 28.6139, oLng: 77.2090, dLat: 27.1767, dLng: 78.0081,
			duration: 210,
			waypoints: []Waypoint{
				{28.6139, 77.2090, "New Delhi"},
				{28.4089, 77.3178, "Faridabad"},
				{27.4924, 77.6737, "Mathura"},
				{27.1767, 78.0081, "Agra"},
			},
		},
		{
			id: "mumbai-pune", name: "Mumbai → Pune (Mumbai-Pune Expressway)",
			desc:   "Expressway east through Khopoli and Lonavala to Pune",
			origin: "Mumbai", dest: "Pune",
			oLat: 19.0760, oLng: 72.8777, dLat: 18.5204, dLng: 73.8567,
			duration: 210,
			waypoints: []Waypoint{
				{19.0760, 72.8777, "Mumbai"},
				{18.7866, 73.2130, "Khopoli"},
				{18.7481, 73.4072, "Lonavala"},
				{18.5204, 73.8567, "Pune"},
			},
		},
		{
			id: "beijing-shanghai", name: "Beijing → Shanghai (G2 Jinghu)",
			desc:   "G2 expressway south via Jinan, Nanjing and Suzhou to Shanghai",
			origin: "Beijing", dest: "Shanghai",
			oLat: 39.9042, oLng: 116.4074, dLat: 31.2304, dLng: 121.4737,
			duration: 720,
			waypoints: []Waypoint{
				{39.9042, 116.4074, "Beijing"},
				{36.6512, 117.1201, "Jinan"},
				{32.0603, 118.7969, "Nanjing"},
				{31.2990, 120.5853, "Suzhou"},
				{31.2304, 121.4737, "Shanghai"},
			},
		},

		// ── Australia ─────────────────────────────────────────────────────────
		{
			id: "sydney-melbourne", name: "Sydney → Melbourne (Hume Highway)",
			desc:   "Hume Highway southwest through Goulburn, Albury and Wodonga",
			origin: "Sydney", dest: "Melbourne",
			oLat: -33.8688, oLng: 151.2093, dLat: -37.8136, dLng: 144.9631,
			duration: 540,
			waypoints: []Waypoint{
				{-33.8688, 151.2093, "Sydney"},
				{-34.7549, 149.7188, "Goulburn"},
				{-36.0737, 146.9135, "Albury"},
				{-36.1218, 146.8878, "Wodonga"},
				{-37.8136, 144.9631, "Melbourne"},
			},
		},
		{
			id: "sydney-canberra", name: "Sydney → Canberra (Federal Highway)",
			desc:   "Federal Highway southwest through Goulburn to Canberra",
			origin: "Sydney", dest: "Canberra",
			oLat: -33.8688, oLng: 151.2093, dLat: -35.2809, dLng: 149.1300,
			duration: 210,
			waypoints: []Waypoint{
				{-33.8688, 151.2093, "Sydney"},
				{-34.7549, 149.7188, "Goulburn"},
				{-35.2809, 149.1300, "Canberra"},
			},
		},

		// ── Africa ────────────────────────────────────────────────────────────
		{
			id: "cairo-alexandria", name: "Cairo → Alexandria (Desert Road)",
			desc:   "Desert Road northwest through Tanta to Alexandria",
			origin: "Cairo", dest: "Alexandria",
			oLat: 30.0444, oLng: 31.2357, dLat: 31.2001, dLng: 29.9187,
			duration: 150,
			waypoints: []Waypoint{
				{30.0444, 31.2357, "Cairo"},
				{30.7865, 31.0004, "Tanta"},
				{31.2001, 29.9187, "Alexandria"},
			},
		},
		{
			id: "nairobi-mombasa", name: "Nairobi → Mombasa (A109)",
			desc:   "A109 southeast through Athi River and Voi to Mombasa",
			origin: "Nairobi", dest: "Mombasa",
			oLat: -1.2921, oLng: 36.8219, dLat: -4.0435, dLng: 39.6682,
			duration: 480,
			waypoints: []Waypoint{
				{-1.2921, 36.8219, "Nairobi"},
				{-1.4561, 37.0007, "Athi River"},
				{-3.3965, 38.5559, "Voi"},
				{-4.0435, 39.6682, "Mombasa"},
			},
		},

		// ── South America ─────────────────────────────────────────────────────
		{
			id: "sao-paulo-rio", name: "São Paulo → Rio de Janeiro (Via Dutra)",
			desc:   "Via Dutra (BR-116) northeast through Volta Redonda and Petropolis",
			origin: "São Paulo", dest: "Rio de Janeiro",
			oLat: -23.5505, oLng: -46.6333, dLat: -22.9068, dLng: -43.1729,
			duration: 360,
			waypoints: []Waypoint{
				{-23.5505, -46.6333, "São Paulo"},
				{-22.5230, -44.0997, "Volta Redonda"},
				{-22.5050, -43.1789, "Petropolis"},
				{-22.9068, -43.1729, "Rio de Janeiro"},
			},
		},
		{
			id: "buenos-aires-rosario", name: "Buenos Aires → Rosario (A008)",
			desc:   "A008 north through Zarate and San Nicolas to Rosario",
			origin: "Buenos Aires", dest: "Rosario",
			oLat: -34.6037, oLng: -58.3816, dLat: -32.9442, dLng: -60.6505,
			duration: 180,
			waypoints: []Waypoint{
				{-34.6037, -58.3816, "Buenos Aires"},
				{-34.0983, -59.0246, "Zarate"},
				{-33.3369, -60.2117, "San Nicolas"},
				{-32.9442, -60.6505, "Rosario"},
			},
		},
	}

	for _, r := range routes {
		_, err := db.Exec(ctx, `
			INSERT INTO predefined_routes
				(route_id, name, description, origin_name, destination_name,
				 origin_lat, origin_lng, destination_lat, destination_lng,
				 estimated_duration_minutes)
			VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)
			ON CONFLICT (route_id) DO NOTHING
		`, r.id, r.name, r.desc, r.origin, r.dest,
			r.oLat, r.oLng, r.dLat, r.dLng, r.duration)
		if err != nil {
			return fmt.Errorf("seed route %s: %w", r.id, err)
		}

		for i, wp := range r.waypoints {
			// Deterministic ID so ON CONFLICT (id) DO NOTHING is idempotent.
			wpID := fmt.Sprintf("%s-%d", r.id, i)
			_, err := db.Exec(ctx, `
				INSERT INTO route_waypoints
					(id, route_id, sequence_order, lat, lng, location_name)
				VALUES ($1, $2, $3, $4, $5, $6)
				ON CONFLICT (id) DO NOTHING
			`, wpID, r.id, i, wp.Lat, wp.Lng, wp.Name)
			if err != nil {
				return fmt.Errorf("seed waypoint %s[%d]: %w", r.id, i, err)
			}
		}
	}
	log.Printf("Predefined routes seeded (%d routes)", len(routes))
	return nil
}

// loadRouteWaypoints returns ordered waypoints for a route_id, or nil if not found.
func loadRouteWaypoints(ctx context.Context, routeID string) ([]Waypoint, error) {
	rows, err := db.Query(ctx, `
		SELECT lat, lng, location_name
		FROM route_waypoints
		WHERE route_id = $1
		ORDER BY sequence_order ASC
	`, routeID)
	if err != nil {
		return nil, err
	}
	defer rows.Close()

	var wps []Waypoint
	for rows.Next() {
		var wp Waypoint
		if err := rows.Scan(&wp.Lat, &wp.Lng, &wp.Name); err != nil {
			return nil, err
		}
		wps = append(wps, wp)
	}
	return wps, rows.Err()
}

// listAllRoutes returns all predefined routes with their waypoints.
func listAllRoutes(ctx context.Context) ([]RouteInfo, error) {
	rows, err := db.Query(ctx, `
		SELECT route_id, name, description, origin_name, destination_name,
		       origin_lat, origin_lng, destination_lat, destination_lng,
		       estimated_duration_minutes
		FROM predefined_routes
		ORDER BY name ASC
	`)
	if err != nil {
		return nil, err
	}
	defer rows.Close()

	var routes []RouteInfo
	for rows.Next() {
		var ri RouteInfo
		if err := rows.Scan(
			&ri.RouteID, &ri.Name, &ri.Description,
			&ri.OriginName, &ri.DestinationName,
			&ri.OriginLat, &ri.OriginLng,
			&ri.DestinationLat, &ri.DestinationLng,
			&ri.EstimatedDurationMinutes,
		); err != nil {
			return nil, err
		}
		wps, err := loadRouteWaypoints(ctx, ri.RouteID)
		if err != nil {
			return nil, err
		}
		ri.Waypoints = wps
		routes = append(routes, ri)
	}
	if routes == nil {
		routes = []RouteInfo{}
	}
	return routes, rows.Err()
}

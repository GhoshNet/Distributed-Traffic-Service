# ============================================================
# config.py — Global configuration constants for GDTS
# ============================================================

SYSTEM_NAME = "Globally Distributed Traffic Service (GDTS)"
VERSION = "1.0.0"

# --- Peer Discovery ---
DISCOVERY_PORT = 5001          # UDP broadcast port
DISCOVERY_INTERVAL = 5         # seconds between broadcasts

# --- REST API ---
API_PORT_START = 6000          # first port to try; increments if occupied
REQUEST_TIMEOUT = 4            # seconds for outgoing HTTP calls

# --- Health Monitoring ---
HEARTBEAT_INTERVAL = 3         # seconds between heartbeat sweeps
SUSPECT_THRESHOLD = 3          # consecutive failures → SUSPECT
DEAD_THRESHOLD = 6             # consecutive failures → DEAD

# --- Distributed Coordination ---
TWO_PC_TIMEOUT = 5             # seconds to wait for 2PC PREPARE response
REPLICATION_INTERVAL = 15      # seconds between full replication cycles

# --- Road / Booking ---
MAX_ROAD_CAPACITY = 5          # max simultaneous bookings per road segment
BOOKING_CONFLICT_WINDOW_MIN = 30  # minutes either side to check for conflicts

# --- Data directory ---
DATA_DIR = "./data"

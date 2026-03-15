package main

import (
	"encoding/json"
	"net/http"
	"strconv"
	"time"

	"github.com/gorilla/websocket"
)

var upgrader = websocket.Upgrader{
	CheckOrigin: func(r *http.Request) bool { return true },
}

func healthHandler(w http.ResponseWriter, r *http.Request) {
	w.Header().Set("Content-Type", "application/json")
	json.NewEncoder(w).Encode(map[string]interface{}{
		"status":    "healthy",
		"service":   "notification-service",
		"timestamp": time.Now().UTC().Format(time.RFC3339),
		"version":   "1.0.0",
	})
}

func notificationsHandler(w http.ResponseWriter, r *http.Request) {
	token := r.URL.Query().Get("token")
	if token == "" {
		http.Error(w, `{"detail":"token required"}`, http.StatusUnauthorized)
		return
	}
	userID, err := decodeToken(token)
	if err != nil {
		http.Error(w, `{"detail":"Invalid token"}`, http.StatusUnauthorized)
		return
	}

	limit := 20
	if l := r.URL.Query().Get("limit"); l != "" {
		if n, err := strconv.Atoi(l); err == nil && n >= 1 && n <= 50 {
			limit = n
		}
	}

	notifications := getNotifications(userID, limit)
	w.Header().Set("Content-Type", "application/json")
	json.NewEncoder(w).Encode(map[string]interface{}{
		"notifications": notifications,
		"count":         len(notifications),
	})
}

func wsHandler(w http.ResponseWriter, r *http.Request) {
	token := r.URL.Query().Get("token")
	userID, err := decodeToken(token)
	if err != nil {
		// Upgrade so we can send the proper close code 4001, matching Python behaviour.
		conn, upgradeErr := upgrader.Upgrade(w, r, nil)
		if upgradeErr == nil {
			conn.WriteMessage(websocket.CloseMessage,
				websocket.FormatCloseMessage(4001, "Invalid token"))
			conn.Close()
		}
		return
	}

	conn, err := upgrader.Upgrade(w, r, nil)
	if err != nil {
		return
	}

	registerWS(userID, conn)
	defer func() {
		unregisterWS(userID, conn)
		conn.Close()
	}()

	for {
		_, msg, err := conn.ReadMessage()
		if err != nil {
			break
		}
		if string(msg) == "ping" {
			if err := conn.WriteMessage(websocket.TextMessage, []byte("pong")); err != nil {
				break
			}
		}
	}
}

# ============================================================
# services/health_monitor.py — SERVICE 5: Heartbeat & failure detection
# ============================================================
import threading
import time

import requests

import config
from utils.logger import log


class HealthMonitorService:
    """
    Periodically pings every known peer.
    Tracks consecutive failures and advances peer status:
        ALIVE → SUSPECT (after SUSPECT_THRESHOLD misses)
               → DEAD   (after DEAD_THRESHOLD misses)
    On recovery (SUSPECT/DEAD node responds): triggers replication sync.
    Also switches node into LOCAL_ONLY mode when < 50 % of peers are reachable.
    """

    def __init__(self, node_state):
        self.state = node_state
        self._running = False
        log("HEALTH", "Health monitor service initialised")

    def start(self):
        self._running = True
        t = threading.Thread(target=self._loop, daemon=True, name="health-monitor")
        t.start()
        log("HEALTH",
            f"❤️  Heartbeat started  interval={config.HEARTBEAT_INTERVAL}s  "
            f"suspect_at={config.SUSPECT_THRESHOLD}  dead_at={config.DEAD_THRESHOLD}")

    def stop(self):
        self._running = False

    # ------------------------------------------------------------------
    def _loop(self):
        while self._running:
            if not self.state.failure_simulated:
                self._sweep()
            time.sleep(config.HEARTBEAT_INTERVAL)

    def _sweep(self):
        db = self.state.db
        peers = db.get_all_peers()
        if not peers:
            return

        alive_count = 0

        for peer in peers:
            rname = peer["region_name"]
            url   = f"http://{peer['host']}:{peer['port']}/api/health/ping"
            prev  = peer["status"]
            fails = peer.get("consecutive_failures", 0)

            try:
                resp = requests.get(url, timeout=2)
                if resp.status_code == 200:
                    alive_count += 1
                    if prev in ("SUSPECT", "DEAD"):
                        log("HEALTH",
                            f"💚 Peer [{rname}] recovered  (was {prev})", "SUCCESS")
                        db.update_peer_status(rname, "ALIVE", increment_failures=False)
                        # Trigger replication sync
                        if self.state.replication_service:
                            threading.Thread(
                                target=self.state.replication_service.sync_from_peer,
                                args=(peer,), daemon=True,
                            ).start()
                    else:
                        db.update_peer_last_seen(rname)
                    continue
            except Exception:
                pass

            # ---- missed heartbeat ----
            new_fails = fails + 1
            if new_fails >= config.DEAD_THRESHOLD:
                new_status = "DEAD"
            elif new_fails >= config.SUSPECT_THRESHOLD:
                new_status = "SUSPECT"
            else:
                new_status = prev

            db.update_peer_status(rname, new_status, increment_failures=True)

            if new_status != prev:
                log("HEALTH",
                    f"{'💀' if new_status == 'DEAD' else '⚠️ '} "
                    f"Peer [{rname}] → {new_status}  (fails={new_fails})",
                    "ERROR" if new_status == "DEAD" else "WARN")
                db.log_event("PEER_STATUS_CHANGE",
                             {"region": rname, "status": new_status, "fails": new_fails})

        # Graceful-degradation check
        total = len(peers)
        if total > 0:
            ratio = alive_count / total
            if ratio < 0.5 and not self.state.local_only_mode:
                self.state.local_only_mode = True
                log("HEALTH",
                    f"🔴 GRACEFUL DEGRADATION: only {alive_count}/{total} peers reachable → LOCAL ONLY mode",
                    "ERROR")
                db.log_event("GRACEFUL_DEGRADATION",
                             {"alive": alive_count, "total": total})
            elif ratio >= 0.5 and self.state.local_only_mode:
                self.state.local_only_mode = False
                log("HEALTH",
                    f"🟢 Enough peers back → exiting LOCAL ONLY mode  ({alive_count}/{total})",
                    "SUCCESS")

# Adversarial Viva Q&A — CS7NS6 Exercise 2

> **How to use this document.** Every section ends with a cheat sheet. Skim the cheat sheets first. The body is for deep prep — every Q is phrased the way a hostile examiner would ask it, answered with file:line evidence, and followed by the weak-student trap and the likely drill-down.

---

## Table of Contents

1. [Report vs. Reality — Cross-cutting Mismatches](#report-vs-reality--cross-cutting-mismatches)
2. [User Service](#user-service--parth-deshmukh)
3. [Journey Service](#journey-service--tanmay-ghosh)
4. [Conflict Service](#conflict-service--sneha-meto)
5. [Notification Service](#notification-service--aditya-kumar-singh)
6. [Enforcement Service](#enforcement-service--saurabh-deshmukh)
7. [Analytics Service](#analytics-service--sai-eeshwar-divaakar)
8. [System-wide 10-Minute Cheat Sheet](#system-wide-10-minute-cheat-sheet)

---

# Report vs. Reality — Cross-cutting Mismatches

**These are the FIRST things to prepare for.** The examiner will spot them immediately. Do not pretend they don't exist — own them, explain the gap, and have the "what I would fix" ready.

| # | Report claims (section) | Code reality (file:line) | Severity |
|---|---|---|---|
| M1 | **Sec 4.6, Conflict Service:** "SELECT FOR UPDATE … guarantees that two concurrent booking requests targeting the same cell and time slot cannot both succeed when only one slot remains." | [conflict-service/service.go:330-339](conflict-service/service.go#L330-L339) — the `FOR UPDATE` is inside a predicate `current_bookings >= max_capacity`. When the cell is **not yet full**, zero rows return, zero row locks taken. Correctness is still preserved by Postgres SSI (SERIALIZABLE), not by the row lock the report advertises. | **HIGH** |
| M2 | **Sec 4.6, Conflict Service:** "the second transaction either deadlocks and retries or sees the first reservation committed" | No retry loop on SQLSTATE `40001` anywhere in [conflict-service/service.go](conflict-service/service.go) or [journey-service/app/saga.py](journey-service/app/saga.py). Serialization failures propagate up as internal errors → REJECTED with message "Internal error during booking. Please retry." | **HIGH** |
| M3 | **Sec 4.8, Consistent-Hash Sharding:** "The node where shard == 0 is PRIMARY… forward to shard-0 node" (diagram Fig 6) | Code does **not** forward writes. [user-service/app/replication.py:104-119](user-service/app/replication.py#L104-L119) — `shard_for_email()` only returns `(shard_id, home_label)` and logs it. Every node accepts every write locally. The distributed lock, not the sharding, is what prevents conflicts. | **HIGH** |
| M4 | **Sec 4.6.5, Enforcement:** "populates the cache and returns" on fallback path | [enforcement-service/app/service.py:82-98](enforcement-service/app/service.py#L82-L98) — fallback path in `verify_by_vehicle` does NOT write to Redis after fetching from journey-service. Only `verify_by_license` caches the license→user_id mapping. | **MEDIUM** |
| M5 | **Sec 2.2, NFR table:** "Booking latency <400ms p95 under normal load" and "<250ms on slim stack" | No load-test artefact in repo. The number comes from ad-hoc observation on one laptop, not a measurement with percentile tracking. | **MEDIUM** |
| M6 | **Sec 4.8.2, Sharding diagram:** "Consistent-hash sharding for routes" | [conflict-service/main.go:33-41](conflict-service/main.go#L33-L41) registers routes into a shard table and [conflict-service/main.go:81](conflict-service/main.go#L81) exposes `/internal/shard/info`, but there is no write-routing enforcement on the conflict check path. Every node reserves every slot locally regardless of shard ownership. | **MEDIUM** |
| M7 | **Sec 4.5, Idempotency:** "duplicate booking requests … return the cached result without re-running the saga" | [journey-service/app/service.py:43-52](journey-service/app/service.py#L43-L52) — only stores `journey_id`, then re-fetches via `get_journey`. The result is reconstructed from the DB, not from a stored response cache. Behaviourally equivalent but the word "cached" is slightly misleading. | **LOW** |
| M8 | **Table 5, Testing:** "Concurrent booking storm (10 parallel) — Exactly 1 CONFIRMED, 9 REJECTED" | Actually correct **only if** the 10 bookings share the same driver or vehicle (driver/vehicle overlap check fires cleanly). If 10 different drivers on different vehicles race for the same road cell with capacity 1, you would see 1 CONFIRMED plus a mix of capacity-REJECT and SSI 40001 "Internal error" — not 9 clean "REJECTED"s. The test suite uses the same-driver setup. | **LOW** |
| M9 | **Sec 4.2, Journey Service:** "Partition detector probes … every 5 seconds" | [shared/partition.py](shared/partition.py) — matches (PROBE_INTERVAL=5.0). ✅ No mismatch. | — |
| M10 | **Sec 5.4.5, "Redis Sentinel promotes a replica within ≈15s"** | Matches `down-after-milliseconds 5000` + `failover-timeout 10000` from docker-compose. ✅ | — |
| M11 | **Sec 5.6, Member Failure Detection:** "Each node probes every registered peer's /health endpoint every 10s" | [shared/health_monitor.py](shared/health_monitor.py) — `HEARTBEAT_INTERVAL=10`. ✅ | — |
| M12 | **Sec 4.6.4, Notification:** "notification history … 7-day TTL and a maximum of 50 entries per user" | Need to verify in `notification-service/store.go` — claim is consistent with report structure (`LPUSH`+`LTRIM 0 49`+`EXPIRE 7d`). | ✅ likely |
| M13 | **Sec 3.2, Fault Matrix:** "Full-node kill … browser resilientFetch falls over transparently" | [user-service/app/main.py:85-94](user-service/app/main.py#L85-L94) — middleware returns 503 for all paths except `/health` + simulate endpoints. Client-side logic in `frontend/src/utils/resilientFetch.ts` handles failover. Works as described. ✅ | — |
| M14 | **Sec 4.3, Journey Service:** Circuit breaker failure threshold = 3 | [shared/circuit_breaker.py:70](shared/circuit_breaker.py#L70) — `failure_threshold: int = 3` default. ✅ | — |
| M15 | **Sec 4.6.6, Analytics:** "immutable audit trail" | [analytics-service/consumer.go:215-224](analytics-service/consumer.go#L215-L224) — `event_logs` table is INSERT-only in code, but there is no enforcement (no trigger, no HMAC chain). The examiner will ask "how do you know it's immutable?". Limitations section acknowledges the HMAC chain was not completed. | **MEDIUM** |
| M16 | **Sec 4.6.1, User Service:** "routes reads to replica, writes to primary" via "separate session pools" | User-service/app/database.py exposes two engines; routes select based on HTTP method. The examiner will ask: what stops a read-your-own-write bug on the login-after-register flow? Answer: registration uses primary for the post-insert select, login has no recent write. | LOW |

---

# User Service — Parth Deshmukh

**Stack:** Python 3.11, FastAPI, SQLAlchemy async, PostgreSQL primary+replica, Redis (DB 3 for distributed lock), bcrypt, PyJWT.
**Key files:** [user-service/app/main.py](user-service/app/main.py), [service.py](user-service/app/service.py), [replication.py](user-service/app/replication.py), [database.py](user-service/app/database.py).

## Category A — Why This Over That? (Technology & Middleware)

### A1. Why Python + FastAPI for a high-concurrency auth service? Go or Rust would give you 10× the throughput.

**What they're really testing:** Whether you chose the language on merit or by habit.
**Strong answer:** Authentication is not CPU-bound — it's bcrypt-bound (deliberately slow, ~100ms per hash) and I/O-bound on the Postgres round-trip. Python with FastAPI gives us async I/O on top of uvloop, so concurrent requests are multiplexed on one event loop. The bottleneck is bcrypt work factor 12, not the interpreter. On the slim stack we measured registration in the 150-250ms range, dominated by bcrypt. A Go rewrite would cut ~10ms off HTTP parsing and save nothing on the hash. Python also lets us share `shared/` modules (partition manager, circuit breaker, schemas) with Journey and Enforcement — three services, one codebase.
**Trap to avoid:** Saying "Python was faster to write." That's true but not the right answer — the examiner wants to know you thought about throughput *and decided it didn't matter*.
**Follow-up:** "So prove bcrypt is the bottleneck." → Run `time python -c "from passlib.context import CryptContext; c=CryptContext(schemes=['bcrypt']); c.hash('x'*20)"` — it returns in ~100-250ms on a laptop. Show the Postgres insert is <5ms by comparison.

### A2. Why PostgreSQL instead of Cassandra or DynamoDB? You claim "global scale" — Postgres does not horizontally scale.

**What they're really testing:** Understanding of the database trade-off space.
**Strong answer:** We have three properties we need: (a) strong per-row consistency on email uniqueness (unique index enforcement), (b) streaming replication for read scale-out (NFR table row "Scalability"), and (c) joins between users and vehicles. Cassandra gives up (a) and (c). DynamoDB global tables give (b) cheaply but only with last-write-wins conflict resolution, which breaks the email-uniqueness guarantee across regions. Postgres gives all three at our scale (167 bookings/s peak from FR section — tiny for Postgres). The horizontal-scale argument only bites above ~10K writes/s; below that, a single primary with read replicas is the boring correct answer. We compensate for the single-primary bottleneck at cross-node level with the Redlock-style 2-phase lock, not by picking a different database.
**Trap to avoid:** Claiming Postgres "scales globally." It does not. Admit it's bounded — and defend why the bound is above your target.
**Follow-up:** "What happens at 10M users?" → Shard by email hash at the application level, which is what the consistent-hash sharding in [replication.py:104](user-service/app/replication.py#L104) is a step towards (currently observability-only; the hook is there to become write-routing).

### A3. Why bcrypt over Argon2id? Argon2 won the 2015 password hashing competition.

**What they're really testing:** Crypto currency awareness.
**Strong answer:** Argon2id is objectively stronger against GPU attacks because of its memory-hardness. We used bcrypt because `passlib`'s bcrypt integration is zero-config in FastAPI ecosystems and has the most mature Python binding. For a production traffic-management service I would migrate to Argon2id with memory cost ~64MB. For the scope of this exercise — demonstrating distributed systems patterns, not password-cracking resistance — bcrypt at work factor 12 still costs an attacker ~$100 per hash on commodity GPUs. Not negligent, but not best-in-class.
**Trap to avoid:** Saying "bcrypt is fine" without acknowledging Argon2id exists.
**Follow-up:** "What work factor?" → Default passlib bcrypt = 12. Show [service.py:25](user-service/app/service.py#L25).

### A4. Why Redis for the distributed lock instead of Postgres advisory locks? You already have Postgres.

**What they're really testing:** Understanding Redlock's guarantees vs native pg_advisory_xact_lock.
**Strong answer:** Postgres advisory locks are per-database, not cross-node. A `pg_advisory_xact_lock(hash('email'))` on Laptop A's Postgres does nothing to Laptop B's Postgres — they're independent instances. Redis can run as a shared Sentinel-backed instance *or* we can call it across peers explicitly, which is what our 2-phase protocol does: local Redis SETNX + POST `/internal/users/lock` to every peer ([replication.py:150-213](user-service/app/replication.py#L150-L213)). The lock needs to span Postgres instances, and the only shared coordinator we have is the cross-node HTTP path plus Redis for the local atomicity primitive. Postgres advisory locks would have forced us to designate a single "lock server" Postgres, which re-creates the single point of failure we're trying to avoid.
**Trap to avoid:** Conflating Redlock safety arguments (Martin Kleppmann's critique) with "Redlock is wrong." The critique is about fencing tokens for long critical sections; our critical section is a single INSERT and we have TTL = 15s as a safety net.
**Follow-up:** "Isn't Redlock unsafe per Kleppmann?" → Yes for long critical sections with GC pauses. Ours is a registration INSERT (<50ms), well under the 15s TTL. The only failure mode is "lock TTL expired mid-insert" which our DB unique index catches anyway.

### A5. Why JWT with a shared secret instead of RSA? You now have a symmetric key every node knows.

**What they're really testing:** JWT signature model awareness.
**Strong answer:** HMAC-SHA256 (HS256) with a shared secret lets any node validate a token without a key distribution mechanism — we set `JWT_SECRET` once in docker-compose.yml and every service reads it. RS256 would be cleaner (private key on user-service only, public key on every verifier) but would require a JWKS endpoint or file-based key distribution. For a 2-laptop demo the attack surface is the same: if you compromise one container you've read the env var. In production with untrusted verifier nodes I'd switch to RS256.
**Trap to avoid:** Saying "HS256 is fine" without acknowledging the key-leak blast radius is larger than RS256.
**Follow-up:** "Who signs, who verifies?" → User-service signs on login ([service.py:117-123](user-service/app/service.py#L117-L123)); all services verify via `shared/auth.py` using the same secret.

## Category B — Why Use This At All? (Necessity)

### B1. Why do you even have a distributed lock? Postgres has a UNIQUE index on email — let it enforce uniqueness.

**What they're really testing:** Understanding that a unique index is local to one database, not a cluster.
**Strong answer:** The UNIQUE index in [database.py](user-service/app/database.py) *does* enforce uniqueness on one Postgres instance — and we keep it as the safety net. But each laptop runs its own Postgres with its own unique index. If User A registers "alice@x.com" on Laptop A and User B registers the same email on Laptop B at the same moment, both local unique indices succeed — we end up with two rows for the same email across the cluster. The distributed lock is what prevents that *before* the local INSERTs run. In the lock failure case we still catch it at the local unique index via the IntegrityError handler in [service.py:80-87](user-service/app/service.py#L80-L87).
**Trap to avoid:** Saying you need the lock because the unique index doesn't work. It works — just not across Postgres instances.
**Follow-up:** "What if the lock acquires locally but a peer is unreachable?" → [replication.py:193-194](user-service/app/replication.py#L193-L194): we log a warning and proceed. This is the availability-biased choice. The catch-up sync + unique index at the remote node handle eventual convergence.

### B2. Why do you replicate users across nodes? Users are rarely read by peers — it's a storage waste.

**What they're really testing:** Rationale for active-active vs primary-only.
**Strong answer:** Two specific reasons. (1) JWT validation is stateless but *license lookup* is not — the enforcement service does `GET /api/users/license/{license_number}` which needs local access to the user table to keep enforcement latency <200ms. If enforcement on Laptop B had to call user-service on Laptop A, we'd add a network hop and a single point of failure. (2) Full-node failover: when Laptop A dies, the browser switches to Laptop B via `resilientFetch`, and the user session must continue working immediately (JWT + user row both available locally). Without replication, a full-node kill would log the user out.
**Trap to avoid:** Saying "it's good for reads." True but insufficient — the examiner wants to know *which* reads.
**Follow-up:** "So user records are in sync after failover?" → Eventually (5-min periodic sync + fire-and-forget push at write time [replication.py:236-255](user-service/app/replication.py#L236-L255)). For a 2-second window post-registration, a failover hit might miss the user — the peer will return 404 and the client retries.

### B3. Why maintain a vehicle table at all? Put vehicles inside the user JSON.

**What they're really testing:** Normalisation sanity.
**Strong answer:** Vehicles need their own unique constraint (registration plate must be unique globally) and their own lookup path (enforcement checks `/api/users/vehicles/verify/{registration}` without knowing the user). Embedding vehicles in user JSON would break those lookups and force a full table scan. Also, vehicles are mutable after user creation (users add/remove cars over time), so they must not be a part of the immutable user row.
**Trap to avoid:** Over-explaining normalisation theory. Three sentences: unique constraint, lookup path, mutability.
**Follow-up:** "Why is vehicle registration unique globally, not per-user?" → One registration plate belongs to exactly one car, one owner. The real-world invariant maps directly to the DB constraint.

### B4. Why have a /admin/simulate/fail endpoint? That's not a real failure, it's theatre.

**What they're really testing:** Whether you can justify simulation vs actual fault injection.
**Strong answer:** We need a *reproducible* failure for the demo and the test matrix. A real `kill -9` on the container would work for proving "it recovers," but it doesn't exercise the code path where a node *looks healthy from its own perspective but dead from the outside* (which is the hardest failure mode to reason about). The simulate endpoint sets `_node_failed=True` and installs middleware at [main.py:85-94](user-service/app/main.py#L85-L94) returning 503 on all paths except the recovery endpoint. From a peer's perspective this is indistinguishable from a crash; from inside the node you can still call `/admin/simulate/recover` to reverse it. For the viva, I can show both: simulated (reversible) and `docker kill` (real). Both exhibit the same client-side failover behaviour.
**Trap to avoid:** Claiming the simulate endpoint is a replacement for real crash testing. It's complementary.
**Follow-up:** "Does docker kill actually drop all in-flight connections?" → Yes, because the TCP socket is gone. The client sees connect-refused, `resilientFetch` advances to next peer.

### B5. Why separate "DRIVER" and "ENFORCEMENT_AGENT" roles? You could just have one flag.

**What they're really testing:** Role-based access design.
**Strong answer:** The two roles have disjoint permission sets. Drivers can call `/api/journeys/` but not `/api/enforcement/verify/*`. Agents can call `/api/enforcement/verify/*` but have no driver API at all. Encoding this as roles in the JWT ([service.py:117-123](user-service/app/service.py#L117-L123)) lets `shared/auth.py`'s `require_role()` dependency guard every endpoint uniformly. A single flag would force every route to know about the flag; roles in the JWT keep the check at the FastAPI dependency layer. It also matches real-world trust boundaries — the enforcement side is a different UX and client app.
**Trap to avoid:** Conflating role-based access control with organisational structure. Stay technical.
**Follow-up:** "What if an agent also needs to book their personal car?" → They register a second account with role DRIVER. Or we add a multi-role JWT claim. We chose single-role per account for demo simplicity.

## Category C — Why Haven't You Used X? (Missing Techniques)

### C1. Why no token revocation list? I log out, the token is still valid.

**What they're really testing:** Auth hygiene.
**Strong answer:** Correct — we do not maintain a denylist. The acknowledged limitation is in Sec 3.4 of the report: "Revoked JWT tokens remain valid until their natural expiry." Adding a denylist would require every verifier to check a shared Redis set on every request, which defeats the stateless-JWT design. The production fix is short-lived access tokens (5 min) + refresh tokens stored in a server-side session, so revocation is done on the refresh side while access tokens just expire out. We chose long-lived tokens (typical JWT expiry ~1h) because session-survives-failover is part of the demo and short tokens would require a refresh endpoint to also failover, doubling the resilience surface. This is a deliberate demo-simplicity trade-off.
**Trap to avoid:** Saying "we don't need it." You do need it in production — say you chose to defer it and explain the preferred production fix.
**Follow-up:** "So the JWT secret leak is catastrophic?" → Yes — every outstanding token is forged-able. That's why in production we'd use RS256 with a private key on user-service only.

### C2. Why no password policy, MFA, email verification, rate-limit on login attempts?

**What they're really testing:** Completeness of auth surface.
**Strong answer:** Email verification and MFA are out of scope for the exercise — distributed systems marks are about consistency/fault tolerance, not account security. We *do* have login rate limiting: [api-gateway/nginx.conf:34](api-gateway/nginx.conf#L34) defines `limit_req_zone` for auth at 5 r/s with burst 10, applied to `/api/users/login`. Password policy is enforced only at the schema level (Pydantic min_length=6, which is weak). For the viva I'll acknowledge these as non-goals rather than oversights, and note the login rate limit is the concrete defence against credential stuffing.
**Trap to avoid:** Pretending MFA is somehow implemented.
**Follow-up:** "Show the login rate limit at work." → `curl` 6 times in 1 second to `/api/users/login`; the 6th gets HTTP 503 from nginx.

### C3. Why no connection pooling limit? A burst of registrations will exhaust Postgres connections.

**What they're really testing:** Resource bulkhead awareness.
**Strong answer:** We use SQLAlchemy async with `create_async_engine`, which has a default pool size of 5 and max_overflow 10, so 15 concurrent connections per pod. [database.py](user-service/app/database.py) uses the default — we did not raise or lower it. With Postgres `max_connections = 100` (default) and 6 services × 15 connections = 90, we're within the ceiling. Under sustained 10× load we would need to either raise the pool or add a connection proxy (PgBouncer). We tested with the slim stack at ~50 r/s registration rate without pool exhaustion.
**Trap to avoid:** Saying "we have enough connections" without stating the numbers.
**Follow-up:** "What if Postgres is at 100 connections?" → New connections return "too many clients already"; the request gets 503 via FastAPI exception handler. Failure mode is fail-fast, not hang.

### C4. Why no quorum reads? You replicate users to every peer but a write could still be uncommitted somewhere.

**What they're really testing:** Understanding quorum systems.
**Strong answer:** We use active-active with fire-and-forget replication, not quorum. Reads are served from whichever node received the request — so reading after a write has a bounded staleness window equal to the replication RTT (<200ms on LAN, per Sec 5 test table). Quorum reads would require every read to query W>N/2 nodes and reconcile, which is Dynamo-style eventual consistency on top — more complexity without a clear win for the use case (users rarely read their own profile twice in 200ms). The one flow that does need read-your-writes is the JWT-sign-after-insert path, and that reads from the same local Postgres so there's no staleness.
**Trap to avoid:** Implying we have quorum. We do not — it's leader-per-shard conceptually but the leader only logs its role, no actual gate.
**Follow-up:** "So what consistency level would you claim?" → Per-node strong consistency; cross-node eventual consistency with bounded staleness (≤200ms LAN, ≤5min on partition).

### C5. Why no circuit breaker on the peer lock calls? A dead peer will freeze every registration.

**What they're really testing:** Catching the blast-radius gap in the lock path.
**Strong answer:** Good catch — we have per-peer httpx timeouts (3 seconds, [replication.py:177](user-service/app/replication.py#L177)) but no circuit breaker. The current behaviour: if one peer is dead, every registration pays a 3-second timeout penalty. With 2 peers that's 6 seconds of added latency on every call. The production fix is `shared/circuit_breaker.py` wrapping the httpx POST — identical pattern to what journey-service does for conflict-service. I added it on the journey side but didn't replicate it here. This is a real limitation I should flag before the examiner finds it.
**Trap to avoid:** Pretending the timeout is the same as a circuit breaker. Timeout caps the wait; breaker short-circuits the call entirely.
**Follow-up:** "So what's the blast radius if all peers die?" → Each registration takes 3 × num_dead_peers seconds. With 2 dead peers, 6-second latency per registration, obvious tail-latency alarm.

## Category D — Bottleneck Hunting

### D1. bcrypt is a CPU-bound bottleneck at ~100ms per request. Defend it.

**What they're really testing:** Whether you can quantify the limit.
**Strong answer:** On a single vCPU, bcrypt at cost 12 limits registration throughput to 1/0.1 = 10 registrations/s. With uvicorn running 4 workers (default), we get ~40 reg/s per pod. Our target is 50 bookings/s peak, and registration is far less frequent than booking (maybe 1/100 of book rate), so 40 reg/s is 40× our need. If registration became a hot path we would either (a) lower bcrypt cost (trading security for throughput — not recommended), (b) horizontally scale user-service pods, or (c) offload the hash computation to a worker queue. None of these are needed for the demo.
**Trap to avoid:** Saying "async hides the CPU cost." It doesn't — Python's GIL serialises CPU work even in asyncio.
**Follow-up:** "What's your uvicorn worker count?" → Default 1 in docker-compose (we haven't overridden). So actually 10 reg/s per pod, not 40. Still above target.

### D2. Every login does a bcrypt verify. That's a DoS amplifier — attacker spams `/login` and burns CPU.

**What they're really testing:** DoS awareness.
**Strong answer:** nginx rate limit on `/api/users/login` at 5 r/s ([nginx.conf:34](api-gateway/nginx.conf#L34)) caps attacker throughput to 5 × num-IPs. Each blocked request never reaches user-service so no bcrypt runs. Without the rate limit, a single attacker could burn one CPU core at 10 logins/s. The rate limit is our primary defence; bcrypt cost is the secondary layer.
**Trap to avoid:** Claiming bcrypt cost is a DoS defence. It's the opposite — high cost amplifies DoS unless you rate limit first.
**Follow-up:** "What if the attacker uses 1000 IPs?" → 5000 login attempts/s distributed. We have two nginx pods → 10k/s upstream. bcrypt at 10 verifies/s per worker → saturation. The real fix is a WAF or global IP tracking (Fail2ban style), which we don't implement.

### D3. The 2-phase distributed lock serialises every registration across all nodes. That's a hard global bottleneck.

**What they're really testing:** Whether you acknowledge the serialisation cost.
**Strong answer:** Yes, every registration pays a 2-step network round trip to every peer. With 2 peers and LAN latency of ~10ms, the lock phase adds ~40ms to a ~200ms registration. Throughput is bounded by `1 / (bcrypt_cost + lock_rtt + insert_cost)` ≈ 1/250ms ≈ 4 reg/s. We're below our target, barely. For scale we could (a) hash-shard by email so only the home node acquires the lock ([replication.py:104](user-service/app/replication.py#L104) already computes the shard), avoiding cross-node latency for most registrations, or (b) replace the lock with a leader-per-shard model where only the home shard writes. Both fixes are on the roadmap but not implemented.
**Trap to avoid:** Saying "the lock is fast because Redis is fast." Redis is fast but the HTTP POST to each peer is not.
**Follow-up:** "So 4 reg/s is your ceiling on registrations, cluster-wide?" → Yes, with current implementation. Confirms registration is not a hot path; journey booking is.

### D4. Postgres primary is a single point of failure for writes. Your "replica" is read-only.

**What they're really testing:** Whether you understand physical replication vs failover.
**Strong answer:** Correct. Each node's Postgres primary is a SPOF for writes on that node's shard of users. On primary failure, writes to that node block. Our mitigation is cross-node: if Laptop A's primary dies, the browser `resilientFetch` sends new registration traffic to Laptop B, which has its own primary and its own copy of all users (via replication). The "replica" in our docker-compose is a hot standby for *reads*, not an automatic-failover target. Postgres streaming replication does not auto-promote. For automatic in-node failover you need Patroni + etcd or pg_auto_failover, which we did not integrate.
**Trap to avoid:** Claiming the replica can "take over." It cannot — it's read-only.
**Follow-up:** "How long does cross-node failover take?" → Browser-side: as long as it takes `resilientFetch` to hit a timeout on the primary (httpx default ~10s) and retry. Faster if we shrink the client timeout.

### D5. Your read/write split is application-layer. What stops a developer accidentally reading from primary?

**What they're really testing:** Discipline around replica routing.
**Strong answer:** Nothing but code review. [database.py](user-service/app/database.py) exposes two session factories, and each FastAPI route's `Depends()` picks one. A new developer writing a new route could accidentally use the primary session for a read. We don't have a linter or policy enforcement. In production I'd add a middleware that inspects SQL statements and logs reads going to primary. For the demo we accept the maintenance risk.
**Trap to avoid:** Over-selling "we have read/write separation" as if it were automatic.
**Follow-up:** "What's the lag between primary and replica?" → Visible at [analytics-service `/api/analytics/replica-lag`](analytics-service/main.go#L49). Typically <50ms on LAN. Long writes can push it to seconds.

## Category E — Limitations

### E1. Your consistency model for registration is neither strong nor eventual. Which is it?

**Strong answer:** Per-node strong (UNIQUE index) + cross-node eventual (replication) + opportunistic cross-node strong (distributed lock when all peers reachable). We pick strong consistency *when we can* — the lock succeeds on the happy path — and fall back to eventual when a peer is unreachable ([replication.py:193-194](user-service/app/replication.py#L193-L194) logs the warning and proceeds). The safety net is that if two nodes accept the same email during a partition, the periodic sync + unique index on the receiving node will reject one of the replications on merge, producing a log warning but not a data corruption. In the CAP triangle we chose A over C during a partition.
**Framing:** "We are CP when the network permits and AP when it doesn't — with convergence semantics after heal."
**Follow-up:** "What does the user see on duplicate?" → The second registration will have been accepted locally but its replicated INSERT on the merge target fails at the unique index. Both users exist in their own home node's view. We do not currently reconcile (pick a winner). This is an unaddressed gap and should be framed as "future work: conflict resolution with LWW or explicit user merge."

### E2. The Redlock-style 2-phase lock is vulnerable to stop-the-world pauses per Kleppmann's critique.

**Strong answer:** Acknowledged. Kleppmann's critique applies to long critical sections: if the lock-holder pauses for 20s (GC, swap), the TTL expires, another client acquires, and when the first wakes up it believes it still holds the lock and writes. Our critical section is a single INSERT (<50ms) and our TTL is 15s, so there's a 300× safety margin. For bullet-proof correctness we'd add fencing tokens: the lock returns a monotonically increasing token, every DB write includes it, and the storage rejects writes with a stale token. We did not implement this; we accept the theoretical vulnerability for a demo-scale system.
**Framing:** "Practically safe, theoretically imperfect — production fix is fencing tokens."

### E3. Consistent-hash sharding is observability-only. You have no actual write routing.

**Strong answer:** Correct — this is one of the M3 mismatches I flagged at the top. The `shard_for_email()` function returns `(shard_id, home_node)` but nothing in the registration path checks it or forwards writes. Every node writes locally. The rationale in the report ("sharding governs write authority") is aspirational — the infrastructure is in place (the hash function, the logging) but the enforcement is not wired up. Honest framing: "We laid the sharding groundwork and surfaced it in the activity feed, but the write-routing leg is future work. The distributed lock is what *actually* prevents concurrent registrations today." A hostile examiner will press on this; do not try to hide it.
**Framing:** "Observability complete, enforcement pending."

### E4. You replicate password_hash across all nodes. A compromise of one laptop leaks all hashes.

**Strong answer:** True. Every laptop stores the full bcrypt hash table via [replication.py:310-318](user-service/app/replication.py#L310-L318). bcrypt cost 12 means an attacker with all hashes still has to spend ~100ms per guess per hash on GPU — painful but not impossible. The mitigations are (a) network isolation of the cluster (we rely on the trusted-internal-network assumption in Sec 2.3), (b) bcrypt cost itself, and (c) no plaintext leak, no hash-only DB dumps. A real production system would encrypt hashes at rest using a KMS-backed key; we do not.
**Framing:** "Trusted network assumption + bcrypt cost is our defence; KMS encryption at rest is the production upgrade."

### E5. What's your scalability ceiling for user-service?

**Strong answer:** Bounded by three things: (a) registration throughput ~4 reg/s cluster-wide (bcrypt + lock round trip), (b) login throughput ~10 verifies/s per pod (bcrypt only), (c) Postgres connections ~100 per node. At 10× the target load (500 bookings/s → maybe 5 reg/s), we hit the registration ceiling. Fixes in order of cost: lower bcrypt cost (bad), increase pods (fine, scales linearly), eliminate lock for registrations that are already sharded to the home node only (best).
**Framing:** "Registration is the ceiling at ~4 r/s, login is ~10 r/s per pod, both below projected load by 10×."

## Category F — Failure Scenario Grilling

### F1. Postgres primary on this node dies mid-registration.

**What happens:** The registration `db.commit()` raises `OperationalError`. The `try/except` in [service.py:88-90](user-service/app/service.py#L88-L90) catches it, rolls back the transaction, and raises `RuntimeError("A problem occurred...")`. The client gets HTTP 500. The distributed lock is released via the `finally` path (actually — wait, there is no finally, we only release on the happy path). **Bug:** if the lock was acquired but the commit failed, the lock leaks until TTL expiry (15s). During that 15s, retries with the same email block. This is a real defect I should note.
**Framing:** "Lock holds for up to 15s after a commit failure — self-heals via TTL but is a small bug."
**Follow-up:** "Why didn't you use `finally`?" → Oversight. Fix is trivial: wrap the whole `register` in `try/finally` with `release_distributed_lock` in the finally block.

### F2. A peer user-service returns corrupt JSON from `/internal/users/all`.

**What happens:** [replication.py:290](user-service/app/replication.py#L290) `resp.json()` raises on bad JSON, the outer `except Exception` in [replication.py:347](user-service/app/replication.py#L347) catches it, logs a warning, returns. The sync silently skips the corrupt peer. Next periodic sync tries again. No crash, no partial apply.
**Framing:** "Best-effort sync — bad data is dropped, not applied."
**Follow-up:** "What if the corrupt data is a single user record mid-list?" → Unfortunately the `resp.json()` fails for the whole response, so we lose the whole batch. A streaming JSON parser would be more robust but we use `json.loads`.

### F3. User-service crashes mid-INSERT after the distributed lock was acquired on all peers.

**What happens:** The local insert is rolled back by Postgres (transaction never commits). The distributed lock on peers remains until TTL (15s). Peers will have logged the lock acquire. On restart, user-service is stateless so the crash leaves no bad state — but the peer locks force a 15s wait on any retry with the same email. This is acceptable because the alternative (no lock) risks a double registration during the restart window.
**Framing:** "Stateless crash with a 15s block on retry — correctness preserved by TTL."

### F4. Network partition isolates this laptop from all peers.

**What happens:** `acquire_distributed_lock` in [replication.py:193-194](user-service/app/replication.py#L193-L194) catches the httpx exception and logs "proceeding without it". The local lock is still acquired, the local INSERT proceeds. On the peer side, new registrations also proceed independently. After the partition heals, the 5-minute periodic sync runs and applies missing records; duplicates are caught by the local unique index and produce "already present" logs. The failure mode is: two users could register the same email on opposite sides of the partition, and post-heal each side still sees its own user. No automatic merge. This is the "split-brain" case.
**Framing:** "AP during partition: duplicates possible, rejections after heal, no auto-merge."
**Follow-up:** "So both sides see different versions of the truth?" → Yes. We documented this as a limitation.

### F5. An attacker uses a valid JWT from user A to call `/api/users/me` and somehow mutates user B's data.

**What happens:** Not possible with current routes. `/api/users/me` uses the JWT's `user_id` claim directly — there's no way to spoof without resigning with the secret. Impersonation requires the JWT secret, and we assume the trusted network. If the secret leaks, all tokens are forge-able — catastrophic, no denylist to save us.
**Framing:** "Secret confidentiality is the single trust anchor. No horizontal escalation within the app."

### F6. Clock skew between nodes exceeds 15 seconds — the distributed lock TTL expires before the local write commits.

**What happens:** Each node's Redis uses its own wall clock for TTL expiry. If Laptop A's clock is fast by 15s, its local Redis SETNX expires before Laptop B thinks the lock should be released. Symptoms: a stuck lock on B, a "lock free" on A. If another registration now comes in on A, it acquires fresh; when it pushes to B, B rejects (lock still held on B). Net effect: false rejection on A. No correctness bug (nothing bad committed), but a spurious "email in use" error to the user.
**Framing:** "Clock skew > TTL creates false-rejects, not corruption — no NTP integration in this exercise."

### F7. RabbitMQ is down when user-service publishes `user.registered`.

**What happens:** [shared/messaging.py](shared/messaging.py) `publish()` raises on broker down. User-service catches it and logs a warning — but continues (registration already committed). The user is registered but no `user.registered` event goes out, so analytics-service does not see the registration in its counters. On broker recovery, we do **not** have a user-side outbox, so the event is permanently lost. Note: only journey-service has the outbox pattern. User events are fire-and-forget.
**Framing:** "User events are best-effort; journey events are transactional. Analytics may under-count registrations after broker downtime."
**Follow-up:** "Should user events also use the outbox?" → In production yes. The trade-off here is that `user.registered` is analytics-only, no downstream consumer depends on it for correctness. Journey confirmations go to enforcement cache which *does* need correctness, so those get the outbox.

### F8. Duplicate `/api/users/register` POST from a retrying client.

**What happens:** The second request hits the distributed lock path. Local SETNX sees the lock (if still held) → 409. If the first request has completed and lock released, the second request's local INSERT hits the UNIQUE(email) index → IntegrityError → 409 via [service.py:80-87](user-service/app/service.py#L80-L87). Either way, deduplicated to a 409.
**Framing:** "Idempotent by unique index, not by idempotency key."

### F9. A slow replica lags behind primary by 10 seconds. A user registers then immediately fetches `/api/users/me`.

**What happens:** Registration writes to primary. `/api/users/me` reads from — actually, we need to verify which pool `/api/users/me` uses. If it uses the replica pool, the GET could return 404 until replication catches up. If it uses primary, no issue. Our routes use method-based selection; GET = replica. So yes, there's a 10-second read-your-writes gap on this flow. Mitigation is to hit primary for the post-register GET or to return the full user object from the register response itself (which we do — [service.py:94-101](user-service/app/service.py#L94-L101) returns the created user directly).
**Framing:** "Register returns the user; no subsequent GET needed in practice. If one is issued, staleness window equals replica lag."

### F10. Disk fills up on the primary Postgres.

**What happens:** Postgres enters "disk full" state, all INSERTs fail with "could not extend file". Our error handler at [service.py:88-90](user-service/app/service.py#L88-L90) catches the OperationalError and returns 500. The distributed lock leaks for 15s (same bug as F1). Existing users can still read (replica is fine, primary is still readable). Monitoring would catch this at the analytics-service replica-lag endpoint only indirectly (lag grows because WAL can't be trimmed).
**Framing:** "Writes fail fast; reads continue; disk alarm needed at infra layer."

## Category G — Requirements Defense

### G1. You claim registration latency is below 400ms. Where's the measurement?

**Strong answer:** The 400ms target is for *booking* (NFR table). For registration we do not have an explicit SLO. Observed latency is ~200-250ms dominated by bcrypt + distributed lock RTT. No percentile tracking because we don't have a metrics pipeline — the test was ad-hoc via `time curl`. In production I'd use Prometheus with histogram buckets at 50/100/250/500/1000ms.
**Framing:** "No formal measurement — rough observation on one laptop. Flagged as a testing gap."

### G2. 167 bookings/s is your stated peak. You have 4 registrations/s for user-service. Ratio of reg-to-book?

**Strong answer:** Registration is a one-time event per user. For 1M active drivers over a year, that's ~2.7K registrations/day assuming steady-state churn, or ~0.03 reg/s. Our peak is massively below any real load. The 4-reg/s ceiling comes from the distributed lock cost, not the target load. For the demo we demonstrate registration correctness, not throughput.
**Framing:** "Registration is rare; the 4 r/s cluster ceiling is still ~100× our steady-state."

### G3. "99.5% availability under single node failure" — prove it.

**Strong answer:** 99.5% availability means 3.6 hours of downtime per month. We can demonstrate this at the *user-visible* level because a browser request that fails via `resilientFetch` retries the peer within 1-2 seconds — essentially invisible to the user. The test matrix row "Cascade kill (journey + user 503) PASS" at Sec 6.2 is the evidence: killing Laptop A's user-service leaves the session active on Laptop B with no re-login. The 99.5% is a paper claim based on: (a) full-node kill recovery time ~5s (client-side failover), (b) the probability of both nodes failing simultaneously being much lower than 0.5%.
**Framing:** "Single-node kill tested with <5s recovery; dual-node simultaneous kill not covered in the 99.5% target."

### G4. Show that your JWT cross-node session works at 100% cut-over.

**Strong answer:** JWT is stateless and signed with a shared secret (JWT_SECRET in docker-compose.yml). Any node validates any token. The test matrix row "JWT authentication cross-node PASS" at Sec 6.2 exists. The failure mode would be if a user's row existed only on Laptop A and the failover target Laptop B had not yet received the replication — in which case routes that need the user row (e.g. `/api/users/me`) would 404. Our 5-minute periodic sync + startup sync bounds this to ~5 minutes post-registration, after which cross-node failover is seamless.
**Framing:** "Stateless auth + async row replication; bounded 5-min window of incomplete replication on registration."

### G5. Scalability: at what user count does your system break?

**Strong answer:** Three breakpoints: (a) Postgres primary row count — billion-row tables are fine for Postgres, not the bottleneck; (b) bcrypt verify CPU at login — ~10/s per worker, so 10K concurrent logins needs ~1000 workers or shard at auth-service level; (c) replication push amplification — at N nodes, every write generates N-1 POST calls, quadratic cost. Acceptable up to ~10 nodes, untenable at 100+. Above 10M users or 10+ nodes, the active-active replication model should be replaced with true sharding (home-shard-only writes + read at any node).
**Framing:** "Linear scaling bounded by replication fan-out; hard ceiling at ~10 nodes before architecture must change."

## Category H — Report vs. Reality Mismatches (service-specific)

### H1. The report says sharding routes writes to the home shard. The code only logs the shard label.

**How the examiner will phrase it:** "Show me the code where a write is forwarded to the home shard." → [replication.py:104-119](user-service/app/replication.py#L104-L119) is the only sharding code, and it returns a tuple, nothing else. Honest answer: "The write-routing leg is not implemented. The sharding is currently observability-only. The distributed lock provides correctness; the sharding function is laid down for future write-routing and surfaced in logs and the activity feed."

### H2. The report says reads go to replica. On which routes specifically?

**Strong answer:** All GET routes in `user-service/app/routes.py` that use the `get_read_db` dependency. POST/DELETE routes use `get_db` (primary). Enumerate to the examiner if asked.

### H3. The report claims periodic sync every 5 minutes. [replication.py:56](user-service/app/replication.py#L56) says 300 seconds.

**Strong answer:** Matches (300s = 5min). No mismatch.

### H4. The report claims "no split-brain in email registration." The code allows it during a partition.

**Strong answer:** The report is slightly overselling. During a partition the distributed lock gracefully degrades (peers unreachable = skip, [replication.py:193-194](user-service/app/replication.py#L193-L194)), so two partitioned nodes can accept the same email. The report's framing of "prevents split-brain" is true *outside* of partition scenarios. Under partition we are AP. Honest framing: "The lock prevents concurrent registration during normal operation; during a partition we accept the risk of duplicates in exchange for availability."

### H5. Full-node kill middleware returns 503 — does it really cascade to the user-service from the journey-service /admin/simulate/fail?

**Strong answer:** Yes — [journey-service/app/main.py](journey-service/app/main.py) `_cascade_to_user_service()` POSTs to `http://user-service:8000/admin/simulate/fail` after setting its own flag. Both services flip simultaneously. The examiner might ask: "What if the cascade POST fails?" → The journey side is still 503, so most traffic breaks, but `/api/users/login` would still work because user-service never got the cascade signal. That's a small inconsistency we accept for demo purposes.

## User Service — 10-Minute Cheat Sheet

**3 most likely questions + answers:**
1. **"Why do you need a distributed lock if Postgres has a unique index?"** → Unique index is per-Postgres-instance. With one Postgres per laptop, two concurrent registrations on opposite laptops could both succeed at their local unique index. The 2-phase lock (local Redis SETNX + POST to peers) serialises across instances. The local unique index remains the safety net via the IntegrityError handler.
2. **"What happens during a partition?"** → Lock calls to peers time out (3s httpx timeout). We log a warning and proceed without them. Both sides accept registrations. After heal, the 5-minute periodic sync applies missing records; the local unique index catches duplicates. We are AP during partition.
3. **"How does JWT survive a full-node kill?"** → Shared HS256 secret across all nodes. User row is active-active replicated. Browser `resilientFetch` retries on 503. New node accepts the same token without re-login. Tested and documented in the PASS matrix.

**3 strongest design decisions to lead with:**
1. **Active-active replication with distributed locking.** Hard to get right; we did the both-halves: lock before write, replicate async after commit, periodic catch-up sync. Shows command of Redlock and eventual consistency.
2. **Read/write split at application level.** Simple, effective, read scale-out without extra infra.
3. **Stateless JWT with shared secret for cross-node session continuity.** Makes client-side failover "just work" without session replication.

**3 biggest weaknesses (and the framing):**
1. **Sharding is observability-only, no write routing.** *Framing:* "Infrastructure laid down, enforcement pending. Distributed lock is the correctness mechanism today, sharding was to be the optimisation." **Don't** pretend it routes writes.
2. **No token denylist on logout.** *Framing:* "Acknowledged limitation; production fix is short-lived access tokens + server-side refresh session. We chose long tokens to make failover seamless for the demo."
3. **Split-brain possible during partition.** *Framing:* "AP by choice during partition, CP during normal operation. Unique index catches post-heal duplicates at the log-warning level. Merge/conflict resolution is future work."

**The 1 thing you must NOT say:**
**"Our sharding routes writes to the home node."** — It doesn't. The `shard_for_email()` function returns a label and the registration proceeds locally regardless. Saying this lets the examiner catch you lying, which is the worst possible outcome.

---

# Journey Service — Tanmay Ghosh

**Stack:** Python 3.11, FastAPI, SQLAlchemy async, PostgreSQL primary+replica, httpx for synchronous REST, aio_pika for RabbitMQ, shared circuit breaker + partition manager.
**Key files:** [journey-service/app/main.py](journey-service/app/main.py), [service.py](journey-service/app/service.py), [saga.py](journey-service/app/saga.py), [coordinator.py](journey-service/app/coordinator.py), [conflict_client.py](journey-service/app/conflict_client.py), [outbox_publisher.py](journey-service/app/outbox_publisher.py), [points.py](journey-service/app/points.py), [replication.py](journey-service/app/replication.py).

## Category A — Why This Over That?

### A1. Why a saga orchestrator instead of choreography? Choreography is looser coupling.

**What they're really testing:** Saga topology awareness.
**Strong answer:** Choreographed sagas (each service reacts to each event, no central controller) spread business logic across services and make it hard to reason about the end state. For booking we have exactly one decision point — the conflict check — and we need a single place to read the result, attach it to the journey row, and decide confirmed vs rejected. The orchestrator lives in [service.py:99-118](journey-service/app/service.py#L99-L118): one place, one function, easy to test. Choreography would require the conflict service to emit confirm/reject events that journey-service consumes, adding a round-trip through RabbitMQ to the critical path and ~2 seconds of latency from the outbox poll cycle. We chose orchestration to keep the synchronous path fast.
**Trap to avoid:** Saying "orchestration is simpler." True but not the discriminating reason.
**Follow-up:** "Why not return the conflict result directly and skip the outbox?" → The outbox isn't for the conflict result — it's for fan-out to downstream services. Different problem.

### A2. Why use the transactional outbox over a direct publish after commit?

**What they're really testing:** Understanding the dual-write problem.
**Strong answer:** Direct publish after commit is the dual-write problem: two writes (DB, broker) with no atomicity. Crash between them and you lose the event, with no way to recover. The outbox pattern makes the event a row in the same DB transaction as the journey row — one write, atomic. A background poller ([outbox_publisher.py](journey-service/app/outbox_publisher.py) `POLL_INTERVAL_SECONDS = 2`, `BATCH_SIZE = 50`) drains unpublished events to the broker. On broker down, rows accumulate and drain on reconnect. This gives at-least-once delivery with zero custom recovery code.
**Trap to avoid:** Claiming it gives exactly-once. It doesn't — consumers still need to dedupe.
**Follow-up:** "What if the outbox table grows forever?" → The publisher marks `published=true` after broker ack. Rows stay forever but are filtered out of the `WHERE published=false` scan. For production you'd add a cleanup job; we don't need it for the demo.

### A3. Why synchronous REST between Journey and Conflict instead of RabbitMQ request-reply?

**What they're really testing:** Request-response pattern choice.
**Strong answer:** REST gives us a tight latency envelope (~50ms) and a clear error model (HTTP status codes). RabbitMQ request-reply would need a reply-to queue, correlation ID tracking, and a timeout manager — all duplicating what HTTP gives for free. For a *synchronous* call that must block the booking response, REST is strictly simpler. We use RabbitMQ only for the asynchronous fan-out that runs *after* the booking response has been sent to the client.
**Trap to avoid:** Saying "RabbitMQ is slower." It's comparable; the real reason is the programming model.
**Follow-up:** "How do you retry on a transient failure?" → Per-URL circuit breaker + peer failover in [conflict_client.py](journey-service/app/conflict_client.py). No explicit retry loop; we let the saga reject the booking and the client retry.

### A4. Why httpx over requests? Python already ships with urllib.

**What they're really testing:** Library choice.
**Strong answer:** httpx supports native async/await, urllib does not. Inside an async FastAPI handler, using urllib would block the event loop for the duration of the HTTP call — catastrophic for throughput. httpx also has per-client timeouts and transports we need for circuit-breaker integration. `requests` is synchronous too. The choice was the only viable async HTTP client in Python.
**Trap to avoid:** Bringing up aiohttp without knowing the differences. aiohttp is a valid alternative; httpx won on API ergonomics.

### A5. Why Postgres for journeys instead of a queue (like Kafka as a commit log)?

**What they're really testing:** Understanding the trade-off between a DB-backed state and a log-backed state.
**Strong answer:** Journeys are *queryable* state, not an event stream: the user needs "list my journeys", "get journey X", "cancel this one". A Kafka-style log would force us to build an index on top of the log for every query, essentially reimplementing a database. We use the DB for the state and the outbox table as our event log. Best of both: ACID on state, streaming-like semantics on events. Kafka would be right if we had high-throughput, write-once events with no point lookups.
**Trap to avoid:** Claiming Postgres "is a message queue." It isn't — the outbox pattern uses Postgres *as a staging area* for a real broker.

## Category B — Why Use This At All?

### B1. Why a two-phase commit mode if you already have the saga? Pick one.

**What they're really testing:** Understanding when 2PC adds value.
**Strong answer:** Two reasons. (1) Saga cannot give us "journey row written AND slot reserved" atomically — it writes the row first (PENDING), then calls conflict, then updates the row to CONFIRMED. If the update fails after the slot was reserved, the slot leaks until a compensating cancel. The 2PC/TCC mode in [coordinator.py](journey-service/app/coordinator.py) makes this a TRY/CONFIRM/CANCEL flow where TRY reserves capacity, CONFIRM commits the journey + makes the slot permanent, CANCEL explicitly releases on failure. (2) The demo requires showing a two-phase commit to the marker. Having both modes lets us compare: saga = optimistic, 2PC = pessimistic. Default is saga; `?mode=2pc` opts in.
**Trap to avoid:** Claiming both modes are equally production-ready. 2PC has the phantom-slot leak risk that requires the same-peer-URL preference for CANCEL, and it's not the default.
**Follow-up:** "Which is used in practice?" → Saga. 2PC is a demo-mode toggle for the viva.

### B2. Why do you have idempotency keys? HTTP is supposed to be idempotent already.

**What they're really testing:** REST vs true idempotency.
**Strong answer:** POST is non-idempotent in HTTP semantics; retrying a `POST /api/journeys/` creates a new journey each time. We need application-level idempotency to protect against client retry loops. The `Idempotency-Key` header gets persisted in `idempotency_records` ([service.py:43-52](journey-service/app/service.py#L43-L52)). On duplicate, we return the existing journey, so the client sees the same response twice without side-effects. This is the industry-standard pattern (Stripe, AWS, etc.).
**Trap to avoid:** Claiming PUT would fix it. PUT has different semantics; the pattern here is about *retryable* non-idempotent operations.
**Follow-up:** "What's the TTL on the idempotency record?" → We don't expire them in the current code. They live forever. Should be TTL'd after ~24h for garbage collection.

### B3. Why have a partition manager if you already have circuit breakers?

**What they're really testing:** Understanding that the two solve different problems.
**Strong answer:** Circuit breaker is per-dependency — it wraps a single downstream call and fast-fails on repeated error. Partition manager is *per-service-state*: it tracks whether Postgres, Rabbit, and Conflict Service are all reachable and transitions through CONNECTED → SUSPECTED → PARTITIONED → MERGING. Partition state is exposed on every HTTP response via `X-Partition-Status` header, so downstream clients (e.g. Enforcement) can mark their responses `X-Cache-Stale`. Circuit breakers don't expose state at the HTTP header level; partition manager does. They compose: partition manager aggregates, circuit breakers act.
**Trap to avoid:** Claiming one subsumes the other. They're layered.

### B4. Why the lifecycle scheduler? Let users call an API to mark journeys in-progress.

**What they're really testing:** Whether automation is justified.
**Strong answer:** Journey state transitions (CONFIRMED → IN_PROGRESS at departure_time, → COMPLETED at estimated_arrival_time) are time-driven, not user-driven. If we waited for the user to call an API, the state would lag reality, breaking the enforcement service's "is this journey active right now" check. The scheduler runs as a background task in [main.py](journey-service/app/main.py) lifespan and polls for journeys whose transition time has passed. Alternative designs: (a) compute state at read time from timestamps — but then enforcement cache invalidation is harder; (b) Postgres triggers — unnecessary complexity.
**Trap to avoid:** Claiming "real-time" semantics. The scheduler polls, so there's a bounded delay.
**Follow-up:** "What's the polling interval?" → Check `scheduler.py` if present; usually ~30 seconds.

### B5. Why a per-URL circuit breaker instead of one breaker per service?

**What they're really testing:** Understanding of failure domain granularity.
**Strong answer:** A single breaker for "conflict-service" treats all peer URLs as one unit. If the local conflict-service is flaky (opening the breaker), every peer call also short-circuits even though the peers are healthy. Per-URL breakers in [conflict_client.py](journey-service/app/conflict_client.py) (`get_circuit_breaker(f"conflict-service:{url}")`) isolate failure domains: local breaker can be OPEN while peer A's breaker is CLOSED, so we transparently failover. This is the bulkhead pattern applied at the endpoint level.
**Trap to avoid:** Claiming breakers need per-call granularity. Per-URL is the sweet spot.
**Follow-up:** "Who drives the breaker state transitions?" → Named breakers in a global registry ([circuit_breaker.py:181-196](shared/circuit_breaker.py#L181-L196)), shared across handlers.

## Category C — Why Haven't You Used X?

### C1. Why no saga compensation retry? A permanently-dead conflict service just fails every booking.

**What they're really testing:** Compensation maturity.
**Strong answer:** Correct, and it's explicitly called out as a limitation in Sec 3.4. The current behaviour: on saga failure the journey is marked REJECTED with "Conflict check service unavailable. Please retry." and the user retries manually. A production system would queue the booking and retry on a backoff schedule, or degrade to "reservation pending" with a grace window. We chose to fail fast because the alternative (queuing and retrying) would show the user a success and later flip to failure — worse UX for a traffic-booking app.
**Trap to avoid:** Pretending the retry is handled by the circuit breaker. Circuit breaker short-circuits, it doesn't retry.
**Follow-up:** "Why not exponential backoff?" → In a synchronous HTTP request we can't make the caller wait 30s. We would need to shift to an async booking model where the initial POST returns `202 Accepted` and the final state comes via WebSocket. Future work.

### C2. Why no read repair or hinted handoff?

**What they're really testing:** Cassandra-flavoured eventual consistency patterns.
**Strong answer:** We do not have the Cassandra-style multi-replica write path those patterns are designed for. Our model is: each node writes to its own Postgres, then replicates to peers via fire-and-forget. The analog of "hinted handoff" is the 5-minute periodic sync which backfills missed pushes ([replication.py](journey-service/app/replication.py)). The analog of "read repair" is... absent — we never cross-check reads against peers. If Laptop A has a stale copy of Laptop B's journey, a read on A returns stale data silently. For the demo we accept this because we replicate on every write and sync periodically.
**Trap to avoid:** Confusing our sync with proper read repair. Ours is write-driven, not read-driven.

### C3. Why no backpressure on the saga? A burst of bookings will melt the conflict service.

**What they're really testing:** Flow control.
**Strong answer:** Backpressure lives at the nginx rate limit (10 r/s booking with burst 20, [nginx.conf:35](api-gateway/nginx.conf#L35)). This is application-level backpressure, not in-service backpressure. Inside journey-service we don't explicitly cap concurrent saga executions — FastAPI + uvicorn schedules all incoming handlers. Under 10× the rate limit (100 r/s) we'd see saga latency climb due to conflict-service contention, but the nginx zone prevents this from reaching us in the first place. Alternative: semaphore inside journey-service to cap concurrent outbound `/api/conflicts/check` calls. We don't have it; the upstream rate limit is our defence.
**Trap to avoid:** Implying nginx rate limit is "backpressure." It's rate limiting. Backpressure implies the receiver signals the sender to slow down; rate limit just drops excess.
**Follow-up:** "What's the per-client limit?" → Shared across clients in the zone (zone is keyed by IP). One client gets 10 r/s sustained.

### C4. Why no consensus (Raft/Paxos) for the journey state machine?

**What they're really testing:** Whether consensus is needed here.
**Strong answer:** Journeys are owned by a single user, written to a single node, replicated to peers async. There's no multi-writer conflict on the same journey row — each journey has one owner, one creating node. Consensus is needed when multiple replicas must agree on a log order; we don't have that constraint. The closest we get is cross-node slot reservation in conflict-service, and we documented that millisecond-window double-booking is possible there (Sec 5.5). Adding Raft to either service would be overkill for the demo scale and would prevent us from showing cleaner patterns like the saga + outbox.
**Trap to avoid:** Saying "we didn't need consensus" without acknowledging the specific place it would help (conflict service's cross-node capacity).

### C5. Why no CRDTs?

**What they're really testing:** CRDT awareness.
**Strong answer:** CRDTs (Conflict-free Replicated Data Types) are for data where concurrent updates can be merged automatically — counters, sets, registers. Our journey rows are owned (one writer per journey), and our capacity counters would benefit from a PN-counter (increment-decrement CRDT), but for capacity we chose serialised access per cell instead because capacity has a hard upper bound and a CRDT counter cannot enforce "≤ max_capacity" atomically. A G-counter (increment-only) would let slot counts diverge and potentially exceed max. Our chosen approach (Postgres SERIALIZABLE + async replication) gives correctness per node and documented eventual consistency across.
**Trap to avoid:** Claiming CRDTs solve the capacity problem. They don't — bounded counters are a known CRDT weakness.

## Category D — Bottleneck Hunting

### D1. The outbox poll interval is 2 seconds. That's a fixed 2-second event-delivery delay.

**Strong answer:** Correct. Every outbox event waits up to 2 seconds before the publisher runs the next scan. For notifications (non-critical) this is acceptable; for enforcement cache updates (potentially safety-critical) the 2s delay means an enforcement check during that window could return stale "no active journey". We mitigate for the specific case of cancellation by calling `resilient_conflict_cancel` synchronously on cancel ([service.py:213-220](journey-service/app/service.py#L213-L220)) — the RabbitMQ event is still published via outbox, but the primary correctness path doesn't wait on it. For confirmations we accept the 2s delay because it's a new journey (not a cancellation) and the enforcement cache missing it for 2s just means one fallback REST call on enforcement side.
**Framing:** "2s delay is by design; critical paths have synchronous bypasses."
**Follow-up:** "Why not 200ms?" → Every shorter interval increases DB scan frequency. 2s was empirical middle ground.

### D2. The conflict check is synchronous in the booking path. That's a hard latency floor.

**Strong answer:** Yes — this is the critical-path call. End-to-end booking is dominated by (a) journey insert ~5ms, (b) conflict check ~20-50ms (SERIALIZABLE tx + cell lookups), (c) journey update ~5ms, (d) total ~100-250ms observed. The conflict check dominates. Mitigations: (1) grid-cell model keeps the lock scope small — we only contend on cells the route actually touches, (2) circuit breaker fast-fails on dead conflict service, (3) peer failover keeps the path alive when local conflict dies. What we *don't* do is pre-allocate capacity or cache the check result.
**Framing:** "Critical path hard floor ~100ms; acceptable under SLO of <400ms p95."

### D3. The saga runs synchronously — one request, one Python coroutine. What's your concurrency limit?

**Strong answer:** FastAPI/uvicorn spawns coroutines per request; concurrency is bounded by Postgres pool size (15 default) not by the event loop. Beyond 15 in-flight sagas, new requests queue at the pool. Under sustained 20+ r/s we'd see pool contention. For the 10 r/s nginx cap we're comfortably below this limit. At 100 r/s we'd need to raise pool or add workers.
**Framing:** "Pool size = concurrency ceiling; nginx rate limit keeps us below it."

### D4. Your idempotency table will grow unboundedly.

**Strong answer:** True — we do not TTL idempotency records. In production I'd add a nightly cleanup job that deletes records older than 24h. For the demo, table growth is negligible (~one row per booking). The production fix is a background cron task or Postgres partitioning by created_at.
**Framing:** "Growth is linear, not exponential; cleanup is a trivial post-demo task."

### D5. Every booking does a vehicle-ownership HTTP call to user-service. That's N+1.

**Strong answer:** [service.py:54-57](journey-service/app/service.py#L54-L57) calls `_verify_vehicle_ownership` which makes a synchronous HTTP GET to user-service on every booking. This is a hard dependency — if user-service is down, booking fails. Mitigations: (1) user-service has its own peer failover, (2) journey-service's partition manager would flag user-service down before the call fails, (3) we could cache vehicle ownership in Redis, but we don't. The verification is meaningful: without it, a user could book a vehicle they don't own. Alternative: push vehicle ownership into the JWT claim at login time — then no network call is needed. We did not do this and should have.
**Framing:** "N+1 is real; JWT-claim caching is the clean fix and is not yet implemented."
**Follow-up:** "What if vehicle ownership changes mid-session?" → JWT would have stale data. Acceptable for short tokens or on-demand refresh.

## Category E — Limitations

### E1. The saga + outbox gives at-least-once delivery, not exactly-once.

**Strong answer:** Correct. Downstream consumers must deduplicate — and they do, via Redis SETNX on message IDs ([notification-service/consumer.go:138-167](notification-service/consumer.go#L138-L167), same pattern in enforcement and analytics). The combination of at-least-once + consumer dedup gives effectively-exactly-once from the user's perspective. The word "exactly-once" is misleading without the dedup layer — we never use it in the report without qualifying "effectively".
**Framing:** "At-least-once with dedup, not strict exactly-once."

### E2. The 2PC coordinator has phantom-slot-leak risk on mid-flight failures.

**Strong answer:** Documented and mitigated: the coordinator prefers the same peer URL for CANCEL that executed PREPARE ([coordinator.py](journey-service/app/coordinator.py)). If the PREPARE was on Peer A and the CANCEL accidentally goes to Peer B, the slot on A leaks until its is_active flag is flipped by some other path — which it isn't. The preference is the fix; but if Peer A becomes unreachable between PREPARE and CANCEL, we end up posting the CANCEL to whoever we can reach (may be B), leaking the A slot. Eventually Peer A's periodic sync will reconcile, but the window can be 5 minutes.
**Framing:** "Phantom-slot mitigated by same-peer preference; 5-minute residual window if the PREPARE peer becomes unreachable."

### E3. Your partition state machine has no formal model. How do you know the transitions are correct?

**Strong answer:** The states (CONNECTED/SUSPECTED/PARTITIONED/MERGING) are defined in [shared/partition.py](shared/partition.py) with counter-based transitions (1 miss → SUSPECTED, 3 misses → PARTITIONED). Transitions are deterministic and tested at the unit level by simulating dependency failures. No formal model (TLA+/Alloy) — that would be beyond the exercise scope. The transitions are conservative: we favour false positives (going to PARTITIONED unnecessarily) over false negatives (missing a real partition), so the worst case is a short degradation window, not a missed failure.
**Framing:** "Deterministic counter-based transitions, conservative bias, no formal verification."

### E4. Outbox drainer can publish an event twice if it crashes between publish and update.

**Strong answer:** True. [outbox_publisher.py](journey-service/app/outbox_publisher.py) publishes to the broker first, then marks `published=true`. A crash between those two steps leaves the row unpublished but the broker has already received it. On restart the drainer re-publishes, broker delivers twice. Consumers dedupe via Redis SETNX on message_id. We rely entirely on the consumer dedup layer to turn at-least-once into effectively-exactly-once. The alternative (two-phase commit between drainer and broker) would add complexity with no practical benefit given we already dedupe.
**Framing:** "Two-writes-one-crash scenario handled by consumer dedup, not drainer-side atomicity."

### E5. The saga is not truly stateful — it's stateless code with state in Postgres. What about crash recovery?

**Strong answer:** If journey-service crashes mid-saga, the journey row remains in `PENDING` status in Postgres. On restart, the service does *not* automatically resume PENDING journeys — the saga is request-scoped. Those journeys live forever in PENDING unless a lifecycle scheduler or admin action cleans them up. This is a real gap. The test matrix row "Transactional outbox survives RabbitMQ restart" is about the event side; journey state recovery mid-saga is not tested.
**Framing:** "Stateless orchestrator with no resume-from-crash; PENDING rows can leak. Admin endpoint `/admin/recovery/drain-outbox` helps but doesn't resume sagas."

## Category F — Failure Scenarios

### F1. Postgres dies mid-saga after the conflict check succeeded but before the status update commits.

**What happens:** The journey row is PENDING (from step 1), the slot is reserved in conflict-service. The `UPDATE journey SET status=CONFIRMED + save_outbox_event + commit` at [service.py:114-117](journey-service/app/service.py#L114-L117) raises DB error. The except clause marks the saga failed, but the slot in conflict-service is already reserved. On the next boot, the PENDING journey is visible but has no associated outbox event; conflict-service still holds the slot reservation. Without an admin intervention, the slot leaks. The `/admin/recovery/drain-outbox` endpoint doesn't help here — there's no outbox row to drain. This is one of the real gaps.
**Framing:** "Slot leak on commit-time crash; admin endpoint needed to reconcile."

### F2. The conflict service returns 500 (internal error).

**What happens:** [conflict_client.py](journey-service/app/conflict_client.py) treats 5xx as a failure, opens the per-URL circuit breaker (3 consecutive failures), advances to the next peer URL. If all URLs exhausted, saga returns REJECTED with "Conflict check service unavailable."
**Framing:** "Tried local first, then each peer; circuit breakers isolate; final REJECTED on exhaustion."

### F3. Conflict service returns corrupt response (missing fields).

**What happens:** Pydantic validation on `ConflictCheckResponse` raises ValidationError; [conflict_client.py](journey-service/app/conflict_client.py) catches it as a failure, advances peer. Same as F2.
**Framing:** "Schema validation catches corrupt bodies; treated as a transient failure for retry logic."

### F4. Network partition isolates the journey service from conflict service AND from its peers.

**What happens:** Circuit breakers open on all URLs. Saga fast-fails with "Conflict check service unavailable. Please retry later." Partition manager transitions conflict-service to PARTITIONED. All new booking requests immediately fail. On heal, circuit breakers transition to HALF-OPEN, probe, and re-close on success. Recovery time ≈ 30s (breaker reset timeout).
**Framing:** "Fast-fail during partition; ~30s recovery post-heal via HALF-OPEN probe."

### F5. The outbox drainer gets stuck publishing a message that always fails.

**What happens:** The drainer tries to publish, broker raises error, drainer breaks out of the inner loop without marking published=true ([outbox_publisher.py](journey-service/app/outbox_publisher.py)). Next poll cycle tries again in 2 seconds. If the failure is permanent (malformed message), this is an infinite retry loop. We don't have a poison-message detector. The broker DLX only catches consumer-side failures, not publisher-side.
**Framing:** "Poison publisher messages retry forever — no publisher-side DLQ. Low risk because our payloads are all schema-validated."

### F6. RabbitMQ down on booking.

**What happens:** Outbox row persists in the same DB transaction as the journey ([service.py:114-117](journey-service/app/service.py#L114-L117)). Publisher loop logs the broker error and continues to retry every 2s. Downstream services lag by however long the broker is down. On recovery, the backlog drains; consumer dedup ensures no double-delivery impact.
**Framing:** "Broker-down is a latency event, not a correctness event — by design of the outbox."

### F7. Redis down when journey-service wants to record points.

**What happens:** Wait — points are in Postgres, not Redis ([points.py](journey-service/app/points.py)). Redis isn't involved in points. The real question is if the *conflict-service's* Redis were down for deduplication — but that's a conflict-service concern. Journey-service's Redis dependency is... minimal. Partition manager probes Redis but doesn't fail the booking on Redis down.
**Framing:** "Journey-service doesn't strictly need Redis for correctness."

### F8. Client sends a booking retry with a new idempotency key accidentally.

**What happens:** New key → no cache hit → second journey created. Client sees two journeys, double-booking if the two conflict (vehicle overlap → one REJECTED) or two confirmed if separated. The fix is client-side: always use the same idempotency key for retries of the same logical request. We can't enforce this server-side.
**Framing:** "Idempotency is cooperative; client must re-use the key."

### F9. Journey row update succeeds but the outbox insert fails.

**What happens:** Both are in the same transaction — either both commit or both roll back. No partial state possible.
**Framing:** "Atomic by design — that's the whole point of the outbox pattern."

### F10. A bug causes infinite saga retries in a loop.

**What happens:** Each saga runs once per booking POST. There is no internal retry loop in the saga itself — `execute()` in [saga.py](journey-service/app/saga.py) runs the conflict check once and returns. A bug could only cause retries if the client re-POSTs. nginx rate limit (10 r/s booking) bounds client retry rate.
**Framing:** "No internal retry; client-driven."

## Category G — Requirements Defense

### G1. "Booking latency <400ms p95" — under what load?

**Strong answer:** Under the 10 r/s rate-limited load. Beyond that the Postgres connection pool (15) starts queueing requests, inflating p95. We didn't measure under higher load because the nginx rate limit prevents us from reaching it without multiple clients.
**Framing:** "Target holds at rate-limited throughput; untested above nginx cap."

### G2. "Eventual consistency converges after partition heal" — how fast?

**Strong answer:** Bounded by max(replication_push_rtt, periodic_sync_interval). Replication push is ~50ms per peer (fire-and-forget, best-effort). Periodic sync is 5 minutes. So worst case: 5-minute lag for any write that was made during the partition. After the 5-min tick, all nodes converge.
**Framing:** "≤5-minute convergence; push-on-write is best-effort, periodic sync is the ceiling."

### G3. "No confirmed booking is ever lost" — prove it.

**Strong answer:** The outbox pattern is the proof. The journey row and the outbox event are in the same DB transaction. Either both commit (booking confirmed, event will be published on next drainer cycle) or both roll back (no booking, no event, client sees the error). The broker can lose events only if Postgres loses the outbox row — which requires a disk failure, not a transient crash. Postgres WAL replication gives a hot standby with <50ms lag (visible at `/api/analytics/replica-lag`), so a primary loss has a replica with near-complete state.
**Framing:** "Atomic DB+outbox write; WAL replication for durability; broker-side loss is tolerated by replay."

### G4. Idempotent retries — show me.

**Strong answer:** Curl twice with the same `Idempotency-Key` header — both calls return the same journey. Code path at [service.py:43-52](journey-service/app/service.py#L43-L52). Test row "Idempotent retries PASS" in Sec 6.2.
**Framing:** "One-line test; persistent record table."

### G5. Circuit breaker opens after 3 failures — proof?

**Strong answer:** [shared/circuit_breaker.py:70](shared/circuit_breaker.py#L70) default `failure_threshold=3`. The journey-service uses the default. Test row "Circuit breaker PASS — opens after 3 failures; closes after probe" in Sec 6.2. Can be demonstrated by stopping conflict-service and making 4 booking attempts: first 3 fail slowly (actual HTTP timeout), 4th fails instantly (breaker open).
**Framing:** "Default parameters, named breaker per URL, demonstrable in <30s."

## Category H — Report vs. Reality

### H1. Sec 4.2 says "partition detector probes every 5 seconds". Matches [shared/partition.py](shared/partition.py) `PROBE_INTERVAL = 5.0`. ✅

### H2. Sec 4.2 says "lifecycle scheduler transitions journeys". Confirmed by scheduler in `main.py` lifespan.

### H3. Sec 5.2 says the synchronous path is "100-250ms on slim stack". We haven't formally benchmarked; that's an observational number. A careful examiner will ask for histograms — say honestly we don't have percentile data, only mean latency.

### H4. Sec 5.3 says outbox pattern "replays to RabbitMQ after broker recovery". Confirmed in [outbox_publisher.py](journey-service/app/outbox_publisher.py) — the loop runs every 2s and processes unpublished rows. ✅

### H5. Sec 4.2 claims "peer health model" is in journey-service. [shared/health_monitor.py](shared/health_monitor.py) is present, initialized in journey-service main.py lifespan. ✅

## Journey Service — 10-Minute Cheat Sheet

**3 most likely questions + answers:**
1. **"Why the transactional outbox?"** → Atomic domain write + event write in one DB transaction eliminates the dual-write problem. Background drainer at 2s intervals publishes to RabbitMQ, tolerating broker outage. This is how we guarantee "no confirmed booking is ever lost."
2. **"What happens when conflict-service is down?"** → [conflict_client.py](journey-service/app/conflict_client.py) tries local first, then each peer URL from `PEER_CONFLICT_URLS`, each guarded by its own named circuit breaker. On full exhaustion, saga returns REJECTED with "Conflict check service unavailable." No retry — deliberately fast-fail per the report's documented limitation.
3. **"How does 2PC differ from the saga mode?"** → Saga is optimistic: journey row first, conflict check second, update third. 2PC/TCC is TRY (reserve slot, no journey commit), CONFIRM (commit both), explicit CANCEL on failure against the same peer that did PREPARE (to avoid phantom slots). Default is saga; `?mode=2pc` opts in for demo.

**3 strongest design decisions:**
1. **Transactional outbox pattern.** Clean solution to the dual-write problem. Shows understanding of at-least-once delivery and the role of consumer dedup.
2. **Per-URL circuit breakers with peer failover.** Bulkheads at endpoint granularity — local circuit can be OPEN while peer circuit is CLOSED.
3. **Saga + 2PC coexistence.** We have both, we know when to use each, the report frames the trade-off honestly.

**3 biggest weaknesses + framing:**
1. **No saga compensation retry.** *Framing:* "Documented trade-off (Sec 3.4). Fast-fail preserves honest UX. Retry with a queue is future work."
2. **PENDING journeys leak on mid-saga crash.** *Framing:* "Mid-saga recovery is not implemented; crashed sagas leave PENDING rows. Admin recovery endpoint drains outbox but doesn't resume sagas."
3. **Outbox delays non-critical consumers by 2s.** *Framing:* "Critical paths (like cancellation freeing capacity) use synchronous bypass; non-critical (notifications) accept the 2s delay."

**The 1 thing you must NOT say:**
**"The saga guarantees exactly-once delivery."** — It doesn't. It's at-least-once with consumer dedup = effectively-exactly-once. The examiner will jump on the wrong vocabulary immediately.

---

# Conflict Service — Sneha Meto

**Stack:** Go 1.22, chi router, pgx/v5, PostgreSQL (SERIALIZABLE), RabbitMQ consumer, cross-node HTTP replication.
**Key files:** [conflict-service/service.go](conflict-service/service.go), [replication.go](conflict-service/replication.go), [main.go](conflict-service/main.go), [sharding.go](conflict-service/sharding.go), [handlers.go](conflict-service/handlers.go).

## Category A — Why This Over That?

### A1. Why Go for the conflict service when the rest of your Python services are in FastAPI?

**What they're really testing:** Whether the language choice had a real technical reason or is architectural tourism.
**Strong answer:** The conflict check is the hottest synchronous path in the system — every booking touches it, and each check opens a SERIALIZABLE transaction that holds locks until commit. Go's goroutine scheduler gives us cheap per-request concurrency without the GIL that would serialise any CPU work in Python, and pgx/v5 gives us a first-class Postgres driver that supports row locks, explicit isolation levels, and pipelined queries more cleanly than SQLAlchemy. It also means the conflict service is an independent binary — it cannot share in-process state with Journey Service by accident, which is exactly the service boundary we want.
**Trap to avoid:** "Go is faster." Go is faster in microbenchmarks; on this workload the bottleneck is the Postgres round-trip, not Go vs Python CPU time. The right argument is *concurrency model* and *driver quality*, not raw speed.
**Follow-up:** "Prove the concurrency matters." → One conflict check walks up to ~20 grid cells with a FOR UPDATE on each. With ten concurrent requests against the same corridor, you get blocking chains; Go goroutines + pgx pooling let us process non-conflicting parallel requests without any language-level serialisation. Same code in sync Python would serialise at the GIL + DBAPI layer.

### A2. Why PostgreSQL SERIALIZABLE instead of repeatable read + explicit locks?

**What they're really testing:** Whether you actually understand Postgres's SSI model.
**Strong answer:** SERIALIZABLE in Postgres is implemented as **Serializable Snapshot Isolation (SSI)** — it uses predicate locks and a conflict-detection graph rather than lock upgrades. That matters here because the capacity check is a range predicate (`current_bookings >= max_capacity`), and under REPEATABLE READ two concurrent transactions can each take a snapshot where the cell is not full, both pass the check, and both commit. SSI detects the read-write dependency cycle and aborts one with SQLSTATE `40001` (see `BeginTx(IsoLevel: pgx.Serializable)` at [service.go:67](conflict-service/service.go#L67)). We pay the occasional serialization failure for a correct first-committer-wins outcome.
**Trap to avoid:** Saying "serializable locks everything." It doesn't — SSI is optimistic.
**Follow-up:** "Show me the retry loop for 40001." → I **don't** have one. This is **M2** in the mismatch table. A `40001` currently surfaces as HTTP 500 and the Journey saga treats it as REJECTED. In production I would wrap `checkConflicts` in a loop of up to 3 retries with jittered backoff.

### A3. Why a spatial grid (~1km cells) instead of PostGIS geometry with ST_Intersects?

**What they're really testing:** Trade-off between geometric precision and query simplicity.
**Strong answer:** Three reasons. (1) **Discrete time slots.** Capacity is per grid-cell *per 30-minute slot*. PostGIS gives us spatial indexing but we still need temporal bucketing, and once you have buckets the spatial part becomes a hash lookup too. (2) **Deterministic replay for cancellation.** When a journey is cancelled, we must decrement exactly the same cells that were incremented at booking. A hash of (lat, lng, slot_start) is trivially reproducible; an R-tree query is not. See [service.go:447-458](conflict-service/service.go#L447-L458) — cancellation reconstructs cells by re-walking the same algorithm. (3) **No PostGIS dependency.** Keeps the Postgres image slim and the schema portable. The trade-off is spatial resolution: we round to 0.01° (~1.11km at the equator, less as you go north). For Dublin traffic that's coarser than a single street, but the point of the exercise is to demonstrate capacity reservation, not real-world routing.
**Trap to avoid:** Claiming the grid is "as accurate as PostGIS." It is not — it is deliberately coarser in exchange for a simpler conflict model.
**Follow-up:** "How do you handle the 111km-vs-78km longitude distortion at Dublin's latitude?" → Honest answer: I don't — I use degree-based cells that become rectangles, not squares, at 53° N. For a production system I would project to a local EPSG and grid in metres. For the exercise, the grid is schematic.

### A4. Why push replication over event streaming (Kafka log)?

**What they're really testing:** Whether you reached for the heaviest hammer in the box.
**Strong answer:** RabbitMQ is already in the system for journey events; Kafka would be a new piece of infrastructure for one use case. The cross-node slot replication is (a) low-volume — one message per confirmed booking, <200/s even at peak, (b) small-fanout — currently two or three peers, and (c) idempotent on the receiver ([replication.go:239-293](conflict-service/replication.go#L239-L293) checks `WHERE journey_id = $1` before insert). For that shape, direct HTTP POST with async goroutines is simpler and has fewer moving parts than a durable log. I revisit this decision at scale: if we ever have >10 peers, fan-out per booking becomes O(peers) per writer and a Kafka topic partitioned by route_id is the right move.
**Trap to avoid:** "We didn't need Kafka." True but lazy. The right framing is: we picked the *lightest* mechanism that met the correctness requirement (idempotent, eventually consistent, survives a crash via the periodic sync backfill).
**Follow-up:** "What happens if the HTTP POST is lost?" → The periodic sync every 5 minutes at [main.go:56](conflict-service/main.go#L56) pulls all active slots from every peer and applies any missing ones — the missing slot is backfilled by pull, not retried by push. Maximum staleness window is 5 minutes.

### A5. Why SERIALIZABLE end-to-end instead of smaller critical sections with optimistic concurrency?

**What they're really testing:** Understanding of the cost of long transactions.
**Strong answer:** The critical section here is not small — it has to: lock the driver/vehicle overlap row, walk up to 20 grid cells with FOR UPDATE on each, insert the booking, and increment capacity on every cell. All of this has to be atomic or we get a split-brain between the booked_slots row and the road_segment_capacity counter. Splitting it into multiple transactions with version numbers would make the cancellation path horrific (which version of which cells did you decrement?). The transaction is held for a few milliseconds in the common case, so serialization is not a throughput cliff. When it becomes one, I partition by geographic region and give each region its own Postgres.
**Trap to avoid:** Saying "it's fine." Don't hand-wave a long transaction. Own the shape: "holds N locks for M ms, bounded by path length."

## Category B — Why Use This At All?

### B1. Why do you need a conflict service at all — can't Journey Service check for conflicts itself?

**What they're really testing:** Whether the service boundary is principled or fashionable.
**Strong answer:** Journey Service owns the journey lifecycle (PENDING/CONFIRMED/REJECTED/STARTED/COMPLETED). Conflict Service owns the reservation invariant — **at most one vehicle per road cell per 30-minute window**. Merging them would couple two independent concerns: writing to `journeys` (Journey's database) would have to hold locks on `booked_slots` + `road_segment_capacity` (Conflict's database), which means either one Postgres instance for both (giving up isolation) or 2PC between two Postgres instances (giving up simplicity). Splitting them lets Journey Service scale for user-facing reads (journey history, status polling) independently of Conflict Service, which is lock-bound.
**Trap to avoid:** "Microservices are trendy." Defend the boundary by *invariant*, not by architectural fashion.
**Follow-up:** "But now you have a network hop on every booking." → Yes, deliberate — it buys us independent deployment, independent scaling, and a clean abort path (if conflict service is unreachable, Journey Service sets REJECTED, no partial state).

### B2. Why the 5-minute journey buffer ([service.go:34](conflict-service/service.go#L34) `journeyBufferMinutes = 5`)?

**What they're really testing:** Whether design constants are arbitrary or justified.
**Strong answer:** Real vehicles don't materialise at their departure time — they're in the cell a few minutes before (warming up, loading) and for a few minutes after (parking, unloading). Without the buffer, two journeys booked back-to-back on the same cell would technically not overlap in the DB but would overlap in reality. 5 minutes is a trade-off: too small and you get physical conflicts the system didn't see; too large and you over-reject legitimate rapid turnarounds. It's configurable in code only, not at runtime — a real system would tune it by road type.
**Trap to avoid:** "I picked 5 because it felt right." It's still arbitrary, but at least own the reasoning.

### B3. Why replicate slot data to peers at all if the conflict check is local?

**What they're really testing:** Whether the replication has a purpose or is ceremonial.
**Strong answer:** Multi-laptop mode: each laptop runs its own Postgres, so Laptop A's booked_slots table doesn't know about Laptop B's bookings. Without replication, the same driver could book the same time slot on two laptops and both would succeed because each local check sees an empty overlap window. Replication pushes every confirmed slot to every peer, so when Laptop B receives `slot=X user=U`, Laptop B's next local check for user U will see the row and reject. Replication is what makes the local FOR UPDATE lock *globally* meaningful, eventually. It is not ceremonial — it is the only thing standing between us and per-node double-booking of the same driver.
**Trap to avoid:** "It's for HA." The HA framing is weaker — the real reason is correctness under partitioned Postgres instances.
**Follow-up:** "So it's eventually consistent — can two laptops still both accept the same booking concurrently?" → **Yes**, for a few hundred milliseconds. That is the M2/M8 window. The invariant is "at most one CONFIRMED in the steady state"; during the propagation gap both can locally commit, and the first replication message to the slower node will be an idempotent skip. The driver sees one CONFIRMED on one laptop and would see the foreign one only after sync. This is the AP choice documented in the report.

### B4. Why is the replication async and fire-and-forget instead of sync waited?

**What they're really testing:** Understanding of CAP trade-offs on the write path.
**Strong answer:** Synchronous fan-out would make the booking response latency equal to `max(local_commit, slowest_peer_RTT)`. With N peers one slow or dead peer stalls the whole booking. Fire-and-forget [replication.go:179-201](conflict-service/replication.go#L179-L201) keeps the booking response bounded by local commit time (~5ms) and absorbs peer slowness with eventual consistency. The correctness backstop is the periodic pull-sync every 5 minutes, which heals any push that dropped in transit. We are explicitly AP for replication: we would rather return a potentially-conflicting CONFIRMED to the user than hold the response indefinitely when a peer is down.
**Trap to avoid:** Saying "it's faster." The framing is *availability* — the booking response cannot depend on peer health.

## Category C — Why Haven't You Used X?

### C1. Why no Raft/Paxos for the slot log?

**What they're really testing:** Whether you understand when consensus is required.
**Strong answer:** Consensus is the right answer when you need a single global total order of operations with strong consistency. We explicitly don't need that: bookings partition naturally by (driver, vehicle, geographic cell), and our invariant (one vehicle per cell per slot) tolerates eventual convergence because we *use SERIALIZABLE on the local commit* and we have an application-level uniqueness primitive (journey_id + driver ID). Running Raft over three laptops would add a leader election, a log-compaction story, and a network-partition behaviour that we'd have to explain, for an invariant that is already enforced locally. The cost/benefit doesn't support it at the assignment's scale.
**Trap to avoid:** "Raft is too complex." It's not — the real answer is "the invariant doesn't need global total order."
**Follow-up:** "So what happens during a partition?" → Both partitions accept bookings (AP). On heal, the periodic sync pulls from the other side. If one partition confirmed a booking that conflicts with the other's confirmed booking, we currently *don't* detect it — both rows coexist in the merged state. **This is a real weakness** and a real viva target.

### C2. Why no distributed lock for the capacity counter?

**What they're really testing:** Whether your concurrency primitive choice is defensible.
**Strong answer:** The local Postgres row lock on `road_segment_capacity` + SSI detection handles within-node concurrency correctly. Adding a distributed lock would only help cross-node races, and a distributed lock across three peers with async-replicated state still can't prevent two peers both incrementing "current_bookings" locally before replication — because the lock would guard the *lock acquisition*, not the *data row* (which lives in different databases). The right fix for cross-node is either (a) a single authoritative shard per route — which is what the sharding module is a staging ground for — or (b) consensus. We picked replication + per-node serialization and live with the eventual-consistency window.
**Trap to avoid:** Suggesting that Redlock would solve this. It wouldn't — it would move the problem, not solve it.

### C3. Why no caching (Redis) for hot grid cells?

**What they're really testing:** Whether you see the obvious performance optimization and why you didn't take it.
**Strong answer:** Caching a counter that is part of a SERIALIZABLE transaction is anti-pattern — the cache would have to be invalidated atomically with the DB commit, and without 2PC between Redis and Postgres you get stale reads on the hottest path where correctness matters most. We rely on the Postgres buffer cache for hot rows (a grid cell being actively booked will be in shared_buffers). The measurable win from a dedicated Redis cache is zero for correctness-critical reads; it might help *read* capacity queries for a traffic dashboard, but we don't expose one.
**Trap to avoid:** Saying "Redis is fast." It's fast and wrong for this path.
**Follow-up:** "What about an LRU on the waypoint lookups?" → Fair — `loadRouteWaypoints` is called twice per booking (check + record). Memoizing routes by route_id in-process would save a round-trip. Not implemented; easy win.

### C4. Why no rate limiting on the conflict check endpoint?

**What they're really testing:** Whether you thought about DoS at the gateway boundary.
**Strong answer:** Rate limiting is applied at the nginx gateway layer, not the conflict service itself — see the gateway's `limit_req_zone`. Pushing it inside the conflict service would duplicate the mechanism and give no benefit because every booking goes through the gateway. The conflict service trusts that upstream callers (journey-service, plus gateway-authenticated external requests) have already been rate-limited.
**Trap to avoid:** Over-claiming — we do have rate limiting, just not in the service. The examiner may want to see it; know the nginx config location.

## Category D — Bottleneck Hunting

### D1. What is the slowest operation in a conflict check, and why?

**What they're really testing:** Whether you can profile your own code mentally.
**Strong answer:** The slowest phase is the capacity-check loop at [service.go:317-352](conflict-service/service.go#L317-L352) — it issues one `SELECT ... FOR UPDATE` per grid cell along the path. For a Dublin→Galway route (~200km, ~20 cells at 0.01° resolution) that's 20 sequential queries, each a round-trip to Postgres. At 1ms per round-trip that's 20ms just for capacity checks, on top of the driver/vehicle overlap check and the insert. Total P50 is around 30-40ms; P99 when Postgres is cold is 100-200ms. The loop is sequential because it has to be — each FOR UPDATE must succeed in order for SSI to track the dependency cleanly.
**Trap to avoid:** Claiming bcrypt is the bottleneck (that's user-service). Conflict-service is DB-bound.
**Follow-up:** "Can you batch them?" → Yes — a single query with `WHERE (grid_lat, grid_lng, time_slot_start) IN (...) FOR UPDATE` would lock all matching rows atomically and save 19 round-trips. I didn't implement it because (a) the sequential loop is easier to reason about during demo, and (b) for common short routes (3-5 cells) the batching win is small. Documented as future work.

### D2. Where does the replication bottleneck bite?

**What they're really testing:** Whether replication is a hidden throughput killer.
**Strong answer:** Replication is off the critical path — the `go replicateSlotToPeers(...)` call at [service.go:136](conflict-service/service.go#L136) launches a goroutine and returns immediately. The booking response doesn't wait. The bottleneck is downstream: each peer's `applyReplicatedSlot` acquires its own local transaction and does the same grid-cell walk to mirror the capacity counter. So a single booking on node A causes N-1 same-cost transactions on peer nodes. At scale that's an amplification factor of N, which is fine for 3 nodes, painful for 30.
**Trap to avoid:** "Replication is free." It isn't — it's just off the hot path.
**Follow-up:** "Suppose one peer is 10x slower than the others." → No impact on booking latency (fire-and-forget). Impact on peer: its queue of inbound /internal/slots/replicate POSTs grows. Replication messages don't queue locally (no durability); if overloaded, some will be dropped and recovered by the next periodic sync.

### D3. What does the SELECT FOR UPDATE actually lock?

**What they're really testing:** Whether you know the subtle bug in the report's strong-lock claim (M1).
**Strong answer:** This is the **exact gotcha** called out in the mismatch table as M1. The query at [service.go:330-339](conflict-service/service.go#L330-L339) is `SELECT id FROM road_segment_capacity WHERE ... AND current_bookings >= max_capacity LIMIT 1 FOR UPDATE`. When the cell is **not yet full**, zero rows match the predicate, zero rows are returned, and **zero row locks are held**. The SSI predicate lock is still in effect — two concurrent commits will trip the conflict detector — but the report's phrasing "FOR UPDATE guarantees mutual exclusion" is misleading. Correctness is guaranteed by SSI, not by the row lock.
**Trap to avoid:** Claiming the row lock is what saves you. It does, but only when the cell is already full. When the cell is empty, SSI is the only thing between you and a double-accept.
**Follow-up:** "Fix it." → Re-shape the query so FOR UPDATE hits the row unconditionally: `SELECT id, current_bookings, max_capacity FROM road_segment_capacity WHERE grid_lat=$1 AND grid_lng=$2 AND time_slot_start=$3 FOR UPDATE`, then check `current_bookings >= max_capacity` in application code. That holds a real row lock for the entire critical section and makes the lock the primary mechanism rather than the backup.

## Category E — Limitations

### E1. If two nodes both accept the same booking in the same millisecond and then replicate to each other, what ends up in the merged state?

**What they're really testing:** The worst failure mode of your eventual-consistency model.
**Strong answer:** Both rows survive. Node A has A's booking in its local table; B has B's. A pushes to B; B's `applyReplicatedSlot` sees `WHERE journey_id = $1` — the journey IDs are different, so the uniqueness check passes, and the second row is inserted. Now both tables have both rows. Next conflict check will detect the overlap (because both rows are present) and reject further bookings that collide, but the two original CONFIRMED journeys coexist. We have **no compensation / rollback / priority reconciliation** for this case. It is documented in the report limitations and in M2 of the mismatch table.
**Trap to avoid:** Pretending the invariant is globally preserved. It isn't — it's locally preserved and *asymptotically* preserved cross-node.

### E2. The grid cell resolution is 0.01° — how do you handle a booking whose origin is (51.00001, 0.00001)?

**What they're really testing:** Quantization edge cases.
**Strong answer:** `roundGrid` at [service.go:485](conflict-service/service.go#L485) snaps coordinates to the nearest 0.01° multiple: (51.00, 0.00). Two journeys starting 1 metre apart both resolve to the same cell and are conflated. That is the whole point of the grid — the cell represents a road segment, not a coordinate. The edge case is two *truly different* adjacent roads that share a grid cell: e.g. a ring road and a bypass separated by 500m both hashing into (53.34, -6.25). They would share capacity incorrectly. This is a resolution choice; refining it by 10x (0.001°) quadruples memory and index cost per cell and linearly increases the path-length loop count.
**Trap to avoid:** Claiming the grid is "fine-grained." It's deliberately coarse.

### E3. What happens if the periodic sync ticker dies silently?

**What they're really testing:** Observability of background jobs.
**Strong answer:** [replication.go:374-385](conflict-service/replication.go#L374-L385) launches `go func() { ticker := time.NewTicker(interval); ... for range ticker.C { ... } }()`. If a panic inside `syncFromPeer` propagates up to the goroutine, the goroutine dies and the sync never runs again. We don't have a supervisor. Each `syncFromPeer` call is wrapped in log statements for the happy path but there's no heartbeat telemetry to *prove* it ran recently. An operator would only notice when a late-joining node failed to catch up. This is a real observability gap.
**Trap to avoid:** Saying "Go goroutines are reliable." They are — unless they panic, and then they're dead silently.
**Follow-up:** "Fix it." → Recover inside the ticker loop, increment a "last_sync_at" metric, alert if > 2*interval.

### E4. You populate the shard assignment table at startup from `listAllRoutes` — what happens if a new route is added while the service is running?

**What they're really testing:** Whether the observability-only sharding is completely honest about its limits.
**Strong answer:** The new route won't appear in `/internal/shard/info` until the service restarts — `knownRouteIDs` is only written once at boot by [main.go:33-41](conflict-service/main.go#L33-L41). Because sharding is observability-only (M3/M6: no write routing), the actual correctness of bookings is not affected; only the reported PRIMARY/REPLICA role for that route in the admin dashboard is wrong. If the sharding were load-bearing (real write routing), this would be a silent correctness bug on route addition.
**Trap to avoid:** Presenting the sharding as if it were a real routing mechanism. It's diagnostic only.

## Category F — Failure Scenarios

### F1. What happens if Postgres goes away mid-transaction during a conflict check?

**What they're really testing:** Whether you know the precise failure boundary.
**Strong answer:** pgx/v5 returns a connection error on the in-flight query; the transaction aborts (pgx auto-rolls-back via the `defer tx.Rollback(ctx)` guard at [service.go:71](conflict-service/service.go#L71)). No partial state — either the commit landed or nothing landed. The handler returns 500; journey-service's circuit breaker counts the failure; after 3 failures it opens and subsequent bookings fail fast with REJECTED. Recovery path: Postgres comes back, the circuit breaker half-opens after its reset window, a probe call succeeds, the circuit closes, bookings resume. No stale rows, no replay needed.
**Trap to avoid:** "The goroutine crashes." No — pgx errors propagate as errors, not panics.

### F2. What happens if the Postgres commit succeeds but the `go replicateSlotToPeers` goroutine never runs (process killed between commit and scheduling)?

**What they're really testing:** The transactional outbox gap on the replication side.
**Strong answer:** The slot is in the local DB but was never pushed to peers. Peers don't know about it. The next periodic sync (every 5 min, [main.go:56](conflict-service/main.go#L56)) will pull from this node or this node's peers will pull from it — whichever runs first — and the row will be discovered via the GET `/internal/slots/active` handler. Within at most 5 minutes, peers catch up. During the gap, peers could accept a conflicting booking, and both rows would coexist per E1.
**Trap to avoid:** Claiming the slot is "lost." It's committed locally — just not propagated.
**Follow-up:** "So you don't have a transactional outbox on the conflict service like journey-service has." → Correct. The conflict service relies on pull-sync as its durability mechanism instead. The pull-sync is authoritative: even if every push fails forever, periodic pull will eventually converge. It's a weaker guarantee (up to 5 min stale) but simpler code.

### F3. What happens when a peer comes back from a long outage?

**What they're really testing:** Rejoin convergence.
**Strong answer:** Three things happen. (1) The returning node pings its configured peers via `POST /internal/peers/register`, which triggers `addPeer` at [replication.go:59-77](conflict-service/replication.go#L59-L77) and immediately kicks off `syncFromPeer` to pull the peer's full active-slot snapshot. (2) The `gossipNewPeer` path at [replication.go:106-155](conflict-service/replication.go#L106-L155) tells every existing peer about the returning node, so the mesh rebuilds without operator intervention. (3) Each pull-sync processes rows one by one via `applyReplicatedSlot`, which is idempotent on `journey_id`, so it's safe if a slot is already locally present. Convergence time is bounded by the slot count × applyReplicatedSlot cost (each does its own grid-cell walk to rebuild capacity counters).
**Trap to avoid:** "It auto-heals magically." It auto-heals by *design*: pull-sync + idempotency + gossip. Name the three pieces.

### F4. What happens if gossip reaches some peers but not others during a rejoin?

**What they're really testing:** Whether the mesh formation is tolerant of partial delivery.
**Strong answer:** Each gossip POST is fire-and-forget in its own goroutine ([replication.go:115-131](conflict-service/replication.go#L115-L131)), so if one peer's gossip drops, that peer never learns about the new node until either (a) a future gossip from another peer fills it in, or (b) an operator manually reaches the peer with the registration call. There is **no retry** on gossip failure — the log line says "could not reach" and the goroutine exits. A missed gossip means *replication is asymmetric*: the new node may push to the missed peer (if it has the peer in its own config) but the missed peer won't push back. On next periodic sync, the missed peer will *pull* from the new node regardless of having gossiped — which partially heals the gap.
**Trap to avoid:** Saying "gossip always works." It doesn't — the implementation is single-shot.

### F5. What if the same booking is processed twice by the RabbitMQ consumer?

**What they're really testing:** Idempotency of the async path.
**Strong answer:** The conflict service also runs a RabbitMQ consumer (wired in [main.go:58](conflict-service/main.go#L58)) for journey lifecycle events (e.g. cancellations propagated from journey-service). The consumer path applies cancellations via the same `cancelBookingSlot` function. `cancelBookingSlot` returns `ErrNotFound` if the journey is already inactive — the handler treats NotFound as a no-op ([handlers.go:53-58](conflict-service/handlers.go#L53-L58)), so duplicate cancellations don't double-decrement capacity. The slot-replication receiver is idempotent on `journey_id` via the explicit existence check before insert.
**Trap to avoid:** Forgetting that idempotency at the consumer is separate from at-most-once delivery at the broker.

### F6. Can a malformed `ConflictCheckRequest` panic the service?

**What they're really testing:** Input hardening.
**Strong answer:** The JSON decoder at [handlers.go:26](conflict-service/handlers.go#L26) returns 400 on decode error; the FlexTime custom unmarshaller at [service.go:18-28](conflict-service/service.go#L18-L28) returns an error on unparseable time. Negative `EstimatedDurationMinutes` would produce an arrival time *before* departure, which is silently accepted — the grid-cell walk would still work (the step count is abs-valued), but the SSI bucket math would be on a zero-or-negative duration. No panic, but an incoherent booking. We don't validate positivity. **Gap worth flagging.**
**Trap to avoid:** Claiming input is fully validated. It isn't — we rely on Journey Service upstream to sanity-check.

## Category G — Requirements Defense

### G1. FR4: "No two vehicles may simultaneously occupy a road segment of capacity 1." Prove your service satisfies this.

**What they're really testing:** The central correctness claim.
**Strong answer:** Within a single node, the combination of SSI + the capacity-check loop satisfies it. Two concurrent transactions both try to book (cell C, slot S). Both take a snapshot; both see `current_bookings = 0`. Both proceed to the `recordBookingSlot` phase. The `incrementCapacity` `INSERT ... ON CONFLICT DO UPDATE` fires for both, creating a read-write dependency cycle that SSI detects on commit. The second committer is aborted with SQLSTATE 40001 and its transaction is rolled back — no row written, no counter incremented. The first commit lands cleanly, invariant preserved. Cross-node, the invariant is eventually preserved with a replication window of up to 5 minutes.
**Trap to avoid:** Claiming strong cross-node guarantees. Name the bound: 5-minute pull-sync window.

### G2. FR5: "Cancellation must free the slot." Does it actually free the same cells?

**What they're really testing:** Round-trip symmetry of book + cancel.
**Strong answer:** [service.go:419-468](conflict-service/service.go#L419-L468) `cancelBookingSlot` re-runs the exact same grid-cell algorithm, keyed on either the stored `route_id` (for predefined routes) or straight-line fallback. The key insight is that `pathGridCellsFromWaypoints` is deterministic — same inputs, same outputs — so decrementing walks the same set of (grid_lat, grid_lng, time_slot_start) keys that were incremented at booking. `decrementCapacity` uses `GREATEST(0, current_bookings - 1)` so even if we somehow over-decrement, we never go negative.
**Trap to avoid:** Missing the route_id branch. If we dropped route_id here, straight-line cancellation would try to decrement cells that were never incremented (because booking used waypoints).
**Follow-up:** "What if the route was deleted between booking and cancellation?" → `loadRouteWaypoints` returns an error, we fall through to straight-line, and we decrement a *different* set of cells — leaking capacity on the original waypoint cells. Real bug in a route-delete scenario. Luckily routes are not deletable in the UI.

## Category H — Report vs. Reality (service-specific)

### H1. Sec 4.6 says "SELECT FOR UPDATE guarantees…" — see M1. The FOR UPDATE is on a predicate and only locks rows when the cell is *already* full. Own this in viva.

### H2. Sec 4.6 says "deadlock and retry" — see M2. There is **no** retry loop; 40001 surfaces as 500 and the Journey saga treats it as REJECTED. This is the biggest gap in the report's correctness claims.

### H3. Sec 4.8 says "consistent-hash sharding" — see M3/M6. [sharding.go:19](conflict-service/sharding.go#L19) explicitly documents itself as *"write authority, NOT data isolation — all nodes still store all booked_slots for local conflict detection"*. It is observability-only. The examiner will push on this — be honest.

### H4. Sec 4.6 says "eventual consistency across nodes via push + pull". Push is confirmed at [replication.go:161-202](conflict-service/replication.go#L161-L202); pull at [replication.go:337-369](conflict-service/replication.go#L337-L369); periodic sync at [replication.go:374-385](conflict-service/replication.go#L374-L385). ✅

### H5. Sec 4.6 says "each cancellation replicated to peers". Confirmed at [handlers.go:68](conflict-service/handlers.go#L68) via `go replicateCancelToPeers(journeyID)`. ✅

## Conflict Service — 10-Minute Cheat Sheet

**3 most likely questions + answers:**
1. **"What isolation level and why?"** → PostgreSQL SERIALIZABLE (SSI, predicate-lock based, optimistic). Two concurrent bookings of the same (cell, slot) both take a snapshot showing `current_bookings < max`, both proceed, SSI detects the read-write dependency cycle on commit, second gets SQLSTATE 40001 and aborts. The row-lock FOR UPDATE only fires when the cell is *already* full — SSI is the actual correctness mechanism, per mismatch M1.
2. **"How does cross-node replication stay consistent?"** → Two mechanisms. Push (fire-and-forget goroutine after local commit, async HTTP to each peer) and pull (periodic 5-min sync + on-demand sync when a new peer joins). Receivers are idempotent on `journey_id`. Pull is the authoritative backstop — even if every push fails, pull eventually converges. Consistency model is eventual with a ≤5-min staleness window.
3. **"Why a grid instead of PostGIS?"** → Deterministic replay for cancellation (cancel must decrement the same cells that book incremented), no PostGIS dependency, and time-slot bucketing makes it a hash lookup anyway. Trade-off is 1km resolution and longitude distortion at non-equatorial latitudes.

**3 strongest design decisions:**
1. **SERIALIZABLE + single atomic transaction for the full check+reserve.** Simpler to reason about than breaking it into multiple optimistic phases with compensations.
2. **Fire-and-forget replication + periodic pull-sync.** Booking latency bounded by local commit; convergence is authoritative via pull. No durable-queue complexity.
3. **Piecewise-linear waypoint grid cells.** Real road paths (Dublin → Galway via Athlone) produce a capacity footprint that matches the road, not a straight line through the middle of Ireland.

**3 biggest weaknesses + framing:**
1. **FOR UPDATE on a predicate (M1).** *Framing:* "SSI is the real correctness mechanism; the row lock is a secondary aid that fires only when the cell is already full. I would reshape the query to hold a real row lock unconditionally."
2. **No 40001 retry loop (M2).** *Framing:* "Serialization failures currently surface as rejections. Production would wrap the transaction in a bounded retry with jitter."
3. **Sharding is observability-only (M3/M6).** *Framing:* "The sharding module logs PRIMARY/REPLICA roles for the admin dashboard, but it does not route writes — every node reserves every slot locally and we rely on replication for cross-node convergence. The hook is there to become write-routing."

**The 1 thing you must NOT say:**
**"The FOR UPDATE prevents double-booking."** — It doesn't, on its own. SSI does. If you say this the examiner will immediately point at M1 and you'll have to walk it back. Lead with SSI, mention FOR UPDATE as the cell-full case.

---

# Notification Service — Aditya Kumar Singh

**Stack:** Go 1.22, chi router, amqp091-go (RabbitMQ), gorilla/websocket, go-redis/v9 (Redis DB 3 via Sentinel in multi-node mode), JWT auth.
**Key files:** [notification-service/consumer.go](notification-service/consumer.go), [redis.go](notification-service/redis.go), [handlers.go](notification-service/handlers.go), [main.go](notification-service/main.go).

## Category A — Why This Over That?

### A1. Why RabbitMQ pub/sub instead of Kafka or Redis Streams?

**What they're really testing:** Whether you picked the right broker for the delivery semantics you need.
**Strong answer:** Notification is a fan-out of domain events to interested consumers (notification, analytics, future audit). The event rate is low (one per journey lifecycle transition), order within a single journey matters (can't deliver "completed" before "started") but order *across* journeys does not. RabbitMQ with a topic exchange (`journey_events` at [consumer.go:20](notification-service/consumer.go#L20)) gives us routing by key plus DLQ with TTL for poison messages out of the box. Kafka would give us replay-from-offset, which we don't need — notifications are *present-tense*, an event missed is not replayed an hour later. Redis Streams would put the whole event history in memory on the Redis master, which overloads the component that is also our cache for notification history and distributed lock.
**Trap to avoid:** "RabbitMQ is easier." The real framing is: routing by key + DLQ + TTL + per-consumer ack semantics match the requirements.
**Follow-up:** "Do you lose messages on broker restart?" → No — `ExchangeDeclare(..., durable=true)` and `QueueDeclare(..., durable=true)` at [consumer.go:277, 282](notification-service/consumer.go#L277-L282), plus `x-message-ttl: 86400000` (24h). Published messages survive broker restart; they only die after 24h unprocessed, at which point they land in the DLQ.

### A2. Why WebSocket for push instead of Server-Sent Events or long-polling?

**What they're really testing:** Whether you understand the transport trade-off.
**Strong answer:** WebSocket gives full duplex, meaning the client can send `ping` for liveness and the server can immediately close with a close code 4001 on auth failure ([handlers.go:58-63](notification-service/handlers.go#L58-L63)). SSE is half-duplex — we can push but not receive — and has no close-code channel. Long-polling burns a connection per request and turns a push system into a pull system with variable latency. For notifications which must feel "instant" to the driver, WebSocket gives us sub-50ms end-to-end delivery after the event leaves the broker.
**Trap to avoid:** Claiming WebSocket is "always better." SSE is actually a cleaner choice for pure server-to-client streams through some proxies; WebSocket wins because we use the ping/pong.
**Follow-up:** "How do you handle WebSocket scale?" → Each notification-service instance holds its own `wsConns` map ([consumer.go:63-65](notification-service/consumer.go#L63-L65)). If a user has a tab on Laptop A's notification service, and a journey confirmation arrives on Laptop B's notification service, Laptop B pushes only to its own sockets — Laptop A's user won't see it in real-time. They *will* see it on page refresh (via Redis history), so the degraded mode is just "slower UI, eventually correct." At scale the fix is a Redis pub/sub fan-out so all notification instances receive every event and filter locally; we don't have that yet.

### A3. Why Redis for notification history instead of PostgreSQL?

**What they're really testing:** Storage-choice trade-offs for semi-durable data.
**Strong answer:** Notifications are capped — max 50 per user, 7-day TTL. The storage primitive that fits perfectly is `LPUSH` + `LTRIM 0 49` + `EXPIRE 7d`, which is a 3-line pipeline in Redis ([redis.go:56-62](notification-service/redis.go#L56-L62)) and trivially concurrent-safe because all three commands run in a single MULTI-like pipeline. Doing the same in Postgres would require a table, an insert, a DELETE of anything beyond the 50th entry per user, and a periodic cleanup job for TTL. Redis's list type is *the* right data structure; Postgres isn't. Durability is degraded — a Redis crash between AOF syncs loses recent notifications — but they're not business-critical, and the authoritative record lives in RabbitMQ (ack-on-store) plus the analytics service's immutable `event_logs` table.
**Trap to avoid:** Saying "Redis is faster." The framing is: the data shape is a capped list, and Redis has the native primitive.
**Follow-up:** "What if Redis loses a notification?" → User sees one fewer item in history. They do *not* miss the real-time toast (WebSocket delivered synchronously before `storeNotification` at [consumer.go:207-209](notification-service/consumer.go#L207-L209)). Durable authoritative copy is in analytics.

### A4. Why JWT in the WebSocket query parameter instead of the Authorization header?

**What they're really testing:** Whether you know the WebSocket auth constraint and defended the choice.
**Strong answer:** Browser WebSocket APIs **do not allow custom headers** — the `WebSocket(url, protocols)` constructor only accepts a URL and subprotocol array. There is no way for a browser client to set `Authorization: Bearer …` on the initial HTTP upgrade. The only options are: (a) query parameter, (b) subprotocol field, (c) cookie. We used query parameter because it's the most portable across browsers and the simplest to validate server-side at [handlers.go:54-56](notification-service/handlers.go#L54-L56). The security cost is that tokens *can* appear in server access logs; we mitigate by never logging the full URL with query string for /ws/ paths.
**Trap to avoid:** Claiming query params are fine without acknowledging the logging risk.
**Follow-up:** "What about token expiry mid-session?" → The JWT is decoded once at connection upgrade. A token that's valid at upgrade but expires mid-connection stays valid for the life of the socket. Fix would be a periodic re-validation with a close-code on failure. Not implemented — this is a real gap.

## Category B — Why Use This At All?

### B1. Why a separate notification service at all — can't Journey Service push directly?

**What they're really testing:** Whether the service boundary is justified.
**Strong answer:** Three reasons. (1) **Decoupling**. Journey Service's synchronous path should commit the journey and return 200; it shouldn't be holding a WebSocket connection open to a user that might be on the other side of the world. (2) **Transactional outbox**. Journey Service writes its domain event to the outbox and the outbox publisher drains it at 2s intervals; the notification service consumes the published event and fans it out. That separation is what makes "no notification lost" survivable across a notification-service crash — the event is still in RabbitMQ. (3) **Independent scale**. Notification is WebSocket-bound; Journey is DB-bound. They have completely different scaling profiles and should scale on their own metrics.
**Trap to avoid:** "Separation of concerns." Too vague. Name the three real reasons.

### B2. Why the DLQ? What actually goes there?

**What they're really testing:** Whether you can articulate the poison-message failure mode.
**Strong answer:** The queue is configured with `x-dead-letter-exchange: dlxExchange` at [consumer.go:282-285](notification-service/consumer.go#L282-L285). A message lands in the DLQ when (a) `handleEvent` returns an error and we `msg.Nack(false, false)` with `requeue=false` at [consumer.go:312](notification-service/consumer.go#L312), or (b) the 24h TTL expires without the message being processed. The first case catches structurally broken events (invalid JSON, schema mismatch); the second catches messages that were waiting during a long notification-service outage. DLQ content can be inspected manually; in production we'd add a replay-from-DLQ admin endpoint. Currently the DLQ is terminal — messages sit there until an operator dumps them.
**Trap to avoid:** Missing the requeue=false. With requeue=true, a poison message would retry forever and starve the queue. The requeue=false is critical.

### B3. Why the consumer-side deduplication with Redis keys?

**What they're really testing:** Whether you understand at-least-once delivery.
**Strong answer:** RabbitMQ gives at-least-once — if we ack a message and then crash before the ack reaches the broker, the broker redelivers on reconnect. Without dedup, a duplicate delivery means the user sees two identical toast notifications and two entries in their history list. [consumer.go:138-167](notification-service/consumer.go#L138-L167) hashes the message body (or uses `MessageId` if the producer set one) and stores `notif:processed:<hash>` in Redis with a 24h TTL. Next delivery of the same body hits the cache and is acked-without-processing. The 24h TTL matches the queue's `x-message-ttl` — any duplicate redelivery happens within the dedupe window.
**Trap to avoid:** Claiming this makes the pipeline exactly-once. It doesn't — it's *effectively* exactly-once with a small probability of a race between the processing and the Redis SET.

### B4. Why store per-user notification history if the event is already persisted in analytics?

**What they're really testing:** Whether the duplication is principled.
**Strong answer:** Analytics stores system-wide immutable event logs for audit; it's optimised for time-range queries by event type, not per-user reads. The UI needs "give me my last 20 notifications right now" — a query pattern that would hammer analytics with a per-user scan every time the history panel loads. Redis serves that query in a single LRANGE, ~1ms. The duplication is a read-optimization cache, with the analytics log as the authoritative source on which it's based.
**Trap to avoid:** Saying "because we can." Frame it as a materialised view.

## Category C — Why Haven't You Used X?

### C1. Why no push notifications (APNs, FCM)?

**What they're really testing:** Whether you noticed the obvious requirement and justified excluding it.
**Strong answer:** The exercise scope is a web dashboard, not a mobile app. Driver notifications go to the browser via WebSocket while the dashboard is open. For a production deployment with mobile clients, we'd add FCM/APNs as another consumer of `journey_events` — the notification-service shape *already supports it*: add a new function alongside `pushToWS` that calls the APNs/FCM SDK, and route based on user's registered devices. Documented as future work in the report.
**Trap to avoid:** Claiming WebSocket replaces push. It doesn't — when the browser is closed, the user is unreachable. This is a real coverage gap.

### C2. Why no fan-out to all notification-service instances (Redis pub/sub across pods)?

**What they're really testing:** The scale limitation of the per-instance WebSocket registry.
**Strong answer:** Today each notification-service instance only knows about its own WebSocket connections. A user connected to Laptop A's notification service won't receive an event delivered to Laptop B's consumer. We rely on RabbitMQ topic routing to deliver each event to *all* notification-service instances — but the queue binding is shared across replicas, so RabbitMQ load-balances one message to *one* consumer. That means only one notification-service instance sees the event, and only users on that instance get the real-time push. Other users fall back to "refresh to see" mode. The proper fix is either (a) each consumer has its own queue (broadcast) so every instance sees every event, or (b) a Redis pub/sub fan-out layer. We didn't implement either — this is a meaningful limitation. ✗
**Trap to avoid:** Not knowing this! The examiner will probe it.
**Follow-up:** "Show me the queue-per-instance fix." → Change `QueueDeclare(notificationQueue, ...)` to `QueueDeclare("", ...)` — an anonymous exclusive queue — so each instance gets its own. Per-instance queue binds to the same exchange with the same routing keys, so every instance gets every event.

### C3. Why no message priority?

**What they're really testing:** Whether you thought about different event classes.
**Strong answer:** All our notification events are equally urgent from the user's point of view (confirmed/rejected are both end-user-visible outcomes). RabbitMQ supports `x-max-priority` on the queue, but without a priority difference in the producer, it adds overhead for no benefit. If we later added promotional events ("check out the new route!") those would be low-priority and warrant separation — probably into their own queue rather than via the priority header.
**Trap to avoid:** Adding features for no reason. Defend the omission.

### C4. Why no rate limiting on /ws/notifications?

**What they're really testing:** Whether you hardened the WebSocket endpoint.
**Strong answer:** WebSocket upgrade is rate-limited at the nginx gateway using `limit_conn` (concurrent connections per IP) and `limit_req` for the upgrade request itself. Beyond the upgrade, a held connection sends `ping` and nothing else in the normal case; we don't read or process arbitrary client messages, so a chatty client can't amplify server work. A flood of upgrade requests is the attack surface, and that's handled at the gateway. Inside the service, the `for` loop in [handlers.go:78-88](notification-service/handlers.go#L78-L88) only responds to literal "ping" messages — anything else is silently read and discarded.
**Trap to avoid:** Claiming WebSockets are free. They're not — each one is a goroutine + a file descriptor.

## Category D — Bottleneck Hunting

### D1. What's the rate limit of the notification pipeline?

**What they're really testing:** Whether you can estimate the throughput.
**Strong answer:** The bottleneck is `handleEvent` → `storeNotification` (Redis pipeline) → `pushToWS` (per-socket WriteMessage under a mutex). Redis pipeline is ~1ms. `pushToWS` holds `wsMu.Lock()` while iterating the user's sockets — for a user with 2-3 browser tabs that's maybe 500µs. QoS is set to `ch.Qos(10, 0, false)` at [consumer.go:260](notification-service/consumer.go#L260), so at most 10 messages in flight per consumer. With 1-2ms per message, one consumer processes ~500-1000 msg/s. For the assignment (peak ~167 bookings/s × ~5 events per booking = ~800 events/s) that's right at the edge; in production we'd increase QoS and scale consumers horizontally.
**Trap to avoid:** Claiming the WebSocket write is free. The `wsMu` mutex is coarse-grained — it blocks *all* WebSocket operations across *all* users, not just the one being pushed to. That's a real contention point at scale.

### D2. Is the `wsMu` mutex a contention bottleneck?

**What they're really testing:** Whether you spotted the coarse lock.
**Strong answer:** Yes, it's coarse. [consumer.go:92-93](notification-service/consumer.go#L92-L93) `pushToWS` takes `wsMu.Lock()` (not even RLock — it's the write lock because dead connection cleanup may mutate the map). Concurrent pushes to *different* users serialise through the same lock. For a service with hundreds of concurrent users, this is a clear optimization target: shard the map by user_id hash, or use per-user sync.Mutex wrapping the per-user slice. Not implemented — documented weakness. For the assignment demo (dozens of users max), it's not hit.
**Trap to avoid:** Saying "the map is sync.RWMutex-protected, so it's fine." RWMutex is still a global mutex when a writer needs exclusive access — which pushToWS does because it may remove dead sockets.

### D3. What's the 24h TTL at [consumer.go:284](notification-service/consumer.go#L284)?

**What they're really testing:** Whether you can trace the message lifetime.
**Strong answer:** `x-message-ttl: 86400000` (24h in ms). A message that sits in `notification_events` queue for 24h without being consumed is dead-lettered to the DLQ via the exchange binding. This bounds our max backlog retention: during a long notification-service outage, we keep events for up to 24h; anything older is moved to DLQ and requires manual replay. The number is a compromise — too long and the queue can grow unboundedly during an outage; too short and a weekend outage drops weekday notifications.
**Trap to avoid:** Confusing message TTL with queue TTL. This is the per-message TTL set at queue declaration; it applies to every message put in the queue.

## Category E — Limitations

### E1. If notification-service crashes while a user's WebSocket is open, what happens?

**What they're really testing:** Connection-state durability.
**Strong answer:** The WebSocket closes immediately from the client's perspective (TCP RST or orderly close depending on how the process died). The user's client code must reconnect — we rely on client-side reconnection logic in `frontend/src/utils/resilientFetch.ts` and the notification hook. During the gap (reconnect time), any events delivered to the broker are still acked-and-stored in Redis; the user sees them on the next history fetch or the next /api/notifications/ poll. Real-time toast is lost for those events — we do not replay "missed while disconnected" to the reconnecting socket.
**Trap to avoid:** Claiming the user "sees them on reconnect." They see them in the history panel, not as popup toasts. The toast is real-time-only.

### E2. What if RabbitMQ loses its data volume?

**What they're really testing:** Broker durability boundaries.
**Strong answer:** RabbitMQ persists durable messages to disk; a volume loss wipes them. The fallback is the transactional outbox in journey-service: unpublished outbox rows would be replayed on the next outbox drainer tick ([outbox_publisher.py](journey-service/app/outbox_publisher.py)). For events that *were* published before the broker data loss, those are gone — journey-service marks outbox rows as `published=true` *after* a successful publish, so once the publish succeeds we don't re-push even if RabbitMQ forgets. **This is a real gap:** the outbox gives us at-least-once from app to broker, but broker crash + volume loss after the ack produces lost events. Documented as out-of-scope.
**Trap to avoid:** Claiming the outbox replays "everything." It only replays `published=false` rows.

### E3. The notification TEMPLATE replacement at [consumer.go:187-197](notification-service/consumer.go#L187-L197) uses literal `strings.ReplaceAll`. What about an event with the literal string `{origin}` in the `user_name` field?

**What they're really testing:** Input hygiene / template injection.
**Strong answer:** If `user_name = "{origin}"`, then `strings.ReplaceAll(msg, "{user_name}", "{origin}")` first replaces `{user_name}` with the literal `{origin}`, and the next `ReplaceAll(msg, "{origin}", ...)` then substitutes it again. The attacker can inject any placeholder substitution into their own notification. It's not a security issue (user-name is per-user), but it's a template bug. Fix: use `strings.NewReplacer(...).Replace(msg)` which does a single pass. Not fixed — small issue, unlikely to show up in demo.
**Trap to avoid:** Waving it off as "who cares." Own small bugs.

## Category F — Failure Scenarios

### F1. What happens when RabbitMQ goes down?

**What they're really testing:** Broker failure handling.
**Strong answer:** The consumer loop detects the broker gone via `NotifyClose` at [consumer.go:238](notification-service/consumer.go#L238); the auto-reconnect goroutine fires and enters a reconnect loop with 3s sleep between attempts, up to the broker reappearing. New events published during the outage queue up in the producer (journey-service's outbox rows stay `published=false`). On reconnect, the consumer is re-declared against the same durable queue, and processing resumes. No event loss provided the outbox drainer still has unpublished rows and the broker volume is intact.
**Trap to avoid:** Saying "we retry forever." Confirm the 10-attempt initial connect loop at [consumer.go:217-226](notification-service/consumer.go#L217-L226) — *initial* startup gives up after 10 attempts × 3s. Once running, the reconnect goroutine retries indefinitely.

### F2. What happens when Redis goes down?

**What they're really testing:** Cache-layer failure mode.
**Strong answer:** `storeNotification` and `getNotifications` both `log.Printf` the error and return — the toast is still pushed to the WebSocket live (that code path is at [consumer.go:209](notification-service/consumer.go#L209) before storeNotification). History fetches return empty. Dedup check at [consumer.go:146-156](notification-service/consumer.go#L146-L156) fails open (returns false on Redis error), meaning during a Redis outage we could process a duplicate. That's a deliberate choice: silent duplicates are less bad than silent drops. Once Redis recovers, new notifications store normally and new dedup keys are written.
**Trap to avoid:** Claiming Redis outage breaks the pipeline. It degrades the pipeline: real-time push continues, history and dedup are impaired.

### F3. What if two notification-service instances both receive the same event?

**What they're really testing:** Competing consumers semantics.
**Strong answer:** They don't — today. The `notification_events` queue is a single queue with multiple consumers sharing it; RabbitMQ delivers each message to *exactly one* consumer in round-robin. Only one instance runs `handleEvent` per message. That's good for not storing the notification twice, bad for WebSocket fan-out (see C2). If we switch to exclusive anonymous queues, both instances *will* see each event and we'd need idempotency on `storeNotification` — which we already have via dedup keys.
**Trap to avoid:** Confusing the queue topology with a broadcast.

### F4. What if a consumer is slow and messages pile up?

**What they're really testing:** Back-pressure.
**Strong answer:** QoS is 10 ([consumer.go:260](notification-service/consumer.go#L260)) — prefetch limit. The broker will not send an 11th message until the consumer acks one of the 10 in-flight. Messages pile up in the queue, not in the consumer's memory. If the queue grows past Rabbit's internal limits (paging to disk threshold), performance degrades; if it grows past the 24h TTL, old messages are dead-lettered. This is back-pressure at the broker level — applied upstream to the publisher not via explicit flow control, but via the publisher never blocking (outbox rows just stay `published=false` longer).
**Trap to avoid:** Not knowing what the QoS prefetch does.

### F5. JWT token validation via HS256 shared secret — what if the secret is leaked?

**What they're really testing:** Blast radius of secret compromise.
**Strong answer:** Anyone with the secret can mint tokens for any user_id and open WebSocket connections or fetch history for arbitrary users. The mitigation is operational: the secret lives in an env var, not in source, and is rotated per environment. There's no token revocation — a leaked token is valid until its expiry (JWT `exp` claim). No blacklist. A production fix would be short expiry (~5 min) plus refresh tokens, or a Redis-backed revocation list. Documented as trade-off with the user-service JWT answer A5.
**Trap to avoid:** Claiming the secret is safe. It's "safe enough" for the exercise — own it.

## Category G — Requirements Defense

### G1. FR6: "Drivers must receive acceptance notification before starting." How do you guarantee it?

**What they're really testing:** The real-time delivery requirement.
**Strong answer:** Three layers. (1) Synchronous HTTP response: the booking request returns 200 `CONFIRMED` *before* the event is published to RabbitMQ — the client UI can react to that directly without waiting for the notification. (2) RabbitMQ pub/sub: the journey.confirmed event is fanned out to notification-service, which pushes to any open WebSocket for that user. (3) Redis history: if the browser tab isn't open at event time, the notification is stored for retrieval on next page load (7-day retention). So: we **don't need** the WebSocket to guarantee the requirement, because the sync response itself is the acceptance. The notification pipeline exists for the asynchronous path (cross-tab, history view, future mobile).
**Trap to avoid:** Claiming the requirement depends on WebSocket delivery. It doesn't — the HTTP response is the primary acceptance channel.

### G2. FR7: "Rejections must be communicated." Same as G1?

**Strong answer:** Same mechanism, with one subtlety: rejections always come with a human-readable `rejection_reason` which the template at [consumer.go:44-45](notification-service/consumer.go#L44-L45) interpolates. If the producer omits the reason, the template falls back to "N/A" rather than leaving the placeholder literal in the message. The UI displays the reason in a red toast. No retry — a single delivery attempt, because rejection is informational and the driver can retry the booking.

## Category H — Report vs. Reality (service-specific)

### H1. Sec 4.6.4 says "notification history has 7-day TTL and max 50 entries per user". Confirmed at [redis.go:58-59](notification-service/redis.go#L58-L59): `LTRIM 0, 49` (50 entries) + `Expire 7*24h`. ✅

### H2. Sec 4.3 says "DLX for poison messages". Confirmed: `dlxExchange` declared at [consumer.go:265](notification-service/consumer.go#L265) with the queue bound via `x-dead-letter-exchange`. ✅

### H3. Sec 4.6 says "consumer-side deduplication via Redis keys". Confirmed at [consumer.go:138-167](notification-service/consumer.go#L138-L167) — `notif:processed:<hash>` with 24h TTL. ✅

### H4. Sec 4.6 says "notification events delivered over WebSocket and also stored in history". Matches [consumer.go:207-209](notification-service/consumer.go#L207-L209). ✅

### H5. Sec 4.3 says "notification-service's in-memory WebSocket registry is per-instance and does not fan out across instances". Should be in the limitations section — the examiner may point out it isn't. If it's not explicitly acknowledged in the report, own it as an unstated limitation (see C2).

## Notification Service — 10-Minute Cheat Sheet

**3 most likely questions + answers:**
1. **"How do you prevent duplicate notifications?"** → RabbitMQ is at-least-once, so we dedupe on the consumer side. We hash the message body (or use `MessageId` if set) and store `notif:processed:<hash>` in Redis with a 24h TTL, matching the queue's own `x-message-ttl`. Duplicate delivery within the dedupe window is acked without re-processing. If Redis is down the dedup fails open — we accept a rare duplicate over a silent drop.
2. **"Walk me through a poison message."** → `handleEvent` returns an error (bad JSON, missing field). We `msg.Nack(false, false)` with `requeue=false`, which routes the message through the queue's `x-dead-letter-exchange: journey_events_dlx` to `dead_letter_queue`, where it sits until an operator inspects it. We do *not* retry poison messages in-place — that would starve the queue.
3. **"Why Redis LPUSH+LTRIM+EXPIRE instead of a Postgres table?"** → The data shape is a capped, per-user, time-limited list. Redis has the exact primitive; Postgres would need an insert + delete + cleanup job. Redis serves `/api/notifications/` in ~1ms via LRANGE. Durability degraded but the authoritative copy lives in analytics's immutable event log.

**3 strongest design decisions:**
1. **WebSocket with ping/pong + consumer-side dedup.** Real-time delivery with exactly-once semantics from the user's view.
2. **DLQ with requeue=false.** Poison messages exit the main queue so they don't block live traffic.
3. **Redis capped list for history.** Right primitive for the data shape; fast reads for UI history panel.

**3 biggest weaknesses + framing:**
1. **Per-instance WebSocket registry (C2).** *Framing:* "Each notification-service instance only sees WebSockets it accepted. With shared queue binding, only one instance processes each event, so users on other instances fall back to history-refresh mode. Fix is per-instance anonymous queues OR Redis pub/sub fan-out."
2. **Coarse `wsMu` mutex (D2).** *Framing:* "Serialises pushes across all users. Not hit at demo scale; at hundreds of users this is the next optimization target — shard by user_id or use per-user locks."
3. **JWT in WebSocket query string + no revocation (E1, F5).** *Framing:* "Query-param auth is forced by browser WebSocket constructor limitations. Revocation would need short expiry + refresh tokens, not implemented."

**The 1 thing you must NOT say:**
**"Notifications are exactly-once delivered."** — They're at-least-once with consumer dedup, which is *effectively* exactly-once but not formally. If you claim exactly-once, the examiner will ask you to prove the impossibility argument and you'll be stuck.

---

# Enforcement Service — Saurabh Deshmukh

**Stack:** Python 3.11, FastAPI, httpx, redis-py async (Sentinel DB 4), RabbitMQ consumer for cache warming, shared circuit breaker.
**Key files:** [enforcement-service/app/service.py](enforcement-service/app/service.py), [consumer.py](enforcement-service/app/consumer.py), [main.py](enforcement-service/app/main.py).

## Category A — Why This Over That?

### A1. Why a dedicated Redis cache instead of just querying journey-service directly on every check?

**What they're really testing:** Whether the caching tier has a defensible performance argument.
**Strong answer:** The enforcement use case is roadside — a guard scans a vehicle registration and the system must return valid/invalid in sub-50ms, *while disconnected from anything non-critical*. Going through journey-service adds (a) network RTT to journey-service, (b) a Postgres query on `journeys`, (c) a vulnerable coupling to journey-service availability. Redis at DB 4 ([service.py:34](enforcement-service/app/service.py#L34)) serves the hot key `active_journey:vehicle:<reg>` in ~1ms. The cache is populated *proactively* by consuming `journey.confirmed` / `journey.started` events ([consumer.py:52-80](enforcement-service/app/consumer.py#L52-L80)), so the cache is hot by the time the first roadside query lands. Enforcement remains functional even when journey-service is completely down — as long as the cache has the relevant entry, verification proceeds. Fallback to journey-service is the *second* layer, not the first.
**Trap to avoid:** Claiming Redis is "just a performance boost." It's also the availability layer — cache hit = no dependency on journey-service.
**Follow-up:** "What if the event lands in the broker but this service hasn't consumed it yet?" → Cache miss, fall through to journey-service API at layer 2, apply the result. If journey-service is also down, the verify returns `is_valid=False` — we fail closed on ambiguity. That's the correct choice for enforcement (false negative = extra scrutiny; false positive = someone drives illegally).

### A2. Why a separate Redis database (DB 4) instead of sharing with user/notification service?

**What they're really testing:** Redis logical separation rationale.
**Strong answer:** Each service owns its Redis database index: user-service DB 3 (distributed lock), notification-service DB 3 (in the notification context is actually separate — dedup and notif history), enforcement DB 4. The separation gives us: (a) no key-namespace collisions across services, (b) per-service `FLUSHDB` without nuking unrelated caches, (c) independent TTL policies per database. In production each database could be migrated to its own Redis instance without code changes beyond env vars. Single Redis primary + Sentinel ([service.py:42-48](enforcement-service/app/service.py#L42-L48)) serves all of them today.
**Trap to avoid:** Claiming it's for security. DB indexes are not security boundaries in Redis — any client with the password gets all databases.

### A3. Why two lookup strategies (by vehicle vs by license) with different caches?

**What they're really testing:** Whether you understand the two real-world enforcement patterns.
**Strong answer:** A roadside guard can identify a vehicle visually (number plate) or identify a driver via their license (if pulled over). These map to two different keys: `active_journey:vehicle:<reg>` and `active_journey:user:<user_id>`. Both are populated by the same consumer event ([consumer.py:74-78](enforcement-service/app/consumer.py#L74-L78)) in a single Redis pipeline. License-based lookup needs an extra step: resolve `license_number` → `user_id` via user-service, which is cached separately with its own 24h TTL at [service.py:125, 144](enforcement-service/app/service.py#L125-L144). Splitting into two paths lets each route be optimized independently: vehicle is pure Redis, license is Redis-then-user-service-then-Redis.
**Trap to avoid:** Conflating the two paths in your answer. Be precise about which one needs user-service.

### A4. Why the `departure - 30min` grace window at [service.py:75](enforcement-service/app/service.py#L75)?

**What they're really testing:** Domain reasoning.
**Strong answer:** A driver can legitimately be in their vehicle and ready to depart before the booked start time — loading, checking directions, waiting for a passenger. Hardcoding "valid only at exact departure time" would fail these users. 30 minutes is a compromise: long enough for normal pre-departure activity, short enough that a driver who booked 9:00 AM can't be found "valid" at 6:00 AM on their registered vehicle. It's a policy choice, not a technical one.
**Trap to avoid:** Claiming there's a theoretical basis. It's an ops choice, defend it as such.

## Category B — Why Use This At All?

### B1. Why a separate enforcement service at all? Isn't this just a journey-service read endpoint?

**What they're really testing:** Service boundary justification.
**Strong answer:** Three reasons. (1) **Availability isolation.** Enforcement must work during journey-service outages — the guard at the roadside can't "come back later." Running a separate service with its own cache and its own deployment means journey-service can be down and enforcement still functions as long as the Redis cache has the entry. (2) **Security boundary.** Enforcement runs behind a different auth/role: "enforcement officer" role vs regular driver. Keeping the verification logic in its own service lets us apply different authn/authz policies without polluting journey-service. (3) **Independent scaling.** Enforcement reads are bursty (shift changes, checkpoint setups) and are predominantly cache hits; journey-service's write throughput ceiling shouldn't constrain enforcement read capacity.
**Trap to avoid:** Saying "microservices are better." Defend each of the three axes.

### B2. Why consume `journey.started` / `journey.completed` events instead of just `journey.confirmed`?

**What they're really testing:** Lifecycle-driven caching.
**Strong answer:** `journey.confirmed` puts the row in cache; `journey.started` refreshes the same keys (in case status info or estimated arrival was updated); `journey.completed` and `journey.cancelled` **delete** the cache entry ([consumer.py:82-88](enforcement-service/app/consumer.py#L82-L88)). Without the deletion handlers, a completed journey would sit in enforcement's cache until its TTL expired, and a driver doing a second booking would briefly appear to have two active journeys. Cleanup-on-completion keeps the cache consistent with the lifecycle.
**Trap to avoid:** Forgetting the TTL + the explicit delete work together. TTL alone would be too slow; explicit delete alone would leak on missed events.

### B3. Why Sentinel for Redis?

**What they're really testing:** HA at the cache layer.
**Strong answer:** Enforcement cannot tolerate a Redis outage — when the cache is gone, every query degrades to journey-service fallback, which couples our availability to theirs. Redis Sentinel ([service.py:41-47](enforcement-service/app/service.py#L41-L47) + [consumer.py:27-33](enforcement-service/app/consumer.py#L27-L33)) gives us automatic failover: a primary crash triggers a Sentinel-mediated leader election and promotes a replica, with client-side connection failover. Matches the report's 15s failover window (`down-after-milliseconds 5000` + `failover-timeout 10000`). Without Sentinel we'd need a manual restart plus all clients to reconnect to a new address.
**Trap to avoid:** Claiming Sentinel gives "zero downtime." It gives bounded (~15s) downtime.

## Category C — Why Haven't You Used X?

### C1. Why no offline mode for enforcement? Network goes down at the roadside often.

**What they're really testing:** Real-world deployment awareness.
**Strong answer:** Our enforcement service itself is a backend HTTP API consumed by an enforcement UI. The UI assumes network connectivity. There is **no** offline-capable device app that pre-syncs journeys, which would be the real-world requirement for a production roadside system. Admitting this upfront: our enforcement service is the server-side verification endpoint, not an offline-ready field device. Documented as out of scope.
**Trap to avoid:** Trying to pretend offline is covered. It isn't.

### C2. Why no rate limiting on the enforcement endpoints?

**What they're really testing:** DoS defence for a role-based endpoint.
**Strong answer:** Enforcement endpoints are behind the API gateway which rate-limits per IP. They're also behind authenticated roles — only enforcement-officer JWTs can query. The combination of gateway rate limit + JWT validation is adequate for the assignment. For production, a per-officer rate limit with anomaly detection (unusual scan volume) would catch credential-theft scenarios. Not implemented.
**Trap to avoid:** Claiming it's fully hardened.

### C3. Why no audit trail of enforcement lookups?

**What they're really testing:** Whether you understand the accountability requirement.
**Strong answer:** Who checked whose license when is a regulatory requirement in real systems — without it, you can't audit officer behaviour. Our enforcement service does not log check events into an audit table; it only logs to standard output for operational debugging. The analytics service would be the natural sink for these events, but enforcement doesn't currently publish `enforcement.checked` events to RabbitMQ. **Real gap.** Fix is small: publish an event per verify call. Documented.
**Trap to avoid:** Saying "we have logs." Stdout logs are not an audit trail.

### C4. Why no mutual TLS between enforcement and journey-service?

**What they're really testing:** Security posture on internal traffic.
**Strong answer:** Internal service-to-service traffic runs on plain HTTP inside the Docker network / overlay network. For the assignment scope this is acceptable because the network is trust-zoned. For production: mTLS between services is the standard answer, using a service mesh or hand-rolled certs. We didn't implement either — documented.
**Trap to avoid:** Waving it off. Internal-only is a policy, not a guarantee.

## Category D — Bottleneck Hunting

### D1. What's the P50 / P99 for enforcement vehicle verification?

**What they're really testing:** Cache hit vs miss cost.
**Strong answer:** Cache hit path ([service.py:66-86](enforcement-service/app/service.py#L66-L86)) is a single Redis `GET` + JSON deserialize + datetime parsing — around 2-5ms end-to-end including HTTP round trip. Cache miss path is Redis miss + httpx call to journey-service (Postgres query + JSON) — 30-50ms. The P99 is dominated by cache-miss + journey-service contention, so if journey-service is under load the miss path slows further. Hit rate should approach 100% in steady state because the consumer pre-warms on every `journey.confirmed`; cache misses only happen in the narrow window between journey confirmation and RabbitMQ delivery (~a few hundred ms) or when this instance restarts after the TTL expired.
**Trap to avoid:** Claiming "always sub-5ms." It's sub-5ms on cache hit — which is most, not all.

### D2. Is `verify_by_license` more expensive than `verify_by_vehicle`?

**What they're really testing:** Whether the extra user-service lookup matters.
**Strong answer:** Yes — on cold cache. `verify_by_license` needs the `license → user_id` mapping which is another Redis layer that has to be warmed via a user-service call on the first miss ([service.py:132-147](enforcement-service/app/service.py#L132-L147)). That cache has its own 24h TTL. Once warm, license verification is Redis-GET + Redis-GET + JSON parse — roughly 2x the hit path of vehicle verification. Cold cache: user-service call + journey-service call ~50-80ms. A roadside officer who scans the same license twice in 24h hits the warm path for both lookups.
**Trap to avoid:** Missing that it's *two* cache layers.

### D3. What happens on the "hot" key, a celebrity driver whose license is scanned 1000 times an hour?

**What they're really testing:** Redis single-key contention.
**Strong answer:** Redis serves all reads single-threaded per instance but at a rate of ~100k ops/sec for simple GET. 1000 queries/hour is noise. The bottleneck appears at ~10k ops/sec on a single key, which we're three orders of magnitude below. At that point you'd shard or introduce a local in-process LRU cache with short TTL as a layer 0 cache.
**Trap to avoid:** Pretending Redis has no ceiling. Give the order of magnitude.

## Category E — Limitations

### E1. Mismatch M4: the cache-fallback path in `verify_by_vehicle` does NOT write back to the cache.

**What they're really testing:** Acknowledging the report vs reality gap you flagged yourself.
**Strong answer:** Owned in mismatch M4. [service.py:82-98](enforcement-service/app/service.py#L82-L98) — when Redis misses and we fall through to journey-service, we build the `VerificationResponse` but **do not** write the fetched data back into Redis. The next identical query will miss again and re-hit journey-service. `verify_by_license` **does** cache the license→user_id mapping back ([service.py:144](enforcement-service/app/service.py#L144)), so the two paths are inconsistent. Fix is one `setex` call on the vehicle path, same shape as the consumer writes. Documented and acknowledged.
**Trap to avoid:** Trying to pretend the behaviour is "intentional for freshness." It isn't.

### E2. If a journey is confirmed but `journey.confirmed` is never delivered, enforcement never sees it.

**What they're really testing:** The consumer-vs-API race.
**Strong answer:** Cache layer is populated only via RabbitMQ consumer. If RabbitMQ is down during `journey.confirmed` publication, the event is held in the journey-service outbox and will be published later; if the outbox drainer crashes permanently, the event never arrives and enforcement never caches the journey. Fallback path still works — layer 2 `_query_journey_service` hits the live table — so enforcement is correct, just slower and load-coupled. Eventual consistency with a worst-case falls-through to the authoritative source.
**Trap to avoid:** Claiming the cache is authoritative. It's a hot read-through cache; journey-service's Postgres is authoritative.

### E3. Cache TTL is `arrival - now + 1h` at [consumer.py:59](enforcement-service/app/consumer.py#L59). What if the journey's arrival is updated mid-flight?

**What they're really testing:** TTL staleness.
**Strong answer:** The consumer re-runs on every `journey.started` which refreshes the key with a new TTL. Without a started event, the cached entry would reflect the original arrival estimate and expire at that time + 1h. If the driver is delayed and the estimated arrival slides later, enforcement's cached window ends too early and a legit late-running driver could be briefly marked invalid when the TTL pops. Fallback to journey-service catches it. Small staleness, not correctness-breaking.
**Trap to avoid:** Claiming the TTL auto-tracks reality. It tracks the latest event received, not live state.

### E4. No handling for clock skew between enforcement and journey service.

**What they're really testing:** Time synchronization blind spots.
**Strong answer:** `datetime.utcnow()` at [service.py:63](enforcement-service/app/service.py#L63) is the local clock. If the enforcement node's clock is 2 minutes behind the journey node's clock, the `departure <= now + 30min` check at [service.py:75](enforcement-service/app/service.py#L75) could accept a journey whose "30 min before departure" window hasn't actually started yet. Worst case: a few minutes early/late on the grace window edges. We rely on Docker host clocks being NTP-synced; we do not verify this in code.
**Trap to avoid:** Saying clocks are "fine." They're fine *enough* — bounded by NTP drift.

## Category F — Failure Scenarios

### F1. What happens if BOTH Redis and journey-service are down?

**What they're really testing:** Multi-layer failure.
**Strong answer:** Cache check raises an exception (caught at [service.py:211](enforcement-service/app/service.py#L211) — returns `None`, falls through), fallback httpx call trips the journey-service circuit breaker ([service.py:26-27](enforcement-service/app/service.py#L26-L27) — 3 failures → 30s open). Third request after the breaker opens: `CircuitBreakerOpenError` caught at [service.py:229](enforcement-service/app/service.py#L229), returns `is_valid=False`. Enforcement gracefully degrades to **fail-closed**: every verification returns invalid. The guard operationally falls back to paper / call-in.
**Trap to avoid:** Claiming the service keeps working. It doesn't — it returns invalid for everything.
**Follow-up:** "Is fail-closed the right choice?" → Yes for enforcement. A fail-open mode would let any driver claim a valid booking during the outage — worse than over-caution.

### F2. Circuit breaker opens on journey-service. Then journey-service comes back. What happens to the next enforcement check?

**What they're really testing:** Circuit breaker recovery semantics.
**Strong answer:** The circuit breaker at `shared/circuit_breaker.py` uses three states: CLOSED, OPEN, HALF_OPEN. After `reset_timeout=30.0` seconds in OPEN state, the breaker transitions to HALF_OPEN on the next call. In HALF_OPEN, one probe is allowed through — if it succeeds, breaker returns to CLOSED and all subsequent calls flow normally; if it fails, back to OPEN for another 30s. So a returning journey-service picks up traffic within at most 30s of becoming healthy, with a single probe request as the gate.
**Trap to avoid:** Confusing reset_timeout with failure_threshold. Reset timeout = how long OPEN lasts before HALF_OPEN; failure_threshold = how many consecutive failures transition CLOSED → OPEN.

### F3. What happens when an event arrives for a journey the cache already has?

**What they're really testing:** Consumer idempotency.
**Strong answer:** `setex` in the consumer pipeline ([consumer.py:74-78](enforcement-service/app/consumer.py#L74-L78)) is an unconditional overwrite with a new TTL. Duplicate or replayed events are idempotent — same key, same JSON body, same TTL shape. No special handling needed. Unlike notification-service which dedupes to avoid double-toasting, enforcement *wants* repeated writes because they refresh the TTL.
**Trap to avoid:** Thinking you need dedup. You don't — the operation is natively idempotent.

### F4. What if user-service returns a stale license→user_id mapping (e.g., license was reassigned)?

**What they're really testing:** Cache invalidation on user-owned data.
**Strong answer:** The license→user_id cache has no invalidation channel. User-service doesn't emit events on license changes, so enforcement's 24h cache holds the old mapping for up to 24h. In practice license numbers don't change, but this is a latent bug if they did. Fix would be a `user.license_changed` event consumed by enforcement → delete the stale cache key. Not implemented.
**Trap to avoid:** Claiming 24h is fine because licenses are immutable. They're *mostly* immutable — not strictly.

## Category G — Requirements Defense

### G1. NFR "Enforcement verification P95 < 50ms." Do you actually achieve it?

**Strong answer:** On cache hit: yes — single Redis GET + JSON parse is 2-5ms local, well under 50ms. On cache miss: no — journey-service fallback is 30-50ms on its own, and adding network overhead pushes it over. The P95 holds only if cache hit rate ≥ ~95%, which is true in steady state because the consumer pre-warms on confirmation. After a service restart (before consumer catches up via RabbitMQ replay / next event wave) the hit rate drops and P95 temporarily breaks. No percentile tracking is implemented to prove this.
**Trap to avoid:** Claiming you measure it. You don't have histograms.

### G2. FR8: "Enforcement must tolerate journey-service being unreachable." Prove it.

**Strong answer:** Two-layer lookup: layer 1 (Redis) does not depend on journey-service. Layer 2 (journey-service HTTP) is guarded by a circuit breaker that opens after 3 failures. On a full journey-service outage with a warm cache: layer 1 hit, verification succeeds. On a cold cache: layer 1 miss, circuit breaker opens on the second attempt, subsequent checks fail-closed with `is_valid=False`. So the property holds *conditional on* the cache being warm — which it is in steady state — and degrades safely when it isn't. Documented in F1.
**Trap to avoid:** Claiming absolute tolerance. It's conditional.

## Category H — Report vs. Reality (service-specific)

### H1. Sec 4.6.5 says "Layer 1: cache hit — sub-ms response; Layer 2: fallback to journey-service, populates the cache, returns." See mismatch **M4**: the vehicle path does not populate the cache on fallback ([service.py:82-98](enforcement-service/app/service.py#L82-L98)). License path does. **Own this in viva.**

### H2. Sec 4.6.5 says "Consumer maintains cache via journey events". Confirmed at [consumer.py:52-88](enforcement-service/app/consumer.py#L52-L88) — handles confirmed, started (cache write), cancelled, completed (cache delete). ✅

### H3. Sec 4.6.5 says "circuit breaker on journey-service". Confirmed at [service.py:27](enforcement-service/app/service.py#L27) via shared `CircuitBreaker` — failure_threshold=3, reset_timeout=30s. ✅

### H4. Sec 4.6.5 claims "Redis Sentinel backed for HA". Confirmed at [service.py:41-47](enforcement-service/app/service.py#L41-L47) — uses `AsyncSentinel` if `REDIS_SENTINEL_ADDRS` is set, otherwise plain. ✅ conditional on deployment.

### H5. Report doesn't explicitly call out the fail-closed behaviour when both layers fail. The examiner may ask what you do in that case — **fail-closed (invalid=false)**, described in F1.

## Enforcement Service — 10-Minute Cheat Sheet

**3 most likely questions + answers:**
1. **"Walk me through a roadside verification end-to-end."** → Officer scans vehicle registration via the frontend. Request hits `/api/enforcement/verify/vehicle/:reg`, JWT is validated, `EnforcementService.verify_by_vehicle` runs. Layer 1 reads `active_journey:vehicle:<reg>` from Redis DB 4 — hit returns `VerificationResponse(is_valid=True, ...)` in ~5ms. Miss falls to layer 2: httpx call to journey-service guarded by circuit breaker. Success returns true with live data; failure returns `is_valid=False`. Cache is populated proactively by the RabbitMQ consumer on `journey.confirmed`/`journey.started`, and cleared on `journey.cancelled`/`journey.completed`.
2. **"Why not just hit journey-service every time?"** → Three reasons. **Availability**: enforcement must work during journey-service outages; cache hit path doesn't depend on journey-service. **Latency**: Redis GET is ~2-5ms, journey-service call is 30-50ms. **Coupling**: if enforcement load is bursty (checkpoint setup), hammering journey-service directly would degrade bookings. The cache is the availability layer as much as the performance layer.
3. **"What happens if Redis is down?"** → Layer 1 fails, layer 2 (journey-service fallback) kicks in. Circuit breaker protects journey-service from cascading load. If journey-service is also down, we fail-closed: `is_valid=False`. Fail-closed is the right choice for enforcement — a false negative just means extra scrutiny, while a false positive lets an unauthorized vehicle through.

**3 strongest design decisions:**
1. **Proactive cache warming via RabbitMQ.** Cache is hot before the first query, not cold-loaded on miss. The consumer is the write path; HTTP handlers are read-only.
2. **Two-layer lookup with circuit breaker on layer 2.** Cache hit decouples us from journey-service availability; circuit breaker prevents cascading failure when journey-service is sick.
3. **Dedicated Redis DB 4 with Sentinel HA.** Logical separation from other services, automatic failover within ~15s on primary crash.

**3 biggest weaknesses + framing:**
1. **Mismatch M4: vehicle fallback doesn't cache back.** *Framing:* "One-line fix (`setex` after the journey-service call), identical to how the license path already does it. Acknowledged in the mismatch table."
2. **No audit trail of who checked what when.** *Framing:* "Stdout logs are operational, not regulatory. Fix is to publish `enforcement.checked` events to RabbitMQ, consumed by analytics into the immutable event_logs table."
3. **Clock-skew blind 30-minute grace window.** *Framing:* "Relies on NTP-synced host clocks; documented as an operational assumption, not a code guarantee."

**The 1 thing you must NOT say:**
**"The cache is authoritative."** — It's not. Journey-service's Postgres is authoritative; the cache is a read-through with proactive warming. If you call it authoritative you'll be asked about invalidation guarantees you don't have.

---

# Analytics Service — Sai Eeshwar Divaakar

**Stack:** Go 1.22, chi router, PostgreSQL (event_logs table), RabbitMQ consumer bound to `journey.*` and `user.*`, Redis DB 5 for daily counters + dedup.
**Key files:** [analytics-service/consumer.go](analytics-service/consumer.go), [database.go](analytics-service/database.go), [handlers.go](analytics-service/handlers.go), [main.go](analytics-service/main.go).

## Category A — Why This Over That?

### A1. Why Postgres for the event_logs table instead of a time-series DB (TimescaleDB, InfluxDB)?

**What they're really testing:** Storage fit for append-only audit data.
**Strong answer:** Our event rate is low — one event per journey lifecycle transition, ~1000/day for the demo, <1M/day even at production peak. That's nowhere near the scale where a time-series DB's chunking and compression beat a plain Postgres table with a timestamp index. A flat `event_logs` table with (id, event_type, journey_id, user_id, origin, destination, metadata_json, created_at) covers every query we run: counts by type (Redis), counts in last hour (DB, `WHERE created_at > NOW() - INTERVAL '1 hour'`), full-text reconstruction per journey. TimescaleDB would pay for itself above ~100M rows; we're three orders of magnitude under that. Postgres also lets analytics JOIN against other services' rows in future (it doesn't today, but the schema is a simple extension).
**Trap to avoid:** Claiming Postgres "scales for analytics." It scales for *this* analytics workload. Quote the rate.
**Follow-up:** "Show me the hot query path." → `GetSystemStats` at [consumer.go:246-276](analytics-service/consumer.go#L246-L276) hits Redis for today's counters (HGetAll is O(field count), ~ms) and Postgres twice (`total_events`, `events_last_hour`). Neither is a full scan.

### A2. Why duplicate counters in Redis if the authoritative log is in Postgres?

**What they're really testing:** Cache-as-materialized-view understanding.
**Strong answer:** Because counting rows in Postgres is expensive at query time. A "live stats" endpoint shouldn't run `SELECT count(*) FROM event_logs WHERE created_at::date = CURRENT_DATE GROUP BY event_type` every time the dashboard refreshes — that's a full scan of today's rows on every call. Instead we maintain Redis hash counters incrementally ([consumer.go:229-240](analytics-service/consumer.go#L229-L240)) — each event does `HINCRBY analytics:daily:YYYY-MM-DD eventType 1` in a pipeline. Reading stats is then a single HGetAll. The trade-off: Redis counter may drift from Postgres truth if Redis crashes between incrs — but we have a 48h TTL and Postgres is the recovery source. Redis is a read-optimization; Postgres is truth.
**Trap to avoid:** Claiming Redis is the primary store. It isn't — it's a pre-aggregated view.
**Follow-up:** "How do you reconcile if Redis drifts?" → `GetSystemStats` gracefully degrades: on Redis error the `today` counters return zero but the Postgres-backed `total_events_all_time` and `events_last_hour` still work ([consumer.go:256-273](analytics-service/consumer.go#L256-L273)). A production system would have a nightly reconciliation job replaying the last 24h from Postgres into Redis.

### A3. Why `journey.*` and `user.*` wildcards in the topic subscription?

**What they're really testing:** Subscription semantics.
**Strong answer:** Analytics is interested in *every* event in the system for audit purposes. Binding to specific routing keys would force us to update the binding table every time a new event type is added. The `*` wildcard at [consumer.go:132](analytics-service/consumer.go#L132) matches one word in the routing key, so `journey.confirmed`, `journey.rejected`, `journey.cancelled` etc. are all covered without enumerating them. The notification-service, by contrast, binds to specific keys because it has per-key template logic. Analytics is agnostic — it just logs whatever it gets.
**Trap to avoid:** Claiming `*` is the same as `#`. It isn't — `#` matches zero or more words, `*` matches exactly one. For our single-level keys they're equivalent but the intent differs.

### A4. Why Go for this service too?

**What they're really testing:** Consistency of language choice.
**Strong answer:** Analytics is another consumer of RabbitMQ + writer to Postgres. Same shape as notification-service — so we reused the Go + amqp091-go + pgx pattern for operational consistency. Using Go also gives us the same dedup primitive (SHA-256 + Redis key) trivially. No language-specific feature required — we could have written it in Python without losing anything. The deciding factor was "match the existing messaging consumer pattern."
**Trap to avoid:** Fishing for a "Go is faster" argument. It isn't the real reason here.

## Category B — Why Use This At All?

### B1. Why a separate analytics service rather than a read-only query against the other services' databases?

**What they're really testing:** Why not just query journey-service's database directly.
**Strong answer:** Three reasons. (1) **Schema ownership**. Cross-service SQL joins break the microservice boundary — it means analytics breaks when journey-service renames a column. (2) **Workload isolation**. Analytics queries (full-day aggregates) would compete with journey-service's OLTP workload for the same Postgres instance. (3) **Event-level audit**. Analytics needs the *history* of state transitions (confirmed → started → completed), not just the current state. Querying `journeys` gives you the current snapshot only. Consuming the event stream lets us build a full audit timeline with one row per transition.
**Trap to avoid:** "Microservices prefer it." Defend by *workload isolation* and *event-level vs state-level*.

### B2. Why dedup on the consumer if RabbitMQ gives us "delivery guarantees"?

**What they're really testing:** Delivery-semantics literacy.
**Strong answer:** RabbitMQ gives at-least-once when `durable=true` + `ack` on delivery. The at-least-once comes in because if a consumer reads a message, processes it, and crashes *before* the ack reaches the broker, the broker redelivers on reconnect. Without dedup, the duplicate delivery inserts a second `event_logs` row with a different generated UUID but the same event content — the audit trail is now wrong (shows two confirmations of one journey). Consumer-side dedup via `analytics:processed:<hash>` at [consumer.go:175-186](analytics-service/consumer.go#L175-L186) prevents this. Same 24h TTL as the queue's own TTL.
**Trap to avoid:** Conflating "at-least-once" with "exactly-once." Reject the claim whenever the examiner leads with it.

### B3. Why store the full JSON metadata in `metadata_json` at [consumer.go:213](analytics-service/consumer.go#L213)?

**What they're really testing:** Schema flexibility vs typed columns.
**Strong answer:** Event metadata varies by type. `journey.confirmed` has one set of fields; `user.registered` has another. Rather than having a sparse table with 50 nullable columns, we project the known columns (journey_id, user_id, origin, destination) and dump the full event into `metadata_json`. That preserves any field we didn't explicitly promote, making the event log truly append-only and future-proof: a new event type or a new field needs no schema change. Queries that need the raw metadata can use Postgres's JSON operators; queries that use the promoted columns hit indexes normally.
**Trap to avoid:** Saying "JSON is flexible." Too vague. The right framing is "audit completeness without schema coupling."

## Category C — Why Haven't You Used X?

### C1. Why no HMAC chain for tamper-evidence on the event log?

**What they're really testing:** The mismatch M15 — "immutable audit trail" claim.
**Strong answer:** The report (Sec 4.6.6) calls this an "immutable audit trail". Code-wise, the table is **insert-only in code** — [consumer.go:215-224](analytics-service/consumer.go#L215-L224) only inserts, never updates or deletes. But there's no database-level enforcement (no revoke UPDATE/DELETE, no trigger blocking modifications, no HMAC chain linking rows so a tampered row can be detected). An attacker with direct DB access could edit history and leave no trace. I called this out in mismatch M15 — it's the gap between "insert-only convention" and "cryptographically immutable." Fix would be an HMAC chain: each row stores HMAC(prev_hmac || row_data), and a verification query walks the chain. Not implemented; documented as future work.
**Trap to avoid:** Claiming the log is "immutable" in a viva. It's append-only-by-convention; not cryptographically tamper-evident.
**Follow-up:** "What would the chain look like?" → Row N stores `prev_hmac || sha256(row_bytes)` HMACed with a secret. Verification walks forward. Cost is one HMAC computation per write, one HMAC per row on verification.

### C2. Why no stream processor (Flink/Spark Streaming)?

**What they're really testing:** Whether you can justify the absence of the heavy hammer.
**Strong answer:** Our analytics is a simple event sink with pre-aggregated counters. We don't do windowed joins, complex event processing, sessionization, or anomaly detection — all of which would benefit from a real stream processor. Our "stream processing" is two operations per event: insert to DB + HINCRBY to Redis. Flink for that is catastrophic overkill. The upgrade path is there: if we needed rolling-window latency dashboards or CEP rules, we'd subscribe a Flink job to the same RabbitMQ exchange, leave the existing analytics-service in place as the audit sink.
**Trap to avoid:** Saying "we didn't need it" without naming what Flink would actually buy.

### C3. Why no data retention / archival on event_logs?

**What they're really testing:** Lifecycle management.
**Strong answer:** Currently `event_logs` grows monotonically. For the exercise lifetime this is fine — the volume is tiny. In production the operational fix would be a monthly partition drop (PARTITION BY RANGE on created_at) or a cold archive to S3/Glacier after, say, 90 days, with the hot partition in Postgres. Neither is implemented; there's no cron job pruning old rows and no partitioning scheme. An examiner who pushes on retention should hear: "insert-only design with no retention today, partition-and-archive is the production answer."
**Trap to avoid:** Claiming we use partitioning. We don't.

## Category D — Bottleneck Hunting

### D1. Where does this service become the bottleneck first?

**What they're really testing:** Whether you've thought about analytics as a throughput sink.
**Strong answer:** Postgres insert throughput on `event_logs`. Every event is a single-row INSERT committed immediately. On a local Postgres that's ~5k-10k inserts/sec; on cloud Postgres with fsync=on, closer to 1k/sec. We're receiving maybe 10 events/sec peak demo, so we're 100x below the limit. If we ever needed to scale, the fix is batched inserts: buffer 100 events in-memory, flush via `COPY FROM STDIN`, gaining ~10x throughput. Not implemented — single-row inserts for simplicity and real-time query freshness.
**Trap to avoid:** Claiming Postgres is "fine forever." Give the headroom.

### D2. The dedupe cache never expires keys explicitly before 24h. Does Redis fill up?

**What they're really testing:** Memory growth awareness.
**Strong answer:** Keys are SET with a 24h TTL at [consumer.go:193](analytics-service/consumer.go#L193), so Redis evicts them automatically. At even 10 events/sec × 86400 sec, that's ~860k keys in Redis at steady state — each key ~50 bytes of key + tiny value — ~50MB. Noise for a Redis instance with gigabytes of RAM. Would only matter at ~1000 events/sec sustained, and even then 50MB/sec growth for 24h = 4GB, still manageable.
**Trap to avoid:** Ignoring the upper bound. Always estimate the footprint.

### D3. Is the `HINCRBY` + `HGetAll` dashboard read path a hot key?

**What they're really testing:** Redis key contention on a single counter.
**Strong answer:** All increments hit one key per day: `analytics:daily:YYYY-MM-DD`. Redis is single-threaded so all HINCRBYs serialise against each other. At 10 events/sec this is 10 ops/sec on one key — nothing. At 10k events/sec you'd start seeing contention. Fix is sharding the counter across N keys (`analytics:daily:YYYY-MM-DD:shard-0` through `shard-N`) and summing at read time. Not implemented — not needed at our scale.
**Trap to avoid:** Claiming "Redis is fast enough forever." Name the ceiling.

## Category E — Limitations

### E1. If analytics is down for 24h, what happens to events during that window?

**What they're really testing:** Subscriber outage tolerance.
**Strong answer:** Messages accumulate in the durable `analytics_events` queue on the broker. RabbitMQ persists them. When analytics reconnects ([consumer.go:79-92](analytics-service/consumer.go#L79-L92)), the consumer resumes from the head of the queue and drains the backlog. The catch is the `x-message-ttl: 86400000` — messages in the queue for more than 24h are dead-lettered to `dead_letter_queue`. So a 25h outage loses the oldest hour of events to DLQ unless we run a DLQ replay tool. We don't have one. DLQ is terminal for analytics too.
**Trap to avoid:** Claiming "zero loss on extended outages." 24h is the boundary.

### E2. If two analytics-service instances run, do we double-count events?

**What they're really testing:** Shared-queue consumer semantics with our dedup scheme.
**Strong answer:** RabbitMQ delivers each message to exactly one competing consumer on a shared queue, so with a single `analytics_events` queue and two instances, each event is processed once. Dedup (`analytics:processed:<hash>`) is shared via Redis, so even if both instances happen to see a redelivery, only one will process it. Horizontal scaling is safe — no double-counting. The only invariant to preserve is that all instances point at the same Redis database (DB 5). If they point at different Redis instances, the dedup layer breaks and double counts become possible during redelivery.
**Trap to avoid:** Confusing with the notification-service fan-out issue. Analytics *wants* round-robin delivery; notification *would want* broadcast. Opposite problems.

### E3. The daily counter key at [consumer.go:231](analytics-service/consumer.go#L231) uses `time.Now().UTC().Format("2006-01-02")` — what happens at UTC midnight?

**What they're really testing:** Day-boundary edge cases.
**Strong answer:** At 23:59:59 UTC the counter writes to `analytics:daily:2026-04-16`. At 00:00:00 UTC the next write goes to `analytics:daily:2026-04-17`. The old day's key persists with its 48h TTL ([consumer.go:236](analytics-service/consumer.go#L236)), so you can still query "yesterday's counts" for 24h after rollover. Dashboard queries for "today" naturally flip to the new key because the GetSystemStats path recomputes the key name per call. Clock skew between services means a few events could land in the wrong bucket on the boundary — accepted as a minor drift given reliance on host NTP.
**Trap to avoid:** Missing that the TTL of 48h, not 24h, is what keeps yesterday's numbers queryable.

### E4. `event_logs.metadata_json` is TEXT, not JSONB. Why does that matter?

**What they're really testing:** Postgres JSON subtype awareness.
**Strong answer:** TEXT is opaque to Postgres — a JSON operator like `metadata_json->>'vehicle_registration'` does not work. Querying a specific field in metadata requires app-side parsing. JSONB gives indexing (GIN) on internal fields at the cost of 20% more disk per row. We chose TEXT because all our current queries use the promoted columns, and we never query into the raw JSON. If the requirement ever changes, migration is a one-time `ALTER TABLE ... TYPE JSONB USING metadata_json::jsonb`.
**Trap to avoid:** Pretending TEXT is as flexible as JSONB for reads. It isn't.

## Category F — Failure Scenarios

### F1. What happens if the Postgres insert fails but the RabbitMQ message has already been processed?

**What they're really testing:** The ack ordering.
**Strong answer:** `handleEvent` at [consumer.go:198-243](analytics-service/consumer.go#L198-L243) calls `insertEvent` and logs on error, but **does not return the error to the outer consumer loop**. So even if the insert fails, `handleEvent` returns `nil` and the message is acked ([consumer.go:157](analytics-service/consumer.go#L157)) — the message is consumed but the log row was never written. **This is a real bug** — silent data loss on DB errors. Fix is trivial: propagate the error from insertEvent so the message Nacks to DLQ. Documented.
**Trap to avoid:** Claiming the service is correct. On this failure mode it silently drops events.
**Follow-up:** "When would this bite?" → Any brief Postgres unavailability (restart, failover, lock timeout). Dedup would also mark the message processed in Redis so even a subsequent redelivery is skipped — the event is lost from the log entirely.

### F2. What happens if Redis goes down?

**What they're really testing:** Graceful degradation on the auxiliary store.
**Strong answer:** Three paths are affected. (1) Dedup: `isDuplicate` at [consumer.go:175-186](analytics-service/consumer.go#L175-L186) fails open — `Exists` errors return `false`, so we process the message anyway. During a Redis outage, duplicates *could* slip through and create duplicate event_logs rows. (2) Counter increments: the pipeline execution fails silently with a log message at [consumer.go:238](analytics-service/consumer.go#L238). Counters drift. (3) Dashboard reads: `GetSystemStats` returns zero for the "today" counters but the Postgres-backed totals still work. Graceful but imperfect — the dashboard shows a partial view.
**Trap to avoid:** Saying "Redis is optional." It's optional for correctness on the Postgres side, but the dashboard's live counters are degraded.

### F3. What if RabbitMQ reconnects and replays 500 messages at once?

**What they're really testing:** Burst handling.
**Strong answer:** QoS = 10 ([consumer.go:102](analytics-service/consumer.go#L102)) limits in-flight to 10 messages. The broker will not send an 11th message until one of the 10 is acked. With ~5ms processing per message, we drain 500 messages in ~250ms. Dedup means any pre-crash-acked messages are skipped. Postgres insert rate handles 500 inserts trivially. The burst is absorbed cleanly.
**Trap to avoid:** Claiming there's no back-pressure. QoS is the back-pressure mechanism.

### F4. What's the max latency from event emission to analytics DB visibility?

**What they're really testing:** End-to-end observability delay.
**Strong answer:** Normal path: journey-service's outbox publisher tick is every 2 seconds, so the event is in RabbitMQ at most 2s after journey commit. RabbitMQ delivers to analytics consumer in ~10ms. Analytics processes in ~5ms. Event visible in `event_logs` ~10ms later. Total P50 ~1-2 seconds from journey commit to analytics visibility, dominated by the outbox drainer tick. P99 on a broker/analytics restart could spike to tens of seconds.
**Trap to avoid:** Claiming real-time. It isn't — it's 2-second delayed.

## Category G — Requirements Defense

### G1. NFR: "Audit trail must survive service failures." Prove it.

**Strong answer:** Three persistence layers: (1) Source is the journey-service outbox table — if that survives, replay always works. (2) RabbitMQ with durable queues survives broker restart. (3) Analytics's event_logs in Postgres is append-only and on disk. Loss requires *all three* to fail — outbox drained into broker, then broker loses its volume, then Postgres loses its volume. Any single layer's failure is recoverable. **Caveat**: the insertEvent silent-failure from F1 is a real durability gap that undercuts this claim on the analytics side.
**Trap to avoid:** Not owning the F1 gap.

### G2. Sec 4.6.6 claims "immutable audit trail". Defend or concede?

**Strong answer:** Concede politely. It's append-only by convention (no code path deletes or updates), but there is no cryptographic tamper evidence and no database-level enforcement. A DBA with superuser credentials could rewrite history. The fix — HMAC chain or row-level append-only trigger — is straightforward and documented as future work. This is mismatch **M15**.
**Trap to avoid:** Defending "immutable." You'll lose.

## Category H — Report vs. Reality (service-specific)

### H1. Sec 4.6.6 says "immutable audit trail" — see M15. Insert-only in code, not cryptographically enforced. **Own this.**

### H2. Sec 4.6.6 says "consumes all journey and user events". Confirmed at [consumer.go:132](analytics-service/consumer.go#L132) — `journey.*` and `user.*` wildcards. ✅

### H3. Sec 4.6.6 says "dashboard reads hybrid Redis+Postgres". Confirmed at [consumer.go:246-276](analytics-service/consumer.go#L246-L276) — Redis for today's counters, Postgres for totals and last-hour. ✅

### H4. The **silent insert-failure bug** at [consumer.go:225](analytics-service/consumer.go#L225) (`log.Printf` but no return err) is not mentioned in the report. Not a mismatch but a code-level weakness the examiner may spot. Own it as F1.

### H5. Sec 4.3 says "DLQ with 24h message TTL". Confirmed at [consumer.go:125](analytics-service/consumer.go#L125). ✅

## Analytics Service — 10-Minute Cheat Sheet

**3 most likely questions + answers:**
1. **"Is the audit trail really immutable?"** → Append-only in code (only INSERTs, no UPDATEs or DELETEs in any handler), but *not* cryptographically tamper-evident. A DBA with superuser access could rewrite history and we wouldn't detect it. The fix is an HMAC chain linking each row to the previous, verifiable end-to-end. Documented as mismatch M15 and future work. Don't defend "immutable" as if it meant cryptographic.
2. **"Why duplicate counters in Redis if Postgres has the truth?"** → Postgres is the authoritative audit store; Redis is a pre-aggregated materialized view. Counting rows in Postgres on every dashboard request would force a GROUP BY full scan. Redis HINCRBY on a daily key is O(1) per event, read via HGetAll. On Redis failure, dashboard's "today" counters degrade to zero but the Postgres-backed totals remain.
3. **"How do you handle duplicate events?"** → SHA-256 of message body (or `MessageId` if set) stored as `analytics:processed:<hash>` in Redis with 24h TTL — matches queue message TTL. Duplicate delivery hits the cache, acked without processing. Fails open on Redis error — we accept a rare duplicate over a silent drop.

**3 strongest design decisions:**
1. **Event-stream consumption with wildcard binding.** `journey.*` + `user.*` means we don't need to update the subscription when new event types are added.
2. **Hybrid Redis + Postgres read path.** Redis serves real-time dashboard; Postgres serves historical/aggregate queries. Each optimised for its shape.
3. **Shared RabbitMQ DLQ with 24h TTL.** Same infrastructure as notification-service, so operations/observability are consistent across consumers.

**3 biggest weaknesses + framing:**
1. **Mismatch M15: "immutable" overclaim.** *Framing:* "Append-only by convention, not cryptographically. Fix is an HMAC chain — known design, not implemented for the exercise scope."
2. **Silent insert-failure bug (F1, H4).** *Framing:* "`handleEvent` logs the insert error but still returns nil, so the message is acked and lost. Real bug. One-line fix: return the error to surface it to the ack/nack path."
3. **No retention / partitioning.** *Framing:* "Monotonic growth with no archive pipeline. Production fix is monthly partition drop or S3 archive after N days; not implemented."

**The 1 thing you must NOT say:**
**"The audit log is immutable."** — It isn't. It's append-only. Saying "immutable" gets you the HMAC-chain question immediately and you'll have to walk it back.

---

# System-wide 10-Minute Cheat Sheet

## The One-Page Mental Model

Six services, each with a clear invariant and a clear failure mode:

| Service | Invariant | Primary failure mode | CAP lean |
|---|---|---|---|
| User | Unique email globally | Lock TTL expires during partition → dual registration | AP (with Redlock coordination) |
| Journey | No confirmed journey without a conflict-check + durable event | Saga crashes between phases → PENDING row leak | CP on commit, AP on notification |
| Conflict | One vehicle per road cell per 30-min slot | Two nodes accept same slot during replication gap | AP with eventual convergence |
| Notification | At-least-once to user, deduped to "effectively once" | Per-instance WebSocket registry → miss on other instance | AP |
| Enforcement | Cache-backed active-journey lookup | Fail-closed when both Redis + journey down | CP on fail-closed |
| Analytics | Append-only audit log | Silent insert-failure on Postgres outage (H4 bug) | AP |

## 5 Questions You Will Definitely Be Asked System-Wide

### SW1. "Walk me through a single booking end-to-end across every service."

**Answer template:** 
1. Browser POSTs `/api/journeys` with JWT → API gateway routes to journey-service.
2. Journey-service opens a Postgres transaction, INSERTs a PENDING journey row + outbox row in one atomic commit.
3. Journey-service calls conflict-service `POST /api/conflicts/check` via circuit breaker (local → peer URLs on failure).
4. Conflict-service opens a SERIALIZABLE transaction, walks grid cells, takes row locks, inserts `booked_slots` + increments `road_segment_capacity`, commits. Async fire-and-forget goroutine replicates the slot to every peer conflict-service.
5. Journey-service receives `is_conflict=false`, updates the journey to CONFIRMED, inserts an outbox `journey.confirmed` row, commits.
6. Outbox publisher (2s tick) drains the outbox row → RabbitMQ `journey_events` exchange with routing key `journey.confirmed`.
7. Three consumers receive fanouts: notification-service (toast + Redis history list), enforcement-service (hot-cache populate), analytics-service (insert to event_logs + HINCRBY Redis counter).
8. Browser sees the CONFIRMED response from the sync path (step 5) *before* any async delivery lands. The notification toast is a nice-to-have on top.

### SW2. "Where's the consistency boundary? Which parts are strong-consistent and which are eventual?"

**Answer:** Strong within each service's local Postgres: user table (one primary, streaming replica), journey table (single-primary), conflict booked_slots (SERIALIZABLE, FOR UPDATE). Eventual *across* services and *across* nodes: conflict replication (async push + periodic pull sync), enforcement cache (RabbitMQ-driven), analytics (RabbitMQ-driven). The booking *itself* is strong-consistent — the response arrives only after commit. Downstream consumers converge eventually with max staleness ~5 minutes (periodic sync) or 2 seconds (outbox drainer).

### SW3. "If one laptop dies mid-booking storm, what's the blast radius?"

**Answer:**
- **In-flight bookings on that laptop:** Journey rows that committed CONFIRMED are safe — Postgres is durable. Journey rows still in PENDING (mid-saga crash) leak as orphans; documented gap, no auto-recovery.
- **Replication lag to peers:** Bookings already pushed to peers are safe (peer has the row). Bookings that committed locally but crashed before `go replicateSlotToPeers` scheduled are delayed until the next 5-min periodic pull, at which peers will discover and apply them.
- **Live WebSocket users on dead laptop:** Drop immediately. Reconnect hits the load balancer and lands on another instance's notification-service. Real-time toasts for the reconnect window are lost; history panel still shows them on next fetch.
- **User registrations in flight on dead laptop:** The dual-phase lock on user registration either acquired and committed (safe) or acquired and crashed before commit. Crashed case: Redis lock expires at TTL, retry succeeds.
- **Gateway view:** nginx marks the upstream dead, routes new requests to surviving laptops, no 502s after the failure is detected (~10s).

### SW4. "Which are your three biggest weaknesses system-wide, and how would you fix them in production?"

**Answer:**
1. **No 40001 retry loop in conflict-service (M2).** Every SSI serialization failure surfaces as a rejection. Production fix: wrap `checkConflicts` in a 3-retry loop with jittered backoff. One-day task.
2. **No cryptographic immutability on analytics event log (M15).** Append-only by convention, not enforced. Production fix: HMAC chain linking each row to the previous, verifiable on demand. Three-day task.
3. **Per-instance WebSocket registry in notification-service (C2).** Only the notification-service instance that consumed the event pushes to live sockets; users on other instances miss the real-time toast. Production fix: anonymous per-instance queues bound to the topic exchange, so every instance receives every event. Half-day task.

### SW5. "How do you know your system meets the requirements?"

**Answer:** Honestly, we have a test suite (`test-suite/` — double-booking storm, vehicle overlap, distributed-lock race, partition tolerance) but **no load test with percentile tracking**. NFR latency claims in Sec 2.2 are observational, not measured — mismatch M5. The correctness invariants are verified by the integration tests in the suite (same-driver or same-vehicle double-booking is definitively rejected). The performance claims are weaker and we'd add k6 + histogram dashboards before a production launch.

## 10-minute verbal walkthrough checklist

If you only have 10 minutes to warm up before the viva:

1. ✅ Re-read the **Report vs Reality mismatches** table (top of this doc). These are the first five questions you will be asked.
2. ✅ Memorize the CAP lean per service from the table above. "User and Journey are CP on commit, everything else is AP with eventual convergence."
3. ✅ Rehearse the end-to-end walkthrough (SW1). If you stumble here you'll look shaky.
4. ✅ Know the **three failure scenarios** on your service that you cannot answer cleanly — and have the one-sentence honest concession ready.
5. ✅ Know every service's **dedup primitive** — user-service unique index + distributed lock, journey idempotency key, conflict `journey_id` check, notification/analytics SHA-256 + Redis key, enforcement idempotent setex.
6. ✅ Know your **circuit breaker** numbers: failure_threshold=3, reset_timeout=30s, half-open probe on recovery. This is in `shared/circuit_breaker.py` and used by every Python service calling another service.
7. ✅ If asked "how would you scale to 10x?" — answer by **service** (user: shard by email hash; journey: partition by user region; conflict: hash-route by route_id; notification: Redis pub/sub fan-out; enforcement: add layer-0 LRU cache; analytics: batch inserts + partition by month).

## What the examiner wants to see

- **Honesty.** Own your mismatches and bugs. Defending wrong claims will cost you marks; admitting them and framing the fix will not.
- **Precision.** Cite file paths and function names. "SERIALIZABLE at [service.go:67](conflict-service/service.go#L67)" beats "I think we use SERIALIZABLE."
- **Trade-off reasoning.** Every design decision is a trade-off. Answer every "why did you choose X" with "we chose X because it gives us A at the cost of B, and B is acceptable here because of C."
- **The missing component.** Know what you *didn't* do. HMAC chain, load test, offline enforcement, fan-out for WebSocket, 40001 retry, formal consistency proof. These will be asked.

Good luck.

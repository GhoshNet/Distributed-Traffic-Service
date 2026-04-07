Date : 7 April 2026
5:12pm 

# Repository Audit: Implementation vs. Plan & Review

This document outlines the findings after verifying the repository against the `interim_report.tex` specifications and addressing the issues raised in `Professor-Interim-Report-Review.txt`.

## 1. Code Language Verification

The interim report plans for specific languages per service. Here is the current state of the codebase versus expectations:

| Service | Planned Language | Actual Implemented Language | Status |
| :--- | :--- | :--- | :--- |
| **User Service** | Python 3.12 | Python | ✅ Match |
| **Journey Service** | Python 3.12 | Python | ✅ Match |
| **Enforcement Service** | Python 3.12 | Python | ✅ Match |
| **Notification Service** | Go 1.22 | Go | ✅ Match |
| **Conflict Detection** | Go 1.22 | Go | ✅ Match |
| **Analytics Service** | Go 1.22 | Go | ✅ Match |

> [!NOTE]
> All services now correctly align with the languages specified in the Interim Report following the successful merge of the Go implementations.

---

## 2. Professor's Review Verification

Below is an analysis of how the repository's code measures up against the issues raised by "Vinny".

### Geographic Distribution & Partitioning
* **Professor's Issue:** *"The geographic distribution of components is not clear – the system appears to be centralized. I see no provision for eg partitioning the data."*
* **Audit Finding:** The database data is not geographically partitioned. State is distributed across individual service databases (using the database-per-service pattern), and there is a `PartitionManager` in the code used to simulate network partition failures. However, true spatial or geographic partitioning (e.g., storing data in Europe vs. Asia) is **missing**. Interestingly, a section mentioning a "geographic region grid system" for partitioning road capacity exists in the `interim_report.tex`, but is currently commented out (`% \item \textbf{Road capacity data} ... partitioned by \textbf{geographic region}`). 

### Multi-national Journeys & Consistency
* **Professor's Issue:** *"How is consistency for multi-national journeys addressed? More generally how do the regions cooperate?"*
* **Audit Finding:** The system does not possess any multi-regional federation logic or logic for cross-border journeys. The architecture, while deployed locally via Docker, natively assumes a single overarching logical region.

### Reliability, Consistency, Transactions & Replication
* **Professor's Issue:** *"Again, reliability and consistency requirements are not obviously being addressed? Are you using transactions? Replication?"*
* **Audit Finding:** **Addressed in Code.**
   1. **Replication:** The `docker-compose.yml` explicitly implements read replicas for all Postgres databases (`postgres-users-replica`, `postgres-journeys-replica`, etc.) utilizing WAL streaming. Redis also utilizes replicas alongside Redis Sentinel (`redis-sentinel`) for high availability. 
   2. **Transactions & Consistency:** The Journey Service properly employs the **Saga pattern** (via `saga.py`) alongside an outbox pattern for asynchronous distributed consistency. It also uses traditional `asyncpg` transaction blocks for internal database writes.

### Road Map & Routing Management
* **Professor's Issue:** *"How is the road map managed? Where does routing happen?"*
* **Audit Finding:** The application behaves as a pre-booking verification system but relies purely on arbitrary origin/destination data and pre-computed estimations for conflicts. There is **no actual Map Routing logic** (e.g., Dijkstra/A* pathfinding, or road graph parsing) implemented anywhere in the backend logic.

---

## Summary of Action Required

If you wish to fully comply with the plan and address the professor's comments, the following changes are required:
1. **Implement Geographic Logic:** Add concrete mechanisms—at least logically—for geographic grid partitioning and multi-national boundaries within the conflict service or journey service.
3. **Map Routing Considerations:** Acknowledge or implement a mock routing layer to demonstrate how routing is executed, as currently, regions and road capacities are somewhat opaque.

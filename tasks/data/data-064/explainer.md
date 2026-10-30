# data-064 — explainer

## How to investigate

Two structural clues sit in plain sight. First, the wave pattern: `fixtures/latency-p99.csv` shows cache p99 flat at ~2 ms, then spiking to 180–250 ms at exactly the :00 and :05 marks, recovering within seconds, and repeating every five minutes with zero timeouts between waves. Traffic is not periodic like that — schedules are. Second, the slowlog: `fixtures/slowlog.txt` shows every entry is a `KEYS *` taking ~0.87–0.92 s. Redis is single-threaded; a blocking O(N) iteration over the full keyspace (`db0:keys=2148731` in `fixtures/redis-info.txt`) stalls every client request for its whole duration. A ~0.9 s stall matches the wave shape exactly.

Find who issues `KEYS *`. `fixtures/change-log.txt` (CHG-4471) shows the observability team's `cache-inventory-job` v1.4.0 went in on Tuesday with its export frequency changed from nightly to every 5 minutes (`*/5 * * * *`) — the same cadence as the waves, starting at the same time. The job's key inventory scan is the source.

## Why the red herring misleads

`fixtures/redis-info.txt` carries the tempting story: "Redis problems" usually means memory. It is not the case here — `used_memory` is 3.2G of an 8.0G `maxmemory` (40%), `evicted_keys:0`, fragmentation at 1.12. No evictions means no cache thrash; a memory-pressure diagnosis would push toward resizing the cache tier, which costs money and fixes nothing. Connection suspicion also dies in the same file: 148 of 10,000 clients, `rejected_connections:0` — and the app log confirms the client is never past buffering limits (`Write-Queue`/`Read-Queue` always 0), so nothing is piling up on the client side; the server simply stops responding, then recovers.

## Root cause

The observability `cache-inventory-job` v1.4.0 (change CHG-4471, deployed Tuesday) runs a key inventory scan every 5 minutes that issues `KEYS *` against the shared session cache. `KEYS` is a blocking O(N) command; over 2.1M keys it holds the single-threaded Redis server for ~0.9 s (`fixtures/slowlog.txt`), which produces the sharp five-minute latency waves and bursts of StackExchange.Redis timeout exceptions seen in `fixtures/latency-p99.csv` and `fixtures/app-timeouts.log`.

## Fix

Replace the job's `KEYS *` walk with cursor-based `SCAN` iteration (`SCAN` with a modest `COUNT` hint), or drop the full keyspace walk entirely and source the capacity report from keyspace summaries it can already read. Then make it structural:

```conf
rename-command KEYS ""
```

Disabling `KEYS` (and auditing for other O(N) commands like `SMEMBERS` on large sets) on shared prod caches turns this class of outage into a fast, loud job failure instead of a silent latency tax. Alert on slowlog entries — every one of these scans logged ~90x over the 10 ms threshold before anyone noticed checkout p99.

## Prevention

- **Blocking-command gate:** any job that can issue O(N) commands (`KEYS`, `SMEMBERS`, large `LRANGE`) against a shared cache needs change review; prefer `SCAN`-based enumeration by policy.
- **Schedule-aware deploys:** a config flip from `nightly` to `5m` should call out the 288x increase in work per day; the regression alert should watch the slowlog, which had the evidence two days before checkout latency did.

## Further reading

- [Redis KEYS — O(N), consider SCAN instead](https://redis.io/docs/latest/commands/keys/)
- [Redis SCAN — incremental cursor iteration](https://redis.io/docs/latest/commands/scan/)
- [Azure Cache for Redis best practices](https://learn.microsoft.com/azure/azure-cache-for-redis/cache-best-practices)
- [StackExchange.Redis timeouts](https://learn.microsoft.com/azure/azure-cache-for-redis/cache-troubleshoot-client)

# Capacity and operating limits

BananaWiki uses SQLite and serves pages through Gunicorn. Size the deployment from its measured workload, including plugins, uploads, search, and the size of its navigation tree. Hosting runs a separate Wiki container and database per tenant; the portal is a privileged control plane with Docker access. Use a dedicated host for it.

## Measured standalone workload

On 9 September 2026, an authenticated HTTP workload used 2,000 pages of approximately 5,071 characters each, Gunicorn 26.2.0 with two workers and four threads each, and CPU affinity to two AMD EPYC 9354P cores. The Debian 13 host had 32 GiB of RAM and also ran tests, a disposable VM and unrelated work. The run included short bursts and five minutes of continuous traffic; it does not establish a production service-level guarantee.

| Workload | Requests | Throughput | 95th percentile latency | Result |
| --- | ---: | ---: | ---: | --- |
| Reads, 1 concurrent client | 12 | 9.14/s | 111 ms | All HTTP 200 |
| Reads, 8 concurrent clients | 96 | 14.08/s | 998 ms | All HTTP 200 |
| Reads, 16 concurrent clients | 96 | 13.65/s | 2,511 ms | All HTTP 200 |
| Edits, 8 concurrent clients | 12 | 9.24/s | 789 ms | Every committed edit preserved |
| Continuous reads, 16 clients, 305 seconds | 5,568 | 18.27/s | 1,462 ms | All HTTP 200 |

All 440 health probes during continuous traffic succeeded, with 1,087 ms 95th percentile latency. Peak combined application proportional memory was approximately 242 MiB; summed resident memory was approximately 337 MiB and includes shared pages more than once. Proportional memory grew from 204 to 220 MiB over the sustained phase. SQLite's consistency check passed.

Navigation initially renders at most 100 page links and loads another 50 at a time. Category controls load when opened; sidebar people queries fetch only the displayed profiles without their biographies. Permission checks apply again to each batch, and reordering loaded links preserves omitted pages. The administrator response fell from approximately 2.2 MiB to 475 KiB in the isolated render fixture. A profiled render fell from 391 to 108 ms; those single renders are separate from the HTTP measurements above.

The category tree itself still includes all visible category metadata. These measurements do not establish capacity for extremely deep category trees, much larger databases or hundreds of simultaneous editors. Test the intended dataset before committing to that scale.

Gunicorn 26.2 or newer is required for its corrected threaded-worker shutdown. A regression test sends 400 concurrent requests through repeated worker recycling without retrying dropped connections. Active requests receive the configured graceful shutdown window; an operating-system stop deadline can still interrupt a requested shutdown.

## Deployment choices

Start with a conservative number of workers and retain memory for the OS, filesystem cache, backups, and plugins. More workers do not remove SQLite's single-writer boundary. Keep each database on reliable local storage; do not place a shared writable database on a network filesystem or run the same installation from multiple hosts.

Hosting applies memory, CPU, process and file-descriptor limits to each tenant. Leave headroom for the portal and tenant recovery. Domain-based Hosting defaults to a separate internal Docker network for each tenant. Enabling outbound integration access changes that boundary; apply the operator firewall policy described in [deployment](deployment.md).

Watch application/error logs, HTTP latency and failures, disk space, database lock waits, and tenant restarts. Test backup restoration with real data sizes. Optional TTS inference and external services have their own hardware and rate limits; they were not measured in this workload. See [operations](operations.md) for bounded startup/recovery settings and diagnostic locations.

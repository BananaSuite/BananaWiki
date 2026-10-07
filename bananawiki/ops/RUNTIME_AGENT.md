# Hosting runtime agent: interface for the hosting portal

In 1.4 the hosting portal's service account was in the `docker` group, so any
bug in the internet-facing portal (uploads, ZIP imports, OAuth, the tenant
proxy) was a root compromise of the host. In 1.6 only a small privileged
helper, `<service>-agent.service`, talks to Docker. The portal runs without
Docker access and asks the agent to run tenant containers over a local UNIX
socket. This file is the contract between `bananawiki/ops` (which installs
and runs the agent) and `bananawiki/hosting` (which calls it).

## Deployment

| Item | Value |
|---|---|
| Unit | `<service>-agent.service`, `User=root`, `Group=<service>`, `PrivateNetwork=true`, `PrivateDevices=true`, `CapabilityBoundingSet=CAP_DAC_OVERRIDE CAP_FOWNER CAP_CHOWN CAP_SYS_ADMIN CAP_SYS_PTRACE`, `ProtectSystem=strict`, routes state (0755) and private quota state (0700); mount, ptrace and process-vm syscalls denied |
| Command | `/usr/bin/python3 -E -s <root>/current/banana --root <root> agent serve` (stdlib only) |
| Socket | `/run/<service>-agent/agent.sock`, mode 0660, group `<service>`; directory 0750 |
| Portal setting | `BW_RUNTIME_AGENT_SOCKET` in `config/app.env` (written by the controller) |
| Ordering | portal and maintenance units have `After=`/`Wants=<service>-agent.service` |
| Callers | only uid 0 and the service account (checked with `SO_PEERCRED`) |
| Configuration | read by the agent from `config/installation.json` and `config/app.env` for every request; the portal cannot change it |

Operator-only settings in `app.env` (ceilings for what the portal may ask for):
`HOSTING_CONTAINER_IMAGE` (set by the updater to `bananawiki-tenant:<revision>`),
`INSTANCES_DIR`, `HOSTING_AGENT_MAX_MEMORY_MB` (4096), `HOSTING_AGENT_MAX_CPUS` (4),
`HOSTING_AGENT_MAX_PIDS` (4096), `HOSTING_AGENT_MAX_NOFILE` (65536),
`HOSTING_AGENT_MAX_STORAGE_BYTES` (10 GiB), `HOSTING_AGENT_MAX_INODES` (100,000),
`HOSTING_AGENT_PROJECT_ID_START` (1,000,000), and
`HOSTING_AGENT_STORAGE_RESERVE_BYTES` (256 MiB). Hosting requires a dedicated
XFS filesystem with project accounting/enforcement enabled and an unassigned,
non-inheriting instances parent. Custom instances paths need a matching unit
write-path override. See [hosting](../../docs/hosting.md) for host prerequisites.

`sudo bananawiki agent status` pings the agent.

## Using it from the portal

```python
from bananawiki.ops.agent_client import AgentError, connect

runtime = connect()   # AgentClient on BW_RUNTIME_AGENT_SOCKET
try:
    info = runtime.call("tenant.start", {"tenant": "acme", "env": {...}, "limits": {...}})
except AgentError as error:
    error.code, error.message
```

Transition: when `BW_RUNTIME_AGENT_SOCKET` is not set, the release was
installed by the 1.4 updater and its portal unit still has
`SupplementaryGroups=docker`. `connect()` then returns an in-process
`TenantRuntime` with the same `call()` and the same validation, running
Docker as the portal. This legacy fallback cannot satisfy the private hard-quota
contract and refuses launches and tasks until convergence. The next `bananawiki update` (also when no new revision
exists) or `bananawiki restart` converges the units: the agent is installed,
the portal leaves the `docker` group and the socket variable appears. The
portal must not call `docker` any other way.

## Wire protocol

One request per connection: a JSON object on one line (at most 256 KiB), one
JSON response line back.

```json
{"v": 1, "id": "c0ffee", "op": "tenant.start", "args": {...}}
{"v": 1, "id": "c0ffee", "ok": true, "result": {...}}
{"v": 1, "id": "c0ffee", "ok": false, "error": {"code": "invalid_env", "message": "..."}}
```

Error codes: `forbidden`, `unsupported_protocol`, `invalid_request`,
`unknown_operation`, `invalid_tenant`, `unknown_tenant`, `invalid_env`,
`invalid_owner`, `docker_failed`, `not_configured`, `quota_unavailable`,
`sandbox_outdated`, `sandbox_unavailable`, `too_large`, `internal`;
the client adds `unavailable` (socket unreachable) and `protocol`.

A tenant is named by its directory under `INSTANCES_DIR`: `<slug>` or
`<slug>__apex`, matching `[a-z0-9][a-z0-9-]{0,62}(__apex)?`. The directory
must exist, must not be a symlink, and must be owned by the service account
(never root).

### Operations

| op | args | result |
|---|---|---|
| `ping` | — | `{protocol, docker, image}` |
| `image.status` | — | `{image, present}` |
| `tenant.list` | — | `{tenants: [status…]}` (containers labelled as tenants of this `INSTANCES_DIR`) |
| `tenant.status` | `tenant` | `{tenant, container, running, address, internal_port, data_dir, image, started_at}` or `{tenant, running: false, exists: false}` |
| `tenant.quota` | `tenant`, `prepare` (boolean, default false), optional `limits.storage_bytes` | `{tenant, storage_quota_verified, storage_quota}`; stopped-only adoption, live verification or bounded limit update |
| `tenant.start` | see below | the tenant's status after `docker run` |
| `tenant.stop` | `tenant`, `timeout` (1–120 s, default 15) | `{tenant, stopped: true}`: container and its network removed |
| `tenant.logs` | `tenant`, `lines` (1–1000, default 200) | `{tenant, lines: [...]}` |
| `tenant.task` | `tenant`, `request`, `timeout` (5–600 s, default 120) | `{tenant, result}` (see below) |
| `proxy.routes` | `routes: [{tenant, hosts: [...]}]` | `{routes, changed, reloaded}` (see below) |

`tenant.start` arguments:

| arg | default | rule |
|---|---|---|
| `tenant` | required | as above |
| `env` | `{}` | at most 200 `BW_*` keys, values ≤ 8 KiB without CR/LF/NUL; `BW_SECRET_KEY` and `BW_SETUP_TOKEN` refused. Give container paths (`/data/...`). |
| `limits` | `{}` | `memory_mb` 128–ceiling (512), `cpus` 0.1–ceiling (1), `pids` 32–ceiling (256), `nofile` 128–ceiling (1024), `storage_bytes` 0–finite host ceiling (0 uses host ceiling) |
| `internal_port` | 5001 | 1024–65535; also set as `BW_PORT` |
| `network` | `"isolated"` | `"isolated"`: per-tenant `--internal` bridge; `"outbound"`: per-tenant routed bridge |
| `publish_port` | none | only with `"outbound"`: publishes `127.0.0.1:<port>` → internal port |

The agent always adds, and the portal cannot override: `--read-only`,
`--cap-drop ALL`, `no-new-privileges`, tmpfs `/tmp` (noexec) and `/run`,
`--user <owner uid>:<owner gid>` of the tenant directory, the single bind
mount `<tenant dir> → /data`, the operator's image, `json-file` logs rotated
at 3 × 10 MB, `--restart no`, and these variables: `BW_HOST=0.0.0.0`,
`BW_PROXY_MODE=1`, `BW_MANAGED_HOSTING=1`, `BW_PLUGIN_ISOLATION=container`,
`BW_ENV=production`, `BW_INSTANCE_DIR=/data`, `BW_DATABASE_PATH=/data/bananawiki.db`,
`BW_MAINTENANCE_FILE=/data/.banana-maintenance`,
`BW_EXTERNAL_PLUGINS_DIR=/data/external_plugins`. Variables are passed with a
0600 `--env-file` in the agent's private runtime directory, never on argv
(1.4 leaked the OAuth client secret and GPU token through `ps`).

Container names, labels and networks are identical to 1.4
(`bananawiki-<slug>-<sha256(realpath)[:12]>`, network `<name>-net`, labels
`org.bananawiki.role=tenant`, `org.bananawiki.data-dir=<realpath>`,
`org.bananawiki.internal-port=<port>`), so containers created by either
version are recognised and the updater's readiness check (C10) keeps working.

`tenant.task` runs `python -m bananawiki.ops.tenant_task` inside the tenant's
sandbox: `docker exec -i` in its container while it runs, otherwise a
one-shot `docker run --rm -i --network none` with the same image, user,
mount, hardening flags and env-file rules as `tenant.start` (label
`org.bananawiki.role=tenant-task`, so the updater ignores it). `request`
(at most 64 KiB) is a JSON object whose `action` is one of `seed`,
`migrate`, `list_users`, `set_password`, `remove_user`, `reset_admin`,
`analytics`, `apply_policy`, `quarantine`, `snapshot`, `restore_db`,
`discard`; it is written to the task's stdin (it may carry a password) and
never appears on a command line. `result` is the task's last stdout line,
a JSON object with `ok` (untrusted: the tenant controls that process), or
the request fails with `docker_failed` or `timeout`. This is how the portal
works on tenant databases without opening them host-side.

Start, stop and task operations serialize per tenant. Running-container tasks
also use `/usr/bin/timeout` inside the container, so killing the Docker client
cannot leave an exec process running indefinitely. One-shot tasks have unique
container names and are forcibly removed after the request, including timeout
and failure paths.

`proxy.routes` stores the wanted table (`hosts`: 1–16 lowercase DNS names
per tenant, each hostname once) in `routes.json` and renders
`/var/lib/<service>-routes/tenants.caddy`: one site block per *running*
tenant, proxying its hostnames to the container address that Docker
reports (private, non-loopback addresses only), with on-demand TLS, HSTS
and `X-Forwarded-Prefix` stripped. Each block also answers plain HTTP
(redirected to HTTPS except for Cloudflare "Flexible" requests), passes
`X-Forwarded-For: {client_ip}` (Caddy 2.7+, detected with `caddy version`),
retries a container that is still starting for 5 s and then hands the
request to the portal (`127.0.0.1:<HOSTING_PORT>`), which shows the wiki's
status page. The portal cannot choose upstreams or write Caddy directives. When the file changes the agent runs `systemctl
reload caddy`. A root-owned checksum records the last successfully reloaded
table; failed reloads are retried on the next sync, including after an agent
restart. It re-renders after every `tenant.start` and `tenant.stop`,
so a recreated container is routed at once. The Caddyfile written by
`bananawiki proxy` imports `/var/lib/<service>-routes/*.caddy`; without the
routes directory (in-process fallback) the operation answers
`not_configured`.

A stopped tenant's bridge is kept until Caddy acknowledges the table without
its old upstream. This prevents Docker from reallocating a still-routed IP
to another tenant during a reload outage. Retired bridges are tracked in
`retired-networks.json` and cleaned after a successful sync, including after
an agent restart. Cleanup attempts at most four bridges per sync and rotates
failed removals to the end, so restarted tenants cannot indefinitely block
cleanup of later stopped bridges. A change between isolated and outbound networking also
waits for that acknowledgement before releasing the former bridge.

The controller keeps both ends in step on every `update` (including the
convergence of a release the 1.4 updater installed) and `restart`: it creates
the routes directory, re-renders a Caddyfile it installed (digest in
`config/proxy.json`; the `--email`, `--tls` and `--cloudflare` given to `proxy`
are kept), validates it and
reloads Caddy only when it changed, and puts the previous file back when the
readiness check fails. For hosting releases with the agent in subdomain mode
that check also requires every running wiki the portal publishes to have a
site block pointing at its current container, and the maintenance service
publishes the table right after recovering the wikis.

## Storage launch gate

`tenant.quota` allocates a persistent private project ID and finite byte/inode
limits before the portal creates content. Kernel project accounting,
enforcement, inheritance, filesystem UUID, inode number/generation and exact
limits must match the registry. Uncertain Docker state never authorizes repair;
adoption of restored/legacy trees requires a confirmed stopped container.

Docker mounts the normal tenant path and starts only the fixed stdlib inert
`tenant_guard`. Before release or a task, the root agent opens the actual
`/proc/<Docker State.Pid>/root/data` descriptor and verifies it against the
prepared witness. A substituted directory is destroyed before tenant-controlled
code or data is accessed. The server release socket uses Linux’s abstract UNIX namespace within the
container network namespace; no filesystem marker can release it. The inert
guard disables process dumping and tracing before waiting. Hosting portal and
maintenance units cannot access `/proc`, including the bootstrap startup window,
and start through a trusted stdlib entrypoint that restricts `ioctl` arguments
before loading the application. It permits Python's descriptor/socket setup
requests while denying filesystem project/flag setters, so the service account
cannot change quota assignments outside a tenant's container policy. The root agent
retains the filesystem ioctls needed for quota assignment and verification.
Server gates expire after 120 seconds; task gates
have a bounded lifetime covering the requested task deadline. Running container
status and administrative exec also verify the actual mounted directory.
`CAP_SYS_PTRACE` permits this cross-UID proc descriptor check; ptrace and
process-vm syscalls remain denied. Before stopped repair, restart, stop or a
new one-shot task, the agent removes and confirms absence of that tenant’s
tasks left by an interrupted agent generation. It compares actual mounted
device/inode identities across live servers and tasks, so renaming a folder
cannot hide a writer. A live server using the folder under another name refuses
admission until it is stopped. A held root descriptor binds this inventory to
the inode the quota provisioner subsequently opens; replacing the pathname
refuses before quota mutation. Guard admission is serialized across tenant
names, while unrelated admitted tasks execute concurrently.
The agent accepts no caller-provided PID,
descriptor, quota device or project ID.

## What the portal still owns

* Recreating containers for ready `instances.status='running'` rows after
  start (the updater removes all tenant containers during an update and
  checks each tenant's `/health` on its bridge address).
* Creating tenant directories (as the service account) and the
  `.banana-maintenance` marker handling inside them.
* Reaching tenants: use `address` and `internal_port` from `tenant.status`.
* Tenant database migrations run inside the tenant container at start, and
  every other database operation goes through `tenant.task`; the portal
  never opens a tenant database host-side.

## Not in this version

* Per-tenant UIDs: containers run as the owner of the tenant directory,
  which is the service account, because the portal reads tenant files for
  backups and exports. Moving to per-tenant UIDs needs those file operations
  to move into the agent first (`tenant.archive` / `tenant.restore`); the
  protocol version will be bumped then.
* Managed tenant networking is IPv4-only: bridge creation requests IPv6 off
  and inspects the actual result, refusing enabled or uninspectable networks.
  The `all` and `default` `disable_ipv6=1` container sysctls also prevent
  link-local traffic. Starting/recovering a tenant replaces missing sandbox
  settings even if its image/application policy matches; administrative exec
  refuses an outdated running sandbox. Host Caddy IPv6 clients are unaffected.
  A derived IPv6 runtime needs independent IPv6 INPUT/neighbor-discovery checks;
  IPv4 INPUT rules cannot protect IPv6 traffic.

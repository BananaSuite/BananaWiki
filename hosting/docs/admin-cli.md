# Hosting operator CLI

BananaWiki hosting includes an SSH-only operator CLI for recovery and server
administration when the web portal is unavailable or when an admin needs a
direct override from the server.

Run it from the repository root:

```bash
python -m hosting.admin_cli --help
```

or use the wrapper:

```bash
scripts/hostingctl.py --help
```

The CLI does not use Flask sessions, CSRF tokens, browser cookies, or portal
login state. It is intended only for operators who already have trusted shell
access to the hosting server. Commands mutate the same hosting database and
call the same instance lifecycle helpers used by the web admin UI.

## Examples

```bash
# Show platform stats and settings
python -m hosting.admin_cli status

# Create a hosting account directly
python -m hosting.admin_cli account create alice 'temporaryPassword123'

# Promote or demote an account
python -m hosting.admin_cli account set-admin alice --admin
python -m hosting.admin_cli account set-admin alice --no-admin

# Suspend an account and all of its running/stopped instances
python -m hosting.admin_cli account suspend alice \
  --hours 24 \
  --reason "Billing issue" \
  --reason-visible \
  --time-visible \
  --also-instances

# Create an instance for an account
python -m hosting.admin_cli instance create alice teamwiki

# Pause, resume, or force-restart an instance
python -m hosting.admin_cli instance stop teamwiki
python -m hosting.admin_cli instance restart teamwiki
python -m hosting.admin_cli instance force-restart teamwiki

# Reset a user password inside an instance DB
python -m hosting.admin_cli instance set-user-password teamwiki admin 'newPassword123' --role admin

# Terminate or hard-delete require explicit confirmation
python -m hosting.admin_cli instance terminate teamwiki --yes
python -m hosting.admin_cli instance hard-delete teamwiki--terminated-20260608 --yes
```

Use `--json` for scripts:

```bash
python -m hosting.admin_cli --json account list
```

Use `--hosting-db` and `--instances-dir` for recovery against non-default
paths, staging copies, or tests:

```bash
python -m hosting.admin_cli \
  --hosting-db /srv/bananawiki/hosting.db \
  --instances-dir /srv/bananawiki/instances \
  status
```

## Audit Trail

The CLI appends best-effort JSONL audit records to:

```text
hosting/data/admin_cli_audit.log
```

This file is a local operational trail and does not replace server SSH logs
or centralized audit logging.

# BananaWiki Bandwidth Fuse

This is an optional, manual Linux-level guard for short free-tier pilots.
It watches monthly outbound traffic with `vnstat` and powers off the VM when
traffic reaches a configured limit.

It does not change BananaWiki application code. It is meant for cases where
the safest outcome is "the service goes offline before the host risks paid
bandwidth."

## Recommended Limit

For Azure free-account trials, use a conservative limit:

```text
12 GiB outbound per calendar month
```

Azure documents a 15 GB/month free-account bandwidth allowance and also lists
100 GB/month free internet egress on the general bandwidth pricing page. This
fuse intentionally uses the lower number with margin. Linux `vnstat` counters
and Azure billing counters are not guaranteed to match exactly.

`vnstat` tracks calendar-month usage. If your cloud billing period starts on a
different day, keep the threshold conservative or adjust the service manually
near the billing reset.

## What Happens When The Limit Is Reached

The script:

1. Detects the active default network interface.
2. Reads this month's transmitted traffic from `vnstat`.
3. Stops `nginx` and `bananawiki` if they exist.
4. Calls `systemctl poweroff`.

It powers off the Linux guest, not the Azure VM resource via the Azure API.
For bandwidth protection that is enough: the VM stops serving users and stops
generating normal network traffic. If you need Azure to show
`Stopped (deallocated)`, deallocate it manually from the Azure portal after the
fuse trips.

## Install

Copy the files to the host:

```bash
sudo apt update
sudo apt install -y vnstat jq
sudo systemctl enable --now vnstat

sudo install -m 0644 ops/bandwidth-fuse/bananawiki-bandwidth-fuse.conf \
  /etc/bananawiki-bandwidth-fuse.conf
sudo install -m 0755 ops/bandwidth-fuse/bananawiki-bandwidth-fuse \
  /usr/local/sbin/bananawiki-bandwidth-fuse
sudo install -m 0644 ops/bandwidth-fuse/bananawiki-bandwidth-fuse.service \
  /etc/systemd/system/bananawiki-bandwidth-fuse.service
sudo install -m 0644 ops/bandwidth-fuse/bananawiki-bandwidth-fuse.timer \
  /etc/systemd/system/bananawiki-bandwidth-fuse.timer

sudo systemctl daemon-reload
sudo systemctl enable --now bananawiki-bandwidth-fuse.timer
```

When installing from `/opt/BananaWiki`, run those commands from the repository
root. If the files are elsewhere, adjust the source paths.

## Test Without Powering Off

Set dry-run mode:

```bash
sudo sed -i 's/^DRY_RUN=.*/DRY_RUN=1/' /etc/bananawiki-bandwidth-fuse.conf
sudo /usr/local/sbin/bananawiki-bandwidth-fuse
journalctl -u bananawiki-bandwidth-fuse.service -n 50
```

Restore real mode:

```bash
sudo sed -i 's/^DRY_RUN=.*/DRY_RUN=0/' /etc/bananawiki-bandwidth-fuse.conf
```

To test the trip path without waiting for real traffic, temporarily set a tiny
limit while `DRY_RUN=1`:

```bash
sudo sed -i 's/^LIMIT_GIB=.*/LIMIT_GIB=0.001/' /etc/bananawiki-bandwidth-fuse.conf
sudo /usr/local/sbin/bananawiki-bandwidth-fuse
```

Set it back afterwards:

```bash
sudo sed -i 's/^LIMIT_GIB=.*/LIMIT_GIB=12/' /etc/bananawiki-bandwidth-fuse.conf
```

## Check Usage

Monthly traffic:

```bash
vnstat -m
```

Current month outbound traffic in GiB:

```bash
IFACE="$(ip route get 1.1.1.1 | awk '{for(i=1;i<=NF;i++) if ($i=="dev") print $(i+1)}' | head -n1)"
vnstat --json m 1 -i "$IFACE" | jq '.interfaces[0].traffic.month[0].tx / 1073741824'
```

Live traffic:

```bash
vnstat -l
```

## Reboot And Manual Recovery Behavior

The timer waits 10 minutes after boot before checking usage. That grace period
gives you time to SSH in after restarting the VM.

If the VM powered off because the monthly limit was exceeded, starting it again
in the same month will cause it to power off again after the grace period.
Before that happens, either disable the fuse or raise the limit.

Temporarily disable:

```bash
sudo touch /etc/bananawiki-bandwidth-fuse.disabled
```

Re-enable:

```bash
sudo rm -f /etc/bananawiki-bandwidth-fuse.disabled
```

Change the limit:

```bash
sudo nano /etc/bananawiki-bandwidth-fuse.conf
```

## Logs And Status

```bash
systemctl status bananawiki-bandwidth-fuse.timer
journalctl -u bananawiki-bandwidth-fuse.service -n 100
```

## Remove

```bash
sudo systemctl disable --now bananawiki-bandwidth-fuse.timer
sudo rm -f /etc/systemd/system/bananawiki-bandwidth-fuse.timer
sudo rm -f /etc/systemd/system/bananawiki-bandwidth-fuse.service
sudo rm -f /usr/local/sbin/bananawiki-bandwidth-fuse
sudo rm -f /etc/bananawiki-bandwidth-fuse.conf
sudo rm -f /etc/bananawiki-bandwidth-fuse.disabled
sudo systemctl daemon-reload
```

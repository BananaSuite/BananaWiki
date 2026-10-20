# Tenant seccomp profile

`default-docker-29.8.2.json` is Docker's default seccomp profile, unmodified,
from Moby 29.8.2 (commit `8af9fe3a36bab3e039862a2ab1cef1880c9b4d03`, vendoring
`github.com/moby/profiles/seccomp` v0.2.3). Its SHA256 is
`536529b665dd0972c37bfb569f5d4ac8a53592e7b00752bc39ff063ca9864c74`.

Upstream source:
https://github.com/moby/moby/blob/8af9fe3a36bab3e039862a2ab1cef1880c9b4d03/vendor/github.com/moby/profiles/seccomp/default.json

Upstream license:
https://github.com/moby/profiles/blob/seccomp/v0.2.3/LICENSE

The profile is licensed under Apache-2.0; the license text is in `LICENSE`.

## `tenant.json`

`tenant.json` keeps every upstream rule except the unconditional `ioctl`
allowance. In its place, masked prefixes allow every ioctl command except:

* `FS_IOC_FSSETXATTR` (`0x401c5820`)
* `FS_IOC_SETFLAGS`, in its 32-bit and 64-bit layouts (`0x40046602`,
  `0x40086602`)

Each rule masks the command to its low 32 bits, as the kernel does, so sign
extension and upper bits cannot get around it. The refused commands return
EPERM, the upstream default. The unconditional `ioctl` rule must not come
back: libseccomp lets it override the argument-specific denials.

Without this profile a file owner could change an XFS project ID or clear
project inheritance on a directory, and escape the quota. Read-only attribute
queries and ordinary file, ACL, socket and terminal operations still work.

The profile keeps upstream's x86_64/x86/x32 architecture map. The agent runs
tenants only on x86_64 hosts.

The agent applies the profile to running tenants and to tasks on stopped
ones. `docker exec` inherits the container's profile, so the agent refuses
tasks in a container started with an older profile or network settings, and
replaces that container on its next start. The agent refuses to start
tenants if the profile is missing or changed. When updating Docker's
baseline, keep these ioctl rules.

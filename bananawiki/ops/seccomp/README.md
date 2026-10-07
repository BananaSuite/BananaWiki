# Tenant seccomp profile

`default-docker-29.8.2.json` is the unmodified Docker/Moby 29.8.2 default
allowlist from commit `8af9fe3a36bab3e039862a2ab1cef1880c9b4d03`, vendoring
`github.com/moby/profiles/seccomp` v0.2.3. Its SHA256 is
`536529b665dd0972c37bfb569f5d4ac8a53592e7b00752bc39ff063ca9864c74`.

Upstream source:
https://github.com/moby/moby/blob/8af9fe3a36bab3e039862a2ab1cef1880c9b4d03/vendor/github.com/moby/profiles/seccomp/default.json

Upstream license:
https://github.com/moby/profiles/blob/seccomp/v0.2.3/LICENSE

The profile is Apache-2.0 licensed; its matching license is included here.
Quota-protected hosting currently requires Linux x86_64. The derived profile
retains the upstream x86_64/x86/x32 architecture map, and the agent refuses
other host architectures until their ioctl encodings and enforcement are verified.
`tenant.json` retains every upstream rule except the unconditional ioctl
allowance. It replaces that allowance with masked prefixes covering every
low-32-bit command except `FS_IOC_FSSETXATTR` (`0x401c5820`) and both
32-bit/64-bit `FS_IOC_SETFLAGS` layouts (`0x40046602`, `0x40086602`). Each rule
masks the command argument to its low 32 bits, as the Linux ioctl implementation
does, so sign extension and upper-bit aliases cannot bypass the restriction.
The excluded commands return the upstream default EPERM (1). An unconditional
ioctl allowance must not remain: libseccomp's generic rule would override
argument-specific denials added alongside it.

An ordinary file owner can otherwise change an XFS quota project ID or clear
directory project inheritance, even without capabilities. The profile prevents
tenant code from escaping the agent's project byte/inode quotas. This policy
is one part of the storage boundary: the runtime agent separately provisions
and verifies finite limits before execution; operators must enable and verify
those limits on the hosting filesystem. Read-only attribute queries and
ordinary file, ACL, socket and terminal operations remain available.

The agent applies the profile to live tenants and stopped-tenant tasks.
Docker exec inherits the container's seccomp policy; the agent refuses tasks
in older running containers until they use this exact profile and the current
IPv6-disabled network settings. Starting/recovering a tenant replaces an
outdated sandbox even when its image and application policy match.
Missing or changed profile files fail closed before replacing a running wiki.
Maintain the upstream baseline alongside Docker releases and retain these
quota-changing ioctl denials when updating it.

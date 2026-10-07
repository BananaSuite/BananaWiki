"""Storage must be enforced before launch, repair or any tenant task."""
from __future__ import annotations

import json
import os
import subprocess

import pytest
from test_ops_agent import runtime as runtime

from bananawiki.ops.runtime_agent import (
    DEFAULT_STORAGE_BYTES,
    DEFAULT_STORAGE_INODES,
    MOUNT_INVENTORY_FORMAT,
    AgentError,
)


@pytest.mark.parametrize("operation", ["tenant.start", "tenant.quota", "tenant.task"])
def test_quota_failure_never_removes_or_executes_a_tenant(runtime, operation):
    runtime._injected_quota.failure = "kernel project enforcement disabled"
    with pytest.raises(AgentError, match="enforcement disabled"):
        runtime.call(operation, {"tenant": "acme", "prepare": True, "request": {"action": "list_users"}})
    assert not any(call[1] in {"run", "exec", "rm"} for call in runtime.docker_calls.calls)


def test_missing_quota_configuration_has_no_unlimited_fallback(runtime):
    runtime._injected_quota = None
    with pytest.raises(AgentError) as error:
        runtime.call("tenant.start", {"tenant": "acme"})
    assert error.value.code == "quota_unavailable"
    assert not runtime.docker_calls.calls


def test_zero_portal_limit_keeps_finite_host_byte_and_inode_limits(runtime):
    response = runtime.call("tenant.quota", {"tenant": "acme", "prepare": True, "limits": {"storage_bytes": 0}})
    assert response["storage_quota"]["byte_limit"] == DEFAULT_STORAGE_BYTES
    assert response["storage_quota"]["inode_limit"] == DEFAULT_STORAGE_INODES


@pytest.mark.parametrize("value", [-1, True, "1024", 1.5, DEFAULT_STORAGE_BYTES + 1])
def test_invalid_storage_limits_do_not_touch_the_current_container(runtime, value):
    with pytest.raises(AgentError) as error:
        runtime.call("tenant.start", {"tenant": "acme", "limits": {"storage_bytes": value}})
    assert error.value.code == "invalid_request"
    assert not runtime.docker_calls.calls


@pytest.mark.parametrize("output,code", [("[]", 0), ("broken", 0), ("[]", 1)])
def test_unknown_container_state_never_authorizes_stopped_tree_repair(runtime, output, code):
    def runner(command, **kwargs):
        runtime.docker_calls.calls.append(command)
        if command[1] == "inspect":
            return subprocess.CompletedProcess(command, code, output, "daemon unavailable")
        return subprocess.CompletedProcess(command, 1, "", "daemon unavailable")
    runtime.runner = runner
    with pytest.raises(AgentError) as error:
        runtime.call("tenant.quota", {"tenant": "acme", "prepare": True})
    assert error.value.code == "unavailable"
    assert not runtime._injected_quota.entries
    assert not any(call[1] in {"run", "rm"} for call in runtime.docker_calls.calls)


def test_named_container_cannot_claim_another_tenant_directory(runtime, tmp_path):
    def runner(command, **kwargs):
        item = {"Name": "/wrong", "State": {"Running": True}, "Config": {"Labels": {
            "org.bananawiki.role": "tenant", "org.bananawiki.data-dir": str(tmp_path / "instances/beta")}}}
        return subprocess.CompletedProcess(command, 0, json.dumps([item]), "")
    runtime.runner = runner
    with pytest.raises(AgentError) as error:
        runtime.call("tenant.quota", {"tenant": "acme", "prepare": True})
    assert error.value.code == "sandbox_outdated"
    assert not runtime._injected_quota.entries


@pytest.mark.parametrize("operation", ["tenant.start", "tenant.task"])
def test_wrong_actual_docker_mount_is_destroyed_before_release_or_task(runtime, operation):
    runtime._injected_quota.prepare("acme", byte_limit=DEFAULT_STORAGE_BYTES, inode_limit=DEFAULT_STORAGE_INODES)
    def refuse(pid, witness):
        assert any(call[1] == "run" and "bananawiki.ops.tenant_guard" in call
                   for call in runtime.docker_calls.calls)
        raise AgentError("quota_unavailable", "actual mount differs from verified inode")
    runtime._verify_mount_descriptor = refuse
    with pytest.raises(AgentError, match="actual mount differs"):
        runtime.call(operation, {"tenant": "acme", "request": {"action": "list_users"}})
    assert not any(call[1] == "exec" for call in runtime.docker_calls.calls)
    assert runtime.docker_calls.calls[-1][1:3] == ["rm", "--force"]


def test_restarted_agent_removes_old_tasks_before_stopped_quota_repair(runtime):
    from bananawiki.ops.runtime_agent import TenantRuntime, container_name

    orphan = "a" * 12
    inventory = [orphan]
    original = runtime.runner
    root = runtime.tenant_dir("acme")
    queries = []
    def runner(command, **kwargs):
        if command[1] == "ps" and "label=org.bananawiki.role=tenant-task" in command:
            queries.append(command)
            assert f"name=^/{container_name(str(root))}-task-[0-9a-f]{{12}}$" in command
            return subprocess.CompletedProcess(command, 0, "\n".join(inventory), "")
        if command[1:3] == ["rm", "--force"] and command[3:] == [orphan]:
            inventory.clear()
            return subprocess.CompletedProcess(command, 0, orphan, "")
        return original(command, **kwargs)
    prepare = runtime._injected_quota.prepare
    def guarded_prepare(*args, **kwargs):
        assert kwargs["repair"] is True and not inventory
        assert len(queries) == 2, "successful removal is confirmed before repair"
        return prepare(*args, **kwargs)
    runtime._injected_quota.prepare = guarded_prepare
    restarted = TenantRuntime(runtime.config, runner=runner, private_dir=runtime.private_dir,
                              quota=runtime._injected_quota)
    response = restarted.call("tenant.quota", {"tenant": "acme", "prepare": True})
    assert response["storage_quota_verified"] and not inventory


@pytest.mark.parametrize("failure", ["listing", "malformed", "remove", "still_present"])
def test_unconfirmed_orphan_cleanup_never_authorizes_stopped_repair(runtime, failure):
    original = runtime.runner
    orphan = "a" * 12
    def runner(command, **kwargs):
        if command[1] == "ps" and "label=org.bananawiki.role=tenant-task" in command:
            return subprocess.CompletedProcess(command, 1 if failure == "listing" else 0,
                                                "../unsafe" if failure == "malformed" else orphan, "unavailable")
        if command[1:3] == ["rm", "--force"] and command[3:] == [orphan]:
            return subprocess.CompletedProcess(command, 1 if failure == "remove" else 0, "", "cannot remove")
        return original(command, **kwargs)
    runtime.runner = runner
    with pytest.raises(AgentError) as error:
        runtime.call("tenant.quota", {"tenant": "acme", "prepare": True})
    assert error.value.code == "docker_failed"
    assert not runtime._injected_quota.entries
    assert not any(call[1] in {"run", "exec"} for call in runtime.docker_calls.calls)


def _live_mounts(runtime, mounts):
    """Daemon inventory fixture; mount identities use actual held directory inodes."""
    original = runtime.runner
    active = {item["id"]: item for item, _path in mounts}
    paths = {item["pid"]: path for item, path in mounts}
    def runner(command, **kwargs):
        runtime.docker_calls.calls.append(command)
        if command[1] == "ps" and "label=org.bananawiki.role" in command:
            return subprocess.CompletedProcess(command, 0, "\n".join(active), "")
        if command[1] == "inspect" and MOUNT_INVENTORY_FORMAT in command:
            return subprocess.CompletedProcess(command, 0, "\n".join(json.dumps(item) for item in active.values()), "")
        if command[1:3] == ["rm", "--force"]:
            for identifier in command[3:]:
                active.pop(identifier, None)
            return subprocess.CompletedProcess(command, 0, "", "")
        return original(command, **kwargs)
    def identity(pid):
        info = paths[pid].stat()
        return info.st_dev, info.st_ino
    runtime.runner = runner
    runtime._mounted_identity = identity
    return active


def test_renamed_live_task_is_removed_by_mount_identity_before_repair(runtime, tmp_path):
    root = runtime.tenant_dir("acme")
    unrelated = tmp_path / "unrelated"
    unrelated.mkdir()
    alias = {"id": "a" * 64, "name": "/obsolete-task-name", "role": "tenant-task", "running": True, "pid": 21}
    other = {"id": "b" * 64, "name": "/another-task", "role": "tenant-task", "running": True, "pid": 22}
    # The inode moves; its original path label and name no longer describe it.
    renamed = root.with_name("renamed")
    root.rename(renamed)
    active = _live_mounts(runtime, [(alias, renamed), (other, unrelated)])
    prepare = runtime._injected_quota.prepare
    def checked_prepare(*args, **kwargs):
        assert kwargs["repair"] is True and alias["id"] not in active
        assert other["id"] in active and os.fstat(kwargs["expected_descriptor"]).st_ino == renamed.stat().st_ino
        return prepare(*args, **kwargs)
    runtime._injected_quota.prepare = checked_prepare
    assert runtime.call("tenant.quota", {"tenant": "renamed", "prepare": True})["storage_quota_verified"]
    assert set(active) == {other["id"]}


@pytest.mark.parametrize("operation", ["tenant.quota", "tenant.start", "tenant.task", "tenant.stop"])
def test_live_server_alias_refuses_repair_launch_task_and_delete(runtime, operation):
    root = runtime.tenant_dir("acme")
    runtime._injected_quota.prepare("acme", byte_limit=DEFAULT_STORAGE_BYTES, inode_limit=DEFAULT_STORAGE_INODES)
    server = {"id": "c" * 64, "name": "/old-server-name", "role": "tenant", "running": True, "pid": 23}
    active = _live_mounts(runtime, [(server, root)])
    before = dict(runtime._injected_quota.entries)
    with pytest.raises(AgentError) as error:
        runtime.call(operation, {"tenant": "acme", "prepare": True, "request": {"action": "list_users"}})
    assert error.value.code == "tenant_running"
    assert runtime._injected_quota.entries == before and server["id"] in active
    assert not any(call[1] in {"run", "exec", "rm"} for call in runtime.docker_calls.calls)


def test_replacement_after_mount_inventory_refuses_native_prepare(runtime):
    root = runtime.tenant_dir("acme")
    original = runtime._quiesce_mount
    def substitute(descriptor, **kwargs):
        original(descriptor, **kwargs)
        root.rename(root.with_name("held"))
        root.mkdir()
    runtime._quiesce_mount = substitute
    with pytest.raises(AgentError, match="trusted pre-scan descriptor"):
        runtime.call("tenant.quota", {"tenant": "acme", "prepare": True})
    assert not runtime._injected_quota.entries


@pytest.mark.parametrize("failure", ["inventory", "inspection", "missing_pid", "remove", "confirmation"])
def test_uncertain_actual_alias_inventory_never_allows_repair(runtime, failure):
    root = runtime.tenant_dir("acme")
    task = {"id": "d" * 64, "name": "/old-task", "role": "tenant-task", "running": True, "pid": 24}
    _live_mounts(runtime, [(task, root)])
    original = runtime.runner
    def runner(command, **kwargs):
        if command[1] == "ps" and "label=org.bananawiki.role" in command:
            if failure == "inventory" or (failure == "confirmation" and "--all" in command):
                return subprocess.CompletedProcess(command, 0, "unsafe inventory", "")
        if command[1] == "inspect" and MOUNT_INVENTORY_FORMAT in command and failure == "inspection":
            return subprocess.CompletedProcess(command, 0, "[]", "")
        if command[1:3] == ["rm", "--force"] and failure == "remove":
            return subprocess.CompletedProcess(command, 1, "", "unavailable")
        return original(command, **kwargs)
    runtime.runner = runner
    if failure == "missing_pid":
        runtime._mounted_identity = lambda pid: (_ for _ in ()).throw(AgentError("quota_unavailable", "missing mount"))
    with pytest.raises(AgentError):
        runtime.call("tenant.quota", {"tenant": "acme", "prepare": True})
    assert not runtime._injected_quota.entries

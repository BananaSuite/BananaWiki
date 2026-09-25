"""Lifecycle actions cannot stop unrelated listeners or hide Docker failures."""

import socket
import subprocess

import pytest

from hosting import container_runtime, instance_manager


def test_occupied_port_does_not_trigger_a_process_kill(monkeypatch):
    monkeypatch.setattr(subprocess, "run", lambda *_a, **_k: pytest.fail("Port waiting must not inspect or kill arbitrary listeners"))
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = listener.getsockname()[1]
        assert instance_manager._wait_for_port_free(port, timeout=0.01) is False
        with socket.create_connection(("127.0.0.1", port), timeout=1):
            pass
    assert instance_manager._wait_for_port_free(port, timeout=0.1) is True


def test_docker_removal_failure_cannot_be_reported_as_stopped(monkeypatch, tmp_path):
    monkeypatch.setattr(container_runtime, "_docker", lambda *_a, **_k: subprocess.CompletedProcess([], 1, "", "permission denied"))
    assert container_runtime.stop_container(tmp_path) is False
    monkeypatch.setattr(instance_manager.config, "HOSTING_INSTANCE_RUNTIME", "docker")
    with pytest.raises(RuntimeError, match="confirm"):
        instance_manager._stop_process(tmp_path)


def test_already_removed_container_is_an_idempotent_stop(monkeypatch, tmp_path):
    monkeypatch.setattr(container_runtime, "_docker", lambda *_a, **_k: subprocess.CompletedProcess([], 1, "", "Error: No such container: fixture"))
    assert container_runtime.stop_container(tmp_path) is True

"""Quota assignment precedes seed, copy, import and platform restore writes."""
from __future__ import annotations

from pathlib import Path

import pytest

from bananawiki.hosting.runtime import RuntimeFailure, platform_backup
from bananawiki.ops.runtime_agent import AgentError

from .agent_fakes import make_runtime, make_spec, provision


def test_initial_quota_is_assigned_on_an_empty_root_before_seed(tmp_path):
    runtime, agent = make_runtime(tmp_path)
    original = agent.tenant_quota
    observed = []
    def check(args):
        observed.append(list((Path(runtime._cfg().instances_dir) / args["tenant"]).iterdir()))
        return original(args)
    agent.tenant_quota = check
    provision(runtime, make_spec())
    assert observed[0] == []
    operations = [op for op, _ in agent.calls]
    assert operations.index("tenant.quota") < operations.index("tenant.task") < operations.index("tenant.start")


@pytest.mark.parametrize("kind", ["create", "import", "copy"])
def test_quota_refusal_prevents_new_tenant_content_or_execution(tmp_path, kind):
    runtime, agent = make_runtime(tmp_path)
    if kind == "copy":
        provision(runtime, make_spec("source"))
    agent.failures["tenant.quota"] = AgentError("quota_unavailable", "insufficient quota capacity")
    before = len(agent.calls)
    with pytest.raises(RuntimeFailure) as error:
        if kind == "create":
            provision(runtime, make_spec("target"))
        elif kind == "import":
            runtime.import_archive(make_spec("target"), tmp_path / "never-opened.zip")
        else:
            runtime.duplicate(make_spec("source"), make_spec("target"))
    assert error.value.code == "not_configured"
    root = Path(runtime._cfg().instances_dir) / "target"
    assert not root.exists() or not list(root.iterdir())
    assert not any(op in {"tenant.start", "tenant.task"} and args.get("tenant") == "target"
                   for op, args in agent.calls[before:])


def test_platform_quota_refusal_removes_only_new_restore_roots(tmp_path):
    runtime, _agent = make_runtime(tmp_path)
    staged = tmp_path / "staged"
    for name in ("acme", "beta"):
        (staged / name).mkdir(parents=True)
        (staged / name / "asset.txt").write_text("data")
    prepared = []
    def quota(name, limit):
        root = Path(runtime._cfg().instances_dir) / name
        assert list(root.iterdir()) == []
        prepared.append((name, limit))
        if name == "beta":
            raise RuntimeFailure("not_configured", "capacity exhausted")
    with pytest.raises(RuntimeFailure):
        platform_backup._install_tenants(staged, runtime._cfg(), prepare_tenant=quota,
                                         storage_limits={"acme": 4096, "beta": 8192})
    assert prepared == [("acme", 4096), ("beta", 8192)]
    assert list(Path(runtime._cfg().instances_dir).iterdir()) == []


def test_platform_restore_never_overwrites_or_cleans_preexisting_root(tmp_path):
    runtime, _agent = make_runtime(tmp_path)
    staged = tmp_path / "staged/acme"
    staged.mkdir(parents=True)
    (staged / "asset.txt").write_text("new")
    root = Path(runtime._cfg().instances_dir) / "acme"
    root.mkdir(parents=True)
    (root / "keep.txt").write_text("old")
    with pytest.raises(FileExistsError):
        platform_backup._install_tenants(staged.parent, runtime._cfg())
    assert (root / "keep.txt").read_text() == "old"
    assert not (root / "asset.txt").exists()

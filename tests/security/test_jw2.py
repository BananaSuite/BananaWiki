"""Regression tests for the JW2 findings of the full re-audit pass 5 (hosted wikis' download threads)."""

from __future__ import annotations

from bananawiki.hosting.runtime.agent import TENANT_DOWNLOAD_THREADS, tenant_environment
from bananawiki.ops import tenant_entrypoint
from tests.hosting.agent_fakes import make_runtime, make_spec

# JW2-1: the tenant's --threads keeps threads for short requests next to the download places


def _threads(command):
    return int(command[command.index("--threads") + 1])


def test_jw2_1_hosted_wikis_run_threads_for_their_download_places(tmp_path):
    from bananawiki.wiki.config import load_config

    runtime, _agent = make_runtime(tmp_path)
    env = {**tenant_environment(make_spec(), runtime._cfg(), quarantined=False), "BW_INSTANCE_GUNICORN_THREADS": "4"}
    assert env["BW_DOWNLOAD_THREADS"] == str(TENANT_DOWNLOAD_THREADS)
    places = load_config({**env, "BW_ENV": "test", "BW_INSTANCE_DIR": str(tmp_path / "wiki")},
                         secret_key="k" * 40).download_threads
    # --threads overrides the configuration file: it must hold the tenant's threads plus every download place.
    assert _threads(tenant_entrypoint.web_command(env)) == 4 + places
    assert _threads(tenant_entrypoint.web_command({})) == 4 + 8  # a wiki's own defaults
    assert _threads(tenant_entrypoint.web_command({"BW_INSTANCE_GUNICORN_THREADS": "x"})) == 4 + 8

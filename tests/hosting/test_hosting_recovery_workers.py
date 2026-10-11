"""``BW_HOSTING_RECOVERY_MAX_WORKERS`` sizes both the portal's recovery and the updater's wait for it.

The updater (``bananawiki.ops.system``) derives its readiness timeout from the
number of wikis restarted at once; the maintenance service must restart that
many at once, not a fixed number of its own.
"""

from __future__ import annotations

import pytest

from bananawiki.hosting.config import load_config
from bananawiki.hosting.runtime import agent as agent_module

from .agent_fakes import make_runtime, make_spec, provision
from .hosting_support import portal_environ


@pytest.mark.parametrize(("value", "workers"), [
    (None, 2), ("", 2), ("1", 1), ("4", 4), ("16", 16),
    # Read as the updater reads it: clamped to 1-16, anything else is the default.
    ("99", 16), ("0", 1), ("-3", 1), ("many", 2),
])
def test_the_setting_is_read_as_the_updater_reads_it(tmp_path, value, workers):
    extra = {} if value is None else {"BW_HOSTING_RECOVERY_MAX_WORKERS": value}
    assert load_config(portal_environ(tmp_path, **extra), secret_key="k" * 40).recovery_max_workers == workers


def test_recovery_restarts_that_many_wikis_at_once(tmp_path, monkeypatch):
    sizes = []
    real = agent_module.ThreadPoolExecutor

    def recording(*args, **kwargs):
        sizes.append(kwargs.get("max_workers"))
        return real(*args, **kwargs)

    monkeypatch.setattr(agent_module, "ThreadPoolExecutor", recording)
    runtime, agent = make_runtime(tmp_path, BW_HOSTING_RECOVERY_MAX_WORKERS="3")
    specs = [make_spec(f"wiki{number}") for number in range(5)]
    for spec in specs:
        provision(runtime, spec)
    agent.containers.clear()
    assert runtime.recover(specs) == 5
    assert sizes == [3]

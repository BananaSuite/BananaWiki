"""Hook system for plugins.

Hooks let core code notify plugins when something happens (e.g.
``after_page_update``) and let plugins subscribe to those events.

Usage::

    # In a plugin's __init__.py
    from bananawiki_sdk import hook

    @hook("after_page_update")
    def on_page_update(page, user, **kwargs):
        ...

    # In core code
    from bananawiki_sdk import emit_hook
    emit_hook("after_page_update", page=page_row, user=current_user)
"""

import threading
from collections import defaultdict

# Global registry: hook_name → list of callables
_hook_registry = defaultdict(list)
_hook_lock = threading.Lock()


def hook(hook_name):
    """Decorator that subscribes a function to a named hook.

    Example::

        @hook("after_page_update")
        def handle_page_update(page, user, **kwargs):
            ...
    """
    def decorator(fn):
        with _hook_lock:
            _hook_registry[hook_name].append(fn)
        # Stash metadata so the loader can inspect it
        if not hasattr(fn, "_bw_hooks"):
            fn._bw_hooks = []
        fn._bw_hooks.append(hook_name)
        return fn
    return decorator


def _plugin_is_active(plugin_id):
    """Return whether the plugin registry has *plugin_id* enabled right now.

    Enabling and disabling only run in the Gunicorn worker that handled the
    admin's request, so the in-memory hook registry of every other worker
    can be out of date.  The registry row is the one state all workers
    share, so dispatch asks it instead.  Inside a request the answer comes
    from the map ``app.py`` reads once per request; elsewhere (background
    threads, the CLI) it costs one query.  When the registry cannot be
    read the plugin counts as inactive: a disabled plugin must not run
    because the database hiccuped.
    """
    try:
        from flask import g, has_app_context
        if has_app_context():
            state = getattr(g, "plugin_registry_enabled", None)
            if state is not None:
                return bool(state.get(plugin_id))
    except ImportError:
        pass
    try:
        import db
        return bool(db.is_plugin_enabled(plugin_id))
    except Exception:
        return False


def emit_hook(hook_name, **kwargs):
    """Call every subscriber registered for *hook_name*.

    Subscribers that belong to a plugin (they carry ``_bw_plugin_id``) only
    run while that plugin is enabled in the registry.  Subscribers without
    an owner, such as core code or tests, always run.

    Exceptions in individual subscribers are caught and logged so that one
    broken plugin cannot take down the whole application.
    """
    with _hook_lock:
        subscribers = list(_hook_registry.get(hook_name, []))
    active = {}
    for fn in subscribers:
        owner = getattr(fn, "_bw_plugin_id", None)
        if owner is not None:
            if owner not in active:
                active[owner] = _plugin_is_active(owner)
            if not active[owner]:
                continue
        try:
            fn(**kwargs)
        except Exception:
            import traceback
            traceback.print_exc()


def _unregister_hooks(plugin_id):
    """Remove all hooks that were registered by *plugin_id*.

    Returns the removed subscribers as ``{hook_name: [fn, ...]}`` so the
    loader can put them back with :func:`_restore_hooks` when the plugin is
    enabled again without importing its code a second time.
    """
    removed = {}
    with _hook_lock:
        for name in list(_hook_registry):
            kept = []
            for fn in _hook_registry[name]:
                if getattr(fn, "_bw_plugin_id", None) == plugin_id:
                    removed.setdefault(name, []).append(fn)
                else:
                    kept.append(fn)
            _hook_registry[name] = kept
            if not _hook_registry[name]:
                del _hook_registry[name]
    return removed


def _restore_hooks(removed):
    """Re-register subscribers returned by :func:`_unregister_hooks`."""
    with _hook_lock:
        for name, fns in removed.items():
            registered = _hook_registry[name]
            for fn in fns:
                if not any(existing is fn for existing in registered):
                    registered.append(fn)


def _snapshot_hook_fns():
    """Return a set of ids of all currently registered hook functions."""
    with _hook_lock:
        return {id(fn) for fns in _hook_registry.values() for fn in fns}


def _stamp_new_hooks(plugin_id, snapshot, *, overwrite=False):
    """Stamp ``_bw_plugin_id`` on hook functions registered since *snapshot*.

    *snapshot* is a set of ``id()`` values returned by
    :func:`_snapshot_hook_fns` **before** the plugin module was imported.
    Any function present in the registry now but absent from *snapshot*
    that does not already carry ``_bw_plugin_id`` will be stamped with
    *plugin_id*.

    The plugin loader passes ``overwrite=True``: everything a plugin
    registered while it was being loaded belongs to that plugin, whatever
    id the code wrote on it.  Otherwise a plugin could tag its hooks with
    another plugin's id and keep them running after it is disabled.
    """
    with _hook_lock:
        for fns in _hook_registry.values():
            for fn in fns:
                if id(fn) in snapshot:
                    continue
                if overwrite or not hasattr(fn, "_bw_plugin_id"):
                    fn._bw_plugin_id = plugin_id


def _clear_all_hooks():
    """Remove all registered hooks (used in tests)."""
    with _hook_lock:
        _hook_registry.clear()


def get_registered_hooks():
    """Return a dict of hook_name → count of subscribers (for admin UI)."""
    with _hook_lock:
        return {name: len(fns) for name, fns in _hook_registry.items()}

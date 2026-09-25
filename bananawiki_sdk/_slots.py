"""Template slot system for plugins.

Template slots let plugins inject HTML into named locations in the core
templates (e.g. ``page.below_content``, ``sidebar.bottom``).

Usage::

    from bananawiki_sdk import template_slot

    @template_slot("page.below_content")
    def render_word_count(context):
        return "<p>Word count: 42</p>"
"""

import threading
from collections import defaultdict

from bananawiki_sdk._hooks import _plugin_is_active

# Global registry: slot_name → list of callables
_slot_registry = defaultdict(list)
_slot_lock = threading.Lock()


def template_slot(slot_name):
    """Decorator that registers a function to render into a named template slot.

    The decorated function receives a *context* dict and must return an HTML
    string (or empty string).
    """
    def decorator(fn):
        with _slot_lock:
            _slot_registry[slot_name].append(fn)
        if not hasattr(fn, "_bw_slots"):
            fn._bw_slots = []
        fn._bw_slots.append(slot_name)
        return fn
    return decorator


def render_slot(slot_name, context=None):
    """Render all functions registered for *slot_name* and return their
    concatenated HTML output.

    Automatically includes the CSP nonce in the context if available from
    the Flask request context, so plugins can safely include inline scripts/styles.

    Slot functions whose owning plugin is disabled in the current request
    context (e.g. via EasyWiki mode) are silently skipped so disabled plugins
    never inject UI even if their ``@template_slot`` handler bypasses the
    normal ``db.is_plugin_enabled`` guard.
    """
    if context is None:
        context = {}
    # Include CSP nonce in context if available from Flask g.  Also snapshot
    # the request-level enabled_plugins dict once so we can gate each slot
    # renderer without re-reading g on every iteration.
    enabled_plugins = None
    try:
        from flask import g
        if hasattr(g, "_csp_nonce"):
            context = {**context, "csp_nonce": g._csp_nonce}
        enabled_plugins = getattr(g, "enabled_plugins", None)
    except (ImportError, RuntimeError):
        # Not in Flask context or g not available (e.g. tests without app ctx)
        pass
    with _slot_lock:
        renderers = list(_slot_registry.get(slot_name, []))
    parts = []
    for fn in renderers:
        # Skip renderers whose plugin has been disabled at the request level.
        # This ensures EasyWiki mode (and any other per-request gating done in
        # app.py's _get_enabled_plugins) is honoured even when the underlying
        # DB row still shows the plugin as enabled.
        plugin_id = getattr(fn, "_bw_plugin_id", None)
        if plugin_id:
            if enabled_plugins is not None:
                if not enabled_plugins.get(plugin_id):
                    continue
            elif not _plugin_is_active(plugin_id):
                # No per-request map (a render outside the normal request
                # cycle): ask the registry, as emit_hook does.
                continue
        try:
            result = fn(context)
            if result:
                parts.append(str(result))
        except Exception:
            import traceback
            traceback.print_exc()
    return "\n".join(parts)


def _unregister_slots(plugin_id):
    """Remove all template slots registered by *plugin_id*.

    Returns the removed renderers as ``{slot_name: [fn, ...]}`` so they can
    be put back with :func:`_restore_slots`.
    """
    removed = {}
    with _slot_lock:
        for name in list(_slot_registry):
            kept = []
            for fn in _slot_registry[name]:
                if getattr(fn, "_bw_plugin_id", None) == plugin_id:
                    removed.setdefault(name, []).append(fn)
                else:
                    kept.append(fn)
            _slot_registry[name] = kept
            if not _slot_registry[name]:
                del _slot_registry[name]
    return removed


def _restore_slots(removed):
    """Re-register renderers returned by :func:`_unregister_slots`."""
    with _slot_lock:
        for name, fns in removed.items():
            registered = _slot_registry[name]
            for fn in fns:
                if not any(existing is fn for existing in registered):
                    registered.append(fn)


def _snapshot_slot_fns():
    """Return a set of ids of all currently registered slot functions."""
    with _slot_lock:
        return {id(fn) for fns in _slot_registry.values() for fn in fns}


def _stamp_new_slots(plugin_id, snapshot, *, overwrite=False):
    """Stamp ``_bw_plugin_id`` on slot functions registered since *snapshot*.

    *snapshot* is a set of ``id()`` values returned by
    :func:`_snapshot_slot_fns` **before** the plugin module was imported.
    Any function present in the registry now but absent from *snapshot*
    that does not already carry ``_bw_plugin_id`` will be stamped with
    *plugin_id*.  With ``overwrite=True`` (what the plugin loader uses) an
    existing value is replaced too, for the reason given in
    :func:`bananawiki_sdk._hooks._stamp_new_hooks`.
    """
    with _slot_lock:
        for fns in _slot_registry.values():
            for fn in fns:
                if id(fn) in snapshot:
                    continue
                if overwrite or not hasattr(fn, "_bw_plugin_id"):
                    fn._bw_plugin_id = plugin_id


def _clear_all_slots():
    """Remove all registered slots (used in tests)."""
    with _slot_lock:
        _slot_registry.clear()


def get_registered_slots():
    """Return a dict of slot_name → count of renderers (for admin UI)."""
    with _slot_lock:
        return {name: len(fns) for name, fns in _slot_registry.items()}

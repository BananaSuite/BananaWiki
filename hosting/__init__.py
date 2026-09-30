"""1.4 import path for the hosting portal; the implementation lives in :mod:`bananawiki.hosting`.

Service units written by the 1.4 updater run ``gunicorn -c hosting/gunicorn.conf.py
hosting.wsgi:app`` and ``python -m hosting.maintenance --interval 300`` from the
release root, so this package keeps those names working.
"""

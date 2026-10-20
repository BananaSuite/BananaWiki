"""``python -m hosting.admin_cli``, the same as ``bananawiki hosting-admin``; see :mod:`bananawiki.hosting.admin`."""

from bananawiki.hosting.admin import main

if __name__ == "__main__":
    raise SystemExit(main())

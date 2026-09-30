"""``python -m hosting.maintenance --interval 300``: the 1.4 maintenance service command."""

from bananawiki.hosting.maintenance import main

if __name__ == "__main__":
    raise SystemExit(main())

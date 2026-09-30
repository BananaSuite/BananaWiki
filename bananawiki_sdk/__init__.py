"""BananaWiki 1.4 plugin SDK, kept for plugins written against it.

This package only re-exports :mod:`bananawiki.sdk.compat`. The wiki itself
does not need it on the import path (the plugin loader aliases the adapter
under this name); it exists so a 1.4 plugin can still be imported by its
author's tests and editor.
"""

from bananawiki.sdk.compat import *  # noqa: F403
from bananawiki.sdk.compat import __all__, __version__  # noqa: F401

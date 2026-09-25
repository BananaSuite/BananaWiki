"""Plugin SDK exception hierarchy."""


class PluginError(Exception):
    """Base exception for all plugin errors."""


class PluginConfigError(PluginError):
    """Raised when a plugin manifest is invalid or incomplete."""


class PluginAPIVersionError(PluginError):
    """Raised when a plugin requires an incompatible API version."""

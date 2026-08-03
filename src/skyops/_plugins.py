"""``skyops host``, ``skyops adapter``, ``skyops plugin`` — plugin debug commands.

Re-exports the existing plugin CLI sub-apps from ``agp._plugin_cli``.
"""

from agp._plugin_cli import adapter_app, host_app, plugin_app

__all__ = ["adapter_app", "host_app", "plugin_app"]

"""``skyops host``, ``skyops adapter``, ``skyops plugin`` — plugin debug commands.

Re-exports the existing plugin CLI sub-apps from ``agp._plugin_cli``.
"""

from agp._plugin_cli import host_app, adapter_app, plugin_app

__all__ = ["host_app", "adapter_app", "plugin_app"]

"""Plugin loader — auto-discovers and loads plugins from plugins/ directory."""

import importlib
import inspect
import logging
import os
import sys
from pathlib import Path

log = logging.getLogger(__name__)


def discover_plugins() -> list:
    """Scan plugins/ directory and load all PluginBase subclasses.

    Returns list of instantiated plugin objects.
    """
    plugins_dir = Path(__file__).resolve().parent.parent / "plugins"
    if not plugins_dir.is_dir():
        return []

    # Add plugins dir to path so imports work
    plugins_str = str(plugins_dir)
    if plugins_str not in sys.path:
        sys.path.insert(0, str(plugins_dir.parent))

    from plugins.plugin_base import PluginBase

    loaded = []
    for f in sorted(plugins_dir.glob("*.py")):
        if f.name.startswith("_") or f.name == "plugin_base.py":
            continue
        module_name = f"plugins.{f.stem}"
        try:
            mod = importlib.import_module(module_name)
            for name, obj in inspect.getmembers(mod, inspect.isclass):
                if issubclass(obj, PluginBase) and obj is not PluginBase:
                    plugin = obj()
                    loaded.append(plugin)
                    log.info("Plugin loaded: %s v%s by %s",
                             plugin.NAME, plugin.VERSION, plugin.AUTHOR)
        except Exception as e:
            log.warning("Failed to load plugin %s: %s", f.name, e)

    return loaded

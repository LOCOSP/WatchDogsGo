"""Base class for ESP32 Watch Dogs plugins."""

from dataclasses import dataclass, field


@dataclass
class PluginMenuItem:
    """A menu item exposed by a plugin."""
    key: str          # hotkey (single char)
    label: str        # display name
    action: str       # method name on plugin class


class PluginBase:
    """Base class all plugins must inherit from.

    Plugin discovery: any .py file in plugins/ that contains a class
    inheriting from PluginBase will be loaded. The class must define:

      NAME:       str   — plugin display name
      VERSION:    str   — version string
      AUTHOR:     str   — author name

    Optional overrides:
      menu_items()  — return list of PluginMenuItem for the PLUGINS tab
      on_load(app)  — called when plugin is loaded (app = WatchDogsApp)
      on_unload()   — called on game exit
      on_update()   — called every frame (30 FPS) — keep fast!
      draw(x, y, w, h)  — draw plugin overlay (if has_overlay=True)
    """

    NAME: str = "Unnamed Plugin"
    VERSION: str = "0.1"
    AUTHOR: str = "Unknown"

    def __init__(self):
        self.app = None       # set by loader
        self.enabled = True
        self.has_overlay = False  # set True if plugin draws a screen

    def menu_items(self) -> list[PluginMenuItem]:
        """Return menu items for PLUGINS tab."""
        return []

    def on_load(self, app) -> None:
        """Called when plugin is loaded. app = WatchDogsApp instance."""
        self.app = app

    def on_unload(self) -> None:
        """Called on game exit."""
        pass

    def on_update(self) -> None:
        """Called every frame. Keep this fast (<1ms)."""
        pass

    def draw(self, x: int, y: int, w: int, h: int) -> None:
        """Draw plugin overlay. Only called if has_overlay=True and active."""
        pass

    def msg(self, text: str, color: int = 7) -> None:
        """Show HUD message (convenience wrapper)."""
        if self.app:
            self.app.msg(text, color)

    def term(self, text: str) -> None:
        """Add line to terminal (convenience wrapper)."""
        if self.app:
            self.app._term_add(text, raw=True)

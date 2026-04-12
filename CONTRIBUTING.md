# Contributing to Watch Dogs Go

Thanks for wanting to help. This is a one-person hobby project that grew
into something bigger, so any extra hands are appreciated.

## TL;DR

- **Bug reports** → open an [issue](https://github.com/LOCOSP/esp32-watch-dogs/issues),
  attach `~/.watchdogs/last_run.log` and `~/.watchdogs/launcher.log`.
- **Feature ideas** → open an issue with the `enhancement` label first,
  let's talk before you write code.
- **Pull requests** → welcome, please target `master`. Small focused PRs
  merge faster than big ones.
- **Be nice.** This is meant to be fun.

---

## Reporting bugs

Before opening an issue, please check:

1. The bug isn't already in the **WIP list** in [CHANGELOG.md](CHANGELOG.md)
   (Map Download, BLE HID, HID Type, BlueDucky, RACE Attack are intentionally
   disabled).
2. You're running the latest `master` (`git pull && bash setup.sh`).
3. Your hardware is in the supported list (`ESP32-C5` + `Linux` minimum).

**A good bug report contains:**

- What you tried to do (one sentence)
- What happened instead (one sentence)
- The last 30 lines of `~/.watchdogs/last_run.log`
- The last 20 lines of `~/.watchdogs/launcher.log` if the game crashed
  before opening
- Your hardware: `uname -a`, `lsusb`, `python3 --version`
- A screenshot if it's a UI bug

The log files contain GPS coordinates, BSSIDs, and sometimes captured
credentials. **Review them before pasting into a public issue** — or send
them privately if you'd rather.

---

## Setting up a dev environment

```bash
git clone https://github.com/LOCOSP/esp32-watch-dogs.git
cd esp32-watch-dogs
bash setup.sh        # creates .venv, installs deps
sudo ./run.sh        # launches the game (needs sudo for GPIO/serial)
```

You don't need an ESP32 to test most UI changes — the game starts in a
"no ESP32" mode and the menus, plugins, map renderer, terminal and
overlays all work without hardware.

For LoRa and AIO v2 features you need a ClockworkPi uConsole with the
AIO v2 module. For wardriving with attacks you need an ESP32-C5 running
[projectZero](https://github.com/LOCOSP/projectZero).

---

## Code style

- **Python 3.10+**, no type-checking required but typed signatures
  welcome.
- Match the surrounding style of the file you're editing.
- Keep functions small. `app.py` is already 5000+ lines — please don't
  add to that monster without strong justification, prefer a new module
  in `watchdogs/`.
- **No emoji in code or commit messages** unless the surrounding context
  uses them.
- **No `Co-Authored-By: Claude` lines** in commits — if you used AI to
  help, that's fine, but the commit author is you.

## Commit messages

Format: `<type>: <one-line summary>`

Where `<type>` is one of `feat`, `fix`, `docs`, `refactor`, `chore`,
`perf`, `test`. Examples from the existing history:

```
feat: badge sync — push game badges to server, pull server badges to game
fix: every fresh user broadcasted as literal 'WatchDogs' on LoRa mesh
docs: add one-liner install command to README
chore: bump pyxel to 2.5.0
```

Body (after a blank line) should explain *why*, not *what* — the diff
shows what.

---

## Plugin development

Plugins live in `plugins/` and are auto-discovered. The simplest plugin:

```python
# plugins/my_plugin.py
from plugins.plugin_base import PluginBase, PluginMenuItem

class MyPlugin(PluginBase):
    NAME = "My Plugin"
    VERSION = "1.0"
    AUTHOR = "yourname"

    def menu_items(self):
        return [PluginMenuItem("m", "My Plugin", "open_overlay")]

    def open_overlay(self):
        self.msg("Hello from my plugin!", 11)
```

Drop it in `plugins/`, restart the game, and it appears under the
**PLUGINS** menu tab. See `plugins/wardrive_upload.py` for a full example
with overlay UI, network requests, and persistent state.

See `plugins/wardrive_upload.py` for a real-world example with overlay
UI, threaded HTTP requests, persistent state and background workers.
The plugin API is small and stable — read `plugins/plugin_base.py` for
the full base class.

---

## What needs help right now

- Fixing one of the WIP features (BlueDucky / RACE / Map Download)
- Splitting `app.py` into smaller modules
- Adding tests (any tests at all — there are currently zero)
- Better documentation for the plugin API
- Linux-distro support beyond Debian/Ubuntu (Fedora, Arch, Alpine)
- macOS support for the game frontend (Pyxel works there, but lots of
  code assumes Linux serial paths and `lgpio`)
- Translation of in-game strings (currently EN-only)

---

## License

By contributing, you agree that your contributions will be licensed under
the same license as the project (see [LICENSE](LICENSE)).

---

## Where to find me

- **GitHub Issues** — best for bugs and feature discussions
- **wdgwars.pl** — if you have an account, find me as `locosp`
- **MeshCore on 869 MHz** — node `WDG_locosp` if you're in radio range

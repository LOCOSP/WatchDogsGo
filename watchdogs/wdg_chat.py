"""WDGWars #general (Reticulum/LXMF) in the Messenger: runs wdg_lxmf_chat.py.

The helper needs RNS and LXMF, which live in the desktop user's Nomad Network
pipx venv, and it must run as that user to share Nomad Network's identity and
store. The game runs as root, so the helper is started with `sudo -u <owner of
this checkout>`. SUDO_USER cannot be used for that: run.sh calls sudo a second
time, so inside the game it reads "root".
"""

import json
import os
import pwd
import subprocess
import threading
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
HELPER = _ROOT / "wdg_lxmf_chat.py"
LOG = Path("/tmp/wdg_lxmf_chat.log")


class WdgChat:
    """Owns the helper process. Events arrive on a reader thread and are handed
    to `on_event`, which must be thread-safe (the app puts them on a queue)."""

    def __init__(self, on_event):
        self._on_event = on_event
        self._proc = None
        self._lock = threading.Lock()
        self.status = "off"

    @property
    def running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def start(self) -> None:
        with self._lock:
            if self.running:
                return
            user = pwd.getpwuid(_ROOT.stat().st_uid)
            py = Path(user.pw_dir) / ".local/share/pipx/venvs/nomadnet/bin/python"
            if not py.exists():
                self._event({"t": "status", "stopped": True,
                             "text": "Nomad Network (RNS + LXMF) is not installed"})
                return
            cmd = [str(py), str(HELPER)]
            if os.geteuid() == 0 and user.pw_uid != 0:
                cmd = ["sudo", "-u", user.pw_name, "-H", "--"] + cmd
            self._proc = subprocess.Popen(
                cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=open(LOG, "ab"), text=True, bufsize=1, cwd=str(_ROOT))
            self.status = "starting"
            threading.Thread(target=self._read, args=(self._proc,),
                             daemon=True).start()

    def _read(self, proc) -> None:
        for line in proc.stdout:
            try:
                evt = json.loads(line)
            except ValueError:
                continue
            self._event(evt)
        code = proc.wait()
        self._event({"t": "status", "stopped": True,
                     "text": f"WDGWars link stopped (exit {code})"})

    def _event(self, evt: dict) -> None:
        if evt.get("t") == "status":
            self.status = evt.get("text", "")
        try:
            self._on_event(evt)
        except Exception:
            pass

    def send(self, text: str) -> bool:
        if not self.running:
            return False
        try:
            self._proc.stdin.write(json.dumps({"t": "send", "text": text}) + "\n")
            self._proc.stdin.flush()
            return True
        except (BrokenPipeError, OSError):
            return False

    def stop(self) -> None:
        proc = self._proc
        if proc is None or proc.poll() is not None:
            return
        try:
            proc.stdin.close()          # EOF tells the helper to save and exit
        except OSError:
            pass
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.terminate()

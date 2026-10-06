#!/usr/bin/env python3
"""WDGWars #general over Reticulum (LXMF), for Watch Dogs Go's Messenger.

Started by watchdogs/wdg_chat.py as the desktop user, with the Nomad Network
interpreter: RNS and LXMF live in that pipx venv, not in the game's venv, and the
game itself runs as root. It attaches to the running rnsd shared instance, which
holds the TCP link to rns.wdgwars.pl.

It uses Nomad Network's identity AND its LXMF store, so posts appear under the
WDGWars account that identity is linked to, and the two programs never hand out
different ratchet keys (a second store for the same identity is the suspected
cause of "Decryption with ratchets failed"). One LXMF router per identity, so it
refuses to start while Nomad Network is running.

Protocol, one JSON object per line:
  out: {"t": "status", "text": ...}
       {"t": "msg", "src": "#general" | <8 hex>, "text": ..., "ts": ...}
       {"t": "sent", "ok": true | false, "text": ..., "why": ...}
  in:  {"t": "send", "text": ...}      EOF on stdin = shut down
"""
import json
import os
import sys
import threading
import time

# RNS and LXMF print to stdout, which would corrupt the protocol. Keep a private
# copy of the real stdout for JSON and point fd 1 at stderr for everything else.
_proto = os.fdopen(os.dup(1), "w", buffering=1)
os.dup2(2, 1)
sys.stdout = sys.stderr
_out_lock = threading.Lock()


def emit(**kw):
    with _out_lock:
        _proto.write(json.dumps(kw) + "\n")
        _proto.flush()


NOMAD_DIR = os.path.expanduser("~/.nomadnetwork")
STORAGE = os.path.join(NOMAD_DIR, "storage")          # Nomad Network's LXMRouter storagepath
IDENTITY_PATH = os.path.join(STORAGE, "identity")
GENERAL = bytes.fromhex("b29de9f746bd9d7a421ee84f3e0a6941")    # wdgwars.pl/mesh "POST TO #GENERAL"
PROP_NODE = bytes.fromhex("90ab9d448f17f3a121dc0f1230af39be")  # wdgwars.pl/mesh propagation node
ANNOUNCE_EVERY = 30 * 60
SYNC_EVERY = 10 * 60
ECHO_WINDOW = 5 * 60
# The propagation node keeps everything sent while this identity was offline: the
# first sync on 2026-10-06 returned 592 messages, the newest 17 days old. Show only
# recent ones and report the rest as a count, or the Messenger is buried.
MAX_AGE = 12 * 3600


def nomadnet_running():
    """True if a Nomad Network process (daemon or TUI) is up. Reads /proc, so it
    cannot match its own command line the way `pgrep -f` would."""
    me = os.getpid()
    for pid in os.listdir("/proc"):
        if not pid.isdigit() or int(pid) == me:
            continue
        try:
            argv = open(f"/proc/{pid}/cmdline", "rb").read().split(b"\0")
        except OSError:
            continue
        if any(a.endswith(b"/bin/nomadnet") or a == b"nomadnet" for a in argv):
            return True
    return False


def main():
    if nomadnet_running():
        emit(t="status", text="Nomad Network is running: close it first (same identity)")
        return 2

    import RNS
    import LXMF
    from RNS.vendor import umsgpack

    emit(t="status", text="connecting to Reticulum")
    rns = RNS.Reticulum(loglevel=RNS.LOG_WARNING)
    if not getattr(rns, "is_connected_to_shared_instance", False):
        emit(t="status", text="rnsd is not running: no Reticulum connection")
        return 3

    identity = RNS.Identity.from_file(IDENTITY_PATH)
    display_name = "Anonymous Peer"
    try:
        with open(os.path.join(STORAGE, "peersettings"), "rb") as f:
            display_name = umsgpack.unpackb(f.read()).get("display_name") or display_name
    except Exception:
        pass

    router = LXMF.LXMRouter(identity=identity, storagepath=STORAGE)
    source = router.register_delivery_identity(identity, display_name=display_name)
    router.set_outbound_propagation_node(PROP_NODE)
    recent_sends = {}          # text -> time sent, to drop the bridge echoing it back
    skipped = [0]

    def on_message(m):
        ts = m.timestamp or time.time()
        if time.time() - ts > MAX_AGE:
            skipped[0] += 1
            return
        text = m.content_as_string() or ""
        if m.source_hash == GENERAL:
            now = time.time()
            for sent_text, t0 in list(recent_sends.items()):
                if now - t0 > ECHO_WINDOW:
                    del recent_sends[sent_text]
                elif text.endswith(": " + sent_text):
                    return     # our own post, relayed back by the bridge
            src = "#general"
        else:
            src = RNS.hexrep(m.source_hash, delimit=False)[:8]
        emit(t="msg", src=src, text=text, ts=ts)

    router.register_delivery_callback(on_message)

    def send(text):
        ident = RNS.Identity.recall(GENERAL)
        if ident is None:
            RNS.Transport.request_path(GENERAL)
            for _ in range(30):
                time.sleep(1)
                ident = RNS.Identity.recall(GENERAL)
                if ident:
                    break
        if ident is None:
            emit(t="sent", ok=False, text=text, why="no route to #general yet")
            return
        dest = RNS.Destination(ident, RNS.Destination.OUT, RNS.Destination.SINGLE,
                               "lxmf", "delivery")
        msg = LXMF.LXMessage(dest, source, text, "",
                             desired_method=LXMF.LXMessage.DIRECT)
        msg.try_propagation_on_fail = True
        msg.register_delivery_callback(lambda _m: emit(t="sent", ok=True, text=text))
        msg.register_failed_callback(
            lambda _m: emit(t="sent", ok=False, text=text, why="delivery failed"))
        recent_sends[text] = time.time()
        router.handle_outbound(msg)

    def report_route():
        for _ in range(120):
            if RNS.Transport.has_path(GENERAL):
                emit(t="status", text=f"online, #general {RNS.Transport.hops_to(GENERAL)} hop(s)")
                return
            time.sleep(1)
        emit(t="status", text="no route to #general yet (server side?), still listening")

    stop = threading.Event()

    def read_commands():
        for line in sys.stdin:
            try:
                cmd = json.loads(line)
            except ValueError:
                continue
            if cmd.get("t") == "send" and cmd.get("text"):
                threading.Thread(target=send, args=(str(cmd["text"])[:500],),
                                 daemon=True).start()
        stop.set()             # EOF: the game closed the pipe or exited

    router.announce(source.hash)
    RNS.Transport.request_path(GENERAL)
    router.request_messages_from_propagation_node(identity)
    threading.Thread(target=report_route, daemon=True).start()
    threading.Thread(target=read_commands, daemon=True).start()

    last_announce = last_sync = time.time()
    reported = 0
    while not stop.wait(5):
        now = time.time()
        if skipped[0] != reported:
            reported = skipped[0]
            emit(t="status", text=f"skipped {reported} stored messages older than 12 h")
        if now - last_announce > ANNOUNCE_EVERY:
            router.announce(source.hash)
            last_announce = now
        if now - last_sync > SYNC_EVERY:
            router.request_messages_from_propagation_node(identity)
            last_sync = now
    router.exit_handler()
    return 0


if __name__ == "__main__":
    sys.exit(main())

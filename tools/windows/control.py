"""Lump Control: a small Windows tray app to test Lump and change his settings.

A tray icon (right-click: open, play an animation, sound on/off, restart the bridge) and a window with
four tabs: Status (what he's showing and why), Play (animations, mood previews, tap sensitivity), Sounds
and Settings (Wi-Fi host, calendar, Slack). It only talks to the bridge's own HTTP API on 127.0.0.1:47823
and edits ~/.desk-buddy/config.json and sounds.json, which the bridge re-reads by itself, so nothing here
needs the bridge restarted. With the bridge stopped it says so and offers to start it.

tools/windows/install.ps1 installs it as ~/.desk-buddy/control.py and starts it at logon (pythonw, no
console). Run by hand: ~/.desk-buddy/venv/Scripts/pythonw tools/windows/control.py
Needs pystray and Pillow for the tray icon (installed by install.ps1); without them it's just the window.

Remote Lump (Lump plugged into another computer, see tools/lump_relay.py): the Status tab shows when he's on
the remote link and whether his board is plugged in over there; Settings shows whether the remote link is set
up, copies its token, or makes a new one (which disconnects the relay until it gets the new token). install.ps1
uses the same token helpers from the command line (--ensure-relay-token, --remove-relay-token, --relay-command),
none of which ever prints the token.
"""
import json, os, secrets, socket, subprocess, sys, threading, time, urllib.error, urllib.request

HOME = os.environ.get("DESKBUDDY_HOME") or os.path.expanduser("~/.desk-buddy")
CONFIG_FILE = os.path.join(HOME, "config.json")
SOUNDS_FILE = os.path.join(HOME, "sounds.json")
BRIDGE_URL = os.environ.get("DESKBUDDY_URL") or "http://127.0.0.1:47823"
SHOW_PORT = 47825                      # a second launch asks the running one to show its window
# Moods the board knows (the bridge sends these with "s <mood>").
MOODS = ("idle", "awake", "working", "alert", "waiting", "done", "longhaul", "night", "letter", "meeting")
REFRESH_MS = 2000


# ---------- talking to the bridge ----------
class Bridge:
    """The bridge's HTTP API. Every call returns None when the bridge isn't running."""

    def __init__(self, url=BRIDGE_URL, timeout=2):
        self.url, self.timeout = url, timeout

    def _call(self, path, body=None):
        req = urllib.request.Request(self.url + path, data=None if body is None else body.encode("utf-8"),
                                     method="GET" if body is None else "POST")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                return r.status, r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf-8", "replace")
        except (OSError, ValueError):
            return None

    def status(self):
        r = self._call("/status")
        return json.loads(r[1]) if r and r[0] == 200 else None

    def animations(self):
        r = self._call("/play")
        return json.loads(r[1]) if r and r[0] == 200 else []

    def play(self, name):
        r = self._call("/play", name)
        return None if r is None else r[0] == 200

    def cmd(self, text):
        r = self._call("/cmd", text)
        return None if r is None else r[0] == 200

    def resync(self):
        r = self._call("/resync", "")
        return None if r is None else r[0] == 200

    def sound(self, text):
        r = self._call("/sound", text)
        return None if r is None else r[1]

    def calendar_now(self):
        return self._call("/calendar", "") is not None


# ---------- settings files ----------
def read_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def update_json(path, changes, remove=()):
    """Merge changes into a JSON file (keeping every other key) and write it atomically.
    Keys in `remove`, or with a value of None or "", are deleted."""
    data = read_json(path)
    for k, v in changes.items():
        if v is None or v == "":
            data.pop(k, None)
        else:
            data[k] = v
    for k in remove:
        data.pop(k, None)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")
    os.replace(tmp, path)
    return data


def set_moment(path, moment, on):
    """Switch one of sounds.json's moments on or off, keeping its picks."""
    moments = dict(read_json(path).get("moments") or {})
    m = dict(moments.get(moment) or {})
    m["on"] = bool(on)
    moments[moment] = m
    return update_json(path, {"moments": moments})


def parse_vips(text):
    return [n.strip() for n in text.replace("\n", ",").split(",") if n.strip()]


def describe(status):
    """One line for the tray tooltip / status header."""
    if not status:
        return "Bridge not running"
    link = {"usb": "USB", "wifi": "Wi-Fi", "relay": "the remote link"}.get(status.get("link"), status.get("link") or "")
    board = f"board on {link}" if status.get("device") else "board not connected"
    return f"{status.get('mood', '?')} · {board}"


def describe_relay(status):
    """The remote link in a few words, or None when it isn't set up (no relay_token, so no listener)."""
    r = (status or {}).get("relay")
    if not r:
        return None
    if not r.get("paired"):
        return "Remote Lump: waiting for the relay to connect"
    board = {True: "plugged in there", False: "not plugged in there"}.get(r.get("board"), "not reported yet")
    return "Remote Lump: relay connected, board %s%s" % (board, " (in use)" if r.get("active") else "")


# ---------- the remote link's token (config.json "relay_token") ----------
def new_relay_token(path=CONFIG_FILE, replace=False):
    """Make a relay token (128 random bits as hex) unless there is one already (or replace is set).
    Returns whether a new one was written. The bridge notices within a few seconds."""
    if read_json(path).get("relay_token") and not replace:
        return False
    update_json(path, {"relay_token": secrets.token_hex(16)})
    return True


def remove_relay_token(path=CONFIG_FILE):
    """Turn the remote link off: the bridge stops listening and drops the relay within a few seconds."""
    had = bool(read_json(path).get("relay_token"))
    if had:
        update_json(path, {}, remove=("relay_token",))
    return had


def relay_setup_command(bridge_host):
    """What to run on the computer Lump is plugged into. It never contains the token: the installer asks for it
    there (hidden), so it doesn't end up in a command line, a shell history or a screenshot."""
    return ("powershell -ExecutionPolicy Bypass -File tools\\windows\\install.ps1 -Relay -BridgeHost %s" % bridge_host)


def cli(argv):
    """The command-line helpers install.ps1 uses. Returns an exit code, or None when argv asks for none of them."""
    if "--ensure-relay-token" in argv:
        print("made a new relay token" if new_relay_token() else "relay token already set")
        return 0
    if "--remove-relay-token" in argv:
        print("relay token removed" if remove_relay_token() else "no relay token was set")
        return 0
    if "--relay-command" in argv:
        i = argv.index("--relay-command")
        host = argv[i + 1] if i + 1 < len(argv) else ""
        if not host or not all(c.isalnum() or c in ".-" for c in host):
            print("--relay-command needs this PC's Tailscale name"); return 2
        print(relay_setup_command(host))
        return 0
    return None


def bridge_ctl(action):
    """Start or stop the installed bridge (tools/windows/bridge.ps1, copied next to this file)."""
    script = os.path.join(HOME, "bridge.ps1")
    if not os.path.exists(script):
        script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bridge.ps1")
    subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", script, "-" + action],
                   capture_output=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


# ---------- one copy at a time ----------
def already_running():
    """If Lump Control is already running, ask it to show its window and say so."""
    try:
        with socket.create_connection(("127.0.0.1", SHOW_PORT), timeout=1) as s:
            s.sendall(b"show\n")
        return True
    except OSError:
        return False


def listen_for_show(on_show):
    srv = socket.socket()
    srv.bind(("127.0.0.1", SHOW_PORT))
    srv.listen(2)

    def loop():
        while True:
            conn, _ = srv.accept()
            with conn:
                if conn.recv(16).startswith(b"show"):
                    on_show()
    threading.Thread(target=loop, daemon=True).start()
    return srv


# ---------- the window ----------
class ControlWindow:
    def __init__(self, root, bridge):
        import tkinter as tk
        from tkinter import ttk
        self.tk, self.ttk, self.root, self.bridge = tk, ttk, root, bridge
        self.status = None
        root.title("Lump Control")
        root.geometry("560x600")
        root.minsize(480, 480)
        root.protocol("WM_DELETE_WINDOW", self.hide)
        icon = os.path.join(HOME, "lump_icon.png")
        if os.path.exists(icon):
            try:
                big = tk.PhotoImage(file=icon)
                self._icon = big.subsample(max(1, big.width() // 64))
                root.iconphoto(True, self._icon)
            except tk.TclError:
                pass
        style = ttk.Style()
        style.configure("Big.TLabel", font=("Segoe UI", 16, "bold"))
        style.configure("Note.TLabel", foreground="#666")

        self.header = ttk.Label(root, text="…", style="Big.TLabel", padding=(12, 10, 12, 0))
        self.header.pack(anchor="w")
        self.notice = ttk.Label(root, text="", style="Note.TLabel", padding=(12, 0, 12, 6))
        self.notice.pack(anchor="w")
        nb = ttk.Notebook(root)
        nb.pack(fill="both", expand=True, padx=8, pady=(0, 8))
        nb.enable_traversal()                  # Ctrl+Tab / Ctrl+Shift+Tab switch tabs
        self.build_status(nb)
        self.build_play(nb)
        self.build_sounds(nb)
        self.build_settings(nb)
        self.refresh()

    # --- helpers
    def say(self, text):
        self.notice.config(text=text)
        self.root.after(6000, lambda: self.notice.cget("text") == text and self.notice.config(text=""))

    def run(self, what, fn, *a):
        """Call the bridge off the UI thread; report the outcome."""
        def work():
            ok = fn(*a)
            msg = (f"{what}: done" if ok else f"{what}: the board isn't connected" if ok is False else
                   f"{what}: the bridge isn't running")
            self.root.after(0, self.say, msg)
        threading.Thread(target=work, daemon=True).start()

    def frame(self, nb, title):
        f = self.ttk.Frame(nb, padding=10)
        nb.add(f, text=title)
        return f

    # --- Status
    def build_status(self, nb):
        ttk, tk = self.ttk, self.tk
        f = self.frame(nb, "Status")
        top = ttk.Frame(f); top.pack(fill="x")
        self.swatch = tk.Canvas(top, width=36, height=36, highlightthickness=0)
        self.swatch.pack(side="left", padx=(0, 10))
        self.swatch_rect = self.swatch.create_oval(2, 2, 34, 34, fill="#FF7A45", outline="")
        self.facts = ttk.Label(top, text="", justify="left")
        self.facts.pack(side="left", anchor="w")
        ttk.Label(f, text="Sessions", padding=(0, 12, 0, 4)).pack(anchor="w")
        cols = ("session", "state", "age", "folder")
        self.sessions = ttk.Treeview(f, columns=cols, show="headings", height=8)
        for c, w in zip(cols, (90, 80, 60, 260)):
            self.sessions.heading(c, text=c.title()); self.sessions.column(c, width=w, anchor="w")
        self.sessions.pack(fill="both", expand=True)
        row = ttk.Frame(f, padding=(0, 10, 0, 0)); row.pack(fill="x")
        self.start_btn = ttk.Button(row, text="Start bridge", command=lambda: self.ctl("Start"))
        ttk.Button(row, text="Restart bridge", command=self.restart).pack(side="left")
        ttk.Button(row, text="Open log", command=lambda: os.startfile(os.path.join(HOME, "bridge.log"))).pack(side="left", padx=6)
        ttk.Button(row, text="Open folder", command=lambda: os.startfile(HOME)).pack(side="left")

    def ctl(self, action):
        self.say(f"{action.lower()}ing the bridge…" if action != "Stop" else "stopping the bridge…")
        threading.Thread(target=lambda: (bridge_ctl(action), self.root.after(0, self.refresh)), daemon=True).start()

    def restart(self):
        self.say("restarting the bridge…")
        threading.Thread(target=lambda: (bridge_ctl("Stop"), bridge_ctl("Start"), self.root.after(0, self.refresh)), daemon=True).start()

    def show_status(self, st):
        self.status = st
        self.header.config(text="Lump: " + describe(st))
        if not st:
            self.facts.config(text="The bridge isn't running, so Lump can't hear from Claude.")
            self.swatch.itemconfig(self.swatch_rect, fill="#BBBBBB")
            self.start_btn.pack(side="left", padx=(0, 6), before=self.start_btn.master.winfo_children()[1])
            self.sessions.delete(*self.sessions.get_children())
            return
        self.start_btn.pack_forget()
        colour = "#" + (st.get("colour") or "FF7A45")
        self.swatch.itemconfig(self.swatch_rect, fill=colour)
        usage = st.get("usage") or {}
        u = lambda k: f"{usage[k]['pct']:.0f}%" if k in usage and usage[k].get("pct") is not None else "–"
        c = st.get("counts") or {}
        meeting = st.get("next_meeting")
        lines = [f"Colour #{st.get('colour')}   usage: 5-hour {u('five_hour')}, week {u('seven_day')}",
                 f"Working {c.get('working', 0)} · waiting on you {c.get('alert', 0)} · finished, unseen {c.get('unseen', 0)}",
                 f"Sound {'on' if st.get('sound') else 'off'} · Claude app in front: {'yes' if (st.get('focus') or {}).get('claude_in_front') else 'no'}"]
        if meeting:
            lines.append("Next meeting: %s at %s" % (meeting.get("title") or "untitled", time.strftime("%H:%M", time.localtime(meeting["start"]))))
        remote = describe_relay(st)
        if remote:
            lines.append(remote)
        self.facts.config(text="\n".join(lines))
        if hasattr(self, "relay_state"):
            self.show_relay_state(st)
        self.sessions.delete(*self.sessions.get_children())
        for sid, s in sorted((st.get("sessions") or {}).items(), key=lambda kv: kv[1].get("age_s", 0)):
            age = s.get("age_s", 0)
            self.sessions.insert("", "end", values=(sid, s.get("state"), f"{age // 60}m" if age >= 60 else f"{age}s",
                                                    os.path.basename((s.get("cwd") or "").rstrip("\\/")) or s.get("cwd") or ""))

    def refresh(self):
        threading.Thread(target=lambda: self.root.after(0, self.show_status, self.bridge.status()), daemon=True).start()

    def tick(self):
        if self.root.state() != "withdrawn":
            self.refresh()
        self.root.after(REFRESH_MS, self.tick)

    # --- Play
    def build_play(self, nb):
        ttk, tk = self.ttk, self.tk
        f = self.frame(nb, "Play")
        ttk.Label(f, text="Animations (play once, then he goes back to what he was doing)").pack(anchor="w")
        self.anim_box = ttk.Frame(f, padding=(0, 6)); self.anim_box.pack(fill="x")
        self.fill_animations()
        ttk.Label(f, text="Preview a mood (lasts until something changes, or Back to live)", padding=(0, 10, 0, 0)).pack(anchor="w")
        moods = ttk.Frame(f, padding=(0, 6)); moods.pack(fill="x")
        for i, m in enumerate(MOODS):
            ttk.Button(moods, text=m, width=10, command=lambda m=m: self.run(f"mood {m}", self.bridge.cmd, f"s {m}")).grid(row=i // 5, column=i % 5, padx=2, pady=2)
        row = ttk.Frame(f); row.pack(fill="x")
        ttk.Button(row, text="Showcase every mood", command=lambda: self.run("showcase", self.bridge.cmd, "s demo")).pack(side="left")
        ttk.Button(row, text="Back to live", command=lambda: self.run("back to live", self.bridge.resync)).pack(side="left", padx=6)
        ttk.Label(f, text="Tap sensitivity: how hard a tap on the glass has to be (lower = gentler)", padding=(0, 14, 0, 0)).pack(anchor="w")
        tap = ttk.Frame(f, padding=(0, 6)); tap.pack(fill="x")
        self.tap_var = tk.DoubleVar(value=float(read_json(CONFIG_FILE).get("tap_threshold") or 0.35))
        self.tap_label = ttk.Label(tap, width=6)
        ttk.Scale(tap, from_=0.1, to=1.5, variable=self.tap_var, length=260,
                  command=lambda v: self.tap_label.config(text=f"{float(v):.2f} g")).pack(side="left")
        self.tap_label.pack(side="left", padx=6)
        self.tap_label.config(text=f"{self.tap_var.get():.2f} g")
        ttk.Button(tap, text="Apply", command=self.apply_tap).pack(side="left")
        ttk.Button(tap, text="Default", command=lambda: (self.tap_var.set(0.35), self.tap_label.config(text="0.35 g"), self.apply_tap())).pack(side="left", padx=6)

    def fill_animations(self):
        def work():
            names = self.bridge.animations() or []
            self.root.after(0, self._show_animations, names)
        threading.Thread(target=work, daemon=True).start()

    def _show_animations(self, names):
        for w in self.anim_box.winfo_children():
            w.destroy()
        if not names:
            self.ttk.Label(self.anim_box, text="(start the bridge to see them)", style="Note.TLabel").grid(row=0, column=0)
            self.root.after(5000, self.fill_animations)
            return
        for i, n in enumerate(names):
            self.ttk.Button(self.anim_box, text=n.replace("_", " "), width=13,
                            command=lambda n=n: self.run(f"play {n}", self.bridge.play, n)).grid(row=i // 4, column=i % 4, padx=2, pady=2)

    def apply_tap(self):
        t = round(self.tap_var.get(), 2)
        update_json(CONFIG_FILE, {"tap_threshold": None if abs(t - 0.35) < 0.005 else t})
        self.run(f"tap sensitivity {t:.2f} g", self.bridge.cmd, f"T {t:.2f}")

    # --- Sounds
    def build_sounds(self, nb):
        ttk, tk = self.ttk, self.tk
        f = self.frame(nb, "Sounds")
        cfg = read_json(SOUNDS_FILE)
        self.snd_on = tk.BooleanVar(value=bool(cfg.get("enabled")))
        ttk.Checkbutton(f, text="Lump makes sounds", variable=self.snd_on, command=self.toggle_sound).pack(anchor="w")
        self.quiet = tk.BooleanVar(value=bool(cfg.get("quiet_at_night", True)))
        ttk.Checkbutton(f, text="Quiet at night unless you've been around", variable=self.quiet,
                        command=lambda: update_json(SOUNDS_FILE, {"quiet_at_night": self.quiet.get()})).pack(anchor="w", pady=(2, 0))
        vol = ttk.Frame(f, padding=(0, 8)); vol.pack(fill="x")
        ttk.Label(vol, text="Volume").pack(side="left")
        self.vol = tk.DoubleVar(value=float(cfg.get("volume", 1.0)))
        s = ttk.Scale(vol, from_=0.1, to=2.0, variable=self.vol, length=260)
        s.pack(side="left", padx=8)
        s.bind("<ButtonRelease-1>", lambda e: update_json(SOUNDS_FILE, {"volume": round(self.vol.get(), 2)}))
        ttk.Button(vol, text="Test", command=lambda: self.run("test sound", self.bridge.sound, "play hello")).pack(side="left")
        ttk.Label(f, text="Moments (tick to hear it; ▶ plays it now)", padding=(0, 6, 0, 4)).pack(anchor="w")
        box = ttk.Frame(f); box.pack(fill="both", expand=True)
        canvas = tk.Canvas(box, highlightthickness=0)
        sb = ttk.Scrollbar(box, orient="vertical", command=canvas.yview)
        inner = ttk.Frame(canvas)
        inner.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.create_window((0, 0), window=inner, anchor="nw")
        canvas.configure(yscrollcommand=sb.set)
        canvas.pack(side="left", fill="both", expand=True); sb.pack(side="right", fill="y")
        self.moment_vars = {}
        for i, (name, m) in enumerate(sorted((cfg.get("moments") or {}).items())):
            v = tk.BooleanVar(value=bool((m or {}).get("on")))
            self.moment_vars[name] = v
            ttk.Checkbutton(inner, text=name.replace("_", " "), variable=v,
                            command=lambda n=name, v=v: set_moment(SOUNDS_FILE, n, v.get())).grid(row=i, column=0, sticky="w")
            ttk.Button(inner, text="▶", width=3, command=lambda n=name: self.run(f"sound {n}", self.bridge.sound, f"play {n}")).grid(row=i, column=1, padx=6)

    def toggle_sound(self):
        on = self.snd_on.get()
        def work():
            if self.bridge.sound("on" if on else "off") is None:      # bridge down: write the file ourselves
                update_json(SOUNDS_FILE, {"enabled": on})
            self.root.after(0, self.say, f"sounds {'on' if on else 'off'}")
        threading.Thread(target=work, daemon=True).start()

    # --- Settings
    def build_settings(self, nb):
        ttk, tk = self.ttk, self.tk
        f = self.frame(nb, "Settings")
        cfg = read_json(CONFIG_FILE)
        self.fields = {}

        def field(row, key, label, value="", secret=False, note=""):
            ttk.Label(f, text=label).grid(row=row, column=0, sticky="w", pady=3)
            v = tk.StringVar(value=value)
            e = ttk.Entry(f, textvariable=v, width=44, show="•" if secret else "")
            e.grid(row=row, column=1, sticky="we", pady=3)
            if note:
                ttk.Label(f, text=note, style="Note.TLabel").grid(row=row + 1, column=1, sticky="w")
            self.fields[key] = v

        f.columnconfigure(1, weight=1)
        ttk.Label(f, text="Lump's connection", font=("Segoe UI", 10, "bold")).grid(row=0, column=0, columnspan=2, sticky="w")
        field(1, "lump_host", "Wi-Fi host", cfg.get("lump_host", ""), note="used when he's not on USB (default lump.local)")
        ttk.Label(f, text="Wi-Fi pairing: " + ("set up" if cfg.get("lump_token") else "not set up (run tools/wifi_setup.py with him plugged in)"),
                  style="Note.TLabel").grid(row=3, column=1, sticky="w")
        ttk.Label(f, text="Calendar", font=("Segoe UI", 10, "bold")).grid(row=4, column=0, columnspan=2, sticky="w", pady=(12, 0))
        field(5, "calendar_ics", "Private iCal link", cfg.get("calendar_ics", ""), secret=True)
        field(6, "my_email", "Your email", cfg.get("my_email", ""), note="skips meetings you've declined")
        ttk.Label(f, text="Slack", font=("Segoe UI", 10, "bold")).grid(row=8, column=0, columnspan=2, sticky="w", pady=(12, 0))
        field(9, "slack_token", "User token (xoxp-…)", cfg.get("slack_token", ""), secret=True)
        field(10, "slack_vips", "VIPs", ", ".join(cfg.get("slack_vips") or []), note="names as Slack shows them, comma separated")
        field(12, "slack_vip_minutes", "Letter lasts (minutes)", str(cfg.get("slack_vip_minutes") or ""))
        row = ttk.Frame(f, padding=(0, 14, 0, 0)); row.grid(row=13, column=0, columnspan=2, sticky="w")
        ttk.Button(row, text="Save", command=self.save_settings).pack(side="left")
        ttk.Label(f, text="The bridge picks up changes by itself.", style="Note.TLabel").grid(row=14, column=0, columnspan=2, sticky="w", pady=(6, 0))
        # Lump plugged into another computer, whose relay dials in here (tools/lump_relay.py)
        ttk.Label(f, text="Remote Lump", font=("Segoe UI", 10, "bold")).grid(row=15, column=0, columnspan=2, sticky="w", pady=(12, 0))
        self.relay_state = ttk.Label(f, text="", justify="left")
        self.relay_state.grid(row=16, column=0, columnspan=2, sticky="w")
        row = ttk.Frame(f, padding=(0, 6, 0, 0)); row.grid(row=17, column=0, columnspan=2, sticky="w")
        ttk.Button(row, text="Copy token", command=self.copy_relay_token).pack(side="left")
        ttk.Button(row, text="New token", command=self.renew_relay_token).pack(side="left", padx=6)
        self.show_relay_state(None)

    def show_relay_state(self, st):
        if not read_json(CONFIG_FILE).get("relay_token"):
            text = "Not set up. On this PC run: tools\\windows\\install.ps1 -AllowRelay"
        else:
            text = "Set up (token in config.json). " + (describe_relay(st) or "Waiting for the bridge to start listening.")
        self.relay_state.config(text=text)

    def copy_relay_token(self):
        tok = read_json(CONFIG_FILE).get("relay_token")
        if not tok:
            self.say("the remote link isn't set up (install.ps1 -AllowRelay)"); return
        self.root.clipboard_clear(); self.root.clipboard_append(tok)
        self.say("relay token copied: paste it where the relay installer asks for it")

    def renew_relay_token(self):
        from tkinter import messagebox
        if not read_json(CONFIG_FILE).get("relay_token"):
            self.say("the remote link isn't set up (install.ps1 -AllowRelay)"); return
        if not messagebox.askyesno("New relay token", "Make a new relay token? The remote Lump disconnects until "
                                   "its computer gets the new one (run install.ps1 -Relay there again)."):
            return
        new_relay_token(replace=True)
        self.show_relay_state(self.status)
        self.say("new relay token made: the relay is disconnected until it has it")

    def save_settings(self):
        v = {k: var.get().strip() for k, var in self.fields.items()}
        mins = v.pop("slack_vip_minutes")
        try:
            mins = float(mins) if mins else None
        except ValueError:
            self.say("Letter minutes must be a number"); return
        vips = parse_vips(v.pop("slack_vips"))
        old = read_json(CONFIG_FILE)
        update_json(CONFIG_FILE, {**v, "slack_vips": vips or None, "slack_vip_minutes": mins})
        if v["calendar_ics"] != old.get("calendar_ics", "") or v["my_email"] != old.get("my_email", ""):
            threading.Thread(target=self.bridge.calendar_now, daemon=True).start()
        self.say("settings saved")

    # --- show / hide
    def show(self):
        self.root.deiconify(); self.root.lift(); self.root.focus_force()
        self.refresh()

    def hide(self):
        self.root.withdraw()


# ---------- the tray icon ----------
def start_tray(root, win, bridge):
    try:
        import pystray
        from PIL import Image
    except ImportError:
        return None
    path = os.path.join(HOME, "lump_icon.png")
    if not os.path.exists(path):
        path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "lumpbar", "icon.png")
    image = Image.open(path) if os.path.exists(path) else Image.new("RGBA", (64, 64), (255, 122, 69, 255))
    ui = lambda fn, *a: root.after(0, fn, *a)
    names = bridge.animations() or ["tap", "disco", "shock", "hello"]

    def sound_on(item=None):
        return bool((win.status or {}).get("sound"))

    def toggle_sound(icon, item):
        bridge.sound("toggle") if bridge.status() else update_json(SOUNDS_FILE, {"enabled": not read_json(SOUNDS_FILE).get("enabled")})

    def player(n):                       # pystray wants actions taking exactly (icon, item)
        return lambda icon, item: bridge.play(n)

    menu = pystray.Menu(
        pystray.MenuItem("Open Lump Control", lambda icon, item: ui(win.show), default=True),
        pystray.MenuItem("Play", pystray.Menu(*[pystray.MenuItem(n.replace("_", " "), player(n)) for n in names])),
        pystray.MenuItem("Sound", toggle_sound, checked=sound_on),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Restart bridge", lambda icon, item: ui(win.restart)),
        pystray.MenuItem("Quit Lump Control", lambda icon, item: (icon.stop(), ui(root.destroy))),
    )
    icon = pystray.Icon("lump-control", image, "Lump", menu)
    icon.run_detached()

    def keep_title():
        while True:
            st = bridge.status()
            win.status = st if st is not None else None
            try:
                icon.title = ("Lump: " + describe(st))[:120]
                icon.update_menu()
            except Exception:
                pass
            time.sleep(5)
    threading.Thread(target=keep_title, daemon=True).start()
    return icon


def main():
    code = cli(sys.argv[1:])
    if code is not None:
        sys.exit(code)
    if already_running():
        return
    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)          # crisp text on high-DPI screens
    except Exception:
        pass
    import tkinter as tk
    root = tk.Tk()
    bridge = Bridge()
    win = ControlWindow(root, bridge)
    listen_for_show(lambda: root.after(0, win.show))
    tray = start_tray(root, win, bridge)
    if tray and "--show" not in sys.argv:
        root.withdraw()                                         # at logon: just the tray icon
    root.after(REFRESH_MS, win.tick)
    root.mainloop()
    if tray:
        tray.stop()


if __name__ == "__main__":
    main()

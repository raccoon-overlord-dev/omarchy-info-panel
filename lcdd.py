#!/usr/bin/env python
"""lcdd: draw CPU/RAM/GPU/VRAM/NET stats on the AX206 USB LCD (1908:0102, 480x320).

usage: lcdd.py            run daemon (SIGHUP reloads config)
       lcdd.py --png F    render one frame to F, no hardware
Config: ~/.config/lcdd/config.toml
  layout = "rows" | "tiles"       theme = "omarchy" (follow system theme) | "neutral"
  refresh = 1.0 (seconds)         brightness = 7 (0-7)
  language = "en" | "it"          show_weather = true    show_tailscale = true
  weather_city = ""              (empty = use the Omarchy weather location)
"""
import glob, json, os, signal, struct, subprocess, sys, threading, time, tomllib
from urllib.parse import quote
from PIL import Image, ImageChops, ImageDraw, ImageFont

W, H = 480, 320
CFG = os.path.expanduser("~/.config/lcdd/config.toml")
OMARCHY_COLORS = os.path.expanduser("~/.local/state/omarchy/current/theme/colors.toml")
OMARCHY_WEATHER = os.path.expanduser("~/.local/state/omarchy/settings/weather.json")
DEFAULTS = {"refresh": 1.0, "brightness": 7, "layout": "rows", "theme": "omarchy", "language": "en", "show_weather": True, "show_tailscale": True, "weather_city": ""}
TR = {
    "en": dict(days="Mon Tue Wed Thu Fri Sat Sun".split(), months="Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split(), used="used", free="free"),
    "it": dict(days="lun mar mer gio ven sab dom".split(), months="gen feb mar apr mag giu lug ago set ott nov dic".split(), used="usati", free="liberi"),
}
NEUTRAL = dict(bg=(16, 16, 24), fg=(232, 232, 224), dim=(136, 136, 144), bar_bg=(48, 48, 56), divider=(48, 48, 56),
               outline=(64, 64, 72), accent=(88, 200, 160), ok=(88, 200, 160), mid=(232, 184, 72), warn=(240, 88, 72))
# theme token -> omarchy colors.toml key; dim is derived below
OMARCHY_MAP = dict(bg="background", fg="foreground", bar_bg="selection", divider="selection", outline="muted",
                   accent="accent", ok="green", mid="yellow", warn="red")
FONT_DIR = "/usr/share/fonts/TTF/"
# shortcut: only the Nerd Font build is installed and it has no Medium weight, so Medium uses Regular
FONT_FILES = {"regular": "JetBrainsMonoNerdFont-Regular.ttf", "medium": "JetBrainsMonoNerdFont-Regular.ttf", "bold": "JetBrainsMonoNerdFont-Bold.ttf"}
_fonts = {}


def font(weight, size):
    if (weight, size) not in _fonts:
        try:
            _fonts[weight, size] = ImageFont.truetype(FONT_DIR + FONT_FILES[weight], size)
        except OSError:
            _fonts[weight, size] = ImageFont.load_default(size)
    return _fonts[weight, size]


def load_cfg():
    cfg = dict(DEFAULTS)
    try:
        with open(CFG, "rb") as f:
            cfg.update(tomllib.load(f))
    except FileNotFoundError:
        pass
    cfg["refresh"] = max(0.2, float(cfg["refresh"]))
    cfg["brightness"] = min(7, max(0, int(cfg["brightness"])))
    if cfg["layout"] not in ("rows", "tiles"):
        print(f"unknown layout {cfg['layout']!r}, using rows", file=sys.stderr)
        cfg["layout"] = "rows"
    if cfg["theme"] not in ("omarchy", "neutral"):
        print(f"unknown theme {cfg['theme']!r}, using omarchy", file=sys.stderr)
        cfg["theme"] = "omarchy"
    if cfg["language"] not in TR:
        print(f"unknown language {cfg['language']!r}, using en", file=sys.stderr)
        cfg["language"] = "en"
    cfg["weather_city"] = str(cfg["weather_city"]).strip()
    cfg["show_weather"], cfg["show_tailscale"] = bool(cfg["show_weather"]), bool(cfg["show_tailscale"])
    return cfg


def hexrgb(h):
    h = h.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def load_theme(name):
    th = dict(NEUTRAL)
    if name != "omarchy":
        return th
    try:
        with open(OMARCHY_COLORS, "rb") as f:
            c = tomllib.load(f)
        for tok, key in OMARCHY_MAP.items():
            if key in c:
                th[tok] = hexrgb(c[key])
        if "muted" in c and "foreground" in c:  # muted alone is too faint at backlight 3-4
            m, f = hexrgb(c["muted"]), hexrgb(c["foreground"])
            th["dim"] = tuple(round(a + 0.35 * (b - a)) for a, b in zip(m, f))
    except (OSError, tomllib.TOMLDecodeError, ValueError):
        pass  # missing/broken file: keep neutral values
    return th


def theme_stamp():
    try:
        return os.stat(OMARCHY_COLORS).st_mtime_ns
    except OSError:
        return None


# wttr.in weatherCode -> Weather Icons glyph (day, night); same mapping as Omarchy's weather widget (Model.js iconForCode)
ICONS = {}
for codes, day, night in (
    ((113,), 0xE30D, 0xE32B), ((116,), 0xE302, 0xE32E), ((119, 122), 0xE33D, 0xE33D), ((143, 149, 248, 260), 0xE313, 0xE346),
    ((176, 263, 353), 0xE308, 0xE333), ((179, 227, 230, 323, 326, 368), 0xE30A, 0xE327),
    ((182, 185, 281, 284, 311, 314, 317, 320, 350, 362, 365, 374, 377), 0xE3AD, 0xE3AD),
    ((200, 386, 389, 392, 395), 0xE31D, 0xE31D), ((266, 293, 296, 299, 302, 305, 308, 356, 359), 0xE318, 0xE318),
    ((329, 332, 335, 338, 371), 0xE31A, 0xE31A),
):
    for c in codes:
        ICONS[c] = (chr(day), chr(night))


# wttr.in's own lang_it text leaks English for some codes (e.g. "Smoky haze"), so Italian is mapped from the code
COND_IT = {}
for codes, label in (
    ((113,), "Sereno"), ((116,), "Poco nuvoloso"), ((119,), "Nuvoloso"), ((122,), "Coperto"), ((143,), "Foschia"), ((149,), "Foschia fumosa"),
    ((176,), "Pioggia sparsa"), ((179,), "Neve sparsa"), ((182,), "Nevischio"), ((185, 281, 284), "Pioviggine gelata"),
    ((200,), "Temporali"), ((227,), "Neve e vento"), ((230,), "Bufera di neve"), ((248,), "Nebbia"), ((260,), "Nebbia gelata"),
    ((263, 266), "Pioviggine"), ((293, 296), "Pioggia debole"), ((299, 302), "Pioggia"), ((305, 308), "Pioggia forte"),
    ((311, 314), "Pioggia gelata"), ((317, 320), "Nevischio"), ((323, 326), "Neve debole"), ((329, 332), "Neve"),
    ((335, 338), "Neve forte"), ((350, 374, 377), "Grandine"), ((353,), "Rovesci"), ((356, 359), "Rovesci forti"),
    ((362, 365), "Rovesci di nevischio"), ((368,), "Rovesci di neve"), ((371,), "Forti nevicate"),
    ((386, 389), "Temporale"), ((392, 395), "Temporale con neve"),
):
    for c in codes:
        COND_IT[c] = label


def is_night(astro):
    """shortcut: compares sunrise/sunset (location time) with this machine's clock; wrong only for a city in another timezone."""
    try:
        fmt = lambda t: time.strptime(t, "%I:%M %p")
        now = time.localtime()
        mins = lambda t: t.tm_hour * 60 + t.tm_min
        return not mins(fmt(astro["sunrise"])) <= mins(now) < mins(fmt(astro["sunset"]))
    except (KeyError, ValueError):
        return False


def fetch_weather(lang, city=""):
    """(icon, temp, condition) from wttr.in for `city`, else the location Omarchy uses, or None. Metric units.
    shortcut: always metric; Omarchy's imperial/metric override is not read."""
    place = quote(city)
    if not city:
        try:
            with open(OMARCHY_WEATHER) as f:
                loc = json.load(f)
            place = f"{loc['latitude']},{loc['longitude']}" if loc.get("latitude") is not None and loc.get("longitude") is not None else quote(loc.get("name") or "")
        except (OSError, ValueError, AttributeError):
            pass  # no location set: wttr.in detects it from the IP, as Omarchy does
    try:
        out = subprocess.run(["curl", "-fsS", "--max-time", "6", f"https://wttr.in/{place}?m&lang={lang}&format=j1"],
                             capture_output=True, text=True, timeout=8).stdout
        d = json.loads(out)
        cur = d["current_condition"][0]
        code = int(cur["weatherCode"])
        day, night = ICONS.get(code, ICONS[119])
        cond = (lang == "it" and COND_IT.get(code)) or (cur.get(f"lang_{lang}") or cur["weatherDesc"])[0]["value"].strip()
        return night if is_night(d["weather"][0]["astronomy"][0]) else day, f"{cur['temp_C']}°C", cond
    except (OSError, ValueError, KeyError, IndexError, subprocess.TimeoutExpired):
        return None


def fetch_ts():
    """(running, peers_online); (False, 0) when stopped, logged out or tailscale missing."""
    try:
        d = json.loads(subprocess.run(["tailscale", "status", "--json"], capture_output=True, text=True, timeout=3).stdout)
        if d.get("BackendState") != "Running":
            return False, 0
        return True, sum(1 for p in d.get("Peer", {}).values() if p.get("Online"))
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return False, 0


def poll(get_cfg, extra):
    """Background thread so slow curl/tailscale calls never stall the display. Only polls what the config enables."""
    seen, t_w, t_ts = None, 0, 0
    while True:
        cfg, now = get_cfg(), time.monotonic()
        if cfg["show_weather"] and (now - t_w > 600 or seen != (cfg["language"], cfg["weather_city"])):
            t_w, seen = now, (cfg["language"], cfg["weather_city"])
            w = fetch_weather(*seen)
            if w:
                extra["weather"] = w
            else:
                t_w = now - 540  # failed: retry in 60 s, keep showing the last good value
        if cfg["show_tailscale"] and now - t_ts > 10:
            t_ts, extra["ts"] = now, fetch_ts()
        time.sleep(1)


class Stats:
    def __init__(self):
        self.cpu_prev, self.net_prev, self.t_prev = self._cpu(), self._net(), time.monotonic()
        self.cpu_temp = next((p + "/temp1_input" for p in glob.glob("/sys/class/hwmon/hwmon*") if open(p + "/name").read().strip() == "k10temp"), None)
        self.cpu_freq = glob.glob("/sys/devices/system/cpu/cpu[0-9]*/cpufreq/scaling_cur_freq")

    @staticmethod
    def _cpu():
        v = [int(x) for x in open("/proc/stat").readline().split()[1:9]]
        return v[3] + v[4], sum(v)  # idle(+iowait), total

    @staticmethod
    def _net():
        rx = tx = 0
        for line in list(open("/proc/net/dev"))[2:]:
            name, rest = line.split(":")
            if name.strip() != "lo":
                f = rest.split()
                rx, tx = rx + int(f[0]), tx + int(f[8])
        return rx, tx

    @staticmethod
    def _gpu():
        try:
            out = subprocess.run(
                ["nvidia-smi", "--query-gpu=utilization.gpu,clocks.gr,temperature.gpu,memory.used,memory.total", "--format=csv,noheader,nounits"],
                capture_output=True, text=True, timeout=2).stdout
            u, mhz, t, mu, mt = (float(x) for x in out.split(","))
            return dict(pct=u, mhz=mhz, temp=t, vused=mu / 1024, vtotal=mt / 1024)  # GiB
        except (OSError, ValueError, subprocess.TimeoutExpired):
            return None

    def read(self):
        now = time.monotonic()
        dt = max(now - self.t_prev, 1e-3)
        idle, total = self._cpu()
        di, dtot = idle - self.cpu_prev[0], total - self.cpu_prev[1]
        cpu = 100 * (1 - di / dtot) if dtot else 0
        rx, tx = self._net()
        net = ((rx - self.net_prev[0]) / dt, (tx - self.net_prev[1]) / dt)
        self.cpu_prev, self.net_prev, self.t_prev = (idle, total), (rx, tx), now
        mem = {l.split(":")[0]: int(l.split()[1]) for l in open("/proc/meminfo")}
        rtot, rfree = mem["MemTotal"] / 2**20, mem["MemAvailable"] / 2**20  # GiB
        mhz = sum(int(open(f).read()) for f in self.cpu_freq) / len(self.cpu_freq) / 1000
        temp = int(open(self.cpu_temp).read()) / 1000 if self.cpu_temp else None
        return dict(cpu=dict(pct=cpu, mhz=mhz, temp=temp), ram=dict(used=rtot - rfree, total=rtot), net=net, gpu=self._gpu())


def human(bps):
    for u in ("B", "K", "M", "G"):
        if bps < 1000 or u == "G":
            return f"{bps:.0f}{u}/s" if u == "B" else f"{bps:.1f}{u}/s"
        bps /= 1000


def ghz(mhz):
    return f"{mhz / 1000:.1f} GHz" if mhz >= 1000 else f"{mhz:.0f} MHz"


def state(pct, mid, warn):
    return "ok" if pct < mid else "mid" if pct <= warn else "warn"


def metrics(s, tr):
    """Per metric: label, pct (None = n/a), load state, rows detail, tiles detail, secondary text, secondary colour token."""
    c, r, g = s["cpu"], s["ram"], s["gpu"]

    def temp(t):
        if t is None:
            return "--", "dim"
        return f"{t:.0f}°C", {"ok": "fg", "mid": "mid", "warn": "warn"}[state(t, 70, 80)]

    ct, ctc = temp(c["temp"])
    rp = 100 * r["used"] / r["total"]
    out = [
        ("CPU", c["pct"], state(c["pct"], 60, 85), ghz(c["mhz"]), ghz(c["mhz"]), ct, ctc),
        ("RAM", rp, state(rp, 60, 90), f"{tr['used']} · {r['total'] - r['used']:.1f}G {tr['free']}", f"{r['total'] - r['used']:.1f} {tr['free']}", f"{r['used']:.1f}G", "fg"),
    ]
    if g:
        gt, gtc = temp(g["temp"])
        vp = 100 * g["vused"] / g["vtotal"]
        vfree = g["vtotal"] - g["vused"]
        out += [("GPU", g["pct"], state(g["pct"], 60, 85), ghz(g["mhz"]), ghz(g["mhz"]), gt, gtc),
                ("VRAM", vp, state(vp, 60, 90), f"{tr['used']} · {vfree:.1f}G {tr['free']}", f"{vfree:.1f} {tr['free']}", f"{g['vused']:.1f}G", "fg")]
    else:
        out += [(n, None, "ok", "", "", "--", "dim") for n in ("GPU", "VRAM")]
    return out


def bar(d, th, x, y, n, w, h, gap, pct, st):
    """n segments of w x h px (rectangle() is inclusive, so w-1/h-1)."""
    filled = 0 if pct is None else int(min(pct, 100) / 100 * n + 0.5)
    for i in range(n):
        d.rectangle((x + (w + gap) * i, y, x + (w + gap) * i + w - 1, y + h - 1), fill=th[st] if i < filled else th["bar_bg"])


def render(s, th, cfg):
    img = Image.new("RGB", (W, H), th["bg"])
    d = ImageDraw.Draw(img)
    layout, tr, now = cfg["layout"], TR[cfg["language"]], time.localtime()
    m = metrics(s, tr)
    clock = time.strftime("%H:%M", now)
    date = f"{tr['days'][now.tm_wday]} {now.tm_mday:02d} {tr['months'][now.tm_mon - 1]}"

    def text(xy, t, f, tok, anchor="ls"):
        d.text(xy, t, font=f, fill=th[tok], anchor=anchor)

    def weather(y, left, right):
        """Icon, temperature, condition: centred on the screen, kept between clock and date."""
        if not (cfg["show_weather"] and s.get("weather")):
            return
        icon, temp, cond = s["weather"]
        tf, cf = font("medium", 22), font("regular", 14)
        tw = d.textlength(temp, font=tf)
        while cond and 30 + tw + 8 + d.textlength(cond, font=cf) > right - left:
            cond = cond[:-1]
        w = 30 + tw + (8 + d.textlength(cond, font=cf) if cond else 0)
        x = max(left, min(W / 2 - w / 2, right - w))
        text((x, y), icon, font("regular", 24), "accent")
        text((x + 30, y), temp, tf, "fg")
        if cond:
            text((x + 38 + tw, y), cond, cf, "dim")

    def tailscale(y):
        """Dot + 'TS n' (peers online) at the right edge of the network line."""
        if not (cfg["show_tailscale"] and s.get("ts")):
            return
        on, peers = s["ts"]
        label, f = f"TS {peers}" if on else "TS off", font("regular", 16)
        text((464, y), label, f, "fg" if on else "dim", "rs")
        cx = 464 - d.textlength(label, font=f) - 12
        d.ellipse((cx - 5, y - 11, cx + 5, y - 1), fill=th["ok"] if on else th["dim"])

    def load(x, y, label_pct, st, f, anchor):
        text((x, y), "n/a" if label_pct is None else f"{label_pct:.0f}", f, "dim" if label_pct is None else "warn" if st == "warn" else "fg", anchor)

    if layout == "rows":
        text((16, 37), clock, font("medium", 30), "fg")
        text((464, 32), date, font("regular", 16), "dim", "rs")
        weather(35, 16 + d.textlength(clock, font=font("medium", 30)) + 12, 464 - d.textlength(date, font=font("regular", 16)) - 12)
        d.line((16, 48, 463, 48), fill=th["divider"])
        d.line((16, 278, 463, 278), fill=th["divider"])
        for i, (label, pct, st, detail, _, sec, sec_tok) in enumerate(m):
            y = 54 + 56 * i
            text((16, y + 18), label, font("bold", 16), "dim")
            text((64, y + 17), detail, font("regular", 14), "dim")
            bar(d, th, 16, y + 26, 19, 11, 16, 2, pct, st)
            text((372, y + 38), sec, font("medium", 24), sec_tok, "rs")
            load(446, y + 40, pct, st, font("bold", 34), "rs")
            if pct is not None:
                text((464, y + 38), "%", font("regular", 16), "dim", "rs")
        d.polygon([(16, 294), (32, 294), (24, 306)], fill=th["accent"])
        text((38, 306), human(s["net"][0]), font("regular", 20), "fg")
        d.polygon([(256, 304), (272, 304), (264, 292)], fill=th["accent"])
        text((278, 306), human(s["net"][1]), font("regular", 20), "fg")
        tailscale(306)
    else:
        text((12, 31), clock, font("medium", 26), "fg")
        text((468, 28), date, font("regular", 16), "dim", "rs")
        weather(30, 12 + d.textlength(clock, font=font("medium", 26)) + 12, 468 - d.textlength(date, font=font("regular", 16)) - 12)
        for i, (label, pct, st, _, detail, sec, sec_tok) in enumerate(m):
            x0, y0 = 8 + (i % 2) * 236, 44 + (i // 2) * 120
            d.rectangle((x0, y0, x0 + 227, y0 + 111), outline=th["outline"], width=1)
            text((x0 + 12, y0 + 24), label, font("bold", 15), "dim")
            text((x0 + 216, y0 + 24), detail, font("regular", 14), "dim", "rs")
            load(x0 + 91, y0 + 72, pct, st, font("bold", 44), "rs")  # right-aligned = U+2007-padded to 3 chars
            if pct is not None:
                text((x0 + 93, y0 + 72), "%", font("regular", 18), "dim")
            sec = sec.replace("G", " GB") if sec.endswith("G") else sec
            text((x0 + 216, y0 + 70), sec, font("medium", 22), sec_tok, "rs")
            bar(d, th, x0 + 12, y0 + 86, 17, 10, 14, 2, pct, st)
        d.polygon([(12, 296), (28, 296), (20, 308)], fill=th["accent"])
        text((34, 308), human(s["net"][0]), font("regular", 20), "fg")
        d.polygon([(248, 306), (264, 306), (256, 294)], fill=th["accent"])
        text((270, 308), human(s["net"][1]), font("regular", 20), "fg")
        tailscale(308)
    return img


class Lcd:
    def __init__(self):
        import usb.core
        self.usb = usb.core
        dev = usb.core.find(idVendor=0x1908, idProduct=0x0102)
        if dev is None:
            raise OSError("LCD 1908:0102 not found")
        dev.reset()  # device keeps stale protocol state; first read times out without this
        time.sleep(2)
        self.dev = usb.core.find(idVendor=0x1908, idProduct=0x0102)
        try:
            if self.dev.is_kernel_driver_active(0):
                self.dev.detach_kernel_driver(0)
        except (NotImplementedError, usb.core.USBError):
            pass
        self.dev.set_configuration()
        self.tag = 0

    def cmd(self, c, data=b""):
        self.tag += 1
        cbw = struct.pack("<4sIIBBB16s", b"USBC", self.tag, len(data), 0, 0, 16, bytes(c).ljust(16, b"\0"))
        self.dev.write(0x01, cbw, 2000)
        if data:
            self.dev.write(0x01, data, 5000)
        self.dev.read(0x81, 13, 2000)  # CSW

    def brightness(self, v):
        self.cmd([0xCD, 0, 0, 0, 0, 6, 0x01, 0x01, 0x00, v, 0])

    def blit(self, img, x0, y0):
        raw = img.convert("RGB").tobytes()
        px = struct.pack(f">{len(raw) // 3}H", *[((r & 0xF8) << 8) | ((g & 0xFC) << 3) | (b >> 3) for r, g, b in zip(raw[0::3], raw[1::3], raw[2::3])])
        x1, y1 = x0 + img.width - 1, y0 + img.height - 1  # protocol end is inclusive
        self.cmd([0xCD, 0, 0, 0, 0, 6, 0x12, x0 & 255, x0 >> 8, y0 & 255, y0 >> 8, x1 & 255, x1 >> 8, y1 & 255, y1 >> 8], px)


def dirty_boxes(prev, img):
    """Bounding box of changes per 40 px band: a clock tick and a load change don't force one big rectangle."""
    if prev is None:
        return [(0, 0, W, H)]
    boxes = []
    for y in range(0, H, 40):
        b = ImageChops.difference(prev.crop((0, y, W, y + 40)), img.crop((0, y, W, y + 40))).getbbox()
        if b:
            boxes.append((b[0], y + b[1], b[2], y + b[3]))
    return boxes


def main():
    cfg = load_cfg()
    if "--png" in sys.argv:
        s = Stats()
        time.sleep(0.5)
        st = s.read()
        st["weather"] = fetch_weather(cfg["language"], cfg["weather_city"]) if cfg["show_weather"] else None
        st["ts"] = fetch_ts() if cfg["show_tailscale"] else None
        render(st, load_theme(cfg["theme"]), cfg).save(sys.argv[sys.argv.index("--png") + 1])
        return
    state = {"reload": False, "run": True}
    signal.signal(signal.SIGHUP, lambda *_: state.update(reload=True))
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: state.update(run=False))
    stats, lcd, prev = Stats(), None, None
    extra = {"weather": None, "ts": None}
    threading.Thread(target=poll, args=(lambda: cfg, extra), daemon=True).start()  # reads the live cfg after reloads
    theme, stamp = load_theme(cfg["theme"]), theme_stamp()
    while state["run"]:
        t0 = time.monotonic()
        try:
            if lcd is None:
                lcd = Lcd()
                lcd.brightness(cfg["brightness"])
                prev = None
            if state["reload"]:
                state["reload"] = False
                cfg = load_cfg()
                lcd.brightness(cfg["brightness"])
                theme, stamp, prev = load_theme(cfg["theme"]), theme_stamp(), None
            elif cfg["theme"] == "omarchy" and theme_stamp() != stamp:  # omarchy theme switched
                theme, stamp, prev = load_theme("omarchy"), theme_stamp(), None
            st = stats.read()
            st.update(extra)
            img = render(st, theme, cfg)
            for box in dirty_boxes(prev, img):
                lcd.blit(img.crop(box), box[0], box[1])
            prev = img
        except Exception as e:  # USB unplug/hang: retry from scratch
            print(f"lcd error: {e!r}, retrying in 3s", file=sys.stderr)
            lcd, prev = None, None
            time.sleep(3)
            continue
        time.sleep(max(0, cfg["refresh"] - (time.monotonic() - t0)))


if __name__ == "__main__":
    main()

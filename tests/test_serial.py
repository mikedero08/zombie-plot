"""Two-way serial: a fake Roland that answers queries, has a small buffer and cuts at a fixed rate.

Checks that zombiePlot reads the model and width, paces the data so the buffer never
overflows, reports real progress and a real finish, never has two HP-GL queries
outstanding (the plotter's "Error 10"), confirms a stop, and passes on plotter errors."""
import json, os, re, sys, tempfile, threading, time, urllib.request, urllib.error
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import zombieplot as P
from http.server import ThreadingHTTPServer

P.WATCH_EVERY = 1.0            # check the plotter every second, so the test is quick
P.LOST_RETRY = 1.0

fails = []
def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + ((" :: " + str(detail)[:400]) if detail and not cond else ""))
    if not cond:
        fails.append(name)


class FakeRoland(threading.Thread):
    SIZE = 2048

    def __init__(self, fd):
        super().__init__(daemon=True)
        self.fd = fd
        self.lock = threading.Lock()
        self.buf = bytearray()          # HP-GL waiting to be cut
        self.inq = b""                  # raw bytes not yet sorted into device control / buffer
        self.cut = bytearray()          # every HP-GL command it carried out (queries left out)
        self.rate = 8000                # bytes per second it works through
        self.paused = False             # not working through its buffer (PAUSE key, a menu)
        self.silent = False             # answers nothing (switched off)
        self.overflow = 0
        self.overlap = 0                # HP-GL queries in the buffer at once: Roland "Error 10"
        self.max_fill = 0
        self.clip = "0,0,23200,10000"
        self.oe = 0
        self.got_abort = 0
        self.queries = []

    def reply(self, s):
        if not self.silent:
            os.write(self.fd, s.encode() + b"\r")

    def free(self):
        return self.SIZE - len(self.buf)

    def take(self, data):
        self.inq += data
        while self.inq:
            if self.inq.startswith(b"\x1b"):
                if len(self.inq) < 3:
                    return
                code, self.inq = self.inq[:3], self.inq[3:]
                self.queries.append(code)
                if code == b"\x1b.B":
                    self.reply(str(self.free()))
                elif code == b"\x1b.K":
                    self.got_abort += 1
                    self.buf.clear()
                continue
            i = self.inq.find(b"\x1b")
            chunk = self.inq if i < 0 else self.inq[:i]
            self.inq = b"" if i < 0 else self.inq[i:]
            room = self.free()
            if len(chunk) > room:
                self.overflow += len(chunk) - room
                chunk = chunk[:room]
            self.buf += chunk
            self.max_fill = max(self.max_fill, len(self.buf))
            if len(re.findall(rb"O[IHE];", bytes(self.buf))) > 1:
                self.overlap += 1

    def work(self, n):
        while n > 0 and not self.paused:
            j = self.buf.find(b";")
            if j < 0:
                return
            cmd = bytes(self.buf[:j + 1])
            del self.buf[:j + 1]
            n -= len(cmd)
            if cmd == b"OI;":
                self.reply("1210")
            elif cmd == b"OH;":
                self.reply(self.clip)
            elif cmd == b"OE;":
                self.reply(str(self.oe))
            else:
                self.cut += cmd

    def run(self):
        import select
        last = time.monotonic()
        while True:
            r, _, _ = select.select([self.fd], [], [], 0.01)
            with self.lock:
                if r:
                    try:
                        self.take(os.read(self.fd, 4096))
                    except OSError:
                        return
                now = time.monotonic()
                self.work(int((now - last) * self.rate))
                last = now


def api(path, obj=None):
    req = urllib.request.Request(BASE + path, data=None if obj is None else json.dumps(obj).encode(),
                                 method="GET" if obj is None else "POST", headers={"X-ZombiePlot": "1"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())

def status():
    return api("/api/status")[1]

def wait(cond, limit=20):
    end = time.time() + limit
    st = status()
    while time.time() < end:
        if cond(st):
            return st
        time.sleep(0.1)
        st = status()
    return st


tmp = tempfile.mkdtemp(prefix="zpserial")
data_dir, lib = os.path.join(tmp, "data"), os.path.join(tmp, "lib")
os.makedirs(data_dir); os.makedirs(lib)
SVG = b'<svg xmlns="http://www.w3.org/2000/svg" width="4in" height="2in" viewBox="0 0 400 200"><rect width="400" height="200"/><circle cx="100" cy="100" r="50"/></svg>'
open(os.path.join(lib, "decal.svg"), "wb").write(SVG)
master, slave = os.openpty()
json.dump({"port": os.ttyname(slave), "link": "serial", "flow": "xonxoff"}, open(os.path.join(data_dir, "config.json"), "w"))
plotter = FakeRoland(master); plotter.start()

app = P.App(data_dir, lib)
P.Handler.app = app
srv = ThreadingHTTPServer(("127.0.0.1", 0), P.Handler)
BASE = "http://127.0.0.1:%d" % srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start()
app.start_watch()

try:
    # --- the idle watch: model and width ---
    st = wait(lambda s: s["port"].get("model") and s["port"].get("width_mm"))
    check("model read from the plotter", st["port"].get("model") == "1210", st["port"])
    check("width read from the plotter (X across)", st["port"].get("width_mm") == 580.0, st["port"])
    st = wait(lambda s: s["vinyl"]["from_plotter"])
    check("vinyl width taken from the plotter", abs(st["vinyl"]["width_mm"] - 580.0) < 0.01 and st["vinyl"]["from_plotter"], st["vinyl"])
    check("status ready", st["port"]["state"] == "connected" and st["port"]["answers"] is True, st["port"])

    # --- a cut: paced, tracked, finished ---
    ids = {f["name"]: f["id"] for f in api("/api/files")[1]["files"]}
    polys, w, h, _ = P.parse_svg(SVG)
    polys, w, h = P.orient(polys, 0, False)
    cfg = app.cfg.snapshot()
    _, _, offs, jw, jh = P.grid(w, h, 40, min(cfg["vinyl_width_mm"], cfg["cut_width_mm"]), cfg["copy_gap_mm"])
    with plotter.lock:
        plotter.cut.clear()
    c, j = api("/api/cut", {"id": ids["decal.svg"], "rot": 0, "mirror": False, "copies": 40})
    check("cut accepted", c == 200, j)
    total = j["bytes"]
    samples = []
    end = time.time() + 60
    while time.time() < end:
        s = status()["job"]
        with plotter.lock:
            samples.append((s["sent"], len(plotter.cut), s["state"], s["tracked"]))
        if s["state"] != "sending":
            break
        time.sleep(0.2)
    s = status()["job"]
    mid = [x for x in samples if x[2] == "sending" and 0 < x[0] < total]
    check("progress is live while cutting", len(mid) >= 3 and all(x[3] for x in mid), samples[:5])
    check("progress never runs ahead of the cutting", all(x[0] <= x[1] + 40 for x in mid), [x for x in mid if x[0] > x[1] + 40][:3])
    with plotter.lock:
        finished_cut = len(plotter.cut)
    check("done only once the plotter has cut it all", s["state"] == "done" and "finished cutting" in s["message"]
          and finished_cut >= total - 10, (s, finished_cut, total))
    check("buffer never overflowed", plotter.overflow == 0 and plotter.max_fill <= plotter.SIZE, (plotter.overflow, plotter.max_fill))
    check("plotter got exactly the job", bytes(plotter.cut).startswith(b"IN;PA;") and len(plotter.cut) == total, (len(plotter.cut), total))
    check("job took longer than the send alone would", total / plotter.rate > 0.5)

    # --- a plotter error after the cut ---
    plotter.oe = 3
    c, j = api("/api/cut", {"id": ids["decal.svg"], "rot": 0, "mirror": False, "copies": 1})
    s = wait(lambda s: s["job"]["state"] != "sending")["job"]
    check("plotter error reported", s["state"] == "done" and "error 3" in s["message"] and "out of range" in s["message"], s)
    plotter.oe = 0

    # --- stop while the plotter is paused with a full buffer ---
    plotter.paused = True
    c, j = api("/api/cut", {"id": ids["decal.svg"], "rot": 0, "mirror": False, "copies": 40})
    time.sleep(2.0)
    s = status()["job"]
    check("paused plotter holds the job", s["state"] == "sending" and s["sent"] < j["bytes"], s)
    check("buffer full but not overflowed", plotter.overflow == 0 and plotter.max_fill > plotter.SIZE / 2, (plotter.overflow, plotter.max_fill))
    api("/api/stop", {})
    s = wait(lambda s: s["job"]["state"] in ("stopped", "error"))["job"]
    check("stop confirmed by the plotter", s["state"] == "stopped" and "emptied its buffer" in s["message"], s)
    check("plotter got the abort and its buffer is empty", plotter.got_abort >= 1 and len(plotter.buf) <= 4, (plotter.got_abort, bytes(plotter.buf)))

    # --- never two HP-GL queries at once (Roland Error 10), even when it is not answering them ---
    plotter.buf.clear()
    q0 = len([q for q in plotter.queries if q == b"\x1b.B"])
    time.sleep(6)                                   # the watch keeps checking; OH waits in the buffer
    check("one HP-GL query at a time", plotter.overlap == 0, plotter.overlap)
    with plotter.lock:
        waiting = bytes(plotter.buf)
    check("watch waits for its unanswered query", waiting in (b"", b"OH;", b"OI;"), waiting)
    plotter.paused = False
    time.sleep(2.5)
    check("late answer accepted, watch carries on", plotter.overlap == 0 and status()["port"]["answers"] is True)

    # --- width you type is kept until the plotter reports a new one ---
    api("/api/settings", {"vinyl_width_mm": 300})
    time.sleep(2.5)
    st = status()
    check("typed width kept", abs(st["vinyl"]["width_mm"] - 300) < 0.01 and not st["vinyl"]["from_plotter"], st["vinyl"])
    plotter.clip = "0,0,20000,10000"               # new vinyl: 500 mm
    st = wait(lambda s: abs(s["vinyl"]["width_mm"] - 500) < 0.01)
    check("new vinyl width taken from the plotter", abs(st["vinyl"]["width_mm"] - 500) < 0.01 and st["vinyl"]["from_plotter"], st["vinyl"])
    api("/api/settings", {"auto_width": False})
    plotter.clip = "0,0,16000,10000"
    time.sleep(2.5)
    st = status()
    check("auto width off: width left alone", abs(st["vinyl"]["width_mm"] - 500) < 0.01 and st["port"]["width_mm"] == 400.0, st)
    c, r = api("/api/settings", {"auto_width": True})
    check("auto width on again: taken at once", abs(r["settings"]["vinyl_width_mm"] - 400) < 0.01, r["settings"]["vinyl_width_mm"])
    c, r = api("/api/settings", {"swap_axes": True})
    check("swapped axes: width is the plotter's Y", abs(r["settings"]["vinyl_width_mm"] - 250) < 0.01, r["settings"]["vinyl_width_mm"])

    # --- plotter switched off, then on ---
    plotter.silent = True
    st = wait(lambda s: s["port"]["state"] == "notready", 15)
    check("silent plotter shows not ready", st["port"]["state"] == "notready" and "stopped answering" in st["port"]["message"], st["port"])
    plotter.silent = False
    with plotter.lock:
        plotter.buf.clear()
    st = wait(lambda s: s["port"]["state"] == "connected", 15)
    check("plotter back on: ready again", st["port"]["state"] == "connected" and st["port"]["answers"], st["port"])
    check("still never an overlap", plotter.overlap == 0, plotter.overlap)
    check("never overflowed", plotter.overflow == 0, plotter.overflow)
finally:
    srv.shutdown()

print("\n%d failed: %s" % (len(fails), fails) if fails else "\nALL SERIAL TESTS PASSED")
sys.exit(1 if fails else 0)

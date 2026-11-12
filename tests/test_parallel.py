import json, os, select, subprocess, sys, tempfile, time, urllib.request, urllib.error, urllib.parse
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import zombieplot as P
PORT = 8767; BASE = "http://127.0.0.1:%d" % PORT
fails = []
def check(n, c, d=""):
    print(("PASS " if c else "FAIL ") + n + ((" :: " + str(d)[:300]) if d and not c else ""))
    if not c: fails.append(n)
def getj(p):
    try:
        with urllib.request.urlopen(BASE + p, timeout=10) as r: return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e: return e.code, json.loads(e.read())
def post(p, o=None):
    req = urllib.request.Request(BASE + p, data=json.dumps(o or {}).encode(), method="POST", headers={"X-ZombiePlot": "1"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r: return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e: return e.code, json.loads(e.read())
def wait_state(states, limit=25):
    end = time.time() + limit
    while time.time() < end:
        s = getj("/api/status")[1]["job"]
        if s["state"] in states: return s
        time.sleep(0.1)
    return s

tmp = tempfile.mkdtemp(prefix="plotpar"); data, lib = os.path.join(tmp, "d"), os.path.join(tmp, "l")
os.makedirs(data); os.makedirs(lib)
SVG = b'<svg xmlns="http://www.w3.org/2000/svg" width="4in" height="2in" viewBox="0 0 400 200"><rect width="400" height="200"/><circle cx="100" cy="100" r="50"/></svg>'
open(os.path.join(lib, "a.svg"), "wb").write(SVG)
fifo = os.path.join(tempfile.mkdtemp(dir="/dev/shm"), "lp0"); os.mkfifo(fifo)
rd = os.open(fifo, os.O_RDONLY | os.O_NONBLOCK)          # the "plotter" end
def drain(quiet=0.6):
    buf = b""
    while True:
        r, _, _ = select.select([rd], [], [], quiet)
        if not r: return buf
        try:
            chunk = os.read(rd, 65536)
        except BlockingIOError:
            continue
        if not chunk: return buf
        buf += chunk
json.dump({"port": fifo, "link": "parallel"}, open(os.path.join(data, "config.json"), "w"))
srv = subprocess.Popen([sys.executable, os.path.join(ROOT, "zombieplot.py"), "--port", str(PORT), "--data", data, "--library", lib], stderr=subprocess.PIPE)
try:
    time.sleep(0.8)
    st = getj("/api/status")[1]["port"]
    check("parallel link detected", st["state"] == "connected" and st["link"] == "parallel", st)
    polys, w, h, _ = P.parse_svg(SVG); polys, w, h = P.orient(polys, 0, False)
    cols, rows, offs, jw, jh = P.grid(w, h, 2, 584.0, 5.0)
    expect = P.build_hpgl(polys, offs, jw, jh, dict(P.DEFAULTS))
    c, j = post("/api/cut", {"id": "lib:a.svg", "rot": 0, "copies": 2})
    got = drain()
    check("parallel bytes match exactly (%d)" % len(expect), c == 200 and got == expect, (c, j, got[:60]))
    check("parallel job done", wait_state(("done", "error"))["state"] == "done")
    check("default overcut 1 mm runs past start", got.count(b"PD") > expect.count(b"PD") - 1)

    c, j = post("/api/cut", {"id": "lib:a.svg", "rot": 0, "copies": 300})
    total = j["bytes"]
    time.sleep(4.5)
    s = getj("/api/status")[1]["job"]
    check("blocks when plotter stops taking data, reports waiting", s["state"] == "sending" and 0 < s["sent"] < total and s["waiting"], s)
    post("/api/stop")
    s = wait_state(("stopped", "error"))
    check("stop with full buffer explains what to do", s["state"] == "error" and "Switch the plotter off" in s["message"], s)
    got = drain()
    check("nothing sent after stop besides what was queued", len(got) < total, (len(got), total))
    post("/api/stop")
    got = drain()
    check("idle stop sends abort over parallel", got == b"\x1b.K;PU;", got)
    c, j = post("/api/cut", {"id": "lib:a.svg", "copies": 1})
    got = drain()
    check("cuts again after stop", c == 200 and len(got) == j["bytes"])
    wait_state(("done", "error"))

    os.rename(fifo, fifo + ".gone")
    st = getj("/api/status")[1]["port"]
    check("unplugged cable reported", st["state"] == "missing" and "cable" in st["message"], st)
    os.rename(fifo + ".gone", fifo)
    check("replugged cable picked up", getj("/api/status")[1]["port"]["state"] == "connected")
    c, j = post("/api/settings", {"link": "bogus"}); check("bad link rejected", c == 400)
finally:
    srv.terminate()
    err = srv.stderr.read().decode()
    if "error on" in err or "Traceback" in err: print(err); fails.append("stderr")
print("\n%d failed: %s" % (len(fails), fails) if fails else "\nALL PARALLEL TESTS PASSED")
sys.exit(1 if fails else 0)

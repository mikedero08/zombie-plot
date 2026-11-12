import json, os, select, subprocess, sys, tempfile, time, urllib.request, urllib.error, urllib.parse
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import zombieplot as P

PORT = 8765
BASE = "http://127.0.0.1:%d" % PORT
fails = []
def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + ((" :: " + str(detail)[:400]) if detail and not cond else ""))
    if not cond:
        fails.append(name)

def get(path):
    try:
        with urllib.request.urlopen(BASE + path, timeout=10) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()

def getj(path):
    c, b = get(path)
    return c, json.loads(b)

def post(path, obj=None, raw=None):
    data = raw if raw is not None else json.dumps(obj or {}).encode()
    req = urllib.request.Request(BASE + path, data=data, method="POST", headers={"X-ZombiePlot": "1"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())

def drain(fd, quiet=0.6, limit=20):
    """Read from the fake plotter until nothing arrives for `quiet` seconds."""
    buf = b""
    end = time.time() + limit
    while time.time() < end:
        r, _, _ = select.select([fd], [], [], quiet)
        if not r:
            break
        buf += os.read(fd, 65536)
    return buf

def wait_state(states, limit=20):
    end = time.time() + limit
    while time.time() < end:
        s = getj("/api/status")[1]["job"]
        if s["state"] in states:
            return s
        time.sleep(0.1)
    return s

tmp = tempfile.mkdtemp(prefix="plottest")
data_dir, lib = os.path.join(tmp, "data"), os.path.join(tmp, "lib")
os.makedirs(data_dir); os.makedirs(os.path.join(lib, "Signs", "Truck"))
SVG_A = b'<svg xmlns="http://www.w3.org/2000/svg" width="4in" height="2in" viewBox="0 0 400 200"><rect width="400" height="200"/><circle cx="100" cy="100" r="50"/></svg>'
SVG_WIDE = b'<svg xmlns="http://www.w3.org/2000/svg" width="30in" height="3in" viewBox="0 0 3000 300"><rect width="3000" height="300"/></svg>'
SVG_HUGE = b'<svg xmlns="http://www.w3.org/2000/svg" width="30in" height="30in" viewBox="0 0 30 30"><rect width="30" height="30"/></svg>'
open(os.path.join(lib, "decal.svg"), "wb").write(SVG_A)
open(os.path.join(lib, "Signs", "Truck", "Door Banner.SVG"), "wb").write(SVG_WIDE)
open(os.path.join(lib, "Signs", "huge.svg"), "wb").write(SVG_HUGE)
open(os.path.join(lib, "notes.txt"), "w").write("x")
open(os.path.join(tmp, "secret.svg"), "wb").write(SVG_A)
os.utime(os.path.join(lib, "decal.svg"), (time.time() - 100, time.time() - 100))

master, slave = os.openpty()
slave_path = os.ttyname(slave)
json.dump({"port": slave_path, "flow": "none"}, open(os.path.join(data_dir, "config.json"), "w"))

srv = subprocess.Popen([sys.executable, os.path.join(ROOT, "zombieplot.py"), "--port", str(PORT), "--data", data_dir, "--library", lib],
                       stderr=subprocess.PIPE)
try:
    for _ in range(50):
        try:
            get("/api/status"); break
        except Exception:
            time.sleep(0.1)

    c, b = get("/")
    check("page served", c == 200 and b"<title>zombiePlot</title>" in b)
    c, b = get("/thing")
    check("Car Thing page served", c == 200 and b"<title>zombiePlot</title>" in b and b'id="keys"' in b)
    c, b = get("/manifest.json"); check("manifest", c == 200 and json.loads(b)["display"] == "standalone")
    c, b = get("/icon.svg"); check("icon", c == 200 and b.startswith(b"<svg"))
    c, j = getj("/nope"); check("404", c == 404 and j["ok"] is False)

    c, st = getj("/api/status")
    check("status: port found via config", st["port"]["state"] == "connected" and st["port"]["device"] == slave_path and st["job"]["state"] == "idle", st)

    c, fl = getj("/api/files")
    names = [(f["name"], f["folder"]) for f in fl["files"]]
    check("file list (sub-folders, .SVG, newest first, txt skipped)", fl["library_ok"] and len(names) == 3 and names[-1] == ("decal.svg", "") and ("Door Banner.SVG", "Signs/Truck") in names, names)
    ids = {f["name"]: f["id"] for f in fl["files"]}

    c, pv = getj("/api/preview?id=" + urllib.parse.quote(ids["decal.svg"]) + "&rot=auto&mirror=0&copies=1")
    check("preview basics", c == 200 and abs(pv["w"] - 101.6) < 1e-6 and abs(pv["h"] - 50.8) < 1e-6 and pv["rot"] == 0 and pv["fits"] and pv["shapes"] == 2 and pv["d"].count("M") == 2 and pv["warnings"] == [], {k: v for k, v in pv.items() if k != "d"})
    c, pv = getj("/api/preview?id=" + urllib.parse.quote(ids["decal.svg"]) + "&rot=90&mirror=1&copies=12")
    check("preview rotate + copies", pv["rot"] == 90 and abs(pv["w"] - 50.8) < 1e-6 and pv["cols"] == 10 and pv["rows"] == 2 and len(pv["offsets"]) == 12 and pv["mirror"] is True, {k: v for k, v in pv.items() if k != "d"})
    c, pv = getj("/api/preview?id=" + urllib.parse.quote(ids["Door Banner.SVG"]) + "&rot=auto")
    check("auto-rotate a long banner to fit", pv["rot"] == 90 and pv["fits"] and pv["note"] != "" and abs(pv["job_h"] - 762) < 1e-6, {k: v for k, v in pv.items() if k != "d"})
    c, pv = getj("/api/preview?id=" + urllib.parse.quote(ids["Door Banner.SVG"]) + "&rot=0")
    check("too wide is flagged", pv["fits"] is False and "Too wide" in pv["warnings"][0] and "30.00 in" in pv["warnings"][0], pv["warnings"])
    c, j = post("/api/cut", {"id": ids["huge.svg"], "rot": 0, "mirror": False, "copies": 1})
    check("too wide refuses to cut", c == 400 and "Too wide" in j["error"], j)

    for bad in ("lib:../secret.svg", "lib:/etc/passwd", "up:../../secret.svg", "zzz:decal.svg", "lib:", "lib:missing.svg"):
        c, j = getj("/api/preview?id=" + urllib.parse.quote(bad))
        check("rejects " + bad, c == 400 and j["ok"] is False, j)

    # --- cut through the fake serial port and compare byte-for-byte ---
    polys, w, h, _ = P.parse_svg(SVG_A)
    polys, w, h = P.orient(polys, 0, False)
    cols, rows, offs, jw, jh = P.grid(w, h, 3, 584.0, 5.0)
    expect = P.build_hpgl(polys, offs, jw, jh, dict(P.DEFAULTS))
    c, j = post("/api/cut", {"id": ids["decal.svg"], "rot": 0, "mirror": False, "copies": 3})
    check("cut accepted", c == 200 and j["bytes"] == len(expect), j)
    got = drain(master)
    check("serial bytes match exactly (%d bytes)" % len(expect), got == expect, (got[:80], expect[:80]))
    check("job ends in done", wait_state(("done", "error", "stopped"))["state"] == "done")
    check("stream is plain HP-GL", got.startswith(b"IN;PA;PU") and got.endswith(b"PU0,0;") and set(got) <= set(b"INPAUD0123456789,;"), got[:60])

    # --- settings ---
    c, j = post("/api/settings", {"flow": "hardware", "cut_width_mm": 300, "units": "mm", "swap_axes": True})
    check("settings saved", c == 200 and j["settings"]["flow"] == "hardware" and j["settings"]["cut_width_mm"] == 300.0)
    check("settings persisted to disk", json.load(open(os.path.join(data_dir, "config.json")))["swap_axes"] is True)
    c, j = post("/api/settings", {"baud": 1234}); check("bad setting rejected", c == 400 and "baud" in j["error"], j)
    c, j = post("/api/settings", {"port": "/etc/passwd"}); check("bad port rejected", c == 400, j)
    c, j = post("/api/settings", {"cut_width_mm": "abc"}); check("bad number rejected", c == 400, j)
    c, s = getj("/api/settings"); check("settings read back", s["settings"]["units"] == "mm" and s["library"] == lib and s["device"] == slave_path)
    c, st = getj("/api/status")
    check("hardware flow on a port with no modem lines reads ready", st["port"]["state"] == "ready", st)

    # --- test square, hardware-flow path, swapped axes ---
    post("/api/session/new"); post("/api/settings", {"overcut_mm": 0})
    c, j = post("/api/cut", {"id": "test", "rot": 0, "mirror": False, "copies": 1})
    got = drain(master)
    check("test square", c == 200 and got.startswith(b"IN;PA;PU0,0;PD0,2032;PD2032,2032;PD2032,0;PD0,0;") and b"PU400,600;PD1600,600;PD1600,1400;PU1040,1160;PD1040,600;PU0,0;" in got, got)
    wait_state(("done", "error"))
    post("/api/settings", {"swap_axes": False, "cut_width_mm": 584, "units": "in", "flow": "none"})

    # --- stop in the middle of a job: fake plotter stops taking data ---
    c, j = post("/api/cut", {"id": ids["decal.svg"], "rot": 0, "mirror": False, "copies": 200})
    total = j["bytes"]
    time.sleep(1.5)
    s = getj("/api/status")[1]["job"]
    check("job blocks when plotter stops taking data", s["state"] == "sending" and 0 < s["sent"] < total, s)
    c, j2 = post("/api/cut", {"id": ids["decal.svg"], "copies": 1}); check("second job refused while busy", c == 400 and "already" in j2["error"], j2)
    time.sleep(2.2)
    s = getj("/api/status")[1]["job"]
    check("reports waiting after 3 s stall", s["waiting"] is True, s)
    post("/api/stop")
    s = wait_state(("stopped", "error"))
    check("stop ends the job", s["state"] == "stopped", s)
    got = drain(master)
    check("stop sent abort + knife up, nothing after", got.endswith(b"\x1b.K;PU;") and len(got) < total, (len(got), total, got[-30:]))

    # --- stop while idle (plotter still working through its own buffer) ---
    post("/api/stop")
    got = drain(master)
    check("idle stop sends abort", got == b"\x1b.K;PU;", got)
    check("port recovers after abort", getj("/api/status")[1]["port"]["state"] == "connected")
    c, j = post("/api/cut", {"id": ids["decal.svg"], "copies": 1})
    got = drain(master)
    check("cut works again after a stop", c == 200 and len(got) == j["bytes"] and got.endswith(b"PU0,0;"))
    wait_state(("done", "error"))

    # --- upload ---
    c, j = post("/api/upload?name=" + urllib.parse.quote("My Sign (2).svg"), raw=SVG_A)
    check("upload", c == 200 and j["id"] == "up:My Sign (2).svg" and os.path.exists(os.path.join(data_dir, "uploads", "My Sign (2).svg")), j)
    c, pv = getj("/api/preview?id=" + urllib.parse.quote(j["id"])); check("uploaded file previews", c == 200 and pv["shapes"] == 2)
    c, j = post("/api/upload?name=" + urllib.parse.quote("../../evil.svg"), raw=SVG_A)
    check("upload name is confined", c == 200 and j["id"] == "up:evil.svg" and not os.path.exists(os.path.join(tmp, "evil.svg")), j)
    c, j = post("/api/upload?name=x.svg", raw=b"this is not an svg"); check("upload rejects non-svg content", c == 400, j)
    c, j = post("/api/upload?name=x.exe", raw=SVG_A); check("upload rejects other extensions", c == 400, j)
    c, fl = getj("/api/files"); check("uploads listed first", fl["files"][0]["folder"] == "Sent from phone", fl["files"][0])

    # --- adapter unplugged ---
    post("/api/settings", {"port": "/dev/ttyDOESNOTEXIST"})
    st = getj("/api/status")[1]
    check("missing adapter reported", st["port"]["state"] == "missing" and "cable" in st["port"]["message"], st)
    c, j = post("/api/cut", {"id": ids["decal.svg"], "copies": 1}); check("cut refused with no adapter", c == 400 and "cable" in j["error"], j)
    post("/api/settings", {"port": slave_path})
    check("adapter plugged back in is picked up with no restart", getj("/api/status")[1]["port"]["state"] == "connected")

    # --- library folder gone (server share offline) ---
    os.rename(lib, lib + ".off")
    c, fl = getj("/api/files")
    check("offline share reported, uploads still listed", fl["library_ok"] is False and len(fl["files"]) == 2, fl)
    os.rename(lib + ".off", lib)

    # --- restart: settings and files survive ---
    srv.terminate(); srv.wait(5)
    srv = subprocess.Popen([sys.executable, os.path.join(ROOT, "zombieplot.py"), "--port", str(PORT), "--data", data_dir, "--library", lib], stderr=subprocess.PIPE)
    for _ in range(50):
        try:
            get("/api/status"); break
        except Exception:
            time.sleep(0.1)
    st = getj("/api/status")[1]
    check("after restart: port connected with no manual steps", st["port"]["state"] == "connected")
    c, j = post("/api/cut", {"id": ids["decal.svg"], "copies": 1})
    got = drain(master)
    check("after restart: cut works", c == 200 and len(got) == j["bytes"])
finally:
    srv.terminate()
    err = srv.stderr.read().decode()
    if "error on" in err or "Traceback" in err:
        print("SERVER STDERR:\n" + err); fails.append("server stderr")

print("\n%d failed: %s" % (len(fails), fails) if fails else "\nALL SERVER TESTS PASSED")
sys.exit(1 if fails else 0)

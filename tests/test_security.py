import os, sys, json, subprocess, tempfile, time, urllib.request, urllib.error
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import zombieplot as P
PORT = 8768; BASE = "http://127.0.0.1:%d" % PORT
fails = []
def check(n, c, d=""):
    print(("PASS " if c else "FAIL ") + n + ((" :: " + str(d)[:300]) if d and not c else ""))
    if not c: fails.append(n)
def req(method, path, body=None, headers=None):
    h = dict(headers or {})
    data = None if body is None else (body if isinstance(body, bytes) else json.dumps(body).encode())
    r = urllib.request.Request(BASE + path, data=data, method=method, headers=h)
    try:
        with urllib.request.urlopen(r, timeout=10) as resp:
            return resp.status, json.loads(resp.read() or b"{}"), resp.headers
    except urllib.error.HTTPError as e:
        raw = e.read()
        try: j = json.loads(raw)
        except ValueError: j = {}
        return e.code, j, e.headers
OK = {"X-ZombiePlot": "1"}

# unit: host check
for h in ("192.168.1.5", "10.134.2.43:80", "[fe80::1]:80", "zombieplot.local", "plotter", "localhost:8080", "box.lan", "box.home.arpa"):
    check("host allowed: " + h, P.host_allowed(h))
for h in ("evil.com", "attacker.example:80", "192.168.1.5.nip.io", "zombieplot.local.evil.com"):
    check("host blocked: " + h, not P.host_allowed(h))
os.environ["ZOMBIEPLOT_ALLOWED_HOSTS"] = "plotter.mydomain.net"
check("host allowed by setting", P.host_allowed("plotter.mydomain.net"))
del os.environ["ZOMBIEPLOT_ALLOWED_HOSTS"]
# unit: pin hashing
hsh = P.hash_pin("2468")
check("pin hash format", hsh.startswith("pbkdf2$20000$") and "2468" not in hsh)
check("pin verify", P.check_pin("2468", hsh) and not P.check_pin("2469", hsh) and not P.check_pin("2468", "garbage"))

tmp = tempfile.mkdtemp(); data = os.path.join(tmp, "d"); lib = os.path.join(tmp, "l"); os.makedirs(data); os.makedirs(lib)
open(os.path.join(lib, "a.svg"), "w").write('<svg xmlns="http://www.w3.org/2000/svg" width="1in" height="1in"><rect width="10" height="10"/></svg>')
json.dump({"port": "/dev/null", "link": "parallel"}, open(os.path.join(data, "config.json"), "w"))
srv = subprocess.Popen([sys.executable, os.path.join(ROOT, "zombieplot.py"), "--port", str(PORT), "--data", data, "--library", lib], stderr=subprocess.PIPE)
try:
    time.sleep(0.8)
    # cross-site requests
    c, j, _ = req("POST", "/api/session/new", {})
    check("POST without page header refused (cross-site protection)", c == 403 and "did not come from" in j["error"], (c, j))
    c, j, _ = req("POST", "/api/cut", b'{"id":"lib:a.svg"}', {"Content-Type": "text/plain"})
    check("text/plain form-style POST refused", c == 403)
    c, j, _ = req("POST", "/api/upload?name=x.svg", b"<svg/>")
    check("upload without header refused", c == 403)
    c, j, _ = req("GET", "/api/files", headers={"Host": "evil.com"})
    check("DNS-rebinding host refused", c == 403 and "unexpected name" in j["error"], (c, j))
    c, j, _ = req("GET", "/api/files", headers={"Host": "zombieplot.local"})
    check("normal .local host works", c == 200)
    # path traversal
    for bad in ("lib:../d/config.json", "lib:/etc/passwd", "up:../config.json"):
        c, j, _ = req("GET", "/api/preview?id=" + urllib.request.quote(bad))
        check("traversal refused: " + bad, c == 400)
    # settings cannot set or reveal the pin hash
    c, j, _ = req("POST", "/api/settings", {"pin_hash": P.hash_pin("1111")}, OK)
    c, j, _ = req("GET", "/api/files")
    check("pin_hash cannot be set through settings", c == 200)
    c, j, _ = req("GET", "/api/settings")
    check("settings never return the pin hash", "pin_hash" not in j["settings"] and j["pin_set"] is False)
    # PIN flow
    c, j, h = req("POST", "/api/pin", {"pin": "12"}, OK)
    check("too-short pin refused", c == 400)
    c, j, h = req("POST", "/api/pin", {"pin": "4321"}, OK)
    cookie = h.get("Set-Cookie", "")
    check("set pin returns a session cookie", c == 200 and j["pin_set"] and "HttpOnly" in cookie and "SameSite=Strict" in cookie, (c, j, cookie))
    tok = cookie.split(";")[0]
    c, j, _ = req("GET", "/api/files")
    check("locked without cookie", c == 401 and j.get("auth") is True)
    with urllib.request.urlopen(BASE + "/", timeout=10) as r:
        check("page itself still loads (shows unlock screen)", r.status == 200 and b"v-login" in r.read())
    c, j, _ = req("GET", "/api/files", headers={"Cookie": tok})
    check("cookie unlocks", c == 200)
    c, j, _ = req("POST", "/api/cut", {"id": "lib:a.svg"}, OK)
    check("cut refused without cookie", c == 401)
    c, j, _ = req("POST", "/api/update/install", {}, OK)
    check("update refused without cookie", c == 401)
    c, j, _ = req("GET", "/api/status")
    check("local status still answers (used by installer and updater)", c == 200)
    c, j, _ = req("POST", "/api/login", {"pin": "0000"}, OK)
    check("wrong pin", c == 401 and "Wrong" in j["error"])
    for i in range(4): req("POST", "/api/login", {"pin": "0000"}, OK)
    c, j, _ = req("POST", "/api/login", {"pin": "4321"}, OK)
    check("locked out after 5 wrong tries, even with the right pin", c == 429 and "Wait" in j["error"], (c, j))
    data_cfg = json.load(open(os.path.join(data, "config.json")))
    check("pin stored only as a hash", "4321" not in json.dumps(data_cfg) and data_cfg["pin_hash"].startswith("pbkdf2$"))
    sess = open(os.path.join(data, "sessions.json")).read()
    check("session tokens stored only as hashes", tok.split("=", 1)[1] not in sess)
    # restart keeps sessions
    srv.terminate(); srv.wait(5)
    srv = subprocess.Popen([sys.executable, os.path.join(ROOT, "zombieplot.py"), "--port", str(PORT), "--data", data, "--library", lib], stderr=subprocess.PIPE)
    time.sleep(0.8)
    c, j, _ = req("GET", "/api/files", headers={"Cookie": tok})
    check("session survives restart (updates do not log you out)", c == 200)
    c, j, _ = req("POST", "/api/login", {"pin": "4321"}, OK)
    check("login with right pin after restart", c == 200)
    # remove pin from the command line
    srv.terminate(); srv.wait(5)
    out = subprocess.run([sys.executable, os.path.join(ROOT, "zombieplot.py"), "--clear-pin", "--data", data], capture_output=True, text=True)
    srv = subprocess.Popen([sys.executable, os.path.join(ROOT, "zombieplot.py"), "--port", str(PORT), "--data", data, "--library", lib], stderr=subprocess.PIPE)
    time.sleep(0.8)
    c, j, _ = req("GET", "/api/files")
    check("--clear-pin unlocks", out.returncode == 0 and c == 200, out.stdout + out.stderr)
finally:
    srv.terminate()
    err = srv.stderr.read().decode()
    if "Traceback" in err: print(err); fails.append("stderr")

# root-written status file never follows a planted link
d2 = tempfile.mkdtemp(); target = os.path.join(d2, "victim.txt"); open(target, "w").write("keep me")
st = P.UpdateState(d2)
os.symlink(target, os.path.join(d2, "update-status.json.tmp"))
st.save(state="ok")
check("status write ignores planted .tmp link", open(target).read() == "keep me")
os.remove(os.path.join(d2, "update-status.json")); os.symlink(target, os.path.join(d2, "update-status.json"))
st.save(state="ok2")
check("status write replaces a planted link instead of writing through it", open(target).read() == "keep me" and not os.path.islink(os.path.join(d2, "update-status.json")))
print("\n%d failed: %s" % (len(fails), fails) if fails else "\nALL SECURITY TESTS PASSED")
sys.exit(1 if fails else 0)

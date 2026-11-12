import os, sys, json, tempfile, threading, time
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from functools import partial
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
fails = []
def check(n, c, d=""):
    print(("PASS " if c else "FAIL ") + n + ((" :: " + str(d)[:300]) if d and not c else ""))
    if not c: fails.append(n)

# A local stand-in for raw.githubusercontent.com
web = tempfile.mkdtemp()
os.makedirs(os.path.join(web, "me", "repo", "main"))
class Quiet(SimpleHTTPRequestHandler):
    def log_message(self, *a): pass
srv = ThreadingHTTPServer(("127.0.0.1", 0), partial(Quiet, directory=web))
threading.Thread(target=srv.serve_forever, daemon=True).start()
os.environ["ZOMBIEPLOT_RAW_BASE"] = "http://127.0.0.1:%d" % srv.server_address[1]
os.environ["ZOMBIEPLOT_REPO"] = "me/repo"
import zombieplot as P
def latest(obj):
    with open(os.path.join(web, "me", "repo", "main", "latest.json"), "w") as f:
        f.write(obj if isinstance(obj, str) else json.dumps(obj))

check("version order", P.parse_version("1.10") > P.parse_version("1.9") > P.parse_version("1.2.3") > P.parse_version("1.2"))
check("odd version text", P.parse_version("junk") == (0,))
latest({"version": "9.1", "tag": "v9.1", "notes": "x"})
check("reads latest.json", P.latest_release() == {"version": "9.1", "tag": "v9.1", "notes": "x"})
latest({"version": "9.2", "ref": "94e8878f0c3c1d2e4b5a69788796a5b4c3d2e1f0"})
check("ref (commit) preferred over tag", P.latest_release()["tag"] == "94e8878f0c3c1d2e4b5a69788796a5b4c3d2e1f0")
for bad in ('{"version": "9.1; rm -rf", "tag": "v9"}', '{"version": "9.1", "tag": "../../etc"}', '[1,2]', 'not json'):
    latest(bad)
    try:
        P.latest_release(); check("rejects bad release info " + bad[:20], False)
    except P.UpdateError:
        check("rejects bad release info " + bad[:20], True)
os.remove(os.path.join(web, "me", "repo", "main", "latest.json"))
try:
    P.latest_release(); check("missing release info is an error", False)
except P.UpdateError as e:
    check("missing release info is an error", "Could not reach GitHub" in str(e))

data = tempfile.mkdtemp(); lib = os.path.join(data, "lib"); os.makedirs(lib)
app = P.App(data, lib)
latest({"version": "9.1", "tag": "v9.1"})
info = app.update_check()
check("check marks update available", info["available"] and info["latest"] == "9.1" and info["installed"] == P.VERSION)
info = app.update_request("install")
check("install request file written", os.path.exists(os.path.join(data, "update-request")) and open(os.path.join(data, "update-request")).read() == "install")
try:
    app.update_request("install"); check("duplicate request refused", False)
except P.UserError:
    check("duplicate request refused", True)
os.remove(os.path.join(data, "update-request"))
latest({"version": "0.1", "tag": "v0.1"})
app.update_check()
try:
    app.update_request("install"); check("no install when already newest", False)
except P.UserError as e:
    check("no install when already newest", "up to date" in str(e))
try:
    app.update_request("rollback"); check("no rollback without a previous version", P.previous_version() is not None)
except P.UserError:
    check("no rollback without a previous version", True)

# updater side: action read from request file and consumed; auto off does nothing
open(os.path.join(data, "update-request"), "w").write("nonsense")
check("unknown request ignored and consumed", P.run_updater(data) == 0 and not os.path.exists(os.path.join(data, "update-request")))
check("auto off does nothing", P.run_updater(data, auto=True) == 0)
latest({"version": "0.1", "tag": "v0.1"})
open(os.path.join(data, "update-request"), "w").write("install")
check("install when already newest reports current", P.run_updater(data) == 0 and P.UpdateState(data).load()["state"] == "current")
print("\n%d failed: %s" % (len(fails), fails) if fails else "\nALL UPDATER TESTS PASSED")
srv.shutdown()
sys.exit(1 if fails else 0)

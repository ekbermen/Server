#!/usr/bin/env python3
"""
Notlio Runner - Piston uyumlu, tek dosyalik kod calistirma sunucusu.
Bagimlilik yok (sadece Python 3.9+ standart kutuphanesi).

Uc noktalar:
  GET  /                    saglik kontrolu + yuklu diller (anahtar gerekmez)
  GET  /api/v2/runtimes     yuklu diller
  POST /api/v2/execute      kodu calistir  (anahtar gerekir)

Anahtar: ?key=... veya X-Api-Key veya Authorization: Bearer ...
"""
import hmac
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

PORT = int(os.environ.get("PORT", "10000"))
KEY = os.environ.get("RUNNER_KEY", "")
RUN_TIMEOUT = float(os.environ.get("RUN_TIMEOUT", "10"))
COMPILE_TIMEOUT = float(os.environ.get("COMPILE_TIMEOUT", "40"))
MAX_OUT = int(os.environ.get("MAX_OUTPUT", str(256 * 1024)))
MAX_BODY = 1024 * 1024
MAX_PARALLEL = int(os.environ.get("MAX_PARALLEL", "2"))
QUEUE_WAIT = float(os.environ.get("QUEUE_WAIT", "8"))
RUN_UID = int(os.environ.get("RUN_UID", "10001"))
RUN_GID = int(os.environ.get("RUN_GID", "10001"))
BASE = os.environ.get("WORK_DIR", "/tmp/notlio-runs")
GOCACHE = os.path.join(BASE, "_gocache")

SEM = threading.BoundedSemaphore(MAX_PARALLEL)
IS_ROOT = hasattr(os, "geteuid") and os.geteuid() == 0

# {src} kaynak dosya, {out} derlenmis cikti, {cls} Java sinif adi
# as_mb: bellek (adres alani) siniri; None = JVM/Go/Node gibi sanal bellek ayiranlar icin koymuyoruz
LANGS = {
    "python": dict(aliases=["py", "python3"], ext="py", need="python3", run=["python3", "-u", "{src}"], ver=["python3", "--version"], as_mb=1024),
    "javascript": dict(aliases=["js", "node", "nodejs"], ext="js", need="node", run=["node", "--max-old-space-size=256", "{src}"], ver=["node", "--version"], as_mb=None),
    "typescript": dict(aliases=["ts"], ext="ts", need="tsx", run=["tsx", "{src}"], ver=["tsx", "--version"], as_mb=None),
    "c": dict(aliases=["gcc"], ext="c", need="gcc", compile=["gcc", "-O1", "-o", "{out}", "{src}", "-lm"], run=["./{out}"], ver=["gcc", "--version"], as_mb=1024),
    "c++": dict(aliases=["cpp", "cplusplus", "g++"], ext="cpp", need="g++", compile=["g++", "-O1", "-std=c++17", "-o", "{out}", "{src}"], run=["./{out}"], ver=["g++", "--version"], as_mb=1024),
    "java": dict(aliases=[], ext="java", need="javac", compile=["javac", "{src}"], run=["java", "-Xmx256m", "-Xss16m", "-cp", ".", "{cls}"], ver=["javac", "-version"], as_mb=None),
    "kotlin": dict(aliases=["kt"], ext="kt", need="kotlinc", compile=["kotlinc", "{src}", "-include-runtime", "-d", "main.jar"], run=["java", "-Xmx256m", "-jar", "main.jar"], ver=["kotlinc", "-version"], as_mb=None),
    "csharp": dict(aliases=["cs", "c#", "mono", "dotnet"], ext="cs", need="mcs", compile=["mcs", "-out:main.exe", "{src}"], run=["mono", "main.exe"], ver=["mcs", "--version"], as_mb=None),
    "go": dict(aliases=["golang"], ext="go", need="go", run=["go", "run", "{src}"], ver=["go", "version"], as_mb=None),
    "rust": dict(aliases=["rs"], ext="rs", need="rustc", compile=["rustc", "-C", "opt-level=1", "-o", "{out}", "{src}"], run=["./{out}"], ver=["rustc", "--version"], as_mb=None),
    "php": dict(aliases=[], ext="php", need="php", run=["php", "{src}"], ver=["php", "--version"], as_mb=1024),
    "ruby": dict(aliases=["rb"], ext="rb", need="ruby", run=["ruby", "{src}"], ver=["ruby", "--version"], as_mb=1024),
    "perl": dict(aliases=["pl"], ext="pl", need="perl", run=["perl", "{src}"], ver=["perl", "--version"], as_mb=1024),
    "lua": dict(aliases=[], ext="lua", need="lua5.4", run=["lua5.4", "{src}"], ver=["lua5.4", "-v"], as_mb=1024),
    "bash": dict(aliases=["sh", "shell"], ext="sh", need="bash", run=["bash", "{src}"], ver=["bash", "--version"], as_mb=1024),
    "sqlite3": dict(aliases=["sql", "sqlite"], ext="sql", need="sqlite3", run=["sqlite3", "-header", "-column", ":memory:", ".read {src}"], ver=["sqlite3", "--version"], as_mb=1024),
    "dart": dict(aliases=[], ext="dart", need="dart", run=["dart", "{src}"], ver=["dart", "--version"], as_mb=None),
    "swift": dict(aliases=[], ext="swift", need="swiftc", compile=["swiftc", "-o", "{out}", "{src}"], run=["./{out}"], ver=["swiftc", "--version"], as_mb=None),
}

ALIAS = {}
for _name, _spec in LANGS.items():
    ALIAS[_name] = _name
    for _a in _spec["aliases"]:
        ALIAS[_a] = _name

_ver_cache = {}


def available(name):
    return shutil.which(LANGS[name]["need"]) is not None


def version_of(name):
    if name in _ver_cache:
        return _ver_cache[name]
    v = "?"
    try:
        r = subprocess.run(LANGS[name]["ver"], capture_output=True, text=True, timeout=8)
        txt = (r.stdout + " " + r.stderr).strip().split("\n")[0]
        m = re.search(r"\d+(?:\.\d+)+", txt)
        v = m.group(0) if m else txt[:30]
    except Exception:
        pass
    _ver_cache[name] = v
    return v


def runtimes():
    out = []
    for name, spec in LANGS.items():
        if available(name):
            out.append({"language": name, "version": version_of(name), "aliases": spec["aliases"]})
    return out


def clean_name(n):
    n = os.path.basename(str(n or "")).strip()
    n = re.sub(r"[^A-Za-z0-9._-]", "_", n)
    return n[:80] or "main"


def read_capped(stream, bucket):
    """Akisi okur, MAX_OUT'tan fazlasini atar (pipe dolup kilitlenmesin diye okumaya devam eder)."""
    size = 0
    try:
        while True:
            chunk = stream.read(8192)
            if not chunk:
                break
            if size < MAX_OUT:
                take = chunk[: MAX_OUT - size]
                bucket.append(take)
                size += len(take)
    except Exception:
        pass


def run_cmd(cmd, cwd, stdin_text, timeout, env, as_mb):
    """Komutu sinirlarla calistirir. (stdout, stderr, kod, sinyal, sure_asimi) dondurur."""
    limits = ["prlimit", "--core=0", "--nofile=512", "--nproc=1024", "--fsize=%d" % (20 * 1024 * 1024), "--cpu=%d" % int(timeout + 5)]
    if as_mb:
        limits.append("--as=%d" % (as_mb * 1024 * 1024))
    full = (limits + ["--"] + cmd) if shutil.which("prlimit") else cmd
    kwargs = {}
    if IS_ROOT:
        kwargs.update(user=RUN_UID, group=RUN_GID, extra_groups=[])
    p = subprocess.Popen(
        full,
        cwd=cwd,
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
        **kwargs,
    )
    out_b, err_b = [], []
    t1 = threading.Thread(target=read_capped, args=(p.stdout, out_b), daemon=True)
    t2 = threading.Thread(target=read_capped, args=(p.stderr, err_b), daemon=True)
    t1.start()
    t2.start()

    def feed():
        try:
            if stdin_text:
                p.stdin.write(stdin_text.encode("utf-8", "replace")[:MAX_BODY])
            p.stdin.close()
        except Exception:
            pass

    t3 = threading.Thread(target=feed, daemon=True)
    t3.start()
    timed_out = False
    try:
        p.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        try:
            os.killpg(p.pid, signal.SIGKILL)
        except Exception:
            p.kill()
        try:
            p.wait(timeout=3)
        except Exception:
            pass
    # artik surecler (cocuklar) kalmasin
    try:
        os.killpg(p.pid, signal.SIGKILL)
    except Exception:
        pass
    t1.join(timeout=2)
    t2.join(timeout=2)
    code = p.returncode
    sig = None
    if code is not None and code < 0:
        try:
            sig = signal.Signals(-code).name
        except Exception:
            sig = "SIG%d" % (-code)
    return (
        b"".join(out_b).decode("utf-8", "replace"),
        b"".join(err_b).decode("utf-8", "replace"),
        code,
        sig,
        timed_out,
    )


def java_class(src):
    m = re.search(r"public\s+(?:final\s+|abstract\s+)*(?:class|interface|enum|record)\s+(\w+)", src)
    if m:
        return m.group(1)
    for m in re.finditer(r"class\s+(\w+)", src):
        return m.group(1)
    return "Main"


def execute(payload):
    lang_in = str(payload.get("language", "")).lower().strip()
    name = ALIAS.get(lang_in)
    if not name:
        return 400, {"message": "Bilinmeyen dil: %s" % lang_in}
    if not available(name):
        return 400, {"message": "%s bu sunucuda kurulu degil" % name}
    spec = LANGS[name]
    files = payload.get("files") or []
    if not files or not isinstance(files, list):
        return 400, {"message": "files bos"}
    total = sum(len(str((f or {}).get("content", ""))) for f in files)
    if total > 200000:
        return 400, {"message": "Kod cok buyuk (en fazla 200 KB)"}
    stdin_text = str(payload.get("stdin", "") or "")

    if not SEM.acquire(timeout=QUEUE_WAIT):
        return 503, {"message": "Sunucu mesgul, birazdan tekrar dene"}
    work = os.path.join(BASE, uuid.uuid4().hex)
    try:
        os.makedirs(work, exist_ok=True)
        main_src = None
        cls = "Main"
        for i, f in enumerate(files):
            content = str((f or {}).get("content", ""))
            fname = clean_name((f or {}).get("name") or ("main." + spec["ext"]))
            if i == 0:
                if name == "java":
                    cls = java_class(content)
                    fname = cls + ".java"
                elif "." not in fname:
                    fname = fname + "." + spec["ext"]
                main_src = fname
            with open(os.path.join(work, fname), "w", encoding="utf-8") as fh:
                fh.write(content)
        if IS_ROOT:
            for root, dirs, fs in os.walk(work):
                os.chown(root, RUN_UID, RUN_GID)
                for x in fs:
                    os.chown(os.path.join(root, x), RUN_UID, RUN_GID)

        env = {
            "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
            "HOME": work,
            "TMPDIR": work,
            "LANG": "C.UTF-8",
            "GOCACHE": GOCACHE,
            "GOPATH": os.path.join(work, "gopath"),
            "CGO_ENABLED": "0",
            "GO111MODULE": "off",
            "NODE_OPTIONS": "--max-old-space-size=256",
            "DOTNET_CLI_TELEMETRY_OPTOUT": "1",
            "DART_SUPPRESS_ANALYTICS": "true",
        }

        def fill(cmd):
            return [c.replace("{src}", main_src).replace("{out}", "main.out").replace("{cls}", cls) for c in cmd]

        result = {"language": name, "version": version_of(name)}
        if spec.get("compile"):
            so, se, code, sig, to = run_cmd(fill(spec["compile"]), work, "", COMPILE_TIMEOUT, env, None)
            result["compile"] = {"stdout": so, "stderr": se, "output": so + se, "code": code if code is not None else 1, "signal": sig}
            if to:
                result["compile"]["stderr"] += "\nDerleme zaman asimi (%d sn)" % COMPILE_TIMEOUT
                result["compile"]["output"] = result["compile"]["stderr"]
                result["compile"]["code"] = 1
            if result["compile"]["code"] != 0:
                result["run"] = {"stdout": "", "stderr": "", "output": "", "code": None, "signal": None}
                return 200, result

        rt = float(payload.get("run_timeout") or 0) / 1000.0 if payload.get("run_timeout") else RUN_TIMEOUT
        rt = max(1.0, min(rt, RUN_TIMEOUT))
        so, se, code, sig, to = run_cmd(fill(spec["run"]), work, stdin_text, rt, env, spec.get("as_mb"))
        if to:
            se += ("\n" if se else "") + "Zaman asimi: program %d saniyede bitmedi." % int(rt)
        elif sig == "SIGKILL":
            se += ("\n" if se else "") + "Program sonlandirildi (bellek/CPU siniri asilmis olabilir)."
        result["run"] = {"stdout": so, "stderr": se, "output": so + se, "code": code, "signal": sig}
        return 200, result
    except Exception as e:  # beklenmeyen hata
        return 500, {"message": "Sunucu hatasi: %s" % e}
    finally:
        SEM.release()
        shutil.rmtree(work, ignore_errors=True)


class Handler(BaseHTTPRequestHandler):
    server_version = "NotlioRunner/1.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def _send(self, status, obj):
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(data)

    def _authorized(self, query):
        if not KEY:
            return True
        got = ""
        if "key" in query:
            got = query["key"][0]
        elif self.headers.get("X-Api-Key"):
            got = self.headers.get("X-Api-Key")
        elif (self.headers.get("Authorization") or "").startswith("Bearer "):
            got = self.headers.get("Authorization")[7:]
        return hmac.compare_digest(got.encode(), KEY.encode())

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Api-Key, Authorization")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        if u.path in ("/", "/health", "/healthz"):
            self._send(200, {"ok": True, "name": "notlio-runner", "languages": [r["language"] for r in runtimes()], "auth": bool(KEY)})
        elif u.path in ("/api/v2/runtimes", "/api/v2/piston/runtimes"):
            if not self._authorized(q):
                return self._send(401, {"message": "Gecersiz anahtar"})
            self._send(200, runtimes())
        else:
            self._send(404, {"message": "Bulunamadi"})

    def do_POST(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        if u.path not in ("/api/v2/execute", "/api/v2/piston/execute", "/execute"):
            return self._send(404, {"message": "Bulunamadi"})
        if not self._authorized(q):
            return self._send(401, {"message": "Gecersiz anahtar (adresin sonuna ?key=... ekle)"})
        try:
            n = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            n = 0
        if n <= 0 or n > MAX_BODY:
            return self._send(413 if n > MAX_BODY else 400, {"message": "Istek govdesi gecersiz veya cok buyuk"})
        try:
            payload = json.loads(self.rfile.read(n).decode("utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("json nesnesi degil")
        except Exception:
            return self._send(400, {"message": "Gecersiz JSON"})
        status, body = execute(payload)
        self._send(status, body)


def janitor():
    try:
        now = time.time()
        for d in os.listdir(BASE):
            p = os.path.join(BASE, d)
            if d != "_gocache" and os.path.isdir(p) and now - os.path.getmtime(p) > 600:
                shutil.rmtree(p, ignore_errors=True)
    except Exception:
        pass


def main():
    os.makedirs(BASE, exist_ok=True)
    os.makedirs(GOCACHE, exist_ok=True)
    os.chmod(BASE, 0o755)
    if IS_ROOT:
        os.chown(GOCACHE, RUN_UID, RUN_GID)
    janitor()
    if not KEY:
        print("UYARI: RUNNER_KEY tanimli degil, sunucu herkese acik!", file=sys.stderr)
    print("Notlio Runner :%d  | diller: %s" % (PORT, ", ".join(r["language"] for r in runtimes())), flush=True)
    ThreadingHTTPServer.daemon_threads = True
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()

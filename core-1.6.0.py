#!/usr/bin/env python3
"""Syntax Academy runtime: the native window, saving, the code runner, the AI bridge and auto-updates.
Started by the stable launcher stub (/usr/bin/syntax-academy), and itself updatable through the signed feed."""
import base64
import fcntl
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

# GPU rendering is the default: the whole Frutiger Aero sky is a WebGL shader. "Safe graphics" turns
# off WebKit's DMA-BUF renderer for the rare GPU/driver combos that show a blank window.
SAFE_GFX = "--safe-graphics" in sys.argv or os.environ.get("SYNTAX_ACADEMY_SAFE_GRAPHICS") == "1"
if "--safe-graphics" in sys.argv:
    sys.argv.remove("--safe-graphics")
if SAFE_GFX:
    os.environ.setdefault("WEBKIT_DISABLE_DMABUF_RENDERER", "1")

APP_NAME = "syntax-academy"
APP_ID = "io.github.SyntaxAcademy"
APP_DIR = os.environ.get("SYNTAX_ACADEMY_DIR", "/usr/share/syntax-academy")
DEBUG = bool(os.environ.get("SYNTAX_ACADEMY_DEBUG"))


def _user_dir(env, fallback):
    return os.path.join(os.environ.get(env) or os.path.expanduser(fallback), APP_NAME)


DATA_DIR = _user_dir("XDG_DATA_HOME", "~/.local/share")
CACHE_DIR = _user_dir("XDG_CACHE_HOME", "~/.cache")
STATE_FILE = os.path.join(DATA_DIR, "state.json")
WINDOW_FILE = os.path.join(DATA_DIR, "window.json")
# Written when GPU drawing froze on this computer: that version then starts in safe graphics.
GFX_FALLBACK_FILE = os.path.join(DATA_DIR, "gfx-fallback.json")
# The page may only reach the optional AI providers: Claude's API or a local Ollama.
HTTP_HOSTS = {"api.anthropic.com", "localhost", "127.0.0.1", "::1"}
UPDATE_DIR = os.path.join(DATA_DIR, "updates")
CURRENT_FILE = os.path.join(UPDATE_DIR, "current.json")
CHECK_FILE = os.path.join(UPDATE_DIR, "last-check.json")
# Written at build time: this package's version, the update feed and the release public key.
UPDATE_CONF = {}
MAX_MANIFEST, MAX_APP = 64 * 1024, 60 * 1024 * 1024
CHECK_EVERY = 3600
SAVE_DIRS = {"pictures": "XDG_PICTURES_DIR", "documents": "XDG_DOCUMENTS_DIR"}


def read_json(path, fallback):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return fallback


def write_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f)
    os.replace(tmp, path)


def vtuple(v):
    try:
        return tuple(int(x) for x in str(v).split("."))
    except ValueError:
        return (0,)


# =====================================================================================
# The code runner. Every program runs in a throwaway folder inside a bubblewrap sandbox
# (no network, no access to your home folder, its own process tree), inside a systemd
# scope that caps memory and process count, with time and output limits. That matters
# because in pair labs the code you run may have been typed by someone else.
# =====================================================================================
RUN_ROOT = os.path.join(CACHE_DIR, "runs")
TOOL_CACHE = os.path.join(CACHE_DIR, "toolchains")  # build caches (Go, Zig, Nim...) that survive between runs
OUT_CAP = 256 * 1024
SANDBOX_WORK, SANDBOX_CACHE = "/tmp/work", "/tmp/cache"

# id: file name, optional build step, run step, the program that must exist, Arch packages, extra env.
# {cache} is the persistent per-language cache folder inside the sandbox.
LANGS = {
    "python": {"file": "main.py", "run": ["python3", "-u", "main.py"], "need": "python3", "pkgs": ["python"], "ver": ["python3", "--version"]},
    "javascript": {"file": "main.js", "run": ["node", "main.js"], "need": "node", "pkgs": ["nodejs"], "ver": ["node", "--version"]},
    # slow: Deno type-checks the whole program on every run, which can take several seconds on low-end laptops.
    "typescript": {"file": "main.ts", "run": ["deno", "run", "--check", "--quiet", "--no-prompt", "--no-config", "main.ts"], "need": "deno", "pkgs": ["deno"], "slow": True,
                   "env": {"DENO_DIR": "{cache}", "DENO_NO_UPDATE_CHECK": "1", "NO_COLOR": "1"}, "ver": ["deno", "--version"],
                   "alt": {"file": "main.ts", "run": ["node", "--no-warnings", "main.ts"], "need": "node"}},
    "java": {"file": "Main.java", "run": ["java", "-Xss8m", "-XX:+UseSerialGC", "-XX:TieredStopAtLevel=1", "-Xshare:auto", "Main.java"], "need": "java", "pkgs": ["jdk-openjdk"], "ver": ["java", "--version"]},
    "kotlin": {"file": "main.kt", "build": ["kotlinc", "-nowarn", "main.kt", "-include-runtime", "-d", "main.jar"], "run": ["java", "-Xss8m", "-XX:+UseSerialGC", "-jar", "main.jar"],
               "need": "kotlinc", "pkgs": ["kotlin", "jdk-openjdk"], "ver": ["kotlinc", "-version"], "slow": True},
    "c": {"file": "main.c", "build": ["gcc", "-std=c17", "-O1", "-Wall", "-fdiagnostics-color=never", "-o", "main", "main.c", "-lm"], "run": ["./main"], "need": "gcc", "pkgs": ["gcc"], "ver": ["gcc", "--version"]},
    "cpp": {"file": "main.cpp", "build": ["g++", "-std=c++20", "-O1", "-Wall", "-fdiagnostics-color=never", "-o", "main", "main.cpp"], "run": ["./main"], "need": "g++", "pkgs": ["gcc"], "ver": ["g++", "--version"]},
    "csharp": {"file": "main.cs", "run": ["dotnet", "run", "main.cs"], "need": "dotnet", "pkgs": ["dotnet-sdk"], "slow": True,
               "env": {"DOTNET_CLI_HOME": "{cache}", "NUGET_PACKAGES": "{cache}/nuget", "DOTNET_NOLOGO": "1", "DOTNET_CLI_TELEMETRY_OPTOUT": "1", "DOTNET_SKIP_FIRST_TIME_EXPERIENCE": "1"}, "ver": ["dotnet", "--version"]},
    "fsharp": {"file": "main.fsx", "run": ["dotnet", "fsi", "--quiet", "--nologo", "main.fsx"], "need": "dotnet", "pkgs": ["dotnet-sdk"], "slow": True,
               "env": {"DOTNET_CLI_HOME": "{cache}", "NUGET_PACKAGES": "{cache}/nuget", "DOTNET_NOLOGO": "1", "DOTNET_CLI_TELEMETRY_OPTOUT": "1"}, "ver": ["dotnet", "--version"]},
    "go": {"file": "main.go", "run": ["go", "run", "main.go"], "need": "go", "pkgs": ["go"], "env": {"GOCACHE": "{cache}/build", "GOPATH": "{cache}/path", "GOFLAGS": "-mod=mod", "GOTOOLCHAIN": "local", "GO111MODULE": "off"}, "ver": ["go", "version"], "slow": True},
    "rust": {"file": "main.rs", "build": ["rustc", "--edition", "2021", "-C", "opt-level=0", "-C", "debuginfo=0", "--color", "never", "-o", "main", "main.rs"], "run": ["./main"], "need": "rustc", "pkgs": ["rust"], "ver": ["rustc", "--version"]},
    "swift": {"file": "main.swift", "build": ["swiftc", "-o", "main", "main.swift"], "run": ["./main"], "need": "swiftc", "pkgs": [], "ver": ["swiftc", "--version"]},
    "ruby": {"file": "main.rb", "run": ["ruby", "main.rb"], "need": "ruby", "pkgs": ["ruby"], "ver": ["ruby", "--version"]},
    "php": {"file": "main.php", "run": ["php", "-d", "display_errors=stderr", "main.php"], "need": "php", "pkgs": ["php"], "ver": ["php", "--version"]},
    "lua": {"file": "main.lua", "run": ["lua", "main.lua"], "need": "lua", "pkgs": ["lua"], "ver": ["lua", "-v"]},
    "perl": {"file": "main.pl", "run": ["perl", "main.pl"], "need": "perl", "pkgs": ["perl"], "ver": ["perl", "-e", "print \"Perl $^V\""]},
    "bash": {"file": "main.sh", "run": ["bash", "main.sh"], "need": "bash", "pkgs": ["bash"], "ver": ["bash", "--version"]},
    "sql": {"file": "main.sql", "run": ["sqlite3", "-batch", "-bail", ":memory:", ".read seed.sql", ".read main.sql"], "need": "sqlite3", "pkgs": ["sqlite"], "ver": ["sqlite3", "--version"]},
    "dart": {"file": "main.dart", "run": ["dart", "run", "--no-serve-devtools", "main.dart"], "need": "dart", "pkgs": ["dart"], "env": {"PUB_CACHE": "{cache}/pub", "DART_SUPPRESS_ANALYTICS": "true"}, "ver": ["dart", "--version"], "slow": True},
    "haskell": {"file": "main.hs", "build": ["ghc", "-dynamic", "-O0", "-v0", "-outputdir", "build", "-o", "main", "main.hs"], "run": ["./main"], "need": "ghc", "pkgs": ["ghc"], "ver": ["ghc", "--version"], "slow": True},
    "elixir": {"file": "main.exs", "run": ["elixir", "main.exs"], "need": "elixir", "pkgs": ["elixir"], "ver": ["elixir", "--short-version"]},
    "erlang": {"file": "main.erl", "run": ["escript", "main.erl"], "need": "escript", "pkgs": ["erlang-core"], "ver": ["erl", "-noshell", "-eval", "io:format(\"Erlang/OTP ~s\", [erlang:system_info(otp_release)]), halt()."]},
    "gleam": {"file": "src/main.gleam", "run": ["gleam", "run", "--no-print-progress"], "need": "gleam", "pkgs": ["gleam", "erlang-core"], "ver": ["gleam", "--version"], "template": "gleam"},
    "clojure": {"file": "main.clj", "run": ["clojure", "-M", "main.clj"], "need": "clojure", "pkgs": ["clojure", "jdk-openjdk"], "env": {"CLJ_CONFIG": "{cache}/config", "CLJ_CACHE": "{cache}/cpcache"}, "ver": ["clojure", "--version"], "slow": True, "mvn": True},
    "commonlisp": {"file": "main.lisp", "run": ["sbcl", "--script", "main.lisp"], "need": "sbcl", "pkgs": ["sbcl"], "ver": ["sbcl", "--version"]},
    "scheme": {"file": "main.scm", "run": ["guile", "--no-auto-compile", "-s", "main.scm"], "need": "guile", "pkgs": ["guile"], "ver": ["guile", "--version"]},
    "racket": {"file": "main.rkt", "run": ["racket", "main.rkt"], "need": "racket", "pkgs": ["racket"], "ver": ["racket", "--version"]},
    "ocaml": {"file": "main.ml", "run": ["ocaml", "main.ml"], "need": "ocaml", "pkgs": ["ocaml"], "ver": ["ocaml", "-version"]},
    "julia": {"file": "main.jl", "run": ["julia", "--startup-file=no", "--history-file=no", "-O0", "--compile=min", "main.jl"], "need": "julia", "pkgs": ["julia"], "env": {"JULIA_DEPOT_PATH": "{cache}"}, "ver": ["julia", "--version"], "slow": True},
    "r": {"file": "main.R", "run": ["Rscript", "--vanilla", "main.R"], "need": "Rscript", "pkgs": ["r"], "ver": ["Rscript", "--version"]},
    "zig": {"file": "main.zig", "run": ["zig", "run", "main.zig"], "need": "zig", "pkgs": ["zig"], "env": {"ZIG_GLOBAL_CACHE_DIR": "{cache}", "ZIG_LOCAL_CACHE_DIR": "{cache}/local"}, "ver": ["zig", "version"], "slow": True},
    "nim": {"file": "main.nim", "build": ["nim", "c", "--hints:off", "--verbosity:0", "--colors:off", "--passC:-w", "--nimcache:{cache}/nimcache", "-o:main", "main.nim"], "run": ["./main"], "lock": True, "need": "nim", "pkgs": ["nim"], "ver": ["nim", "--version"]},
    "crystal": {"file": "main.cr", "build": ["crystal", "build", "--no-color", "-o", "main", "main.cr"], "run": ["./main"], "lock": True, "need": "crystal", "pkgs": ["crystal"], "env": {"CRYSTAL_CACHE_DIR": "{cache}"}, "ver": ["crystal", "--version"], "slow": True},
    "d": {"file": "main.d", "build": ["dmd", "-of=main", "main.d"], "run": ["./main"], "need": "dmd", "pkgs": ["dmd"], "ver": ["dmd", "--version"]},
    "fortran": {"file": "main.f90", "build": ["gfortran", "-fdiagnostics-color=never", "-o", "main", "main.f90"], "run": ["./main"], "need": "gfortran", "pkgs": ["gcc-fortran"], "ver": ["gfortran", "--version"]},
    "cobol": {"file": "main.cob", "build": ["gcobol", "-ffree-form", "-rdynamic", "-o", "main", "main.cob"], "run": ["./main"], "need": "gcobol", "pkgs": ["gcc-gcobol"], "ver": ["gcobol", "--version"]},
    "pascal": {"file": "main.pas", "build": ["fpc", "-v0", "-omain", "main.pas"], "run": ["./main"], "need": "fpc", "pkgs": ["fpc"], "ver": ["fpc", "-iV"]},
    "ada": {"file": "main.adb", "build": ["gnatmake", "-q", "main.adb"], "run": ["./main"], "need": "gnatmake", "pkgs": ["gcc-ada"], "ver": ["gnatmake", "--version"]},
    "asm": {"file": "main.asm", "build": ["sh", "-c", "nasm -f elf64 main.asm -o main.o && ld -o main main.o"], "run": ["./main"], "need": "nasm", "pkgs": ["nasm"], "ver": ["nasm", "-v"]},
    "prolog": {"file": "main.pl", "run": ["swipl", "-q", "main.pl"], "need": "swipl", "pkgs": ["swi-prolog"], "ver": ["swipl", "--version"]},
    "groovy": {"file": "main.groovy", "run": ["groovy", "main.groovy"], "need": "groovy", "pkgs": ["groovy", "jdk-openjdk"], "ver": ["groovy", "--version"], "slow": True},
    "octave": {"file": "main.m", "run": ["octave-cli", "--no-gui", "--quiet", "--norc", "--no-history", "main.m"], "need": "octave-cli", "pkgs": ["octave"], "ver": ["octave-cli", "--version"]},
    "tcl": {"file": "main.tcl", "run": ["tclsh", "main.tcl"], "need": "tclsh", "pkgs": ["tcl"], "ver": ["sh", "-c", "echo 'puts [info patchlevel]' | tclsh"]},
    "awk": {"file": "main.awk", "run": ["awk", "-f", "main.awk"], "need": "awk", "pkgs": ["gawk"], "ver": ["awk", "--version"]},
    "odin": {"file": "main.odin", "run": ["odin", "run", "main.odin", "-file"], "need": "odin", "pkgs": ["odin"], "ver": ["odin", "version"]},
}
# Packages pacman may install from the app: only ones named in LANGS.
ALLOWED_PKGS = {p for spec in LANGS.values() for p in spec["pkgs"]}
_versions = {}
_install_lock = threading.Lock()


def lang_spec(lang):
    """The runner spec to use for a language, or None. Falls back to an alternative runtime (TypeScript on Node)."""
    spec = LANGS.get(lang)
    if not spec:
        return None
    if shutil.which(spec["need"]):
        return spec
    alt = spec.get("alt")
    if alt and shutil.which(alt["need"]):
        return {**spec, **alt}
    return None


def toolchains():
    """Which languages can run on this computer, and how to get the others."""
    out = {}
    for lang, spec in LANGS.items():
        s = lang_spec(lang)
        out[lang] = {"ok": bool(s), "via": s["need"] if s else "", "pkgs": spec["pkgs"], "version": _versions.get(lang, ""), "slow": bool(spec.get("slow"))}
    return {"langs": out, "sandbox": bool(shutil.which("bwrap")), "scope": bool(shutil.which("systemd-run")), "pacman": bool(shutil.which("pacman")), "pkexec": bool(shutil.which("pkexec"))}


_gpu = None


def gpu_name():
    """The graphics card's name, e.g. "Intel HD Graphics 520". WebKit hides it from WebGL for privacy."""
    global _gpu
    if _gpu is not None:
        return _gpu
    _gpu = ""
    try:
        out = subprocess.run(["lspci", "-mm"], capture_output=True, text=True, timeout=5).stdout
        for line in out.splitlines():
            parts = re.findall(r'"([^"]*)"', line)
            if len(parts) >= 3 and re.search(r"VGA|3D|Display", parts[0]):
                vendor = re.sub(r"\b(Corporation|Corp\.|Inc\.|Technologies|Co\., Ltd\.)", "", parts[1].split("[")[0]).strip()
                model = (re.search(r"\[([^\]]+)\]", parts[2]) or [None, parts[2]])[1].strip()
                _gpu = model if model.lower().startswith(vendor.lower().split()[0]) else f"{vendor} {model}"
                if "Intel" not in _gpu:  # prefer a discrete card over the integrated one
                    break
    except (OSError, subprocess.SubprocessError):
        pass
    return _gpu


def lang_version(lang):
    s = lang_spec(lang)
    if not s:
        return ""
    if lang in _versions:
        return _versions[lang]
    try:
        r = subprocess.run(s["ver"], capture_output=True, text=True, timeout=30, env=dict(os.environ, NO_COLOR="1"))
        line = next((ln.strip() for ln in (r.stdout + "\n" + r.stderr).splitlines() if ln.strip()), "")
        _versions[lang] = line[:80]
    except (OSError, subprocess.SubprocessError):
        _versions[lang] = ""
    return _versions[lang]


def _sandbox_prefix(work, cache, env, net=False):
    """bubblewrap + systemd scope wrapper. Returns (argv prefix, sandboxed?)."""
    pre = []
    if shutil.which("systemd-run") and os.environ.get("SYNTAX_ACADEMY_NO_SCOPE") != "1":
        pre = ["systemd-run", "--user", "--scope", "--quiet", "--collect", "-p", "TasksMax=384", "-p", "MemoryMax=2G", "-p", "MemorySwapMax=0"]
    if not shutil.which("bwrap") or os.environ.get("SYNTAX_ACADEMY_NO_SANDBOX") == "1":
        return pre, False
    bw = ["bwrap", "--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc", "--tmpfs", "/tmp", "--tmpfs", "/home", "--tmpfs", "/run", "--tmpfs", "/var/tmp",
          "--bind", work, SANDBOX_WORK, "--bind", cache, SANDBOX_CACHE, "--chdir", SANDBOX_WORK,
          "--unshare-all", "--die-with-parent", "--new-session", "--clearenv"]
    if net:
        bw.append("--share-net")
    for k, v in env.items():
        bw += ["--setenv", k, v]
    return pre + bw + ["--"], True


ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(\x07|\x1b\\)")


def _waiting_for_input(root, pipe_ino):
    """True when a process started by this run is blocked reading our (still open) stdin pipe, which means
    the program has used up its input and is waiting for the user to type more."""
    parents = {}
    for d in os.listdir("/proc"):
        if d.isdigit():
            try:
                with open(f"/proc/{d}/stat") as f:
                    st = f.read()
                parents[int(d)] = int(st[st.rindex(")") + 2:].split()[1])
            except (OSError, ValueError, IndexError):
                pass
    tree, grew = {root}, True
    while grew:
        grew = False
        for pid, ppid in parents.items():
            if ppid in tree and pid not in tree:
                tree.add(pid)
                grew = True
    ours = f"pipe:[{pipe_ino}]"
    for pid in tree:
        try:
            for tid in os.listdir(f"/proc/{pid}/task"):
                with open(f"/proc/{pid}/task/{tid}/syscall") as f:
                    call = f.read().split()
                # syscall 0 is read() on x86-64 (63 on arm64); its first argument is the file descriptor,
                # which isn't always 0: .NET reads from a duplicate of stdin, for example.
                if len(call) > 1 and call[0] in ("0", "63") and os.readlink(f"/proc/{pid}/fd/{int(call[1], 16)}") == ours:
                    return True
        except (OSError, ValueError):
            pass
    return False


def _run_proc(argv, cwd, env, stdin, timeout, sandboxed, stop_on_wait=False):
    """Runs one step with a time limit and an output cap. Returns a dict. With stop_on_wait, stdin stays
    open after the given input, and the run stops (marked starved) as soon as the program waits for more."""
    t0 = time.time()
    try:
        p = subprocess.Popen(argv, cwd=cwd, env=None if sandboxed else env, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
    except OSError as e:
        return {"code": 127, "stdout": "", "stderr": str(e), "ms": 0, "timedOut": False, "truncated": False}
    bufs = {"stdout": bytearray(), "stderr": bytearray()}
    state = {"truncated": False}

    def pump(stream, key):
        while True:
            chunk = stream.read1(65536) if hasattr(stream, "read1") else stream.read(65536)
            if not chunk:
                break
            if len(bufs["stdout"]) + len(bufs["stderr"]) < OUT_CAP:
                bufs[key] += chunk
            elif not state["truncated"]:
                state["truncated"] = True
                _kill(p)

    threads = [threading.Thread(target=pump, args=(p.stdout, "stdout"), daemon=True), threading.Thread(target=pump, args=(p.stderr, "stderr"), daemon=True)]
    for t in threads:
        t.start()
    starved = False
    try:
        p.stdin.write((stdin or "").encode())
        p.stdin.flush()
        if not stop_on_wait:
            p.stdin.close()
    except (BrokenPipeError, OSError):
        pass
    timed_out = False
    try:
        if stop_on_wait:
            ino = os.fstat(p.stdin.fileno()).st_ino
            deadline = time.time() + timeout
            while True:
                try:
                    p.wait(timeout=0.04)
                    break
                except subprocess.TimeoutExpired:
                    if _waiting_for_input(p.pid, ino):
                        starved = True
                        time.sleep(0.05)  # let the last output reach our pipe
                        _kill(p)
                        p.wait()
                        break
                    if time.time() > deadline:
                        raise
        else:
            p.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        _kill(p)
        p.wait()
    finally:
        if stop_on_wait:
            try:
                p.stdin.close()
            except OSError:
                pass
    for t in threads:
        t.join(2)
    # Terminal colour codes mean nothing in the app's Output panel (some tools send them even with NO_COLOR).
    dec = lambda b: ANSI.sub("", bytes(b[:OUT_CAP]).decode("utf-8", "replace")).replace(SANDBOX_WORK + "/", "").replace(cwd + "/", "")
    # JVM launchers announce option variables on every run; that's noise for learners.
    err = re.sub(r"^(NOTE: )?Picked up (_JAVA_OPTIONS|JAVA_TOOL_OPTIONS|JDK_JAVA_OPTIONS):.*\n?", "", dec(bufs["stderr"]), flags=re.M)
    # Deno's advice after a type error ("re-run with --no-check") is for terminals, not for learners here.
    err = re.sub(r"^\s*(info: The program failed type-checking, but it still might work correctly\.|hint: Re-run with --no-check to skip type-checking\.)\n?", "", err, flags=re.M)
    out = dec(bufs["stdout"])
    # `dotnet run` prints compiler warnings and errors on stdout, mixed into the program's own output, so a
    # harmless warning (like CS8600 for an unchecked Console.ReadLine()) would make correct answers fail.
    diags = DOTNET_DIAG.findall(out)
    if diags:
        out = DOTNET_DIAG.sub("", out)
        err = "".join(diags) + err
    res = {"code": p.returncode, "stdout": out, "stderr": err, "ms": int((time.time() - t0) * 1000), "timedOut": timed_out, "truncated": state["truncated"]}
    if stop_on_wait:
        res["starved"] = starved
    return res


def _kill(p):
    try:
        os.killpg(p.pid, 9)
    except OSError:
        try:
            p.kill()
        except OSError:
            pass


def _safe_name(name):
    name = str(name).replace("\\", "/").lstrip("/")
    return name if re.fullmatch(r"[\w.-]+(/[\w.-]+)?", name) and ".." not in name else None


_TEMPLATE_LOCK = threading.Lock()


def prepare_template(lang, cache):
    """Languages that need a project around one file (Gleam): build a template once, with network, outside the sandbox."""
    if lang != "gleam":
        return None
    with _TEMPLATE_LOCK:  # two runs at once must not build (and delete) the same template together
        return _build_template(cache)


def _build_template(cache):
    tpl = os.path.join(cache, "template")
    # Gleam's standard library can't read stdin, so lessons use the small "input" package from Hex.
    if os.path.isdir(os.path.join(tpl, "build", "packages", "input")):
        return tpl
    shutil.rmtree(tpl, ignore_errors=True)
    os.makedirs(tpl, exist_ok=True)
    with open(os.path.join(tpl, "gleam.toml"), "w", encoding="utf-8") as f:
        f.write('name = "main"\nversion = "1.0.0"\ntarget = "erlang"\n\n[dependencies]\ngleam_stdlib = ">= 0.40.0 and < 2.0.0"\ninput = ">= 1.0.0 and < 2.0.0"\n')
    os.makedirs(os.path.join(tpl, "src"), exist_ok=True)
    with open(os.path.join(tpl, "src", "main.gleam"), "w", encoding="utf-8") as f:
        f.write('import gleam/io\n\npub fn main() {\n  io.println("ready")\n}\n')
    r = subprocess.run(["gleam", "build"], cwd=tpl, capture_output=True, text=True, timeout=300)
    if r.returncode:
        raise RuntimeError("Couldn't set up Gleam (it downloads its standard library once, so it needs the internet the first time):\n" + (r.stderr or r.stdout)[-800:])
    return tpl


# Beginner TypeScript lessons read input with a browser-style prompt(): the next input line, or null when
# there's none. Deno's own prompt() returns null whenever input is piped (as it is here), so this one
# reads standard input itself. It works in Deno without permissions and in the Node fallback.
DOTNET_DIAG = re.compile(r"^\S+\.(?:cs|fsx?)\(\d+,\d+\): (?:warning|error) [A-Z]+\d+: .*(?:\n|$)", re.M)
STARVED_MARK = "\n[[no-more-input]]\n"  # the shim writes this to stderr when it runs out of input
TS_PROMPT = 'const prompt = ((): ((q?: string) => string | null) => { let buf = "", eof = false; const g: any = globalThis, b = new Uint8Array(65536), d = new TextDecoder(); const rd = (): number => g.Deno ? (g.Deno.stdin.readSync(b) ?? 0) : g.process.getBuiltinModule("fs").readSync(0, b, 0, b.length, null); return (q?: string): string | null => { if (q) { if (g.Deno) g.Deno.stdout.writeSync(new TextEncoder().encode(q)); else g.process.stdout.write(q); } while (!eof && !buf.includes("\\n")) { const n = rd(); if (n) buf += d.decode(b.subarray(0, n), { stream: true }); else eof = true; } if (buf) { const i = buf.indexOf("\\n"), line = i < 0 ? buf : buf.slice(0, i); buf = i < 0 ? "" : buf.slice(i + 1); return line; } const m = "\\n[[no-more-input]]\\n"; if (g.Deno) g.Deno.stderr.writeSync(new TextEncoder().encode(m)); else g.process.stderr.write(m); return null; }; })();  '


def _starved(res):
    """Turns the prompt shim's out-of-input marker into a starved flag (the Output terminal then asks for more)."""
    res["starved"] = bool(res.get("starved")) or STARVED_MARK in res.get("stderr", "")
    res["stderr"] = res.get("stderr", "").replace(STARVED_MARK, "")


def run_code(msg):
    """{lang, code, stdin, files?} → {ok, phase, code, stdout, stderr, ms, timedOut, sandboxed}"""
    lang = str(msg.get("lang", ""))
    spec = lang_spec(lang)
    if not spec:
        return {"ok": False, "phase": "missing", "stderr": "This language isn't installed on this computer yet.", "stdout": "", "code": -1}
    os.makedirs(RUN_ROOT, exist_ok=True)
    cache = os.path.join(TOOL_CACHE, lang)
    os.makedirs(cache, exist_ok=True)
    work = tempfile.mkdtemp(prefix=lang + "-", dir=RUN_ROOT)
    try:
        tpl = prepare_template(lang, cache)
        if tpl:
            shutil.copytree(tpl, work, dirs_exist_ok=True, symlinks=True)
        files = dict(msg.get("files") or {})
        code = str(msg.get("code", ""))
        shimmed = lang == "typescript" and bool(re.search(r"\bprompt\s*\(", code))
        # The Output terminal (stopStarved) stops a program at the first question nobody has answered yet.
        # Only for languages that flush their output before reading: otherwise the question itself would
        # be lost when the program is stopped. (Java's Scanner programs crash at the end of input instead.)
        stop_on_wait = bool(msg.get("stopStarved")) and not msg.get("stdins") and (
            lang in ("typescript", "csharp") or (lang == "java" and not re.search(r"Scanner|System\.in|BufferedReader", code))
            # C++: reading cin flushes cout first (they're tied), so questions always get out. C stdio doesn't.
            or (lang == "cpp" and not re.search(r"\b(scanf|printf|puts|getchar|fgets|putchar)\b", code)))
        if shimmed:
            code = TS_PROMPT + code  # same line, so error line numbers stay right
        files[spec["file"]] = code
        for name, text in files.items():
            name = _safe_name(name)
            if not name:
                continue
            path = os.path.join(work, name)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                f.write(str(text))
        os.makedirs(os.path.join(work, ".home", ".local", "share"), exist_ok=True)  # .NET needs "local app data" to exist
        sandboxed_paths = shutil.which("bwrap") and os.environ.get("SYNTAX_ACADEMY_NO_SANDBOX") != "1"
        wdir, cdir = (SANDBOX_WORK, SANDBOX_CACHE) if sandboxed_paths else (work, cache)
        env = {"PATH": "/usr/local/sbin:/usr/local/bin:/usr/bin:/usr/lib/jvm/default/bin", "HOME": wdir + "/.home", "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8",
               "TERM": "dumb", "NO_COLOR": "1", "TMPDIR": "/tmp", "USER": "coder"}
        for k, v in (spec.get("env") or {}).items():
            env[k] = v.replace("{cache}", cdir)
        if not sandboxed_paths and os.environ.get("SYNTAX_ACADEMY_EXTRA_PATH"):  # test tools only: portable toolchains
            env["PATH"] = os.environ["SYNTAX_ACADEMY_EXTRA_PATH"] + ":" + env["PATH"]
        steps = [("build", spec["build"])] if spec.get("build") else []
        steps.append(("run", spec["run"]))
        out = {"ok": True, "phase": "run", "code": 0, "stdout": "", "stderr": "", "ms": 0, "buildMs": 0, "timedOut": False}
        for phase, cmd in steps:
            cmd = [c.replace("{cache}", cdir) for c in cmd]
            # Clojure fetches its own libraries from Maven the first time it runs.
            net = bool(spec.get("mvn")) and not os.path.isdir(os.path.join(cache, "cpcache"))
            pre, sandboxed = _sandbox_prefix(work, cache, env, net=net)
            limit = (180 if spec.get("slow") else 60) if phase == "build" else float(msg.get("timeout") or 10) + (20 if spec.get("slow") else 0)
            first_in = (msg.get("stdins") or [msg.get("stdin", "")])[0] if phase == "run" else ""
            # Compilers with one shared cache (Nim, Crystal) must not build two programs at once.
            lock = open(os.path.join(cache, ".build.lock"), "w") if phase == "build" and spec.get("lock") else None
            try:
                if lock:
                    fcntl.flock(lock, fcntl.LOCK_EX)
                res = _run_proc(pre + cmd, work, {**os.environ, **env}, str(first_in or ""), limit, sandboxed, stop_on_wait and phase == "run")
            finally:
                if lock:
                    lock.close()
            out.update({"sandboxed": sandboxed, "phase": phase})
            if phase == "build":
                out["buildMs"] = res["ms"]
                out["buildOutput"] = (res["stdout"] + res["stderr"]).strip()[-20000:]
                if res["code"] != 0 or res["timedOut"]:
                    out.update({"ok": False, "code": res["code"], "stderr": out["buildOutput"] or "The build failed.", "timedOut": res["timedOut"]})
                    return out
            else:
                if shimmed:
                    _starved(res)
                out.update({k: res[k] for k in ("code", "stdout", "stderr", "ms", "timedOut", "truncated", "starved") if k in res})
                out["ok"] = res["code"] == 0 and not res["timedOut"]
                # Grading: build once, then run the same program against every test's input.
                stdins = msg.get("stdins")
                if isinstance(stdins, list) and stdins:
                    runs = [{k: res[k] for k in ("code", "stdout", "stderr", "ms", "timedOut", "truncated", "starved") if k in res}]
                    for extra in stdins[1:30]:
                        pre, _ = _sandbox_prefix(work, cache, env)
                        r2 = _run_proc(pre + cmd, work, {**os.environ, **env}, str(extra), limit, sandboxed)
                        if shimmed:
                            _starved(r2)
                        runs.append({k: r2[k] for k in ("code", "stdout", "stderr", "ms", "timedOut", "truncated", "starved") if k in r2})
                    out["runs"] = runs
        return out
    except Exception as e:  # never crash the window over a run
        return {"ok": False, "phase": "error", "code": -1, "stdout": "", "stderr": str(e)[:2000]}
    finally:
        shutil.rmtree(work, ignore_errors=True)


def install_langs(lang):
    """Installs a language's packages with pacman through pkexec (shows the system password dialog)."""
    spec = LANGS.get(lang)
    if not spec or not spec["pkgs"]:
        return {"ok": False, "message": "This language can't be installed from the app. See its page for instructions."}
    pkgs = [p for p in spec["pkgs"] if p in ALLOWED_PKGS]
    if not shutil.which("pacman"):
        return {"ok": False, "message": "One-click installs need pacman (Arch, CachyOS, Manjaro, EndeavourOS...). Install these packages with your package manager: " + " ".join(pkgs)}
    if not shutil.which("pkexec"):
        return {"ok": False, "message": "Run this in a terminal: sudo pacman -S --needed " + " ".join(pkgs)}
    if not _install_lock.acquire(blocking=False):
        return {"ok": False, "message": "Another install is still running."}
    try:
        r = subprocess.run(["pkexec", "pacman", "-S", "--needed", "--noconfirm", *pkgs], capture_output=True, text=True, timeout=3600)
        _versions.pop(lang, None)
        if r.returncode == 126 or r.returncode == 127 and "dismissed" in (r.stderr or ""):
            return {"ok": False, "message": "The password dialog was cancelled."}
        if r.returncode:
            return {"ok": False, "message": ((r.stderr or "") + (r.stdout or "")).strip()[-600:] or "pacman failed"}
        return {"ok": True, "message": "Installed " + " ".join(pkgs), "version": lang_version(lang)}
    except (OSError, subprocess.SubprocessError) as e:
        return {"ok": False, "message": str(e)[:300]}
    finally:
        _install_lock.release()


def feed_allowed(url):
    u = urllib.parse.urlparse(url)
    if u.scheme == "https":
        return True
    # Local feeds are only for testing a release before publishing it.
    return os.environ.get("SYNTAX_ACADEMY_INSECURE_FEED") == "1" and (u.scheme == "file" or u.hostname in ("localhost", "127.0.0.1"))


def fetch(url, limit):
    req = urllib.request.Request(url, headers={"User-Agent": "SyntaxAcademy/%s" % UPDATE_CONF.get("version", "?"), "Cache-Control": "no-cache"})
    with urllib.request.urlopen(req, timeout=30) as r:
        data = r.read(limit + 1)
    if len(data) > limit:
        raise ValueError("download is larger than expected")
    return data


def downloaded_update():
    """The newest downloaded app, if it's newer than the installed package and still intact."""
    cur = read_json(CURRENT_FILE, None)
    if not cur:
        return None
    ver = str(cur.get("version", ""))
    if vtuple(ver) <= vtuple(UPDATE_CONF.get("version", "0")):
        shutil.rmtree(UPDATE_DIR, ignore_errors=True)  # the package caught up: drop old downloads
        return None
    path = os.path.join(UPDATE_DIR, ver, "index.html")
    try:
        with open(path, "rb") as f:
            if hashlib.sha256(f.read()).hexdigest() != cur.get("sha256"):
                return None
    except OSError:
        return None
    return {"version": ver, "path": path, "notes": cur.get("notes", []), "date": cur.get("date", ""),
            "core_changed": bool(cur.get("core_sha256")) and cur.get("core_sha256") != RUNNING_CORE_SHA}


def download_update(feed, pub, running):
    """Check the signed feed and download a newer app (and runtime) if there is one.
    Returns the state for the page: ready, latest or error. Never raises."""
    base = feed if feed.endswith("/") else feed + "/"
    try:
        import ed25519
        raw = fetch(base + "latest.json", MAX_MANIFEST)
        sig = bytes.fromhex(fetch(base + "latest.json.sig", 1024).decode().strip())
        if not ed25519.verify(bytes.fromhex(pub), raw, sig):
            raise ValueError("the update's signature doesn't match, so it was ignored")
        man = json.loads(raw)
        ver, name = str(man.get("version", "")), str(man.get("file", ""))
        if man.get("app") != APP_NAME or not re.fullmatch(r"SyntaxAcademy-[\d.]+\.html", name):
            raise ValueError("the update feed looks wrong")
        write_json(CHECK_FILE, {"at": time.time()})
        have = downloaded_update()
        best = max(vtuple(running), vtuple(have["version"]) if have else (0,))
        if vtuple(ver) <= best:
            ready = have and vtuple(have["version"]) > vtuple(running)
            return {"state": "ready", "version": have["version"], "notes": have["notes"]} if ready else {"state": "latest", "version": running}
        data = fetch(base + name, MAX_APP)
        if len(data) != man.get("size") or hashlib.sha256(data).hexdigest() != man.get("sha256"):
            raise ValueError("the download was damaged, it will be retried later")
        core = None
        if man.get("core"):
            cname = str(man["core"])
            if not re.fullmatch(r"core-[\d.]+\.py", cname):
                raise ValueError("the update feed looks wrong")
            core = fetch(base + cname, MAX_MANIFEST * 16)
            if len(core) != man.get("core_size") or hashlib.sha256(core).hexdigest() != man.get("core_sha256"):
                raise ValueError("the download was damaged, it will be retried later")
        folder = os.path.join(UPDATE_DIR, ver)
        os.makedirs(folder, exist_ok=True)
        for fname, blob in (("index.html", data), ("core.py", core)):
            if blob is None:
                continue
            tmp = os.path.join(folder, fname + ".tmp")
            with open(tmp, "wb") as f:
                f.write(blob)
            os.replace(tmp, os.path.join(folder, fname))
        write_json(CURRENT_FILE, {"version": ver, "sha256": man["sha256"], "core_sha256": man.get("core_sha256") if core else None, "notes": man.get("notes", []), "date": man.get("date", "")})
        for old in os.listdir(UPDATE_DIR):
            if old != ver and os.path.isdir(os.path.join(UPDATE_DIR, old)):
                shutil.rmtree(os.path.join(UPDATE_DIR, old), ignore_errors=True)
        return {"state": "ready", "version": ver, "notes": man.get("notes", [])}
    except Exception as e:  # network down, bad signature, disk full...: never crash the app over updates
        return {"state": "error", "message": str(getattr(e, "reason", e))[:200]}


def _running_core_sha():
    try:
        with open(os.path.abspath(__file__), "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()
    except (OSError, NameError):
        return ""


RUNNING_CORE_SHA = _running_core_sha()


def main_gui():
    global SAFE_GFX
    if not SAFE_GFX and read_json(GFX_FALLBACK_FILE, {}).get("version") == ((downloaded_update() or {}).get("version") or UPDATE_CONF.get("version", "")):
        SAFE_GFX = True
        os.environ.setdefault("WEBKIT_DISABLE_DMABUF_RENDERER", "1")
    import gi
    gi.require_version("Gtk", "3.0")
    gi.require_version("WebKit2", "4.1")
    from gi.repository import Gio, GLib, Gtk, WebKit2

    NOTIFY = ("org.freedesktop.Notifications", "/org/freedesktop/Notifications", "org.freedesktop.Notifications")

    def notify(title, body, actions=()):
        """Shows a desktop notification and returns its id (0 if there's no notification service)."""
        try:
            bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
            acts = [x for pair in actions for x in pair]
            hints = {"desktop-entry": GLib.Variant("s", APP_NAME), "urgency": GLib.Variant("y", 1)}
            args = GLib.Variant("(susssasa{sv}i)", ("Syntax Academy", 0, APP_NAME, str(title)[:120], GLib.markup_escape_text(str(body)[:400]), acts, hints, -1))
            res = bus.call_sync(*NOTIFY, "Notify", args, GLib.VariantType("(u)"), Gio.DBusCallFlags.NONE, 5000, None)
            return res.unpack()[0]
        except GLib.Error:
            return 0

    def on_notification_click(handler):
        try:
            bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        except GLib.Error:
            return
        bus.signal_subscribe(NOTIFY[0], NOTIFY[2], "ActionInvoked", NOTIFY[1], None, Gio.DBusSignalFlags.NONE,
                             lambda _c, _s, _p, _i, _n, params: handler(*params.unpack()))

    SYSTEMD_USER = os.path.join(GLib.get_user_config_dir(), "systemd", "user")
    TIMER_NAME = APP_NAME + "-reminder"

    def set_reminder(on, hour):
        """Turns the daily streak reminder on (at hour:00 every day) or off. Returns {ok, message}."""
        if not shutil.which("systemctl"):
            return {"ok": False, "message": "This system doesn't use systemd, so reminders can't run while the app is closed."}
        svc, tmr = (os.path.join(SYSTEMD_USER, TIMER_NAME + ext) for ext in (".service", ".timer"))
        run = lambda *a: subprocess.run(["systemctl", "--user", *a], capture_output=True, text=True, timeout=20)
        try:
            if not on:
                run("disable", "--now", TIMER_NAME + ".timer")
                for f in (svc, tmr):
                    if os.path.exists(f):
                        os.remove(f)
                run("daemon-reload")
                return {"ok": True, "message": "off"}
            hour = max(0, min(23, int(hour)))
            exe = os.environ.get("SYNTAX_ACADEMY_STUB") or shutil.which(APP_NAME) or "/usr/bin/" + APP_NAME
            os.makedirs(SYSTEMD_USER, exist_ok=True)
            with open(svc, "w", encoding="utf-8") as f:
                f.write("[Unit]\nDescription=Syntax Academy daily streak reminder\n\n[Service]\nType=oneshot\nExecStart=%s --remind\n" % exe)
            with open(tmr, "w", encoding="utf-8") as f:
                f.write("[Unit]\nDescription=Syntax Academy daily streak reminder\n\n[Timer]\nOnCalendar=*-*-* %02d:00:00\nPersistent=true\n\n[Install]\nWantedBy=timers.target\n" % hour)
            run("daemon-reload")
            r = run("enable", "--now", TIMER_NAME + ".timer")
            if r.returncode:
                return {"ok": False, "message": (r.stderr or "systemctl failed").strip()[:200]}
            return {"ok": True, "message": "on"}
        except (OSError, ValueError, subprocess.SubprocessError) as e:
            return {"ok": False, "message": str(e)[:200]}

    def run_reminder():
        state = read_json(STATE_FILE, None) or {}
        if not (state.get("settings", {}).get("notify", {}) or {}).get("reminder"):
            return 0
        days = (state.get("prog") or {}).get("days") or {}
        day = lambda n: time.strftime("%Y-%m-%d", time.localtime(time.time() - n * 86400))
        if days.get(day(0)):
            return 0
        streak = 0
        while days.get(day(streak + 1)):
            streak += 1
        name = (state.get("profile") or {}).get("name") or "hey"
        if streak:
            title, body = "Your %d-day streak ends at midnight \U0001f525" % streak, "%s, one quick lesson keeps it alive. Byte believes in you." % name
        else:
            title, body = "Five minutes of code? \U0001fae7", "%s, one lesson or a quick challenge today starts a new streak." % name
        nid = notify(title, body, [("default", "Open Syntax Academy")])
        if not nid:
            return 1
        loop = GLib.MainLoop()

        def clicked(i, _key):
            if i == nid:
                subprocess.Popen([os.environ.get("SYNTAX_ACADEMY_STUB") or APP_NAME], start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                loop.quit()
        on_notification_click(clicked)
        GLib.timeout_add_seconds(600, loop.quit)
        loop.run()
        return 0

    def special_dir(kind):
        k = {"pictures": GLib.UserDirectory.DIRECTORY_PICTURES, "documents": GLib.UserDirectory.DIRECTORY_DOCUMENTS}.get(kind)
        return (GLib.get_user_special_dir(k) if k is not None else None) or os.path.expanduser("~")

    class Window(Gtk.ApplicationWindow):
        def __init__(self, app):
            super().__init__(application=app, title="Syntax Academy")
            self.set_icon_name(APP_NAME)
            geo = read_json(WINDOW_FILE, {})
            self.set_default_size(geo.get("w", 1280), geo.get("h", 820))
            if geo.get("max"):
                self.maximize()
            self.set_size_request(760, 520)
            self.connect("delete-event", self.on_close)

            manager = WebKit2.WebsiteDataManager(base_data_directory=os.path.join(DATA_DIR, "web"), base_cache_directory=os.path.join(CACHE_DIR, "web"))
            context = WebKit2.WebContext.new_with_website_data_manager(manager)
            ucm = WebKit2.UserContentManager()
            self.ucm = ucm
            self.update_state = {"state": "idle"}
            self.notif_routes = {}
            on_notification_click(self.on_notification_click)
            ucm.connect("script-message-received::sa", self.on_message)
            ucm.register_script_message_handler("sa")

            self.view = WebKit2.WebView(web_context=context, user_content_manager=ucm)
            settings = self.view.get_settings()
            settings.set_enable_developer_extras(DEBUG)
            settings.set_allow_file_access_from_file_urls(True)
            settings.set_enable_smooth_scrolling(True)
            for name, val in (("set_enable_webgl", True), ("set_enable_write_console_messages_to_stdout", DEBUG)):
                try:
                    getattr(settings, name)(val)
                except (AttributeError, TypeError):
                    pass
            try:  # draw everything on the graphics card
                settings.set_hardware_acceleration_policy(WebKit2.HardwareAccelerationPolicy.NEVER if SAFE_GFX else WebKit2.HardwareAccelerationPolicy.ALWAYS)
            except (AttributeError, TypeError, GLib.Error):
                pass
            self.view.connect("context-menu", lambda *a: not DEBUG)
            self.view.connect("decide-policy", self.on_policy)
            self.view.connect("load-changed", self.on_load_changed)
            self.add(self.view)

            self.load_app(os.environ.pop("SYNTAX_ACADEMY_ROUTE", ""))
            if self.feed() and self.auto_updates():
                GLib.timeout_add_seconds(8, self.auto_check)
                GLib.timeout_add_seconds(CHECK_EVERY, self.auto_check)

        # ---------- which app to run ----------
        def load_app(self, route=""):
            upd = downloaded_update()
            index = upd["path"] if upd else os.path.join(APP_DIR, "index.html")
            self.running = upd["version"] if upd else UPDATE_CONF.get("version", "")
            info = {"running": self.running, "bundled": UPDATE_CONF.get("version", ""), "fromUpdate": bool(upd), "feed": bool(self.feed()), "signed": bool(UPDATE_CONF.get("pubkey"))}
            gfx = "safe" if SAFE_GFX or os.environ.get("WEBKIT_DISABLE_DMABUF_RENDERER") == "1" else "gpu"
            boot = "window.__SA_FRAMES__=0;requestAnimationFrame(function t(){__SA_FRAMES__++;requestAnimationFrame(t)});window.__SA_NATIVE__=true;window.__SA_STATE__=%s;window.__SA_UPDATE__=%s;window.__SA_GFX__=%s;window.__SA_GPU__=%s;" % (json.dumps(read_json(STATE_FILE, None)), json.dumps(info), json.dumps(gfx), json.dumps(gpu_name()))
            self.ucm.remove_all_scripts()
            self.ucm.add_script(WebKit2.UserScript.new(boot, WebKit2.UserContentInjectedFrames.TOP_FRAME, WebKit2.UserScriptInjectionTime.START, None, None))
            uri = GLib.filename_to_uri(index, None)
            if route and re.fullmatch(r"#/[\w./%-]*", route):
                uri += route
            self.view.load_uri(uri)

        # ---------- frozen-drawing watchdog ----------
        # Some GPU/driver combos stop drawing after the first frame (a white window) while the page
        # itself keeps running. If frames don't advance while the window is visible, remember it for
        # this version and restart in safe graphics.
        def on_load_changed(self, _view, event):
            if event == WebKit2.LoadEvent.FINISHED and not SAFE_GFX:
                self.watch_tries = 0
                GLib.timeout_add_seconds(5, self.check_drawing)

        def check_drawing(self):
            def got(view, res):
                try:
                    seen = view.evaluate_javascript_finish(res).to_string()
                except GLib.Error:
                    return
                state, _, frames = seen.partition(":")
                if state != "visible":
                    self.watch_tries += 1
                    if self.watch_tries < 12:
                        GLib.timeout_add_seconds(5, self.check_drawing)
                elif int(frames or 0) < 3:
                    print("syntax-academy: drawing froze; restarting in safe graphics", file=sys.stderr)
                    write_json(GFX_FALLBACK_FILE, {"version": self.running, "at": int(time.time())})
                    os.environ["SYNTAX_ACADEMY_SAFE_GRAPHICS"] = "1"
                    self.restart_process("")
            self.view.evaluate_javascript("document.visibilityState + ':' + (window.__SA_FRAMES__ || 0)", -1, None, None, None, got)
            return False

        # ---------- updates ----------
        def settings(self):
            return (read_json(STATE_FILE, {}) or {}).get("settings", {}).get("updates", {}) or {}

        def feed(self):
            url = self.settings().get("feed") or UPDATE_CONF.get("feed", "")
            return url if url and feed_allowed(url) else ""

        def auto_updates(self):
            return self.settings().get("auto", True) is not False

        def auto_check(self):
            last = read_json(CHECK_FILE, {}).get("at", 0)
            if self.auto_updates() and time.time() - last > CHECK_EVERY - 60 and self.update_state.get("state") != "checking":
                threading.Thread(target=self.check_updates, args=(None,), daemon=True).start()
            return True

        def check_updates(self, rid):
            feed, pub = self.feed(), UPDATE_CONF.get("pubkey", "")
            if not feed or not pub:
                return self.post_update({"state": "unconfigured"}, rid)
            self.update_state = {"state": "checking"}
            GLib.idle_add(self.notify_page, {"state": "checking"})
            self.post_update(download_update(feed, pub, self.running), rid)

        def post_update(self, payload, rid):
            self.update_state = payload
            GLib.idle_add(self.notify_page, payload, rid)

        def notify_page(self, payload, rid=None):
            if rid is not None:
                self.reply(rid, payload)
            self.js("window.SA&&SA.onUpdate&&SA.onUpdate(%s)" % json.dumps(payload))
            return False

        def restart_process(self, route):
            w, h = self.get_size()
            write_json(WINDOW_FILE, {"w": w, "h": h, "max": self.is_maximized()})
            env = dict(os.environ, SYNTAX_ACADEMY_ROUTE=str(route or ""))
            stub = os.environ.get("SYNTAX_ACADEMY_STUB") or "/usr/bin/" + APP_NAME
            os.execve(sys.executable, [sys.executable, stub] + sys.argv[1:], env)

        def on_update_message(self, msg):
            action, rid = msg.get("action"), msg.get("id")
            if action == "check":
                if self.update_state.get("state") == "checking":
                    return self.reply(rid, {"state": "checking"})
                threading.Thread(target=self.check_updates, args=(rid,), daemon=True).start()
            elif action == "status":
                have = downloaded_update()
                self.reply(rid, {**self.update_state, "running": self.running, "bundled": UPDATE_CONF.get("version", ""), "downloaded": have["version"] if have else "", "feed": self.feed(), "signed": bool(UPDATE_CONF.get("pubkey")), "lastCheck": read_json(CHECK_FILE, {}).get("at", 0)})
            elif action == "apply":
                have = downloaded_update()
                if have and have.get("core_changed"):
                    GLib.timeout_add(150, self.restart_process, msg.get("route", ""))
                else:
                    GLib.timeout_add(150, lambda: (self.load_app(str(msg.get("route", ""))), False)[1])
            elif action == "revert":
                shutil.rmtree(UPDATE_DIR, ignore_errors=True)
                self.update_state = {"state": "idle"}
                GLib.timeout_add(150, lambda: (self.load_app(), False)[1])

        def in_thread(self, rid, fn, *args):
            def work():
                try:
                    res = fn(*args)
                except Exception as e:  # report, don't crash
                    res = {"ok": False, "error": str(e)[:500]}
                GLib.idle_add(self.reply, rid, res)
            threading.Thread(target=work, daemon=True).start()

        def on_message(self, _ucm, result):
            try:
                msg = json.loads(result.get_js_value().to_string())
            except (ValueError, AttributeError):
                return
            kind, rid = msg.get("type"), msg.get("id")
            if kind == "save":
                write_json(STATE_FILE, msg.get("state"))
            elif kind == "run":
                self.in_thread(rid, run_code, msg)
            elif kind == "toolchains":
                self.in_thread(rid, toolchains)
            elif kind == "version":
                self.in_thread(rid, lambda: {"version": lang_version(str(msg.get("lang", "")))})
            elif kind == "install":
                self.in_thread(rid, install_langs, str(msg.get("lang", "")))
            elif kind == "open":
                self.open_external(msg.get("url", ""))
            elif kind == "saveFile":
                self.reply(rid, {"path": self.save_file(msg)})
            elif kind == "update":
                self.on_update_message(msg)
            elif kind == "http":
                self.in_thread(rid, self.http, msg)
            elif kind == "notify":
                nid = notify(msg.get("title", ""), msg.get("body", ""), [("default", "Open")])
                route = str(msg.get("route", ""))
                if nid and re.fullmatch(r"#/[\w./%-]*", route):
                    self.notif_routes[nid] = route
                self.reply(rid, {"ok": bool(nid)})
            elif kind == "reminder":
                self.reply(rid, set_reminder(bool(msg.get("on")), msg.get("hour", 19)))

        def on_notification_click(self, nid, _key):
            if nid not in self.notif_routes:
                return
            route = self.notif_routes.pop(nid)
            self.present()
            self.js("SA.ui && SA.ui.go(%s)" % json.dumps(route))

        def save_file(self, msg):
            name = os.path.basename(msg.get("name") or "syntax-academy-file.txt")
            folder = special_dir(msg.get("dir", ""))
            os.makedirs(folder, exist_ok=True)
            path = os.path.join(folder, name)
            if "b64" in msg:
                with open(path, "wb") as f:
                    f.write(base64.b64decode(msg["b64"]))
            else:
                with open(path, "w", encoding="utf-8") as f:
                    f.write(msg.get("text", ""))
            return path.replace(os.path.expanduser("~"), "~", 1)

        def http(self, msg):
            url = msg.get("url", "")
            if urllib.parse.urlparse(url).hostname not in HTTP_HOSTS:
                return {"status": 0, "text": "blocked host"}
            req = urllib.request.Request(url, data=(msg.get("body") or "").encode(), method="POST", headers=msg.get("headers") or {})
            try:
                with urllib.request.urlopen(req, timeout=180) as r:
                    return {"status": r.status, "text": r.read().decode("utf-8", "replace")}
            except urllib.error.HTTPError as e:
                return {"status": e.code, "text": e.read().decode("utf-8", "replace")}
            except (urllib.error.URLError, OSError) as e:
                return {"status": 0, "text": str(getattr(e, "reason", e))}

        def js(self, code):
            self.view.evaluate_javascript(code, -1, None, None, None, None, None)

        def reply(self, rid, data):
            self.js("SA._nativeReply(%s,%s)" % (json.dumps(rid), json.dumps(data)))
            return False

        def open_external(self, uri):
            if uri.startswith(("http://", "https://")):
                Gio.AppInfo.launch_default_for_uri(uri, None)

        def on_policy(self, _view, decision, kind):
            if kind in (WebKit2.PolicyDecisionType.NAVIGATION_ACTION, WebKit2.PolicyDecisionType.NEW_WINDOW_ACTION):
                uri = decision.get_navigation_action().get_request().get_uri()
                if uri.startswith(("http://", "https://")):
                    self.open_external(uri)
                    decision.ignore()
                    return True
            return False

        def on_close(self, *_):
            w, h = self.get_size()
            write_json(WINDOW_FILE, {"w": w, "h": h, "max": self.is_maximized()})
            return False

    class App(Gtk.Application):
        def __init__(self):
            super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.FLAGS_NONE)

        def do_activate(self):
            win = self.get_active_window() or Window(self)
            win.show_all()
            win.present()

    GLib.set_prgname(APP_NAME)
    GLib.set_application_name("Syntax Academy")
    if "--remind" in sys.argv:
        return run_reminder()
    return App().run(sys.argv)


if __name__ == "__main__":
    sys.path.insert(0, APP_DIR)  # ed25519.py ships next to the app
    UPDATE_CONF.update(read_json(os.path.join(APP_DIR, "update.json"), {}))
    if "--run-json" in sys.argv:  # headless: run one program, used by the test suite
        print(json.dumps(run_code(json.load(sys.stdin))))
        sys.exit(0)
    if "--toolchains" in sys.argv:
        print(json.dumps(toolchains(), indent=1))
        sys.exit(0)
    sys.exit(main_gui())

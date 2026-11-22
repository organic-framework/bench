"""Bench, the package manager under OF (docs/bench.md; organic-framework-build reports/framework/metamethod,
11-first-slice.md, parts 1, 2 and 6).

Bench knows a package, its version, where it came from and what it depends on. It reads `Benchfile.spec`,
`Benchfile` and `Benchfile.lock` in a closed Lua environment (bin/of_bench_sandbox.lua, run in a process of its own),
fetches a package from git or takes it from a path, writes the lock, notices a changed file, and forks a package as
a git clone that is rebased on its origin. What a package's `main.lua` means is its framework's (OF's is
bin/of_method.py), called at install through FRAMEWORK below.

Sources in this slice: git and a path. No catalogue, no version resolution between packages.
"""

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
SANDBOX = os.path.join(HERE, "of_bench_sandbox.lua")

LOCK = "Benchfile.lock"
BENCHFILE = "Benchfile"
SPEC = "Benchfile.spec"
INSTALLED = os.path.join(".bench", "packages")

# the words each of Bench's files may use (a declaration is a statement; a value word gives a value)
SPEC_WORDS = {"decl": ["package", "depends", "needs", "includes"], "value": []}
BENCHFILE_WORDS = {"decl": ["source", "package"], "value": []}
LOCK_WORDS = {"decl": ["lockfile", "locked"], "value": []}
NO_WORDS = {"decl": [], "value": []}

# what one evaluation may spend; the instruction and memory budgets are kept inside the process (its count hook), the
# wall and processor time by this side
LIMITS = {"instructions": 20_000_000, "memory_kb": 64 * 1024, "string_bytes": 8 * 1024 * 1024,
          "output_bytes": 8 * 1024 * 1024}
WALL_S = 10
FILE_MAX = 1024 * 1024          # one Lua source
SOURCES_MAX = 8 * 1024 * 1024   # all the Lua sources of one package

# the Lua runtimes the reader runs on, the first found; LuaJIT runs with its compiler off (the hook needs that)
RUNTIMES = ("lua5.4", "lua54", "lua", "luajit", "lua5.1")
RUNTIME_DIRS = ("/usr/bin", "/usr/local/bin", "/opt/homebrew/bin", "/bin")

NAME = re.compile(r"[a-z][a-z0-9_-]{0,63}")
VERSION = re.compile(r"\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?")
REF = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,199}")
COMMIT = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")


class BenchError(Exception):
    pass


class Stopped(BenchError):
    """The reader ended a file's evaluation: REASON is instructions, memory or time."""

    def __init__(self, reason, name):
        super().__init__(f"{name}: stopped by the reader ({reason})")
        self.reason = reason


# ---- the reader ----

def runtime():
    """The Lua runtime this machine has, as an absolute path."""
    for name in RUNTIMES:
        for d in RUNTIME_DIRS:
            p = os.path.join(d, name)
            if os.path.isfile(p) and os.access(p, os.X_OK):
                return p
    raise BenchError("no Lua runtime on this machine: Bench reads with lua 5.4, LuaJIT or lua 5.1 "
                     "(macOS: brew install luajit; Debian or Ubuntu: apt install lua5.4)")


def lua_lit(v, depth=0):
    """V as a Lua literal: strings with every byte outside printable ASCII as a decimal escape."""
    if depth > 64:
        raise BenchError("data nested deeper than 64")
    if v is None:
        return "nil"
    if v is True:
        return "true"
    if v is False:
        return "false"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        if v != v or v in (float("inf"), float("-inf")):
            raise BenchError("a number that is not finite")
        return repr(v)
    if isinstance(v, str):
        out = []
        for b in v.encode("utf-8"):
            c = chr(b)
            out.append(c if 0x20 <= b < 0x7f and c not in '"\\' else "\\%03d" % b)
        return '"' + "".join(out) + '"'
    if isinstance(v, (list, tuple)):
        if any(x is None for x in v):
            raise BenchError("a list with a nil in it")
        return "{" + ",".join(lua_lit(x, depth + 1) for x in v) + "}"
    if isinstance(v, dict):
        items = []
        for k, x in v.items():
            if x is None:
                continue
            if not isinstance(k, (str, int)) or isinstance(k, bool):
                raise BenchError("a key that is neither a string nor a whole number")
            items.append("[" + lua_lit(k) + "]=" + lua_lit(x, depth + 1))
        return "{" + ",".join(items) + "}"
    raise BenchError(f"a {type(v).__name__} cannot enter the reader")


def _limits():
    """Run in the child before it starts Lua: no core file, no file written, no process started, processor time
    bounded, and on Linux the address space too."""
    import resource
    cpu = WALL_S + 1
    for lim, val in ((resource.RLIMIT_CORE, 0), (resource.RLIMIT_FSIZE, 0), (resource.RLIMIT_CPU, cpu),
                     (resource.RLIMIT_NPROC, 0)):
        try:
            resource.setrlimit(lim, (val, val))
        except (ValueError, OSError):
            pass
    if sys.platform.startswith("linux"):
        try:
            resource.setrlimit(resource.RLIMIT_AS, (1 << 30, 1 << 30))
        except (ValueError, OSError):
            pass


def evaluate(files, entry, words, *, mode="read", path=None, args=(), name=None, limits=None, wall_s=None):
    """Evaluate FILES[ENTRY] ({relative path: Lua source}) in the closed environment with WORDS. mode "read": its
    declarations and what it returned, as {"decls": [{word, name, body}], "returned": ...}, every function kept as
    {"@": "function", "path": [...]}. mode "call": the function at PATH of such a read, called with ARGS, as
    {"value": ...}. Raises Stopped when a budget ends it, BenchError for a Lua error or a refusal."""
    name = name or entry
    if entry not in files:
        raise BenchError(f"{name}: no such file")
    req = {"mode": mode, "files": files, "entry": entry, "words": words, "limits": dict(LIMITS, **(limits or {})),
           "path": list(path or []), "args": list(args), "nargs": len(args)}
    if mode == "call" and not req["path"]:
        raise BenchError("a call needs the function's place")
    with open(SANDBOX, encoding="utf-8") as f:
        program = "local REQ = " + lua_lit(req) + "\n" + f.read()
    wall = wall_s or WALL_S
    try:
        p = subprocess.Popen([runtime(), "-"], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             cwd="/", env={"PATH": "/usr/bin:/bin", "LANG": "C"}, close_fds=True,
                             start_new_session=True, preexec_fn=_limits)
    except OSError as e:
        raise BenchError(f"{name}: the reader did not start: {e}")
    try:
        out, err = p.communicate(program.encode("utf-8"), timeout=wall)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(p.pid, 9)
        except OSError:
            p.kill()
        p.communicate()
        raise Stopped("time", name)
    err = err.decode("utf-8", "replace")
    m = re.search(r"bench-sandbox: stopped: (\w+)", err)
    if m:
        raise Stopped(m.group(1), name)
    if p.returncode != 0:
        if p.returncode in (-9, -24, 137, 152) or p.returncode == -getattr(__import__("signal"), "SIGXCPU", 24):
            raise Stopped("time", name)
        if "not enough memory" in err or "memory" in err.lower():
            raise Stopped("memory", name)
        raise BenchError(f"{name}: the reader failed (exit {p.returncode}): {err.strip()[-300:]}")
    try:
        r = json.loads(out.decode("utf-8", "replace"))
    except ValueError:
        raise BenchError(f"{name}: the reader gave no result")
    if not r.get("ok"):
        raise BenchError(f"{name}: {r.get('error')}")
    return r["result"]


def lua_sources(folder):
    """{relative path: source} of every .lua file under FOLDER that a `require` may load, and the entry files beside
    them (Benchfile.spec). Links and files over FILE_MAX are refused."""
    files, total = {}, 0
    for rel in _walk(folder):
        if not (rel.endswith(".lua") or rel in (SPEC,)):
            continue
        p = os.path.join(folder, rel)
        st = os.stat(p, follow_symlinks=False)
        if st.st_size > FILE_MAX:
            raise BenchError(f"{rel}: over {FILE_MAX} bytes")
        total += st.st_size
        if total > SOURCES_MAX:
            raise BenchError(f"{folder}: its Lua sources are over {SOURCES_MAX} bytes")
        files[rel] = _read_text(p)
    return files


def read_file(path, words, name=None):
    """One file read alone (a Benchfile, a lock, a settings file): its declarations and return value."""
    return evaluate({"file.lua": _read_text(path)}, "file.lua", words, name=name or path)


def _read_text(p):
    fd = os.open(p, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(fd, "rb") as f:
        data = f.read(FILE_MAX + 1)
    if len(data) > FILE_MAX:
        raise BenchError(f"{p}: over {FILE_MAX} bytes")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        raise BenchError(f"{p}: not UTF-8 text")


# ---- digests ----

SKIP_DIRS = {".git", ".bench", "__pycache__"}


def _walk(folder):
    """Every regular file under FOLDER, relative, sorted; a link anywhere is refused (a package's content is its own
    files, never what a link points at)."""
    out = []
    for root, dirs, names in os.walk(folder):
        for d in list(dirs):
            if d in SKIP_DIRS:
                dirs.remove(d)
            elif os.path.islink(os.path.join(root, d)):
                raise BenchError(f"{os.path.relpath(os.path.join(root, d), folder)}: a link in a package")
        for n in names:
            p = os.path.join(root, n)
            if os.path.islink(p):
                raise BenchError(f"{os.path.relpath(p, folder)}: a link in a package")
            if not os.path.isfile(p):
                continue
            out.append(os.path.relpath(p, folder).replace(os.sep, "/"))
    return sorted(out)


def file_digest(path):
    h = hashlib.sha256()
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(fd, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return "sha256:" + h.hexdigest()


def tree_digest(files):
    """One digest over {relative path: digest}."""
    h = hashlib.sha256()
    for rel in sorted(files):
        h.update(rel.encode() + b"\0" + files[rel].encode() + b"\n")
    return "sha256:" + h.hexdigest()


def folder_files(folder):
    return {rel: file_digest(os.path.join(folder, rel)) for rel in _walk(folder)}


def changed(locked_files, now_files):
    """What differs between two {path: digest}: changed, added and removed paths, sorted."""
    out = []
    for rel in sorted(set(locked_files) | set(now_files)):
        a, b = locked_files.get(rel), now_files.get(rel)
        if a == b:
            continue
        out.append((rel, "added" if a is None else "removed" if b is None else "changed"))
    return out


# ---- the project's files ----

def _decls(result, word=None):
    return [d for d in result["decls"] if word is None or d.get("word") == word]


def read_benchfile(project):
    """The packages a project's Benchfile names: [{name, source: {path} | {git, ref, dir}}], in order."""
    p = os.path.join(project, BENCHFILE)
    if not os.path.isfile(p):
        raise BenchError(f"{project}: no {BENCHFILE}")
    r = read_file(p, BENCHFILE_WORDS, name=BENCHFILE)
    out, seen = [], set()
    for d in _decls(r, "package"):
        name, body = d.get("name"), d.get("body") or {}
        if not name or not NAME.fullmatch(name):
            raise BenchError(f"{BENCHFILE}: a package needs a name such as \"of-asks\" (got {name!r})")
        if name in seen:
            raise BenchError(f"{BENCHFILE}: package \"{name}\" is named twice")
        seen.add(name)
        if not isinstance(body, dict):
            raise BenchError(f"{BENCHFILE}: package \"{name}\": its options are a table")
        unknown = set(body) - {"path", "git", "ref", "dir"}
        if unknown:
            raise BenchError(f"{BENCHFILE}: package \"{name}\": unknown option {sorted(unknown)[0]!r}")
        if bool(body.get("path")) == bool(body.get("git")):
            raise BenchError(f"{BENCHFILE}: package \"{name}\": give one source, git or path (no catalogue yet)")
        if body.get("path"):
            src = {"path": _str(body["path"], name, "path")}
        else:
            src = {"git": _str(body["git"], name, "git"), "ref": _str(body.get("ref", "HEAD"), name, "ref")}
            if not REF.fullmatch(src["ref"]) or src["ref"].startswith("-") or ".." in src["ref"]:
                raise BenchError(f"{BENCHFILE}: package \"{name}\": ref {src['ref']!r} is not a branch, tag or commit")
            if body.get("dir"):
                src["dir"] = _rel(_str(body["dir"], name, "dir"), f"package \"{name}\" dir")
        out.append({"name": name, "source": src})
    return out


def _str(v, name, key):
    if not isinstance(v, str) or not v:
        raise BenchError(f"{BENCHFILE}: package \"{name}\": {key} is a text")
    return v


def _rel(p, what):
    """A relative path inside its folder, no '..', no absolute path."""
    norm = os.path.normpath(p)
    if os.path.isabs(p) or norm == ".." or norm.startswith("../") or "\\" in p:
        raise BenchError(f"{what}: {p!r} is not a path inside its folder")
    return norm.replace(os.sep, "/")


def read_spec(folder, expect=None):
    """A package's Benchfile.spec as {name, version, main, summary, depends, needs, tests}."""
    p = os.path.join(folder, SPEC)
    if not os.path.isfile(p):
        raise BenchError(f"{folder}: no {SPEC}")
    r = evaluate(lua_sources(folder), SPEC, SPEC_WORDS, name=f"{expect or folder}/{SPEC}")
    pk = _decls(r, "package")
    if len(pk) != 1 or not isinstance(pk[0].get("body"), dict):
        raise BenchError(f"{expect or folder}/{SPEC}: one package {{ ... }} declaration")
    body = pk[0]["body"]
    name, version = body.get("name"), body.get("version")
    if not isinstance(name, str) or not NAME.fullmatch(name):
        raise BenchError(f"{SPEC}: name {name!r} is not a package name")
    if expect and name != expect:
        raise BenchError(f"{SPEC}: the package is \"{name}\", the Benchfile names \"{expect}\"")
    if not isinstance(version, str) or not VERSION.fullmatch(version):
        raise BenchError(f"{name}/{SPEC}: version {version!r} is not MAJOR.MINOR.PATCH")
    main = body.get("main", "main.lua")
    tests = body.get("tests")
    if tests is not None and not (isinstance(tests, list) and tests and all(isinstance(x, str) for x in tests)):
        raise BenchError(f"{name}/{SPEC}: tests is a command, a list of texts")
    depends = [x for d in _decls(r, "depends") for x in (d.get("body") or [])]
    needs = {}
    for d in _decls(r, "needs"):
        needs.update(d.get("body") or {})
    return {"name": name, "version": version, "main": _rel(main, f"{name} main"), "summary": body.get("summary"),
            "depends": depends, "needs": needs, "tests": tests}


def read_lock(project):
    """{name: entry} of the project's Benchfile.lock, {} when it has none."""
    p = os.path.join(project, LOCK)
    if not os.path.isfile(p):
        return {}
    r = read_file(p, LOCK_WORDS, name=LOCK)
    head = _decls(r, "lockfile")
    if not head or (head[0].get("body") or {}).get("version") != 1:
        raise BenchError(f"{LOCK}: not a lock this Bench reads (lockfile {{ version = 1 }})")
    return {d["name"]: d.get("body") or {} for d in _decls(r, "locked")}


def write_lock(project, entries):
    lines = ["-- Benchfile.lock: written by `of bench install`, read by Bench's closed reader. Do not edit it by hand:",
             "-- each package's exact version, its source (for git the commit), and a digest of every file.",
             "", "lockfile { version = 1 }", ""]
    for name in sorted(entries):
        e = entries[name]
        lines.append(f"locked {lua_lit(name)} {{")
        for k in sorted(e):
            lines.append(f"  {k} = {_pretty(e[k], 1)},")
        lines.append("}")
        lines.append("")
    text = "\n".join(lines)
    tmp = os.path.join(project, f".{LOCK}.{os.getpid()}.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, os.path.join(project, LOCK))


def _pretty(v, depth):
    pad = "  " * (depth + 1)
    if isinstance(v, dict) and v:
        inner = ",\n".join(f"{pad}[{lua_lit(k)}] = {_pretty(v[k], depth + 1)}" for k in sorted(v, key=str))
        return "{\n" + inner + ",\n" + "  " * depth + "}"
    if isinstance(v, list) and v:
        inner = ",\n".join(f"{pad}{_pretty(x, depth + 1)}" for x in v)
        return "{\n" + inner + ",\n" + "  " * depth + "}"
    if isinstance(v, (dict, list)):
        return "{}"
    return lua_lit(v)


# ---- git sources ----

def _state_dir():
    import of_events
    return of_events.STATE_DIR


def _git(tree, *args, timeout=300, transport=False, check=True):
    """The system's git with no program the repository names (of_protected); TRANSPORT opens https, ssh and file for
    a fetch or a clone, which of_protected keeps closed."""
    import of_protected
    env = of_protected._env()
    if transport:
        env["GIT_ALLOW_PROTOCOL"] = "https:ssh:file"
    r = subprocess.run(of_protected._git_cmd(tree) + list(args), capture_output=True, text=True, timeout=timeout,
                       env=env)
    if check and r.returncode != 0:
        raise BenchError(f"git {args[0]}: {(r.stderr or r.stdout).strip()[:300]}")
    return r


def _cache(url):
    d = os.path.join(_state_dir(), "bench", "git", hashlib.sha256(url.encode()).hexdigest()[:24] + ".git")
    if not os.path.isdir(d):
        os.makedirs(d, mode=0o700)
        _git(d, "init", "--quiet", "--bare")
    return d


def _url(url, project):
    """A git source as git takes it: a URL, or a local folder (relative to the project)."""
    if re.match(r"^(https|ssh)://|^[A-Za-z0-9._-]+@[A-Za-z0-9.-]+:", url):
        return url
    if url.startswith("file://"):
        return url
    p = url if os.path.isabs(url) else os.path.join(project, url)
    return "file://" + os.path.abspath(p)


def fetch(url, project):
    cache = _cache(url)
    _git(cache, "fetch", "--quiet", "--no-tags", "--prune", "--end-of-options", _url(url, project),
         "+refs/heads/*:refs/heads/*", "+refs/tags/*:refs/tags/*", transport=True)
    return cache


def resolve(cache, ref):
    r = _git(cache, "rev-parse", "--verify", "--quiet", "--end-of-options", f"{ref}^{{commit}}", check=False)
    if r.returncode != 0:
        raise BenchError(f"no commit for {ref!r} in the source")
    return r.stdout.strip()


def export(cache, commit, sub, dest):
    """The files of COMMIT (under SUB) written into the new folder DEST; a link or a submodule is refused."""
    args = ["ls-tree", "-r", "-z", "--full-tree", commit]
    if sub:
        args += ["--", sub]
    listing = _git(cache, *args).stdout
    os.makedirs(dest)
    n = 0
    for item in listing.split("\0"):
        if not item:
            continue
        meta, path = item.split("\t", 1)
        mode, kind, oid = meta.split()
        rel = path[len(sub) + 1:] if sub else path
        if kind != "blob" or mode not in ("100644", "100755"):
            raise BenchError(f"{path}: a {'link' if mode == '120000' else kind} in a package")
        rel = _rel(rel, "a file of the package")
        data = subprocess.run(__import__("of_protected")._git_cmd(cache) + ["cat-file", "blob", oid],
                              capture_output=True, timeout=60, env=__import__("of_protected")._env()).stdout
        target = os.path.join(dest, rel)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                     0o755 if mode == "100755" else 0o644)
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        n += 1
    if sub and not n:
        raise BenchError(f"no folder {sub!r} in commit {commit[:12]}")


# ---- install ----

FRAMEWORK = None   # set by the framework that reads main.lua (bin/of_method.py); called at install


def _framework():
    if FRAMEWORK is not None:
        return FRAMEWORK
    import of_method
    return of_method.on_install


def package_dir(project, pkg, lock_entry=None):
    """Where an installed package's files are: its path, or its export under .bench/packages."""
    src = pkg["source"]
    if "path" in src:
        p = src["path"]
        return p if os.path.isabs(p) else os.path.normpath(os.path.join(project, p))
    base = os.path.join(project, INSTALLED, pkg["name"])
    return os.path.join(base, src["dir"]) if src.get("dir") else base


def install(project, frozen=False, log=print):
    """Install every package of the project's Benchfile and write Benchfile.lock. FROZEN: take each git package at the
    lock's commit and refuse any file that differs from the lock (a second install from the lock)."""
    project = os.path.abspath(project)
    pkgs = read_benchfile(project)
    old = read_lock(project)
    if frozen and not old:
        raise BenchError(f"{project}: no {LOCK} to install from")
    entries, problems = {}, []
    hook = _framework()
    for pkg in pkgs:
        name, src = pkg["name"], pkg["source"]
        prev = old.get(name) or {}
        if frozen and not prev:
            raise BenchError(f"{LOCK}: no entry for \"{name}\", which the Benchfile names")
        source = dict(src)
        if "git" in src:
            cache = fetch(src["git"], project)
            if frozen:
                commit = (prev.get("source") or {}).get("commit")
                if not isinstance(commit, str) or not COMMIT.fullmatch(commit):
                    raise BenchError(f"{LOCK}: \"{name}\" has no commit")
                if _git(cache, "cat-file", "-e", f"{commit}^{{commit}}", check=False).returncode != 0:
                    raise BenchError(f"\"{name}\": the source no longer has the locked commit {commit[:12]}")
            else:
                commit = resolve(cache, src["ref"])
            source["commit"] = commit
            base = os.path.join(project, INSTALLED, name)
            tmp = tempfile.mkdtemp(prefix=f".{name}.", dir=_ensure(os.path.join(project, INSTALLED)))
            shutil.rmtree(tmp)
            export(cache, commit, src.get("dir"), tmp)
            if os.path.isdir(base):
                shutil.rmtree(base)
            os.replace(tmp, base)
        folder = package_dir(project, pkg)
        if not os.path.isdir(folder):
            raise BenchError(f"\"{name}\": no folder {folder}")
        files = folder_files(folder)
        digest = tree_digest(files)
        if frozen and prev.get("digest") != digest:
            for rel, how in changed(prev.get("files") or {}, files):
                problems.append(f"{name}: {rel} {how} since the lock")
            continue
        spec = read_spec(folder, expect=name)
        entry = {"version": spec["version"], "source": source, "digest": digest, "files": files}
        entry.update(hook(project, name, folder, spec) or {})
        if frozen:
            for k in ("settings", "hooks"):
                if prev.get(k) != entry.get(k):
                    problems.append(f"{name}: its {k} differ from the lock")
        entries[name] = entry
        log(f"  {name} {spec['version']} ({'commit ' + source['commit'][:12] if 'commit' in source else src['path']})")
    if problems:
        raise BenchError("the files differ from the lock:\n  " + "\n  ".join(problems))
    if not frozen:
        write_lock(project, entries)
    _ensure(os.path.join(project, ".bench"))
    gi = os.path.join(project, ".bench", ".gitignore")
    if not os.path.exists(gi):
        with open(gi, "w") as f:
            f.write("*\n")
    return entries


def _ensure(d):
    os.makedirs(d, exist_ok=True)
    return d


def check(project):
    """What differs from the lock, without installing: [(package, path, how)]; framework files (settings, hooks) are
    the framework's to check."""
    project = os.path.abspath(project)
    lock = read_lock(project)
    if not lock:
        raise BenchError(f"{project}: no {LOCK}; run of bench install")
    out = []
    pkgs = {p["name"]: p for p in read_benchfile(project)}
    for name in sorted(set(lock) | set(pkgs)):
        if name not in pkgs:
            out.append((name, "", "in the lock, not in the Benchfile"))
            continue
        if name not in lock:
            out.append((name, "", "in the Benchfile, not in the lock"))
            continue
        folder = package_dir(project, pkgs[name])
        now = folder_files(folder) if os.path.isdir(folder) else {}
        for rel, how in changed(lock[name].get("files") or {}, now):
            out.append((name, rel, how))
    return out


# ---- a fork with a rebase ----

FORKS = "forks"
FORK_BRANCH = "organization"


def fork(project, name, log=print):
    """Clone a git package into forks/<name> with its source as origin, at the locked commit on the branch
    `organization`, and point the Benchfile's line at it. Returns the fork's folder."""
    project = os.path.abspath(project)
    pkgs = {p["name"]: p for p in read_benchfile(project)}
    pkg = pkgs.get(name)
    if not pkg:
        raise BenchError(f"the Benchfile names no package \"{name}\"")
    src = pkg["source"]
    if "git" not in src:
        raise BenchError(f"\"{name}\" comes from a path, not git: it is already the organization's own folder")
    commit = ((read_lock(project).get(name) or {}).get("source") or {}).get("commit")
    if not commit:
        raise BenchError(f"\"{name}\" is not installed: run of bench install first")
    dest = os.path.join(project, FORKS, name)
    if os.path.exists(dest):
        raise BenchError(f"{FORKS}/{name} exists already")
    _ensure(os.path.join(project, FORKS))
    _git(project, "clone", "--quiet", "--no-checkout", "--origin", "origin", "--end-of-options",
         _url(src["git"], project), dest, transport=True)
    _git(dest, "checkout", "--quiet", "-B", FORK_BRANCH, commit)
    _git(dest, "config", "bench.upstream", src["ref"])
    path = f"{FORKS}/{name}" + (f"/{src['dir']}" if src.get("dir") else "")
    line = f'package({lua_lit(name)}, {{ path = {lua_lit(path)} }})  -- a fork of {src["git"]} {src["ref"]}'
    if _replace_line(os.path.join(project, BENCHFILE), name, line):
        log(f"Benchfile: \"{name}\" now comes from {path}")
    else:
        log(f"Benchfile: change the line of \"{name}\" to\n  {line}")
    return dest


def _replace_line(benchfile, name, line):
    """Replace the one-line declaration of NAME; False when it is not on one line (the person edits it)."""
    with open(benchfile, encoding="utf-8") as f:
        text = f.read()
    q = re.escape(name)
    rx = re.compile(r'^[ \t]*package[ \t]*\(?[ \t]*["\']' + q + r'["\'][^\n]*$', re.M)
    hits = rx.findall(text)
    if len(hits) != 1 or hits[0].count("{") != hits[0].count("}"):
        return False
    text = rx.sub(lambda m: line, text)
    with open(benchfile, "w", encoding="utf-8") as f:
        f.write(text)
    return True


def rebase(project, name, onto=None, identity=None, log=print):
    """Rebase the fork forks/<name> on its origin's ONTO (else the ref it was forked from), then run the package's
    tests. Returns {"rebased": bool, "conflicts": [...], "tests": result or None}."""
    project = os.path.abspath(project)
    dest = os.path.join(project, FORKS, name)
    if not os.path.isdir(os.path.join(dest, ".git")):
        raise BenchError(f"no fork {FORKS}/{name}: of bench fork {name} makes one")
    _git(dest, "fetch", "--quiet", "--no-tags", "origin", "+refs/heads/*:refs/remotes/origin/*",
         "+refs/tags/*:refs/tags/*", transport=True)
    ref = onto or _git(dest, "config", "bench.upstream", check=False).stdout.strip() or "HEAD"
    if not REF.fullmatch(ref) or ref.startswith("-") or ".." in ref:
        raise BenchError(f"{ref!r} is not a branch, tag or commit")
    target = None
    for cand in (f"refs/tags/{ref}", f"refs/remotes/origin/{ref}", ref):
        r = _git(dest, "rev-parse", "--verify", "--quiet", "--end-of-options", f"{cand}^{{commit}}", check=False)
        if r.returncode == 0:
            target = r.stdout.strip()
            break
    if not target:
        raise BenchError(f"origin has no {ref!r}")
    name_, email = identity or _identity(project)
    r = _git(dest, "-c", f"user.name={name_}", "-c", f"user.email={email}", "-c", "commit.gpgsign=false",
             "rebase", "--quiet", target, check=False)
    if r.returncode != 0:
        conflicts = _git(dest, "diff", "--name-only", "--diff-filter=U", check=False).stdout.split()
        log(f"the rebase stopped on a conflict in {', '.join(conflicts) or 'the fork'}; in {FORKS}/{name}: resolve "
            f"it and `git rebase --continue`, or `git rebase --abort` to go back")
        return {"rebased": False, "conflicts": conflicts, "tests": None}
    log(f"{FORKS}/{name}: rebased on {ref} ({target[:12]}); run of bench install to lock the new content")
    pkg = {p["name"]: p for p in read_benchfile(project)}.get(name)
    folder = package_dir(project, pkg) if pkg and "path" in pkg["source"] else dest
    return {"rebased": True, "conflicts": [], "tests": run_tests(folder, log=log)}


def _identity(project):
    """The person's git name and address, as their own git configuration gives them in the project."""
    vals = []
    for k in ("user.name", "user.email"):
        r = subprocess.run(["git", "-C", project, "config", "--get", k], capture_output=True, text=True, timeout=10)
        if r.returncode != 0 or not r.stdout.strip():
            raise BenchError(f"git has no {k} for this project, which a rebase needs: git config {k} \"…\"")
        vals.append(r.stdout.strip())
    return vals


UNITTEST = re.compile(r"^(test\w*) \(([\w.]+)\)(?:\n.*?)? \.\.\. (ok|FAIL|ERROR|skipped.*|expected failure|"
                      r"unexpected success)$", re.M)
TAP = re.compile(r"^(not ok|ok)\b\s*\d*\s*-?\s*(.*)$", re.M)


def run_tests(folder, timeout=900, log=print):
    """The package's tests (the spec's `tests`, a command run in its folder), and which of them hold:
    {"command", "exit", "held": [...], "broke": [...], "tail"}; None when the package names no tests."""
    spec = read_spec(folder)
    if not spec["tests"]:
        log(f"{spec['name']}: its spec names no tests")
        return None
    try:
        r = subprocess.run(spec["tests"], cwd=folder, capture_output=True, text=True, timeout=timeout)
        output, code = (r.stdout or "") + (r.stderr or ""), r.returncode
    except (OSError, subprocess.TimeoutExpired) as e:
        output, code = str(e), -1
    held, broke = [], []
    for m in UNITTEST.finditer(output):
        (held if m.group(3) in ("ok", "expected failure") or m.group(3).startswith("skipped") else broke).append(
            f"{m.group(2)}.{m.group(1)}" if not m.group(2).endswith(m.group(1)) else m.group(2))
    for m in TAP.finditer(output):
        (held if m.group(1) == "ok" else broke).append(m.group(2).strip())
    res = {"command": spec["tests"], "exit": code, "held": held, "broke": broke, "tail": output[-2000:]}
    log(f"{spec['name']} {spec['version']}: tests {'pass' if code == 0 else 'fail'} (exit {code}); "
        f"{len(held)} hold, {len(broke)} do not")
    for t in broke:
        log(f"  no longer holds: {t}")
    return res


# ---- the command ----

def project_of(args):
    if getattr(args, "project", None):
        return os.path.abspath(args.project)
    if getattr(args, "org", None):
        import of_protected
        for e in of_protected.entries():
            if e.get("org") == args.org and e.get("path"):
                return os.path.expanduser(e["path"])
        raise BenchError(f"orgs.yaml gives no path for {args.org}")
    return os.getcwd()


def main(args):
    try:
        return _main(args)
    except BenchError as e:
        print(f"of bench: {e}", file=sys.stderr)
        return 1


def _main(a):
    if a.bench_cmd == "read":
        words = {"spec": SPEC_WORDS, "benchfile": BENCHFILE_WORDS, "lock": LOCK_WORDS, "none": NO_WORDS}
        if a.words in ("method", "hook"):
            import of_method
            words = {"method": of_method.METHOD_WORDS, "hook": of_method.HOOK_WORDS}
            w = words[a.words]
        else:
            w = words[a.words]
        folder = os.path.dirname(os.path.abspath(a.file))
        files = lua_sources(folder) if a.words in ("spec", "method") else {}
        rel = os.path.basename(a.file)
        files[rel] = _read_text(a.file)
        print(json.dumps(evaluate(files, rel, w, name=a.file), indent=2, ensure_ascii=False))
        return 0
    project = project_of(a)
    if a.bench_cmd == "install":
        install(project, frozen=a.frozen)
        print(f"{LOCK} {'matches' if a.frozen else 'written'}")
        return 0
    if a.bench_cmd == "check":
        diffs = check(project)
        import of_method
        diffs += of_method.check(project)
        for name, rel, how in diffs:
            print(f"{name}: {rel + ' ' if rel else ''}{how}")
        if not diffs:
            print(f"every file matches {LOCK}")
        return 1 if diffs else 0
    if a.bench_cmd == "fork":
        fork(project, a.package)
        return 0
    if a.bench_cmd == "rebase":
        r = rebase(project, a.package, onto=a.onto)
        return 0 if r["rebased"] and (r["tests"] is None or r["tests"]["exit"] == 0) else 1
    if a.bench_cmd == "test":
        pkg = {p["name"]: p for p in read_benchfile(project)}.get(a.package)
        if not pkg:
            raise BenchError(f"the Benchfile names no package \"{a.package}\"")
        r = run_tests(package_dir(project, pkg))
        return 0 if r is None or r["exit"] == 0 else 1
    if a.bench_cmd == "hook":
        import of_method
        try:
            value = json.loads(a.input)
        except ValueError as e:
            raise BenchError(f"--input is not JSON: {e}")
        d = of_method.before(project, a.package, a.module, value)
        print(json.dumps(d, indent=2, ensure_ascii=False))
        return 0 if d["decision"] == "pass" else 2
    if a.bench_cmd == "settings":
        import of_method
        print(json.dumps(of_method.settings(project, a.package), indent=2, ensure_ascii=False))
        return 0
    raise BenchError(f"unknown command {a.bench_cmd}")


def add_parser(sub):
    b = sub.add_parser("bench", help="Bench, the package manager: install, check, fork, rebase (docs/bench.md)")
    bs = b.add_subparsers(dest="bench_cmd", required=True)
    for cmd, h in (("install", "install the Benchfile's packages and write Benchfile.lock"),
                   ("check", "say what differs from Benchfile.lock"),
                   ("fork", "clone a git package into forks/<name> and point the Benchfile at it"),
                   ("rebase", "rebase a fork on its origin and run the package's tests"),
                   ("test", "run a package's tests and say which hold"),
                   ("settings", "a package's settings as the organization set them, checked"),
                   ("hook", "call the organization's hooks before a module with a sample input, as the package would")):
        p = bs.add_parser(cmd, help=h)
        p.add_argument("--project", help="the organization's project folder (default: here)")
        p.add_argument("--org", help="the organization, whose path orgs.yaml gives")
        if cmd == "install":
            p.add_argument("--frozen", action="store_true", help="install exactly what the lock holds")
        if cmd in ("fork", "rebase", "test", "settings", "hook"):
            p.add_argument("package")
        if cmd == "hook":
            p.add_argument("module")
            p.add_argument("--input", required=True, help="the module's input as JSON, such as a card")
        if cmd == "rebase":
            p.add_argument("--onto", help="the origin's branch or tag (default: the one forked from)")
    r = bs.add_parser("read", help="evaluate one file in the closed reader and print its data")
    r.add_argument("file")
    r.add_argument("--words", default="none", choices=("spec", "benchfile", "lock", "method", "hook", "none"))
    return b

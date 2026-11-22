"""Bench's first slice (docs/bench.md; organic-framework-build reports/framework/metamethod/11-first-slice.md):
the closed reader (part 1, with its hostile set), the lock (part 2), settings and a hook before a module as OF reads
them (the mechanisms parts 4 and 5 need), and a fork with a rebase (part 6). Every reader test runs on each Lua
runtime this machine has; in CI that is Lua 5.4 and LuaJIT, and no runtime there is a failure, not a skip."""

import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "bin"))

import bench  # noqa: E402

# main.lua's words as a framework gives them (OF's are organic-framework's bin/of_method.py): the reader takes any
METHOD_WORDS = {"decl": ["roles", "subject", "setting", "module", "extendable", "event"],
                "value": ["skill", "tool", "command", "record", "schedule", "turn_end", "act", "role", "classifier"]}

# These tests are of the in-process layer (the closed environment, its budgets) and of what Bench does with files;
# the outer layer, the operating-system sandbox every real evaluation runs in, has its own tests (OuterLayerTest).
# It cannot start inside an agent session's own sandbox, where these tests also run, so they set it off in code.
bench.OUTER = "off"

FAST = {"instructions": 2_000_000}


def runtimes():
    found = []
    for name in ("lua5.4", "luajit", "lua5.1"):
        for d in bench.RUNTIME_DIRS:
            p = os.path.join(d, name)
            if os.path.isfile(p) and p not in found:
                found.append(p)
                break
    if not found and os.environ.get("CI"):
        raise AssertionError("no Lua runtime in CI: the unit job installs lua5.4 and luajit")
    return found


class _EachRuntime(unittest.TestCase):
    def each(self):
        rts = runtimes()
        if not rts:
            if os.environ.get("CI"):
                self.fail("no Lua runtime in CI")
            self.skipTest("no Lua runtime on this machine")
        saved = bench.RUNTIMES, bench.RUNTIME_DIRS
        try:
            for rt in rts:
                bench.RUNTIMES, bench.RUNTIME_DIRS = (os.path.basename(rt),), (os.path.dirname(rt),)
                with self.subTest(runtime=os.path.basename(rt)):
                    yield rt
        finally:
            bench.RUNTIMES, bench.RUNTIME_DIRS = saved

    def read(self, src, words=bench.NO_WORDS, files=None, **kw):
        files = dict(files or {})
        files["x.lua"] = src
        kw["limits"] = dict(FAST, **kw.get("limits", {}))
        return bench.evaluate(files, "x.lua", words, **kw)


# ---- part 1: the reader ----

SPEC_10 = '''
package {
  name    = "of-asks",
  version = "1.0.0",
  summary = "How an agent asks the owner a question, and how the owner sees and answers it",
  license = "Apache-2.0",
  source  = "https://github.com/organic-framework/of-asks",
  main    = "main.lua",
}

depends { "organic-framework ~> 1" }      -- as a gem that depends on Rails

needs {                                   -- what the package's code will need; shown to whoever installs it
  store = { records = { read = "question.*", write = "question.*" } },
  model = { "small" },
}
'''

MAIN_10 = '''
local card    = require "lib.card"
local routing = require "lib.routing"
local answer  = require "lib.answer"

roles { "asker", "decider" }

subject "question" {
  states = { "needed", "written", "filed", "delivered", "answered", "applied" },
  exits  = { "decided_by_asker", "withdrawn" },
}

setting "language"       { type = "language", default = "en" }
setting "escalate_after" { type = "duration", default = "2h" }

module "decide"     { by = skill "decide-to-ask" }
module "write-card" { by = skill "write-card", output = card.shape, must = card.checks }

module "file" {
  on  = command "ask",
  run = function(q, ctx)
    q.to = routing.decider(q, ctx.organization)
    return ctx.tool("file", q)
  end,
}

module "amend"   { on = command "ask --amend", by = tool "amend" }

module "deliver" {
  on  = record "question.filed",
  run = function(q, ctx) return ctx.show(q, ctx.interfaces_for(q.to)) end,
}

module "remind" {
  on  = schedule(function(s) return "every " .. s.escalate_after end),
  run = function(ctx)
    for _, q in ipairs(ctx.questions { state = "delivered", older_than = ctx.settings.escalate_after }) do
      ctx.escalate(q)
    end
  end,
}

module "answer"        { on = act "answer", by = role "decider", must = answer.checks }
module "take-answer"   { on = record "question.answered", by = skill "take-answer" }
module "spot-question" { on = turn_end(), by = classifier "question" }
module "read-card"     { on = command "ask", by = classifier "card-reader" }

extendable { "write-card", "file", "deliver", "answer" }
'''

LIB_10 = {
    "lib/routing.lua": '''
local M = {}

function M.decider(q, org)
  local unit = org.unit_of(q.from)
  if unit and unit.delegated and not q.escalated then return unit.head end
  return org.owner
end

return M
''',
    "lib/card.lua": 'return { shape = { "decision", "why_now", "context", "options" }, checks = { "no_time_estimates" } }',
    "lib/answer.lua": 'return { checks = { "from_a_person", "names_the_version" } }',
}

BENCHFILE_10 = '''
source "https://bench.useorganic.ai"

package "organic-framework"                     -- brings of-asks and the rest
package "alerts"
package "cost"                                  -- optional: counts what each package spends
package("onboarding",   { path = "forks/onboarding" })       -- its fork
package("cafe-support", { path = "methods/cafe-support" })   -- its own method
'''

OF_SPEC_10 = '''
package {
  name    = "organic-framework",
  version = "1.0.0",
  summary = "A method of organizing agents and people",
}

includes {
  "records      ~> 1",
  "structure    ~> 1",
  "of-asks    ~> 1",
  "claude-code  ~> 1",
}
'''


class ReaderExamplesTest(_EachRuntime):
    """Part 1: the example files of 10-metamethod-v2.md (sections 1, 3 and 6) evaluate to plain data."""

    def test_the_package_spec(self):
        for _ in self.each():
            r = self.read(SPEC_10, bench.SPEC_WORDS)
            words = [d["word"] for d in r["decls"]]
            self.assertEqual(words, ["package", "depends", "needs"])
            self.assertEqual(r["decls"][0]["body"]["version"], "1.0.0")
            self.assertEqual(r["decls"][2]["body"]["model"], ["small"])
            r = self.read(OF_SPEC_10, bench.SPEC_WORDS)
            self.assertIn("of-asks    ~> 1", r["decls"][1]["body"])

    def test_the_method_with_its_lib_files(self):
        for _ in self.each():
            r = self.read(MAIN_10, METHOD_WORDS, files=LIB_10)
            mods = {d["name"]: d["body"] for d in r["decls"] if d["word"] == "module"}
            self.assertEqual(len(mods), 10)                                   # ten modules
            self.assertEqual(mods["file"]["on"], {"@": "command", "args": ["ask"]})
            self.assertEqual(mods["file"]["run"]["@"], "function")             # kept, not called
            self.assertEqual(mods["spot-question"]["on"], {"@": "turn_end", "args": {}})
            self.assertEqual(mods["remind"]["on"]["args"][0]["@"], "function")
            self.assertEqual(mods["write-card"]["output"], ["decision", "why_now", "context", "options"])
            sets = [d for d in r["decls"] if d["word"] == "setting"]
            self.assertEqual([(d["name"], d["body"]["type"]) for d in sets],
                             [("language", "language"), ("escalate_after", "duration")])

    def test_the_benchfile_and_a_settings_file(self):
        for _ in self.each():
            r = self.read(BENCHFILE_10, bench.BENCHFILE_WORDS)
            self.assertEqual([(d["word"], d.get("name")) for d in r["decls"]][:3],
                             [("source", "https://bench.useorganic.ai"), ("package", "organic-framework"),
                              ("package", "alerts")])
            self.assertEqual(r["decls"][4]["body"], {"path": "forks/onboarding"})
            r = self.read('return { language = "en", escalate_after = "4h" }')
            self.assertEqual(r["returned"], {"language": "en", "escalate_after": "4h"})

    def test_a_kept_function_is_called_later_by_its_place(self):
        for _ in self.each():
            src = 'module "m" { run = function(x, ctx) return { got = x.n * 2, by = ctx.who } end }'
            r = self.read(src, {"decl": ["module"], "value": []})
            path = r["decls"][0]["body"]["run"]["path"]
            out = self.read(src, {"decl": ["module"], "value": []}, mode="call", path=path,
                            args=({"n": 21}, {"who": "test"}))
            self.assertEqual(out["value"], {"got": 42, "by": "test"})

    def test_an_unknown_word_is_refused(self):
        for _ in self.each():
            with self.assertRaisesRegex(bench.BenchError, 'unknown name "modul"'):
                self.read('modul "x" {}', METHOD_WORDS)


class ReaderHostileTest(_EachRuntime):
    """Part 1's hostile set: each case is refused or stopped, one test per case (11-first-slice.md, part 1)."""

    def refused(self, src, files=None):
        for _ in self.each():
            with self.assertRaises(bench.BenchError) as cm:
                self.read(src, files=files)
            self.assertNotIsInstance(cm.exception, bench.Stopped)

    def stopped(self, src, reason, **kw):
        for _ in self.each():
            with self.assertRaises(bench.Stopped) as cm:
                self.read(src, **kw)
            self.assertEqual(cm.exception.reason, reason)

    # reading a file
    def test_reading_a_file(self):
        self.refused('return io.open("/etc/hosts"):read("*a")')

    def test_reading_a_file_by_dofile_or_a_loader(self):
        for src in ('return dofile("/etc/hosts")', 'return loadfile("/etc/hosts")', 'return load("return 1")',
                    'return loadstring("return 1")'):
            self.refused(src)

    def test_reaching_the_library_through_the_string_metatable(self):
        for src in ('return getmetatable("").__index.dump', 'return string.dump(print)',
                    'return ("x").dump("y")', 'return rawget(string, "dump")("x")'):
            self.refused(src)

    def test_reaching_the_globals_of_the_runner(self):
        for src in ('return _G.io', 'return _ENV.io', 'return getfenv(0).io', 'return debug.getregistry()',
                    'return collectgarbage("count")', 'return jit.util', 'return rawget(_G, "io")'):
            self.refused(src)

    # opening a socket
    def test_opening_a_socket(self):
        for src in ('return require("socket").connect("127.0.0.1", 80)',
                    'local ffi = require("ffi") ffi.cdef("int socket(int, int, int);") return ffi.C.socket(2, 1, 0)'):
            self.refused(src)

    # starting a process
    def test_starting_a_process(self):
        for src in ('return os.execute("true")', 'return io.popen("true"):read("*a")',
                    'return package.loadlib("libc.so.6", "system")'):
            self.refused(src)

    def test_the_process_writes_nothing(self):
        for _ in self.each():
            d = tempfile.mkdtemp()
            try:
                with self.assertRaises(bench.BenchError):
                    self.read(f'local f = io.open("{d}/out", "w") f:write("x") f:close()')
                self.assertEqual(os.listdir(d), [])
            finally:
                shutil.rmtree(d)

    # require outside the package's folder
    def test_require_outside_the_package(self):
        files = {"lib/ok.lua": "return 1"}
        for src in ('return require("../outside")', 'return require("/etc/hosts")', 'return require("..lib.ok")',
                    'return require("lib..ok")', 'return require("io")', 'return require("os")'):
            self.refused(src, files=files)
        for _ in self.each():
            self.assertEqual(self.read('return require("lib.ok")', files=files)["returned"], 1)

    def test_a_compiled_chunk_is_not_loaded(self):
        for _ in self.each():
            with self.assertRaisesRegex(bench.BenchError, "compiled chunk"):
                bench.evaluate({"x.lua": "\x1bLua"}, "x.lua", bench.NO_WORDS, limits=FAST)
            with self.assertRaisesRegex(bench.BenchError, "compiled chunk"):
                self.read('return require("lib.b")', files={"lib/b.lua": "\x1bLJ"})

    # a loop that does not end
    def test_a_loop_that_does_not_end(self):
        self.stopped("while true do end", "instructions")

    def test_a_loop_that_catches_its_errors(self):
        self.stopped("while true do pcall(function() while true do end end) end", "instructions")

    def test_a_loop_in_a_coroutine(self):
        self.stopped("local f = coroutine.wrap(function() while true do end end) f()", "instructions")

    def test_a_pattern_that_does_not_end(self):
        self.stopped('return string.find(string.rep("a", 60000), "a-a-a-a-a-a-a-a-b")', "time", wall_s=2)

    # memory that does not stop growing
    def test_memory_that_grows_in_a_table(self):
        self.stopped("local t = {} local i = 0 while true do i = i + 1 t[i] = {i, i, i} end", "memory",
                     limits={"instructions": 200_000_000})

    def test_memory_that_doubles_a_string(self):
        self.stopped('local s = "x" while true do s = s .. s end', "memory")

    def test_memory_in_one_library_call(self):
        for src in ('return string.rep("x", 2^40)', 'return ("x"):rep(2^31)',
                    'local s = string.rep("x", 100000) return s:gsub(".", string.rep("y", 1000))',
                    'local t = {} for i = 1, 200 do t[i] = string.rep("x", 100000) end return table.concat(t)'):
            self.refused(src)

    def test_memory_in_one_library_call_by_the_ways_round_its_count(self):
        # of_bench's review, 2026-11-23: a count written as text, format, a join whose values change between reads
        unpack = "(table.unpack or unpack)"
        for src in ('return ("x"):rep("500000000")',
                    'local b = ("x"):rep(8000000) local t = {} for i = 1, 100 do t[i] = b end '
                    f'return string.format(("%s"):rep(100), {unpack}(t))',
                    'local b = ("x"):rep(8000000) local o = setmetatable({}, {__tostring = function() return b end}) '
                    f'local t = {{}} for i = 1, 100 do t[i] = o end return string.format(("%s"):rep(100), {unpack}(t))',
                    'local b, n = ("x"):rep(8000000), 0 local t = setmetatable({}, {__index = function() n = n + 1 '
                    'if n % 2 == 1 then return "" end return b end}) '
                    'return table.concat(t, "", 1, 60)'):
            self.refused(src)

    def test_a_finalizer_is_not_given(self):
        # Lua 5.4 runs __gc with every hook off: a finalizer would spend outside the budgets
        self.refused('for i = 1, 10 do setmetatable({}, {__gc = function() while true do end end}) end return 1')

    def test_a_key_written_twice_is_refused(self):
        self.refused('return { [1] = "a", ["1"] = "b" }')

    def test_the_library_still_works_within_its_caps(self):
        for _ in self.each():
            r = self.read('return { s = string.format("%s=%d %q", "a", 3, "b"), r = ("ab"):rep("3"), '
                          'c = table.concat({"a", "b"}, ",") }')
            self.assertEqual(r["returned"], {"s": 'a=3 "b"', "r": "ababab", "c": "a,b"})

    # of_server's reading, message 01a20547-5da1 (2026-11-23)
    def peak_kb(self):
        import resource
        return resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss   # KiB on Linux; the largest child so far

    def test_a_long_concatenation_is_stopped_with_a_bounded_peak(self):
        many = "..".join(["b"] * 40)
        for _ in self.each():
            with self.assertRaises(bench.Stopped) as cm:
                self.read(f'local b = ("x"):rep(8000000) return {many}', limits={"instructions": 200_000_000})
            self.assertEqual(cm.exception.reason, "memory")
        if sys.platform.startswith("linux"):
            self.assertLess(self.peak_kb(), (bench.PROCESS_MB + 16) * 1024)

    def test_gsub_with_a_number_is_capped(self):
        self.refused('local s = ("x"):rep(1000000) return s:gsub(".", 123456789012)')

    def test_a_to_be_closed_value_is_not_given(self):
        self.refused('setmetatable({}, {__close = function() while true do end end}) return 1')

    def test_recursion_through_C_is_an_error_not_a_crash(self):
        for src in ('local function f(s) return (s:gsub(".", f)) end return f("ab")',
                    'local function r() return coroutine.wrap(r)() end return r()',
                    'local function r() return coroutine.resume(coroutine.create(r)) end return r()',
                    'local function r() return pcall(r) end return r()',
                    'local t = {3, 1, 2} local function c(a, b) table.sort({2, 1}, c) return a < b end '
                    'table.sort(t, c) return t'):
            for _ in self.each():
                try:     # refused, or caught by the file's own pcall or resume and returned: never a crash
                    r = self.read(src)
                    self.assertEqual(r["returned"], True)
                except bench.BenchError as e:
                    self.assertIn("nested deeper than", str(e))

    def test_a_reading_holds_no_address(self):
        for _ in self.each():
            self.assertEqual(self.read("return tostring({}) .. tostring(type)")["returned"], "tablefunction")
        self.refused('return string.format("%p", {})')

    def test_a_number_past_2_53_and_a_text_not_utf8_are_refused(self):
        for src in ("return 2^53", "return -2^63", "return 1e300", "return { [2^63] = 1 }", 'return "\\255"'):
            self.refused(src)
        for _ in self.each():
            self.assertEqual(self.read("return 2^53 - 1")["returned"], 2 ** 53 - 1)

    # of_server's second reading, message 01a20573-e987
    def test_no_address_through_a_first_argument(self):
        for _ in self.each():
            r = self.read('return { (string.gsub({}, "x", "y")), string.format(type), string.format("%%p"), '
                          'table.concat({"a", "b"}, {}) }')["returned"]
            self.assertEqual(r, ["table", "function", "%p", "atableb"])

    def test_a_closing_key_set_later_is_refused(self):
        for src in ('local t = setmetatable({}, {}) getmetatable(t).__close = function() end return 1',
                    'local t = setmetatable({}, {}) getmetatable(t).__gc = function() end return 1',
                    'local t = setmetatable({}, {}) rawset(getmetatable(t), "__close", function() end) return 1',
                    # the guard cannot be taken off the copy, nor the copy read back (of_server, 01a20583-01b4)
                    'local t = setmetatable({}, {}) setmetatable(getmetatable(t), nil) '
                    'getmetatable(t).__close = function() end return 1',
                    'local t = setmetatable({}, {}) local g = getmetatable(getmetatable(t)) g.__newindex = nil return 1',
                    # the table the file passed is not the one installed: closing it later reaches nothing
                    'local mt = {} local t = setmetatable({}, mt) mt.__close = function() while true do end end '
                    'local ok = pcall(function() for _ in function() return nil end, nil, nil, t do end end) '
                    'error("no closing")'):
            self.refused(src)

    def test_an_installed_metatable_still_works_and_is_not_given_out(self):
        for _ in self.each():
            r = self.read('local t = setmetatable({}, { __index = function(_, k) return k .. "!" end, '
                          '__tostring = function() return "T" end }) return { t.a, tostring(t), getmetatable(t) }')
            self.assertEqual(r["returned"], ["a!", "T", False])

    def test_the_random_numbers_are_not_given(self):
        for _ in self.each():
            self.assertEqual(self.read("return { math.random == nil, math.randomseed == nil }")["returned"],
                             [True, True])

    def test_bwraps_status_names_the_runtime_and_a_failed_watch_ends_it(self):
        r, w = os.pipe()
        os.write(w, b'{ "child-pid": 4242 }\n')
        os.close(w)
        try:
            self.assertEqual(bench._child_pid(r, bytearray()), 4242)
        finally:
            os.close(r)
        saved = sys.platform, bench.subprocess.run
        sys.platform = "darwin"
        bench.subprocess.run = lambda *a, **k: (_ for _ in ()).throw(bench.subprocess.TimeoutExpired("ps", 2))
        try:
            with self.assertRaises(bench._WatchFailed):
                bench._rss_kb(2 ** 30)      # no /proc entry for it: the macOS path, where ps times out
        finally:
            sys.platform, bench.subprocess.run = saved

    def test_the_watch_counts_a_whole_process_tree(self):
        # under bwrap the runtime is a grandchild of what bwrap names (of_lab, message 01a2057d-5bde): an outer
        # process whose child holds the memory is measured as the tree
        if not sys.platform.startswith("linux"):
            self.skipTest("the tree is read from /proc")
        p = subprocess.Popen(["/bin/sh", "-c", 'exec 3<&0; python3 -c "import sys; b = bytearray(80 << 20); '
                              'b[::4096] = b\\"x\\" * len(b[::4096]); sys.stdout.write(\\"ok\\\\n\\"); '
                              'sys.stdout.flush(); sys.stdin.read()"; :'],
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        try:
            self.assertEqual(p.stdout.readline().strip(), "ok")
            self.assertLess(bench._rss_kb(p.pid), 20 * 1024)            # the shell itself is small
            self.assertGreater(bench._tree_rss_kb(p.pid), 70 * 1024)    # its tree holds the 80 MiB
        finally:
            p.stdin.close()
            p.wait(10)

    def test_a_limit_that_cannot_be_set_stops_the_start(self):
        saved = bench._limits
        bench._limits = lambda: (_ for _ in ()).throw(OSError("refused"))
        try:
            for _ in self.each():
                with self.assertRaisesRegex(bench.BenchError, "limits could not be set"):
                    self.read("return 1")
        finally:
            bench._limits = saved

    def test_CI_runs_every_runtime(self):
        # in CI the hostile set must run on Lua 5.4, LuaJIT and Lua 5.1 (of_server, message 01a20547-5da1), never skip
        if not os.environ.get("CI"):
            self.skipTest("CI only")
        self.assertEqual(sorted(os.path.basename(r) for r in runtimes()), ["lua5.1", "lua5.4", "luajit"])

    def test_only_lua_5_1_and_5_4_are_taken(self):
        for rt in runtimes():
            self.assertIn(bench._version(rt), ("Lua 5.1", "Lua 5.4"))

    def test_a_cycle_or_a_thread_cannot_leave(self):
        for src in ("local t = {} t.t = t return t", "return coroutine.create(function() end)"):
            self.refused(src)


# ---- a package and a project on disk ----

def git(cwd, *args):
    return subprocess.run(["git", "-C", cwd, "-c", "commit.gpgsign=false", "-c", "tag.gpgsign=false",
                           "-c", "user.name=t", "-c", "user.email=t@example.invalid", *args],
                          capture_output=True, text=True, check=True).stdout.strip()


def write(root, files):
    for rel, text in files.items():
        p = os.path.join(root, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w") as f:
            f.write(textwrap.dedent(text).lstrip())


PKG = {
    "Benchfile.spec": '''
        package { name = "demo-asks", version = "1.0.0", main = "main.lua",
                  tests = { "sh", "tests/run.sh" } }
        depends { "organic-framework ~> 1" }
        ''',
    "main.lua": '''
        local routing = require "lib.routing"
        setting "language"       { type = "language", default = "en" }
        setting "escalate_after" { type = "duration", default = "2h" }
        module "write-card" { by = skill "write-card" }
        module "file" { on = command "ask", run = function(q, ctx) return routing.to(q) end }
        module "remind" { on = schedule(function(s) return "every " .. s.escalate_after end) }
        extendable { "write-card", "file" }
        ''',
    "lib/routing.lua": 'return { to = function(q) return q.to or "owner" end }\n',
    "tests/run.sh": '''
        echo "ok 1 - a card has a decision"
        if grep -q "local routing" main.lua; then echo "ok 2 - routing is required"; else echo "not ok 2 - routing is required"; exit 1; fi
        ''',
}


class _Project(unittest.TestCase):
    def setUp(self):
        if not runtimes():
            if os.environ.get("CI"):
                self.fail("no Lua runtime in CI")
            self.skipTest("no Lua runtime on this machine")
        self.tmp = tempfile.mkdtemp()
        self.state = os.path.join(self.tmp, "state")
        self._saved = bench.STATE_DIR
        bench.STATE_DIR = self.state
        self.upstream = os.path.join(self.tmp, "demo-asks")
        os.makedirs(self.upstream)
        write(self.upstream, PKG)
        git(self.upstream, "init", "-q", "-b", "main")
        git(self.upstream, "add", "-A")
        git(self.upstream, "commit", "-qm", "1.0.0")
        git(self.upstream, "tag", "v1.0.0")
        self.org = os.path.join(self.tmp, "linden")
        os.makedirs(self.org)
        write(self.org, {"Benchfile": f'package("demo-asks", {{ git = "{self.upstream}", ref = "main" }})\n'})
        self.logs = []

    def tearDown(self):
        bench.STATE_DIR = self._saved
        shutil.rmtree(self.tmp, ignore_errors=True)

    def install(self, **kw):
        return bench.install(self.org, log=self.logs.append, **kw)


class LockTest(_Project):
    """Part 2: the lock holds the commit and a digest for git, a digest for a path; a second install from the lock
    gives the same files; a changed file is noticed."""

    def test_git_source_commit_and_digest(self):
        e = self.install()["demo-asks"]
        head = git(self.upstream, "rev-parse", "HEAD")
        self.assertEqual(e["source"]["commit"], head)
        self.assertTrue(e["digest"].startswith("sha256:"))
        lock = bench.read_lock(self.org)                     # the lock is Lua, read back by the closed reader
        self.assertEqual(lock["demo-asks"]["source"]["commit"], head)
        self.assertEqual(lock["demo-asks"]["digest"], e["digest"])
        self.assertIn("main.lua", lock["demo-asks"]["files"])

    def test_a_second_install_from_the_lock_gives_the_same_files(self):
        self.install()
        before = bench.folder_files(os.path.join(self.org, ".bench", "packages", "demo-asks"))
        with open(os.path.join(self.org, bench.LOCK)) as f:
            lock_text = f.read()
        # the source moves on; the lock holds the install where it was
        write(self.upstream, {"lib/routing.lua": 'return { to = function(q) return "finance" end }\n'})
        git(self.upstream, "commit", "-qam", "1.1.0")
        shutil.rmtree(os.path.join(self.org, ".bench"))
        self.install(frozen=True)
        after = bench.folder_files(os.path.join(self.org, ".bench", "packages", "demo-asks"))
        self.assertEqual(before, after)
        with open(os.path.join(self.org, bench.LOCK)) as f:
            self.assertEqual(f.read(), lock_text)

    def test_a_changed_file_is_noticed(self):
        self.install()
        p = os.path.join(self.org, ".bench", "packages", "demo-asks", "lib", "routing.lua")
        with open(p, "a") as f:
            f.write("-- changed\n")
        self.assertEqual(bench.check(self.org), [("demo-asks", "lib/routing.lua", "changed")])
        os.remove(os.path.join(self.org, ".bench", "packages", "demo-asks", "main.lua"))
        self.assertIn(("demo-asks", "main.lua", "removed"), bench.check(self.org))

    def test_a_path_source_has_a_digest_and_a_change_is_noticed(self):
        local = os.path.join(self.org, "methods", "demo-asks")
        shutil.copytree(self.upstream, local, ignore=shutil.ignore_patterns(".git"))
        write(self.org, {"Benchfile": 'package("demo-asks", { path = "methods/demo-asks" })\n'})
        e = self.install()["demo-asks"]
        self.assertEqual(e["source"], {"path": "methods/demo-asks"})
        self.assertTrue(e["digest"].startswith("sha256:"))
        self.assertEqual(bench.check(self.org), [])
        write(local, {"main.lua": PKG["main.lua"] + "\n-- the organization's line\n"})
        self.assertEqual(bench.check(self.org), [("demo-asks", "main.lua", "changed")])
        with self.assertRaisesRegex(bench.BenchError, "main.lua changed since the lock"):
            self.install(frozen=True)

    def test_a_link_in_a_package_is_refused(self):
        os.symlink("/etc/hosts", os.path.join(self.upstream, "hosts"))
        git(self.upstream, "add", "hosts")
        git(self.upstream, "commit", "-qm", "a link")
        with self.assertRaisesRegex(bench.BenchError, "a link in a package"):
            self.install()


    # of_bench's review, 2026-11-23
    def test_a_package_without_ref_takes_the_source_HEAD_on_main(self):
        write(self.org, {"Benchfile": f'package("demo-asks", {{ git = "{self.upstream}" }})\n'})
        e = self.install()["demo-asks"]
        self.assertEqual(e["source"]["commit"], git(self.upstream, "rev-parse", "HEAD"))

    def test_an_executable_bit_and_a_file_in_pycache_are_noticed(self):
        local = os.path.join(self.org, "methods", "demo-asks")
        shutil.copytree(self.upstream, local, ignore=shutil.ignore_patterns(".git"))
        write(self.org, {"Benchfile": 'package("demo-asks", { path = "methods/demo-asks" })\n'})
        self.install()
        os.chmod(os.path.join(local, "main.lua"), 0o755)
        self.assertEqual(bench.check(self.org), [("demo-asks", "main.lua", "changed")])
        os.chmod(os.path.join(local, "main.lua"), 0o644)
        write(local, {"lib/__pycache__/routing.cpython-314.pyc": "planted"})
        self.assertEqual(bench.check(self.org), [("demo-asks", "lib/__pycache__/routing.cpython-314.pyc", "added")])
        with self.assertRaisesRegex(bench.BenchError, "since the lock"):
            self.install(frozen=True)

    def test_frozen_refuses_a_source_that_differs_from_the_lock(self):
        self.install()
        other = os.path.join(self.tmp, "elsewhere")
        shutil.copytree(self.upstream, other)
        write(self.org, {"Benchfile": f'package("demo-asks", {{ git = "{other}", ref = "main" }})\n'})
        with self.assertRaisesRegex(bench.BenchError, "source differs from the lock"):
            self.install(frozen=True)

    def test_the_tests_get_only_a_short_environment(self):
        write(self.upstream, {"Benchfile.spec": PKG["Benchfile.spec"],
                              "tests/run.sh": 'if [ -n "${BENCH_PROBE_SECRET:-}" ]; then echo "not ok 1 - env"; '
                                              'exit 1; fi\necho "ok 1 - env"\n'})
        os.environ["BENCH_PROBE_SECRET"] = "x"
        try:
            r = bench.run_tests(self.upstream, log=self.logs.append)
        finally:
            del os.environ["BENCH_PROBE_SECRET"]
        self.assertEqual((r["exit"], r["held"]), (0, ["env"]))
        self.assertTrue(any("a program of the package's own" in x for x in self.logs))


class ForkTest(_Project):
    """Part 6: a clone of the package with one commit of the organization's; the package gets a new version; the
    fork is rebased on it; the package's tests run on the result and say what still holds."""

    def test_fork_commit_upgrade_rebase_and_tests(self):
        self.install()
        dest = bench.fork(self.org, "demo-asks", log=self.logs.append)
        self.assertEqual(git(dest, "remote", "get-url", "origin"), "file://" + self.upstream)
        self.assertEqual(git(dest, "rev-parse", "--abbrev-ref", "HEAD"), bench.FORK_BRANCH)
        self.assertEqual(bench.read_benchfile(self.org)[0]["source"], {"path": "forks/demo-asks"})
        # the organization's commit
        write(dest, {"lib/routing.lua": 'return { to = function(q) return q.to or "finance" end }\n'})
        git(dest, "commit", "-qam", "route to finance by default")
        # the package's new version, touching another file
        spec = PKG["Benchfile.spec"].replace('"1.0.0"', '"1.1.0"')
        write(self.upstream, {"Benchfile.spec": spec, "README.md": "1.1.0\n"})
        git(self.upstream, "add", "-A")
        git(self.upstream, "commit", "-qm", "1.1.0")
        r = bench.rebase(self.org, "demo-asks", identity=("t", "t@example.invalid"), log=self.logs.append)
        self.assertTrue(r["rebased"])
        self.assertEqual(bench.read_spec(dest)["version"], "1.1.0")
        with open(os.path.join(dest, "lib", "routing.lua")) as f:
            self.assertIn("finance", f.read())
        self.assertEqual(r["tests"]["exit"], 0)
        self.assertEqual(r["tests"]["held"], ["a card has a decision", "routing is required"])
        self.install()                                              # the lock takes the fork as a path
        self.assertEqual(bench.read_lock(self.org)["demo-asks"]["version"], "1.1.0")

    def test_the_tests_say_what_no_longer_holds(self):
        self.install()
        dest = bench.fork(self.org, "demo-asks", log=self.logs.append)
        write(dest, {"main.lua": PKG["main.lua"].replace("local routing", "local route")
                     .replace("routing.to", "route.to")})
        git(dest, "commit", "-qam", "rename")
        write(self.upstream, {"README.md": "1.0.1\n"})
        git(self.upstream, "add", "-A")
        git(self.upstream, "commit", "-qm", "1.0.1")
        r = bench.rebase(self.org, "demo-asks", identity=("t", "t@example.invalid"), log=self.logs.append)
        self.assertTrue(r["rebased"])
        self.assertNotEqual(r["tests"]["exit"], 0)
        self.assertEqual(r["tests"]["broke"], ["routing is required"])
        self.assertTrue(any("no longer holds: routing is required" in x for x in self.logs))

    def test_a_conflict_is_left_for_the_person(self):
        self.install()
        dest = bench.fork(self.org, "demo-asks", log=self.logs.append)
        write(dest, {"lib/routing.lua": 'return { to = function(q) return "a" end }\n'})
        git(dest, "commit", "-qam", "ours")
        write(self.upstream, {"lib/routing.lua": 'return { to = function(q) return "b" end }\n'})
        git(self.upstream, "commit", "-qam", "theirs")
        r = bench.rebase(self.org, "demo-asks", identity=("t", "t@example.invalid"), log=self.logs.append)
        self.assertEqual((r["rebased"], r["conflicts"]), (False, ["lib/routing.lua"]))
        self.assertTrue(any("git rebase --abort" in x for x in self.logs))


CONNECT_PROBE = r"""
local ok, ffi = pcall(require, "ffi")
if not ok then io.write("no ffi") return end
ffi.cdef[[ int socket(int, int, int); int connect(int, const void *, unsigned int); ]]
local addr = ffi.new("uint8_t[16]")
if ffi.os == "OSX" then addr[0] = 16; addr[1] = 2 else addr[0] = 2; addr[1] = 0 end
addr[2], addr[3] = 0x1f, 0x90                       -- port 8080
addr[4], addr[5], addr[6], addr[7] = 127, 0, 0, 1
local s = ffi.C.socket(2, 1, 0)                     -- AF_INET, SOCK_STREAM
if s < 0 then io.write("denied") return end
local rc = ffi.C.connect(s, addr, 16)
local e = ffi.errno()
-- 0: connected; ECONNREFUSED (61 on macOS, 111 on Linux): the network is there and nothing listens
if rc == 0 or e == 61 or e == 111 then io.write("reached " .. tostring(e)) else io.write("denied") end
"""


class OuterLayerTest(unittest.TestCase):
    """The outer layer (of_server, message 01a20566-e23c): every evaluation inside bwrap (Linux) or sandbox-exec
    (macOS), and nothing evaluated when it cannot start. Where it cannot start (an agent session's sandbox) only the
    refusal is tested; CI and the Lab, where it starts, run the rest, and in CI it must start."""

    def setUp(self):
        if not runtimes():
            self.skipTest("no Lua runtime on this machine")
        bench.OUTER = "required"

    def tearDown(self):
        bench.OUTER = "off"

    def starts(self):
        try:
            bench.evaluate({"x.lua": "return 1"}, "x.lua", bench.NO_WORDS)
            return True
        except bench.BenchError as e:
            self.assertIn("the outer layer", str(e))
            return False

    @staticmethod
    def in_a_session_sandbox():
        """True only where the kernel refuses a new namespace because this runs inside another sandbox's (an agent
        session's): the one place the layer may not start without a test failing (of_server, message
        01a20583-01b4: a skip had hidden a profile that did not start on macOS)."""
        if not sys.platform.startswith("linux") or not os.access(bench.BWRAP, os.X_OK):
            return False
        r = subprocess.run([bench.BWRAP, "--unshare-user", "--ro-bind", "/", "/", "/usr/bin/true"],
                           capture_output=True, text=True, timeout=10)
        return r.returncode != 0 and "Creating new namespace failed" in r.stderr

    def test_nothing_is_evaluated_without_it(self):
        if self.starts():
            saved = bench.BWRAP, bench.SANDBOX_EXEC
            bench.BWRAP = bench.SANDBOX_EXEC = "/nonexistent/sandbox"
            try:
                with self.assertRaisesRegex(bench.BenchError, "the outer layer"):
                    bench.evaluate({"x.lua": "return 1"}, "x.lua", bench.NO_WORDS)
            finally:
                bench.BWRAP, bench.SANDBOX_EXEC = saved
        else:
            with self.assertRaisesRegex(bench.BenchError, "the outer layer"):
                bench.evaluate({"x.lua": "return 1"}, "x.lua", bench.NO_WORDS)

    def layer_runs(self, lua):
        """LUA run by the bare runtime, with its whole library, inside the outer layer: what native code that got
        past the closed environment would meet."""
        rt = bench.runtime()
        cmd = bench._outer(rt)
        cmd = cmd[:-1] + ["-e", lua]
        return subprocess.run(cmd, capture_output=True, text=True, timeout=30)

    def test_inside_it_nothing_of_the_machine_is_reached(self):
        if not self.starts():
            if self.in_a_session_sandbox() and not os.environ.get("CI"):
                self.skipTest("the outer layer cannot start here (an agent session's sandbox): CI and the Lab run this")
            self.fail("the outer layer does not start on this machine (not inside another sandbox)")
        self.assertEqual(bench.evaluate({"x.lua": "return 1 + 1"}, "x.lua", bench.NO_WORDS)["returned"], 2)
        # each io.open must give nil (nothing there to open); os.execute must not succeed (5.4 says true, 5.1 says 0)
        # files of the machine, of its system data and of its people, on Linux and macOS (of_lab on macOS, message
        # 01a20573-1c46: a profile that imported bsd.sb read /etc/passwd); a path that does not exist gives nil too
        machine = ["/etc/passwd", "/etc/hosts", "/private/etc/hosts", "/home", "/Users", "/Users/Shared/.localized",
                   "/Library/Preferences/.GlobalPreferences.plist", "/private/var/db/.AppleSetupDone",
                   "/opt/homebrew/etc", "/var/log", "/root", "/Volumes", "/System/Volumes/Data/Users",
                   "/System/Volumes/Data/private/etc/hosts", "/usr/local/etc"]
        home = os.path.expanduser("~")
        machine += [os.path.join(home, ".ssh"), os.path.join(home, ".ssh", "known_hosts")]
        try:   # a real file of the caller's home folder, whatever it holds
            # on macOS ~/.CFUserTextEncoding (the user's text-encoding number, a few bytes) stays readable under any
            # file-read rule of the profile: CoreFoundation reads it at a process's start (of_lab, Darwin 27, message
            # 01a2057f-d0d2), a stated residual with bsd.sb's; every other file of the home is probed
            platform_files = {".CFUserTextEncoding"} if sys.platform == "darwin" else set()
            machine += [os.path.join(home, n) for n in sorted(os.listdir(home))
                        if os.path.isfile(os.path.join(home, n)) and n not in platform_files][:2]
        except OSError:
            pass
        probes = {f"a read of {p}": f'io.write(tostring(io.open("{p}")))' for p in machine}
        probes["a write"] = 'io.write(tostring(io.open("/tmp/bench-outer-probe", "w")))'
        for what, lua in probes.items():
            r = self.layer_runs(lua)
            self.assertEqual((r.returncode, r.stdout.strip()), (0, "nil"), f"{what} was reached: {r.stdout!r} {r.stderr!r}")
        # the watch follows the runtime inside the layer, not the layer's own small process (of_server, message
        # 01a20573-e987): with the address-space limit lifted for this test, it alone must end a runtime that grows
        saved = bench._limits, bench.PROCESS_MB
        bench.PROCESS_MB = 96
        def no_as():
            import resource
            resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        bench._limits = no_as
        try:
            with self.assertRaises(bench.Stopped) as cm:
                bench.evaluate({"x.lua": 'local t = {} for i = 1, 40 do t[i] = ("x"):rep(8000000 - i) end return #t'},
                               "x.lua", bench.NO_WORDS, limits={"memory_kb": 2 ** 21, "instructions": 10 ** 9})
            self.assertEqual(cm.exception.reason, "memory")
        finally:
            bench._limits, bench.PROCESS_MB = saved
        # no connection to 127.0.0.1 (of_server, message 01a2057b-28d4), tried where the runtime has an FFI (LuaJIT):
        # a refused connection (nothing listens) would mean the network is there; a denial or no route means it is not
        r = self.layer_runs(CONNECT_PROBE)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn(r.stdout.strip(), ("denied", "no ffi"), f"127.0.0.1 was reached: {r.stdout!r}")
        r = self.layer_runs('io.write(tostring(os.execute("/bin/sh -c true")))')
        self.assertNotIn(r.stdout.strip(), ("true", "0"), f"a program ran inside the outer layer: {r.stderr!r}")
        self.assertFalse(os.path.exists("/tmp/bench-outer-probe"))
        r = self.layer_runs('local f = io.popen("cat /proc/net/route 2>/dev/null") io.write(tostring(f and f:read("*a")))')
        self.assertNotIn("00000000", r.stdout)   # no route out: no network


if __name__ == "__main__":
    unittest.main()

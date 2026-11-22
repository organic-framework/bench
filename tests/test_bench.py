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

import of_bench  # noqa: E402
import of_method  # noqa: E402

FAST = {"instructions": 2_000_000}


def runtimes():
    found = []
    for name in ("lua5.4", "luajit", "lua5.1"):
        for d in of_bench.RUNTIME_DIRS:
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
            self.skipTest("no Lua runtime on this machine")
        saved = of_bench.RUNTIMES, of_bench.RUNTIME_DIRS
        try:
            for rt in rts:
                of_bench.RUNTIMES, of_bench.RUNTIME_DIRS = (os.path.basename(rt),), (os.path.dirname(rt),)
                with self.subTest(runtime=os.path.basename(rt)):
                    yield rt
        finally:
            of_bench.RUNTIMES, of_bench.RUNTIME_DIRS = saved

    def read(self, src, words=of_bench.NO_WORDS, files=None, **kw):
        files = dict(files or {})
        files["x.lua"] = src
        kw["limits"] = dict(FAST, **kw.get("limits", {}))
        return of_bench.evaluate(files, "x.lua", words, **kw)


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
            r = self.read(SPEC_10, of_bench.SPEC_WORDS)
            words = [d["word"] for d in r["decls"]]
            self.assertEqual(words, ["package", "depends", "needs"])
            self.assertEqual(r["decls"][0]["body"]["version"], "1.0.0")
            self.assertEqual(r["decls"][2]["body"]["model"], ["small"])
            r = self.read(OF_SPEC_10, of_bench.SPEC_WORDS)
            self.assertIn("of-asks    ~> 1", r["decls"][1]["body"])

    def test_the_method_with_its_lib_files(self):
        for _ in self.each():
            r = self.read(MAIN_10, of_method.METHOD_WORDS, files=LIB_10)
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
            r = self.read(BENCHFILE_10, of_bench.BENCHFILE_WORDS)
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
            with self.assertRaisesRegex(of_bench.BenchError, 'unknown name "modul"'):
                self.read('modul "x" {}', of_method.METHOD_WORDS)


class ReaderHostileTest(_EachRuntime):
    """Part 1's hostile set: each case is refused or stopped, one test per case (11-first-slice.md, part 1)."""

    def refused(self, src, files=None):
        for _ in self.each():
            with self.assertRaises(of_bench.BenchError) as cm:
                self.read(src, files=files)
            self.assertNotIsInstance(cm.exception, of_bench.Stopped)

    def stopped(self, src, reason, **kw):
        for _ in self.each():
            with self.assertRaises(of_bench.Stopped) as cm:
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
                with self.assertRaises(of_bench.BenchError):
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
            with self.assertRaisesRegex(of_bench.BenchError, "compiled chunk"):
                of_bench.evaluate({"x.lua": "\x1bLua"}, "x.lua", of_bench.NO_WORDS, limits=FAST)
            with self.assertRaisesRegex(of_bench.BenchError, "compiled chunk"):
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
            self.skipTest("no Lua runtime on this machine")
        self.tmp = tempfile.mkdtemp()
        self.state = os.path.join(self.tmp, "state")
        import of_events
        self._saved = of_events.STATE_DIR
        of_events.STATE_DIR = self.state
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
        import of_events
        of_events.STATE_DIR = self._saved
        shutil.rmtree(self.tmp, ignore_errors=True)

    def install(self, **kw):
        return of_bench.install(self.org, log=self.logs.append, **kw)


class LockTest(_Project):
    """Part 2: the lock holds the commit and a digest for git, a digest for a path; a second install from the lock
    gives the same files; a changed file is noticed."""

    def test_git_source_commit_and_digest(self):
        e = self.install()["demo-asks"]
        head = git(self.upstream, "rev-parse", "HEAD")
        self.assertEqual(e["source"]["commit"], head)
        self.assertTrue(e["digest"].startswith("sha256:"))
        lock = of_bench.read_lock(self.org)                     # the lock is Lua, read back by the closed reader
        self.assertEqual(lock["demo-asks"]["source"]["commit"], head)
        self.assertEqual(lock["demo-asks"]["digest"], e["digest"])
        self.assertIn("main.lua", lock["demo-asks"]["files"])

    def test_a_second_install_from_the_lock_gives_the_same_files(self):
        self.install()
        before = of_bench.folder_files(os.path.join(self.org, ".bench", "packages", "demo-asks"))
        with open(os.path.join(self.org, of_bench.LOCK)) as f:
            lock_text = f.read()
        # the source moves on; the lock holds the install where it was
        write(self.upstream, {"lib/routing.lua": 'return { to = function(q) return "finance" end }\n'})
        git(self.upstream, "commit", "-qam", "1.1.0")
        shutil.rmtree(os.path.join(self.org, ".bench"))
        self.install(frozen=True)
        after = of_bench.folder_files(os.path.join(self.org, ".bench", "packages", "demo-asks"))
        self.assertEqual(before, after)
        with open(os.path.join(self.org, of_bench.LOCK)) as f:
            self.assertEqual(f.read(), lock_text)

    def test_a_changed_file_is_noticed(self):
        self.install()
        p = os.path.join(self.org, ".bench", "packages", "demo-asks", "lib", "routing.lua")
        with open(p, "a") as f:
            f.write("-- changed\n")
        self.assertEqual(of_bench.check(self.org), [("demo-asks", "lib/routing.lua", "changed")])
        os.remove(os.path.join(self.org, ".bench", "packages", "demo-asks", "main.lua"))
        self.assertIn(("demo-asks", "main.lua", "removed"), of_bench.check(self.org))

    def test_a_path_source_has_a_digest_and_a_change_is_noticed(self):
        local = os.path.join(self.org, "methods", "demo-asks")
        shutil.copytree(self.upstream, local, ignore=shutil.ignore_patterns(".git"))
        write(self.org, {"Benchfile": 'package("demo-asks", { path = "methods/demo-asks" })\n'})
        e = self.install()["demo-asks"]
        self.assertEqual(e["source"], {"path": "methods/demo-asks"})
        self.assertTrue(e["digest"].startswith("sha256:"))
        self.assertEqual(of_bench.check(self.org), [])
        write(local, {"main.lua": PKG["main.lua"] + "\n-- the organization's line\n"})
        self.assertEqual(of_bench.check(self.org), [("demo-asks", "main.lua", "changed")])
        with self.assertRaisesRegex(of_bench.BenchError, "main.lua changed since the lock"):
            self.install(frozen=True)

    def test_a_link_in_a_package_is_refused(self):
        os.symlink("/etc/hosts", os.path.join(self.upstream, "hosts"))
        git(self.upstream, "add", "hosts")
        git(self.upstream, "commit", "-qm", "a link")
        with self.assertRaisesRegex(of_bench.BenchError, "a link in a package"):
            self.install()


class SettingsTest(_Project):
    """The mechanism of part 4: the install reads config/settings/<package>.lua against the method's types."""

    def test_a_value_changes_what_the_package_reads(self):
        self.install()
        self.assertEqual(of_method.settings(self.org, "demo-asks"), {"language": "en", "escalate_after": 7200})
        write(self.org, {"config/settings/demo-asks.lua": 'return { language = "ru", escalate_after = "4h" }\n'})
        with self.assertRaisesRegex(of_bench.BenchError, "changed since the install"):
            of_method.settings(self.org, "demo-asks")               # a value nobody checked never acts
        self.install()
        self.assertEqual(of_method.settings(self.org, "demo-asks"), {"language": "ru", "escalate_after": 14400})
        self.assertEqual(of_method.settings(self.org, "demo-asks", parsed=False)["escalate_after"], "4h")

    def test_a_value_of_the_wrong_type_is_refused_at_install(self):
        for text, said in (('return { escalate_after = 4 }', 'escalate_after is 4, not a duration'),
                           ('return { escalate_after = "soon" }', 'escalate_after is "soon", not a duration'),
                           ('return { language = true }', 'language is true, not a language code'),
                           ('return { colour = "red" }', '"colour" is no setting of demo-asks')):
            write(self.org, {"config/settings/demo-asks.lua": text + "\n"})
            with self.assertRaisesRegex(of_bench.BenchError, said):
                self.install()
            self.assertFalse(os.path.exists(os.path.join(self.org, of_bench.LOCK)))   # nothing locked


class HookTest(_Project):
    """The mechanism of part 5: a hook before a module returns a decision, and the record can name the hook."""

    HOOK = '''
        before "file" {
          written_for = "demo-asks 1",
          run = function(card, ctx)
            for _, t in ipairs(card.tags or {}) do
              if t == "money" then return { decision = "stop", reason = "money cards go to finance first" } end
            end
            return "pass"
          end,
        }
        '''

    def test_a_hook_stops_a_card_with_a_tag_and_passes_the_rest(self):
        write(self.org, {"extensions/demo-asks/finance-review.lua": self.HOOK})
        self.install()
        d = of_method.before(self.org, "demo-asks", "file", {"title": "pay", "tags": ["money"]})
        self.assertEqual(d["decision"], "stop")
        self.assertEqual(d["reason"], "money cards go to finance first")
        self.assertEqual(d["by"], 'extensions/demo-asks/finance-review.lua: before "file"')
        self.assertEqual(of_method.before(self.org, "demo-asks", "file", {"tags": ["infra"]}),
                         {"decision": "pass", "by": None})
        self.assertEqual(of_method.before(self.org, "demo-asks", "write-card", {"tags": ["money"]})["decision"],
                         "pass")

    def test_no_hook_means_no_reader_at_work(self):
        self.install()
        saved, entries = of_bench.evaluate, []

        def counted(files, entry, *a, **k):
            entries.append(entry)
            return saved(files, entry, *a, **k)
        of_bench.evaluate = counted
        try:
            self.assertEqual(of_method.before(self.org, "demo-asks", "file", {}), {"decision": "pass", "by": None})
        finally:
            of_bench.evaluate = saved
        self.assertEqual(entries, ["file.lua"])                    # the lock alone: no hook file evaluated

    def test_a_changed_or_failing_hook_stops_the_module(self):
        p = os.path.join(self.org, "extensions", "demo-asks", "finance-review.lua")
        write(self.org, {"extensions/demo-asks/finance-review.lua": self.HOOK})
        self.install()
        with open(p, "a") as f:
            f.write("-- edited after the install\n")
        d = of_method.before(self.org, "demo-asks", "file", {"tags": []})
        self.assertEqual((d["decision"], "changed since the install" in d["reason"]), ("stop", True))
        self.assertIn(("demo-asks", "extensions/demo-asks/finance-review.lua", "changed since the install"),
                      of_method.check(self.org))
        write(self.org, {"extensions/demo-asks/finance-review.lua":
                         'before "file" { written_for = "demo-asks 1", run = function() while true do end end }\n'})
        self.install()
        d = of_method.before(self.org, "demo-asks", "file", {})
        self.assertEqual(d["decision"], "stop")
        self.assertIn("stopped by the reader", d["reason"])
        write(self.org, {"extensions/demo-asks/finance-review.lua":
                         'before "file" { written_for = "demo-asks 1", run = function() return "maybe" end }\n'})
        self.install()
        self.assertIn("no decision", of_method.before(self.org, "demo-asks", "file", {})["reason"])

    def test_a_hook_is_refused_at_install_where_it_cannot_stand(self):
        for text, said in (('before "remind" { written_for = "demo-asks 1", run = function() end }',
                            'module "remind" may not be extended'),
                           ('before "nothing" { written_for = "demo-asks 1", run = function() end }',
                            'no module "nothing"'),
                           ('before "file" { written_for = "demo-asks 2", run = function() end }',
                            'written_for = "demo-asks 1"'),
                           ('before "file" { written_for = "demo-asks 1" }', "needs run = function")):
            write(self.org, {"extensions/demo-asks/h.lua": text + "\n"})
            with self.assertRaisesRegex(of_bench.BenchError, said):
                self.install()


class ForkTest(_Project):
    """Part 6: a clone of the package with one commit of the organization's; the package gets a new version; the
    fork is rebased on it; the package's tests run on the result and say what still holds."""

    def test_fork_commit_upgrade_rebase_and_tests(self):
        self.install()
        dest = of_bench.fork(self.org, "demo-asks", log=self.logs.append)
        self.assertEqual(git(dest, "remote", "get-url", "origin"), "file://" + self.upstream)
        self.assertEqual(git(dest, "rev-parse", "--abbrev-ref", "HEAD"), of_bench.FORK_BRANCH)
        self.assertEqual(of_bench.read_benchfile(self.org)[0]["source"], {"path": "forks/demo-asks"})
        # the organization's commit
        write(dest, {"lib/routing.lua": 'return { to = function(q) return q.to or "finance" end }\n'})
        git(dest, "commit", "-qam", "route to finance by default")
        # the package's new version, touching another file
        spec = PKG["Benchfile.spec"].replace('"1.0.0"', '"1.1.0"')
        write(self.upstream, {"Benchfile.spec": spec, "README.md": "1.1.0\n"})
        git(self.upstream, "add", "-A")
        git(self.upstream, "commit", "-qm", "1.1.0")
        r = of_bench.rebase(self.org, "demo-asks", identity=("t", "t@example.invalid"), log=self.logs.append)
        self.assertTrue(r["rebased"])
        self.assertEqual(of_bench.read_spec(dest)["version"], "1.1.0")
        with open(os.path.join(dest, "lib", "routing.lua")) as f:
            self.assertIn("finance", f.read())
        self.assertEqual(r["tests"]["exit"], 0)
        self.assertEqual(r["tests"]["held"], ["a card has a decision", "routing is required"])
        self.install()                                              # the lock takes the fork as a path
        self.assertEqual(of_bench.read_lock(self.org)["demo-asks"]["version"], "1.1.0")

    def test_the_tests_say_what_no_longer_holds(self):
        self.install()
        dest = of_bench.fork(self.org, "demo-asks", log=self.logs.append)
        write(dest, {"main.lua": PKG["main.lua"].replace("local routing", "local route")
                     .replace("routing.to", "route.to")})
        git(dest, "commit", "-qam", "rename")
        write(self.upstream, {"README.md": "1.0.1\n"})
        git(self.upstream, "add", "-A")
        git(self.upstream, "commit", "-qm", "1.0.1")
        r = of_bench.rebase(self.org, "demo-asks", identity=("t", "t@example.invalid"), log=self.logs.append)
        self.assertTrue(r["rebased"])
        self.assertNotEqual(r["tests"]["exit"], 0)
        self.assertEqual(r["tests"]["broke"], ["routing is required"])
        self.assertTrue(any("no longer holds: routing is required" in x for x in self.logs))

    def test_a_conflict_is_left_for_the_person(self):
        self.install()
        dest = of_bench.fork(self.org, "demo-asks", log=self.logs.append)
        write(dest, {"lib/routing.lua": 'return { to = function(q) return "a" end }\n'})
        git(dest, "commit", "-qam", "ours")
        write(self.upstream, {"lib/routing.lua": 'return { to = function(q) return "b" end }\n'})
        git(self.upstream, "commit", "-qam", "theirs")
        r = of_bench.rebase(self.org, "demo-asks", identity=("t", "t@example.invalid"), log=self.logs.append)
        self.assertEqual((r["rebased"], r["conflicts"]), (False, ["lib/routing.lua"]))
        self.assertTrue(any("git rebase --abort" in x for x in self.logs))


if __name__ == "__main__":
    unittest.main()

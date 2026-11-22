# Bench: the package manager

Bench knows packages: a package's name, its version, where it came from and what it depends on. It does not know
what a package's `main.lua` means: that is the framework's which reads it. Organic Framework is one such framework,
and itself a package; its side is organic-framework's `docs/bench.md` (`of bench`, settings, hooks). In Ruby's words,
Bench is RubyGems and Bundler, and OF is Rails. The design is organic-framework-build's
`reports/framework/metamethod/12-metamethod.md`; this is its first slice (`11-first-slice.md`), as built.

In this slice: the closed reader, the lock, sources from git and from a path, a fork rebased on its origin, and a
package's tests run and read. Not yet: the catalogue, versions resolved between packages, `needs` enforced on
programs (they are shown at install), and the operating-system sandbox around the reader (below, "Not yet").

## Setting it up

Bench needs Python 3, git (`/usr/bin/git`) and a Lua runtime: Lua 5.4, LuaJIT 2.1 or Lua 5.1, whatever the program
is named (Lua 5.2 and 5.3 are refused). On macOS `brew install luajit`; on Debian or Ubuntu `apt install lua5.4`;
on Arch `pacman -S lua`. Clone this repository; the command is `bin/bench`.

## A project

```
linden/
  Benchfile                       -- the packages it uses
  Benchfile.lock                  -- written by `bench install`
  forks/<package>/                -- a fork: a git clone of the package, its source as origin
  .bench/packages/<package>/      -- the installed files of a git package (ignored by git)
```

```lua
-- Benchfile
package("of-asks", { git = "https://github.com/organic-framework/of-asks", ref = "main" })
package("cafe-support", { path = "methods/cafe-support" })
```

A git source takes `git` (a URL, or a local folder), `ref` (a branch, a tag or a commit; the source's HEAD when left
out) and `dir` (a folder inside the repository, when the package is not at its root). A path source takes `path`,
relative to the project.

A package is a folder with a `Benchfile.spec`:

```lua
package {
  name    = "of-asks",
  version = "1.0.0",
  main    = "main.lua",                 -- read by the package's framework, not by Bench
  tests   = { "sh", "tests/run.sh" },   -- a command run in the package's folder
}
depends { "organic-framework ~> 1" }
needs { store = { records = { read = "…", write = "…" } } }   -- shown at install
```

## The commands

| Command | What it does |
|---|---|
| `bench install [--frozen]` | installs every package of the Benchfile and writes `Benchfile.lock`; `--frozen` installs exactly what the lock holds and refuses a source or any file that differs |
| `bench check` | says which file differs from the lock |
| `bench fork <package>` | clones a git package into `forks/<package>` on the branch `organization`, at the locked commit, and points the Benchfile's line at it |
| `bench rebase <package> [--onto <ref>] [--no-tests]` | fetches the fork's origin, rebases the organization's commits on it, and runs the package's tests |
| `bench test <package>` | runs the package's tests and says which hold |
| `bench read <file> [--words …]` | evaluates one file in the closed reader and prints its data |

Each takes `--project <folder>` (default: the current folder). `settings`, `hook` and `read --words method|hook`
are a framework's, and answer only through one (`of bench …`).

## The reader

Every Lua file Bench reads (a Benchfile, a spec, the lock), and every file a framework hands it, is evaluated in a
closed environment, in a Lua process of its own (`bin/bench_sandbox.lua`, run by `bin/bench.py`):

- The process is given no file. Its program and the package's Lua sources come on its input; its result, plain
  data as JSON, goes to its output. It starts with an empty environment, in `/`, with no core file, no file
  written, no process started and bounded processor time; a limit it cannot set stops the start.
- A file is given its words (`package`, `depends`, …, or the framework's), a `require` that loads only the
  package's own sources, and a short list of the standard library: `string` (no `dump`), `table`, `math`,
  `coroutine`, `pairs`, `pcall` and the like. Not given: `io`, `os`, `load`, `loadstring`, `dofile`, `package`,
  `debug`, `collectgarbage`, `getfenv`, `setfenv`, LuaJIT's `ffi` and `jit`, and a metatable with `__gc` or
  `__close` (Lua 5.4 runs those with every hook off). A compiled chunk is refused.
- Budgets: a count hook ends the process on its instruction budget (20 million) and its memory budget (64 MiB); a
  library call that would build a string over 8 MiB is refused before it builds it; calls that re-enter Lua from C
  (a gsub callback, a resumed coroutine, a pcall, a sort's comparison) are refused past 40 deep; the process has a
  ceiling of 256 MiB (the address space on Linux, and on every system a watch on its resident memory that ends it);
  the caller ends it on wall time (10 seconds). LuaJIT runs with its compiler off, since compiled code calls no
  hook.
- What comes back is what Lua returned: a text that is not UTF-8, a whole number or a key past 2^53, and a key
  written twice (`[1]` and `["1"]`) are refused; `tostring` of a table, function or thread gives its type, never
  an address, and `string.format`'s `%p` is refused.
- A function in a file is kept, not called: the result holds its place (`{"@": "function", "path": […]}`), and a
  later call evaluates the file again and calls the function at that place with plain data.
- A name that is not a word of the file is an error, so a misspelt word is caught at install.

The hostile set, one test per case (`tests/test_bench.py`, `ReaderHostileTest`), runs on every runtime the machine
has; CI runs it on Lua 5.4, LuaJIT and Lua 5.1 and fails without them.

## The lock

`Benchfile.lock` is Lua, read back by the same reader. Per package: its version, its source (for git the commit),
a digest of all its files and the digest of each (an executable file is marked in its digest; every file counts
but the package's own top-level `.git`), and what the framework recorded at install. A second
`bench install --frozen` gives the same files; `bench check` names a file that changed, was added or removed.

## A fork with a rebase

`bench fork <package>` makes `forks/<package>`, a git clone of the package with its source as `origin`, on the
branch `organization` at the locked commit, and changes the Benchfile's line to `path = "forks/<package>"` (when
that line is on one line; otherwise it prints the line to write). The organization commits its changes there.

When the package gets a new version, `bench rebase <package>` fetches the origin, rebases the organization's
commits on the branch the fork came from (or `--onto <branch or tag>`), and runs the package's tests. It lists each
test that no longer holds; unittest's verbose lines and TAP lines are read. On a conflict it stops and leaves the
rebase to the person: resolve and `git rebase --continue`, or `git rebase --abort`. Then `bench install` locks the
new content. The tests do not forbid a change; they show which of the package's promises the organization's
version still keeps. A rebase commits under the person's own git name and address, without signing.

**A package's tests are its own program.** After a rebase they are the new upstream's. Bench runs them as an
ordinary process, outside the reader, and says so before it does; they get only a short environment (`PATH`, `HOME`,
the user and language, `TMPDIR`, `OF_HOME`, `BENCH_HOME`), never the rest, where a token or an agent's socket may
be. `bench rebase --no-tests` rebases without running them.

Every git call Bench makes runs the system's git with no program a package's repository names: no hooks, no
fsmonitor, no external diff or pager, no automatic maintenance, no global or system configuration, and transports
opened only for a fetch or a clone.

## A framework on Bench

A framework hands Bench one function, `bench.FRAMEWORK = on_install`, called at install as
`on_install(project, name, folder, spec)`; what it returns goes into the package's lock entry beside Bench's own.
Its module may also give `check(project)`, `before(project, package, module, input)`, `settings(project, package)`,
`METHOD_WORDS` and `HOOK_WORDS`, which `check`, `hook`, `settings` and `read --words method|hook` use. OF's is
organic-framework's `bin/of_method.py`, set by `of bench`.

## Not yet

The operating-system sandbox around the reader, required before Bench evaluates a file the team did not write
(of_server's reading, organic-framework-build message `01a20547-5da1`): bwrap on Linux, a deny-by-default
sandbox-exec profile on macOS. The watch on resident memory reads `ps` on macOS and is tested on Linux only.

## Code

`bin/bench` (the command), `bin/bench.py` (the reader's driver, sources, the lock, the fork, the tests),
`bin/bench_sandbox.lua` (the closed environment). Tests: `tests/test_bench.py`. The skill for an agent that changes
a method: `skills/change-a-method/SKILL.md`.

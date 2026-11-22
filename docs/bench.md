# Bench: the package manager under OF

Bench knows packages: a package's name, its version, where it came from and what it depends on. OF is a framework on
it: what a package's `main.lua` means (modules, settings, what may be extended) is OF's. In Ruby's words, Bench is
RubyGems and Bundler, and OF is Rails. The design is organic-framework-build's
`reports/framework/metamethod/10-metamethod-v2.md`; this page is its first slice (`11-first-slice.md`), as built.

What the slice has: the closed reader, the lock, sources from git and from a path, an organization's settings and
hooks checked at install, a hook called before a module, and a fork rebased on its origin. Not yet: the catalogue,
versions resolved between packages, `needs` enforced on programs, and more than one hook order (the organization's).

## Setting it up

Bench needs a Lua runtime: Lua 5.4, LuaJIT 2.1 or Lua 5.1. On macOS `brew install luajit`; on Debian or Ubuntu
`apt install lua5.4`; on Arch `pacman -S lua`. Nothing else: `of bench` is part of `of`.

## An organization's project

```
linden/
  Benchfile                       -- the packages it uses
  Benchfile.lock                  -- written by `of bench install`
  config/settings/<package>.lua   -- tuning: values of the settings a package offers
  extensions/<package>/*.lua      -- hooks on a package's modules
  forks/<package>/                -- a fork: a git clone of the package, its source as origin
  .bench/packages/<package>/      -- the installed files of a git package (ignored by git)
```

```lua
-- Benchfile
package("of-asks", { git = "https://github.com/organic-framework/of-asks", ref = "main" })
package("cafe-support", { path = "methods/cafe-support" })
```

A git source takes `git` (a URL, or a local folder), `ref` (a branch, a tag or a commit; `HEAD` when left out) and
`dir` (a folder inside the repository, when the package is not at its root). A path source takes `path`, relative
to the project.

## The commands

| Command | What it does |
|---|---|
| `of bench install [--frozen]` | installs every package of the Benchfile and writes `Benchfile.lock`; `--frozen` installs exactly what the lock holds and refuses any file that differs |
| `of bench check` | says which file differs from the lock: a package's, a settings file, a hook |
| `of bench settings <package>` | the package's settings as installed, defaults filled in |
| `of bench fork <package>` | clones a git package into `forks/<package>` on the branch `organization`, at the locked commit, and points the Benchfile's line at it |
| `of bench rebase <package> [--onto <ref>]` | fetches the fork's origin, rebases the organization's commits on it, and runs the package's tests |
| `of bench test <package>` | runs the package's tests and says which hold |
| `of bench hook <package> <module> --input '<json>'` | calls the organization's hooks before the module with a sample input, as the package would, and prints the decision |
| `of bench read <file> [--words …]` | evaluates one file in the closed reader and prints its data |

Each takes `--project <folder>` (default: the current folder) or `--org <name>` (the path `orgs.yaml` gives).

## The reader

Every Lua file Bench or OF reads, at install and at work, is evaluated in a closed environment in a Lua process of
its own (`bin/of_bench_sandbox.lua`, run by `bin/of_bench.py`). Whether that environment is closed well enough is
under the security review of the guard's keeper, which is open; an operating-system sandbox around it is not in this
version:

- The process is given no file. Its program and the package's Lua sources come on its input; its result, plain data
  as JSON, goes to its output. It starts with an empty environment, in `/`, with no core file, no file written and
  no process started (resource limits), and on Linux a bounded address space.
- A file is given its words (`package`, `module`, `setting`, …), a `require` that loads only the package's own
  sources, and a short list of the standard library: `string` (no `dump`), `table`, `math`, `coroutine`, `pairs`,
  `pcall` and the like. Not given: `io`, `os`, `load`, `loadstring`, `dofile`, `package`, `debug`,
  `collectgarbage`, `getfenv`, `setfenv`, LuaJIT's `ffi` and `jit`. A compiled chunk is refused.
- A count hook ends the process on its instruction budget (20 million) and on its memory budget (64 MiB); a library
  call that would build a string over 8 MiB is refused; the caller ends it on wall time (10 seconds). LuaJIT runs
  with its compiler off, since compiled code calls no hook.
- A function in a file is kept, not called: the result holds its place (`{"@": "function", "path": […]}`), and a
  later call evaluates the file again and calls the function at that place with plain data.
- A name that is not a word of the file is an error, so a misspelt word is caught at install.

The hostile set, one test per case (`tests/test_bench.py`, `ReaderHostileTest`): reading a file, opening a socket,
starting a process, `require` outside the package's folder, a loop that does not end, memory that does not stop
growing. Each is meant to be refused or stopped on every runtime the machine has; the tests run on the machine where
they are started (this repository has no CI yet).

## The lock

`Benchfile.lock` is Lua, read back by the same reader. Per package: its version, its source (for git the commit),
a digest of all its files and the digest of each, and what OF checked: the settings with their values and the
settings file's digest, and each hook with its file's digest and its place. A second `of bench install --frozen`
gives the same files; `of bench check` names a file that changed.

## OF's part: settings and hooks

A package's `main.lua` declares its settings and the modules an organization may extend:

```lua
setting "escalate_after" { type = "duration", default = "2h" }
module "file" { on = command "ask", run = function(q, ctx) … end }
extendable { "file" }
```

Setting types: `string`, `int`, `number`, `bool`, `duration` (`"30m"`, `"2h"`, `"1d"`, `"1h30m"`), `language`
(`"en"`, `"pt-BR"`), `enum` (with `values = { … }`).

**Tuning.** `config/settings/<package>.lua` returns the organization's values. The install refuses a name the
package does not offer and a value of the wrong type, and writes nothing. At work `of_method.settings(project,
package)` gives the values as installed (a duration in seconds); it refuses when the file changed since the install,
so a value nobody checked never acts.

```lua
-- config/settings/of-asks.lua
return { escalate_after = "4h" }
```

**A hook.** A file under `extensions/<package>/` names when it runs (`before`, `after`, `around`, `instead`), the
module, the package's major version it was written for, and its function. The install refuses a hook on a module
that is not extendable, on a module that does not exist, or written for another major version.

```lua
-- extensions/of-asks/finance-review.lua
before "file" {
  written_for = "of-asks 1",
  run = function(card, ctx)
    for _, t in ipairs(card.tags or {}) do
      if t == "money" then return { decision = "stop", reason = "money cards go to finance first" } end
    end
    return "pass"
  end,
}
```

A hook sees the module's input as plain data and a `ctx` with the package's name and settings. It returns a
decision: `"pass"`, `"stop"`, `{ decision = "hand", to = … }` or `{ decision = "hold", until = … }`, with an
optional `reason`. A package's code calls `of_method.before(project, package, module, input)` before the module
runs; the hooks run in file-name order and the first that does not pass decides. The result names the hook
(`by`), so the record the module writes can name it. A hook whose file changed since the install, that the reader
stopped, or that returns no decision stops the module, with the reason: a hook before a module is a gate, and a gate
that cannot be read stays closed. With no hook before a module, no Lua runs.

In this slice `before` is called; `after`, `around` and `instead` are read and checked at install, and called by the
package that offers them when it does.

## A fork with a rebase

`of bench fork <package>` makes `forks/<package>`, a git clone of the package with its source as `origin`, on the
branch `organization` at the locked commit, and changes the Benchfile's line to `path = "forks/<package>"` (when
that line is on one line; otherwise it prints the line to write). The organization commits its changes there.

When the package gets a new version, `of bench rebase <package>` fetches the origin, rebases the organization's
commits on the branch the fork came from (or `--onto <branch or tag>`), and runs the package's tests (`tests` in
its `Benchfile.spec`, a command run in its folder). It lists each test that no longer holds; unittest's verbose
lines and TAP lines are read. On a conflict it stops and leaves the rebase to the person: resolve and
`git rebase --continue`, or `git rebase --abort`. Then `of bench install` locks the new content. The tests do not
forbid a change; they show which of the package's promises the organization's version still keeps.

A rebase commits under the person's own git name and address (their git configuration in the project), without
signing.

## Code

`bin/of_bench.py` (Bench: the reader, sources, the lock, the fork), `bin/of_bench_sandbox.lua` (the closed
environment), `bin/of_method.py` (OF: `main.lua`'s words, settings, hooks). Tests: `tests/test_bench.py`. The skill
for an agent that changes a method: `skills/change-a-method/SKILL.md`.

---
name: change-a-method
description: Change how a method (a Bench package such as of-asks) works for your organization, the smallest way that fits - tune a setting, write a hook on one module, or fork the package and rebase it on a new version. Use when an organization wants a package to behave differently: a different value, a check before a step, or a change to the package's own code.
---

# Change a method for your organization

An organization runs methods as Bench packages listed in its project's `Benchfile` (organic-framework's docs/bench.md). Three ways
change one; take the smallest that does what is wanted:

1. **Tune** a value the package offers: `config/settings/<package>.lua`.
2. **Extend** one module with a hook: a file under `extensions/<package>/`.
3. **Fork** the package: change its own code in `forks/<package>/`, and rebase on each new version.

You need:
- the organization's project folder, the one with the `Benchfile` (below, the current folder);
- `of` on the machine, and Bench's checkout where `of owner show` says OF looks for it;
- a Lua runtime (`lua5.4`, `lua5.5`, `luajit` or `lua5.1`).

Check them first:

```sh
ls Benchfile && of bench check
```

`of bench check` says "every file matches Benchfile.lock" or names what differs. If it says there is no lock, run
`of bench install` once. If it says where it looked for Bench and found none, the machine's owner names the folder
with `of owner set bench <folder>`; that is not yours to do.

**Where `of bench` can run.** Every `of bench` command reads Lua: the lock, a package's `Benchfile.spec`, your
settings and hooks. Bench evaluates Lua only inside an operating-system sandbox of its own: `sandbox-exec` on macOS,
bubblewrap on Linux. On Linux an agent session's own sandbox keeps Bench's from starting, and the command then says
"the outer layer did not start, so nothing was evaluated". That is no error in your files. Write the files in your
session, then run the `of bench` commands below in a plain terminal outside it, or ask whoever can. On macOS they
run from a session too.

## What the package offers

Read the package's `main.lua`: its `setting` lines are what you may tune, its `extendable { … }` line names the
modules you may hook, and each `module "<name>"` shows what the module answers to. The installed files are in
`.bench/packages/<package>/` (a git package) or at the `path` the Benchfile gives.

```sh
grep -nE '^(setting|module|extendable)' .bench/packages/of-asks/main.lua
```

The version is in its `Benchfile.spec` (`version = "1.0.0"`); a hook is written for its major version (`1`).

## 1. Tune a setting

Write, or edit, `config/settings/<package>.lua`. It returns a table of the values you change; the rest keep their
defaults.

```lua
-- config/settings/of-asks.lua
return { escalate_after = "4h" }
```

The value must have the setting's type: a duration is `"30m"`, `"2h"`, `"1d"` or `"1h30m"`; a language is `"en"`;
`int`, `number` and `bool` are plain Lua values; an `enum` is one of its listed values. Then:

```sh
of bench install
of bench settings of-asks
```

The install refuses a wrong type or an unknown name, says which, and writes nothing; fix the file and run it again.
`of bench settings` prints the values the package now reads (a duration in seconds). Until you run the install, the
package refuses the changed file and keeps its default, so a value nobody checked never acts.

In of-asks, `escalate_after` is when a card routed to a delegated unit's owner, and still unanswered, rises to the
owner: for a delegation whose `escalate_when` has a bare `ask_unanswered` (with a number, `ask_unanswered > 4h`, the
rule keeps its own time).

## 2. Write a hook on one module

A hook runs at once, sees the module's input as plain data, and returns a decision. It cannot read files, reach the
network or start a program: the reader gives it none of these. Write one file per concern under
`extensions/<package>/`:

```lua
-- extensions/of-asks/finance-review.lua
before "file" {
  written_for = "of-asks 1",            -- the package and its major version
  run = function(card, ctx)
    for _, t in ipairs(card.tags or {}) do
      if t == "money" then
        return { decision = "stop", reason = "money cards go to the finance position first" }
      end
    end
    return "pass"
  end,
}
```

- `before`, `after`, `around` or `instead`, then the module's name, which must be in the package's `extendable`.
- `run` returns `"pass"`, `"stop"`, `{ decision = "hand", to = "<position>" }` or
  `{ decision = "hold", until = "<event>" }`, with an optional `reason`. Anything else stops the module.
- `ctx.settings` holds the package's settings; `ctx.package` its name.
- Several hooks before one module run in file-name order; the first that does not pass decides.
- In of-asks 1 the hooks called are those `before "file"`; the others are checked at install and not yet called.
  The card a hook sees: `question`, `options`, `tags` (the tags the asker declared), `tags_guessed` (tags OF only
  guessed from the words: a guess alone should never stop a card), `to`, `from`, `blocking`, `due`, `task`.
- When a hook stops a card, `of ask` files nothing, prints the hook (`by`) and its `reason` to the asker, and records
  `of.ask_stopped`.

Then:

```sh
of bench install
of bench check
```

The install refuses a hook on a module that may not be extended, one written for another major version, and one
without `run`, and says why. To see the hook decide before you rely on it, call it with a sample input:

```sh
of bench hook of-asks file --input '{"question": "Pay the invoice?", "tags": ["money"]}'
of bench hook of-asks file --input '{"question": "Restart the node?", "tags": ["infra"]}'
```

Each prints the decision and the hook that made it (`by`); it exits 0 on "pass" and 2 otherwise. An edited hook stops the module until the next
`of bench install`: change, install, check.

## 3. Fork the package and rebase it

Fork only when tuning and a hook cannot do it: the change is to the package's own code.

```sh
of bench fork of-asks
```

This clones the package into `forks/of-asks` with its source as `origin`, on the branch `organization` at the
installed commit, and points the Benchfile at it (or prints the line to change, if the line spans several lines).
Make your change there and commit it in the fork:

```sh
cd forks/of-asks
# edit the files
git commit -am "route money cards to finance by default"
cd -
of bench install
of bench test of-asks
```

`of bench test` runs the package's own tests on your version and lists any that no longer hold. They do not forbid
the change; they show which of the package's promises your version still keeps. Say which ones in your commit.

When the package publishes a new version:

```sh
of bench rebase of-asks                 # or --onto <tag or branch>
of bench install
```

The rebase puts your commits on the new version and runs the tests again. The tests are the new version's own
program, run on this machine outside Bench's reader (Bench says so before it runs them); `of bench rebase of-asks
--no-tests` rebases without running them, and `of bench test of-asks` runs them when you have read what changed. On a conflict it stops and names the
files: resolve them in `forks/of-asks`, `git add` them, `git rebase --continue`, then `of bench test of-asks` and
`of bench install`. `git rebase --abort` goes back to where you were.

## Done

You are done when `of bench check` says every file matches the lock, and for each change you made:
- a setting: `of bench settings <package>` shows the new value;
- a hook: a sample input gives the decision you meant, and an input it should pass passes;
- a fork: `of bench test <package>` ran on your version, and you named any test that no longer holds.

Commit the organization's project (Benchfile, Benchfile.lock, config/, extensions/, and the fork's folder as its
own repository) the way your organization commits.

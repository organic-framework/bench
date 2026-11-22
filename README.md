# Bench

A package manager for methods of work: a package's name, its version, where it comes from and what it depends on.
Bench reads `Benchfile.spec`, `Benchfile` and `Benchfile.lock`, which are Lua, in a closed environment in a process
of its own (an operating-system sandbox around it is being built, and its security review is open); installs packages from git or a path; writes a lock with every file's digest; and keeps an
organization's fork of a package rebased on its new versions. What a package's `main.lua` means is the framework's
which reads it: [Organic Framework](https://github.com/organic-framework/organic-framework) is one, and itself a
package.

The documentation is `docs/bench.md`; the skill for an agent that changes a method is `skills/change-a-method/`.

License: Apache-2.0.

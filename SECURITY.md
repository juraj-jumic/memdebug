# Security policy

memdebug is a security tool that reads untrusted data (memory stores, git repositories) and, in one place, writes to
your files (rollback). Reports of problems are welcome and taken seriously.

## Reporting a vulnerability

Please report privately, not in a public issue. Use GitHub's private vulnerability reporting on this project's
repository: https://github.com/juraj-jumic/memdebug/security/advisories/new (Security tab, "Report a vulnerability"). If that is not available, contact the maintainer through the
address on the repository's profile page. Include what you did, what you expected, what happened, and the version
(`memdebug --version` once available, otherwise the commit).

This is a small open-source project without a security team: expect an acknowledgement within about a week and a fix or a
clear answer as soon as the maintainer can manage. There is no bug bounty.

## What counts as a vulnerability

* Reading or writing outside the folders you pointed it at (links, junctions, odd file names, path tricks).
* A repository, memory file or database causing memdebug to run a program, or to crash or hang in a way that hides data.
* Rollback losing data: overwriting or deleting content that was not first saved, rewriting git history, or leaving a
  repository in a half-changed state after a failure.
* The ledger or a snapshot being altered without `memdebug verify` noticing (other than the limits listed below).
* The viewer being usable from a web page or another user, or leaking its secret, or showing stored text as markup.
* Memory text being able to forge, hide or reorder what the command line, the viewer or a report shows.
* A damaged or hostile settings file, witness file or database copy causing a crash, a write outside its place, or reading more than memory.
* memdebug opening or listing any file in an agent's folder other than the memory files it was told to watch.
* memdebug running anything in a container other than its fixed read-only backup script, or a container name being taken as an option.
* A report, witness or settings file written through a link, or readable by other users when it should be private.

## What does not count

The documented limits are known and accepted for now. See [docs/threat-model.md](docs/threat-model.md), especially
"Known limits": for example, the ledger is not encrypted, an attacker who can rewrite the whole ledger file can rebuild
a valid chain, and rollback does not change what a running agent has already loaded.

## Supported versions

Alpha software. Only the latest release receives fixes.

## For users

* Treat the ledger and its snapshots as sensitive: they contain your agent's memory text, including deleted memories.
* Do not paste the viewer's link into chats or screenshots; it contains a secret.
* Keep git up to date. memdebug runs git with the untrusted repository as its working folder.
* Run `memdebug selftest` on every new machine: it proves the protections work there, and says SKIP, never PASS,
  for anything it could not prove.

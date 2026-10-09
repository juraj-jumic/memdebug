# Windows notes

Written for Windows as well, with Windows-specific code paths. The test suite and `memdebug selftest` have been run on Windows 11, and the CI workflow covers `windows-latest`, but run `memdebug selftest` on your own machine anyway; it says SKIP, with a reason, for anything
it cannot prove (for example symlinks need Developer Mode).

- Needs Git for Windows 2.31+ (a real `git.exe`; shims and scripts are refused). `core.autocrlf=true` (the installer default)
  is handled: line endings are normalised before comparing.
- git is found through PATH entries that are absolute paths only; the current folder is never searched.
- Names that mean something special on Windows (`NUL.md`, `con.md`, `file:stream.md`, trailing dots or spaces, `GIT~1`, `.GIT`)
  are rejected on every platform, so the history and the files always agree.
- Paths longer than 259 characters work without changing any Windows setting: memdebug hands Windows the extended-length form of every note, backup and
  log path. A git repository in a very deeply nested folder can still hit git's own limits.
- Directory junctions and symlinks are never followed, including by rollback. A hung git is stopped with `taskkill /T`.
- Ledger file permissions are not enforced by this tool on Windows; the default location is private to your user account.
- Double-clicking the stand-alone program opens a console window that explains it is a command-line tool and waits for Enter; use it from PowerShell
  (`.\memdebug-windows-x64.exe demo`).
- The stand-alone `memdebug-windows-x64.exe` from the GitHub release is not signed, so a copy downloaded in a browser makes Windows show "Windows protected
  your PC" (Microsoft Defender SmartScreen, publisher "Unknown publisher") the first time you start it. Check the file against `SHA256SUMS` first
  (`Get-FileHash .\memdebug-windows-x64.exe`), and `gh attestation verify .\memdebug-windows-x64.exe --repo juraj-jumic/memdebug` if you have the GitHub
  CLI; then choose "Run anyway" (some Windows versions first show a "More info" link that reveals that button). Choose "Don't run" if the checksum differs.
  The warning is about the missing signature, not about what the program does. (A copy fetched with `gh release download` started here without it.)
- If git reports "dubious ownership" for a repository on another drive, fix the ownership; this tool deliberately ignores your
  global `safe.directory` setting.

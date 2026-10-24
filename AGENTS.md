# Working on iPadOS-liuqin

- Read `NOTES.md` first: verification evidence, hardware facts, risks, next steps.
  Distinguish observations from guesses and keep that separation when editing it.
- This port has **never booted on hardware**. Do not describe host-side checks as
  boot results. Say what was run and where.
- The kernel needs Xcode 15's `iig`; `tools/build-local.sh` builds it locally
  without Homebrew/sudo/KDK (see `docs/LOCAL_BUILD.md`). The `xnu.yml` workflow
  is the CI alternative. Record the artifact sha256 in `NOTES.md` whenever a
  new kernel is produced.
- Before any CI run, run `tools/verify-patches.sh` (it reproduces the CI source
  tree). Before touching the shim/ADT tools, run `python3 tests/test_tools.py`.
- Never edit the sweet-origin patches in `xnu/patches/reference/` into the series:
  they target PM6150/GT9896 silicon. They are templates only.
- Keep device dumps, serials and Apple artifacts out of git (`dtb/`, `backup/`,
  `research/` are ignored). Only `userdata` may be overwritten; never
  `boot`, `recovery`, `modem`, `persist`, and confirm each destructive step with
  the user.
- Update the handoff section of `NOTES.md` before stopping.

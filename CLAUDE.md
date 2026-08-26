# CLAUDE.md — instructions for Claude Code working in this repo

**Purpose: stop you from re-deriving what was already measured.** Everything here
was verified against a real Claude Code install. Treat it as fact. Only re-check
a claim if the user says it contradicts reality, or if the CLI's output format
has visibly changed.

## What this repo is

A single-file, zero-dependency Python tool that updates a machine's Claude Code
toolchain in one pass: the CLI and adjacent npm globals, plugin marketplaces,
every installed plugin, and the editor extension.

It was extracted from `qwen-local`, which used to carry a Windows-only
PowerShell equivalent (`scripts/update-claude-stack.ps1`). That script is retired
— this repo replaces it. Do not resurrect it.

## Established facts — do NOT re-research these

### `claude plugin list` output format

Verified against Claude Code 2.1.246, 62 plugins installed:

```
Installed plugins:

  ❯ aws-core@claude-plugins-official
    Version: 1.1.0
    Scope: user
    Status: ✔ enabled
```

- The bullet is **`❯` (U+276F), not ASCII `>`**. This is the single most
  expensive mistake available here: parsing for `>` yields zero plugins while
  dozens are installed, and the tool cheerfully reports "no plugins parsed".
- Plugin names include the marketplace suffix (`name@marketplace`) and
  `claude plugin update` accepts them in that form.
- `Status:` values carry glyphs (`✔ enabled` / `✘ disabled`); `GLYPH_RE` strips
  them so `Plugin.status` is plain text.
- `Scope:` is per-plugin and must be passed back via `--scope`. Real installs mix
  scopes — the reference machine had 61 `user` and 1 `local`.

### `claude plugin update` behaviour

- Exits **1** on failure and prints `✘ Failed to update plugin "...": ...`.
  Exit code alone is a reliable signal; the text check in `classify()` is
  redundant insurance.
- Has `-y/--yes`, **required when stdin or stdout is not a TTY**, to accept a
  marketplace-declared install command that has changed.
- This tool always runs commands with `capture_output=True`, so **stdout is never
  a TTY**. That's why `--yes` exists as a passthrough flag.
- `--yes` is deliberately **opt-in, not the default**: auto-accepting a changed
  install command runs whatever the marketplace now declares, unreviewed. Do not
  "simplify" this by always passing `-y`.

### npm

- `npm ls -g --depth=0 --json` **exits non-zero whenever anything is outdated**,
  so its exit code is ignored on purpose.
- Its stderr must stay **unmerged** (`merge_stderr=False`) or npm's warnings get
  concatenated into the JSON and parsing fails.
- On Windows, `shutil.which("npm")` returns `npm.ps1`, which `CreateProcess`
  cannot execute. `resolve()` tries `.cmd`/`.exe`/`.bat` first. Same trap applies
  to any Node CLI shim.

### Not yet verified

- The exact "already at the latest version" string from `claude plugin update`.
  `classify()` matches `"already"` and `"latest"` loosely; anything unrecognised
  is reported verbatim as changed rather than swallowed.
- The orphan path (`Plugin.is_orphan`, `--prune-orphans`). No orphaned plugin was
  present to test against. It keys off `"failed to load"` in the status line.

If you verify either, update this section with what the CLI actually printed.

## Working style in this repo

- **One file, standard library only.** No dependencies, no packaging, no `src/`
  layout. If a change needs a third-party import, it probably doesn't belong.
- **Immutable dataclasses.** `Plugin` and `Result` are frozen; use
  `dataclasses.replace`, never mutate.
- **Every subprocess call goes through `run()`** so output is ANSI-stripped and
  timeouts are uniform. Don't call `subprocess` directly.
- **`--dry-run` must stay honest**: it prints the exact command that would run.
  Any new stage needs a dry-run branch before it ships.
- Keep it cross-platform. Guard Windows-specific behaviour behind `IS_WINDOWS`.

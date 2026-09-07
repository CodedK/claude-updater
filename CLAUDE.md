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
- **The glyphs are host-dependent.** Under a PowerShell host on a legacy code
  page the CLI transliterates them: the bullet becomes ASCII `>`, and `✔`/`✘`
  become `√`/`×`. `PLUGIN_NAME_RE` and `GLYPH_RE` accept both sets. Do not
  "clean up" either pattern down to one alphabet.
- **`Status:` has four known values**: `enabled`, `disabled`, `failed to load`,
  and `loaded` — the last belongs to plugins loaded from a directory.
- **`Path:` appears only on plugins with no marketplace behind them.** Verified
  on a 62-plugin install: exactly one entry carried it. That makes it the
  structural signal for `Plugin.is_unmanaged`, which is far more reliable than
  matching the marketplace suffix.

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
- **Three of its failures are not retryable**, and telling the operator to
  re-run with `--yes` is wrong for all of them. `classify()` separates them:
  - deleted upstream → `Plugin "<name>" not found` → `orphan`
  - loaded off disk → `... with no marketplace backing — it cannot be updated.`
    → `unmanaged`
  - anything else → `failed`, the only bucket where `--yes` may help.

### npm

- `npm ls -g --depth=0 --json` **exits non-zero whenever anything is outdated**,
  so its exit code is ignored on purpose.
- Its stderr must stay **unmerged** (`merge_stderr=False`) or npm's warnings get
  concatenated into the JSON and parsing fails.
- On Windows, `shutil.which("npm")` returns `npm.ps1`, which `CreateProcess`
  cannot execute. `resolve()` tries `.cmd`/`.exe`/`.bat` first. Same trap applies
  to any Node CLI shim.
- **Install one package per call, never a batch.** `npm install -g a@latest
  b@latest` is a single transaction: if any package fails to build, *none* of
  them move. Observed for real — `@nanonets/graft` bundles `tree-sitter-kotlin`,
  a native addon whose `node-gyp` step needs Visual Studio with the "Desktop
  development with C++" workload, and its failure silently held `@openai/codex`
  22 releases behind while npm reported only peer-dependency warnings.
- **Report the tail of an npm failure, not the head.** `summarize()` takes the
  first 160 characters, which for npm is all `npm warn ERESOLVE` noise; the
  actual `gyp ERR! stack ...` cause is at the end. That is what
  `summarize_failure()` is for.

### Console encoding on Windows

`claude` prints `✔`/`✘` in ordinary success output, not just in `Status:` lines.
Python on Windows gives stdout the **cp1252** locale encoding whenever it is not
a console — a pipe, a redirect, a scheduled-task log — and cp1252 cannot encode
those glyphs. Printing one raised `UnicodeEncodeError` and killed the run partway
through the marketplace stage, *after* npm had already committed its changes.

`configure_stdio()` reconfigures stdout/stderr to UTF-8 with `errors="replace"`
before any stage runs, and `log()` catches `UnicodeEncodeError` as a fallback.
Do not remove either: `GLYPH_RE` only sanitises parsed status text, so it does
not protect the many other places CLI output is echoed verbatim.

### Finding the CLI

- **A native install is not on PATH.** The installer puts the binary in
  `~/.local/bin` and does not necessarily add that directory to PATH, so
  `shutil.which("claude")` finds nothing while a working CLI sits right there.
  `find_claude()` falls back to `NATIVE_CLAUDE_DIRS` for exactly this.
- When the CLI genuinely cannot be found, that is **recorded as a problem**, not
  a silent skip. Before, `main()` dropped `market` and `plugins` from the stage
  list and still printed "all stages completed cleanly" — the tool's headline
  result was a lie on any machine with a native install.
- A native install is **not updated by any of the four stages**; it updates
  itself via `claude update`. Only an npm-installed CLI moves in the `npm` stage.

### Scope, and where plugins actually live

- **`~/.claude/plugins/installed_plugins.json` is the registry.** Schema
  `{"version": 2, "plugins": {"name@marketplace": [ {scope, projectPath,
  installPath, version, installedAt, ...} ]}}`. A project's
  `.claude/settings.json` `enabledPlugins` map is a *separate* thing — it holds
  enable/disable state, not installation. Removing an entry from one does not
  remove it from the other.
- **`--scope project` is resolved against the process's cwd**, matched to the
  `projectPath` in that registry. `claude plugin list` shows project-scoped
  plugins belonging to *other* projects, so the tool routinely lists plugins it
  cannot uninstall from where it is running. `--prune-orphans` can therefore
  only prune a project-scoped orphan when run from that plugin's own project.
- **A drive-letter case mismatch makes an entry unremovable.** One entry on the
  reference machine recorded `projectPath` as `c:\Users\...` while Windows
  reports the cwd as `C:\Users\...`. The comparison never matches, so:
  - `--scope project` → `is not installed in project scope`
  - `--scope user` / `--scope local` → `is enabled at project scope`

  Every scope refuses it. The only repair is deleting the key from
  `installed_plugins.json`. Do not add a workaround for this to the tool — it is
  a CLI bug, and hand-editing its registry is not something a bulk updater
  should do unattended.
- `claude plugin uninstall` **exits 1 on failure** and prints the reason. Log
  that reason: "FAILED" on its own tells you nothing, and a refused uninstall is
  almost always about scope.
- **Hand-editing a project's `enabledPlugins` does not stick.** A running Claude
  Code session rewrites `.claude/settings.json` and restores the key, leaving no
  git diff to show for it. `installed_plugins.json` is the source of truth: once
  the entry is gone from there, `claude plugin list` stops showing the plugin
  even though the stale `false` flag reappears in settings.json. That leftover
  flag is inert - it disables a plugin that is no longer installed.

### Marketplaces can partially fail

`claude plugin marketplace update` exits **1** when *any* marketplace fails,
printing e.g. `1 marketplace could not be refreshed (see --debug): ruflo
✘ Updated 3 marketplaces, but not all`. The others really were updated, so this
is a warning about one source, not a dead stage.

### Verified: "already at the latest version"

Confirmed against Claude Code 2.1.260 — exit code **0**, output verbatim:

```
Checking for updates for plugin "superpowers@claude-plugins-official" at user scope…
✔ superpowers is already at the latest version (6.3.0).
```

So `classify()`'s loose `"already"` + `"latest"` match is correct. Note the name
in the message is the **bare** plugin name (`superpowers`), not the
`name@marketplace` form passed on the command line — don't tighten the match to
expect the full form.

### Verified: the orphan path

Exercised for real against a 62-plugin install, and it found a hole:

- **The orphan path fired**, and revealed that `Plugin.is_orphan` alone is not
  enough. `autofix-bot@claude-plugins-official` was deleted upstream and showed
  `Status: ✘ failed to load`, so the status check caught it. But
  `ruflo-wasm@ruflo` was *also* deleted upstream and showed `Status: ✘ disabled`
  — a disabled plugin reports `disabled` whatever happened to it, hiding the
  orphan. Orphans are therefore collected from two sources: the status line, and
  a `not found` verdict from the update attempt.

Still not verified:

- `--dry-run` cannot predict an update-time orphan. It only knows the status
  line, so it plans an update for a plugin like `ruflo-wasm` and lists no
  uninstall for it. This is inherent — dry run does not run updates — and is
  fine, but do not "fix" it by making dry run mutate anything.

## After EVERY update run: report the breaking changes

A version bump is not a report. Any time `update_claude.py` actually changes
something, fetch the real changelog for every component that moved and say what
**breaks**. Do this unprompted, every run.

Go straight to these sources - do not search for them:

| Component | Where the notes actually live |
|---|---|
| `@anthropic-ai/claude-code` | `raw.githubusercontent.com/anthropics/claude-code/main/CHANGELOG.md` - complete, `## <version>` headings, covers every patch |
| `@openai/codex` | GitHub **releases**: `gh api repos/openai/codex/releases`. Tags are `rust-v<ver>`; skip every `-alpha.*`. Its `CHANGELOG.md` is a 93-byte stub pointing here. |
| `claude-flow` | `gh api repos/ruvnet/claude-flow/releases/tags/v<ver>` |
| `@nanonets/graft` | Repo is **NanoNets/context-graph-engine** (not `nanonets/graft`). No releases, tags only - use `gh api .../compare/v<old>...v<new>` |

Method:

1. `awk` the changelog between the new version and the old one - not the whole file.
2. Grep that range for `breaking|no longer|removed|renamed|deprecat|now requires|must now|changed the default`.
3. **Verify each survivor against this machine.** Read `~/.claude/settings.json`,
   its permission rules, its `env` block. A change is only "breaking" if the
   config here actually trips it. Say "checked N rules, 0 affected" - a verified
   negative is a real result and is worth more than a vague warning.
4. Split the report into **BITES YOU** and **SAFE / FYI**. Never bury a live
   problem in a feature list.

When testing a permission rule for the "text after the closing parenthesis"
break, the test is `starts with Tool(` **and does not end with `)`**. A greedy
`^Tool\(.*\)\s*\S+` backtracks across escaped `\)` inside quoted commands and
flags dozens of perfectly valid rules.

### Pitch: one line per change, ELI15

The operator wants to know **what they got**, not that a number moved. Every
version that moved gets **one line** explaining what it does for them, in plain
words a bright fifteen-year-old would follow.

- `> ruflo-core@ruflo: 0.1.0 -> 0.2.6` is not a report. What arrived *in* 0.2.6
  is the report. The version is the citation, not the finding.
- Order by what they will actually notice: the CLI first, then anything that
  changes daily behaviour, then the long tail.
- Collapse the long tail. Twenty plugins that moved for the same reason are one
  line, not twenty.
- Say plainly when nothing happened. "47 plugins already current" is a complete
  answer in one line.
- **Never invent a changelog.** If you cannot establish what a version changed,
  say it moved and that you did not verify the contents. An invented feature
  list is worse than a bare version number, because the operator cannot tell
  the two apart.

**A moved version does not mean changed code.** Every plugin in a marketplace
shares that repo's commit SHA as its version, so one commit anywhere bumps all
of them. Verified 2026-09-07: ten official plugins reported
`0120fb83da5d -> 85cce0381e78` and were **byte-identical** apart from Claude
Code's own `.in_use` / `.orphaned_at` bookkeeping. Both trees stay in
`~/.claude/plugins/cache/<marketplace>/<plugin>/<sha>/`, so
`diff -rq -x .in_use -x .orphaned_at <old> <new>` settles it offline, with no
changelog and no guessing. Report churn as churn.

### Voice: caveman with a PhD

One line per change. Blunt short words, exact technical content. Verdict first,
mechanism second. No hedging, no paragraphs, no filler.

> Planning tool now sleep. `update_plan` is opt-in since codex 0.152.0 - set
> `tools.update_plan.enabled = true` or the model plans in its head.


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

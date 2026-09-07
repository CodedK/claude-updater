# claude-updater

One command to bring a machine's whole Claude Code toolchain up to date — the
CLI, its plugin marketplaces, every installed plugin, and the editor extension.

No dependencies beyond the standard library. Python 3.10+ (uses `X | None`
syntax), Windows/macOS/Linux.

## Runbook

### The default: run this

```bash
claude update                 # 1. the CLI itself
python update_claude.py       # 2. everything else
# 3. restart Claude Code
```

Three steps, in that order, and it is the whole routine. Step 1 is separate on
purpose: a natively-installed CLI updates *itself* and is not touched by any of
the four stages (see [What it does](#what-it-does)). Step 3 is not optional —
plugins are read at startup, so until you restart you are still running the old
ones.

You do not need to prefix anything with `PATH=` or `PYTHONIOENCODING=`. Locating
a native CLI that is not on `PATH`, and forcing UTF-8 output, are both handled
internally.

### When you want to look before you leap

```bash
python update_claude.py --dry-run
```

Prints the exact command for every package, marketplace, plugin and editor it
would touch, and changes nothing. Cheap, and the fastest way to see what is
installed.

### Housekeeping, occasionally

```bash
python update_claude.py --prune-orphans
```

Uninstalls plugins whose marketplace has deleted them. Run it from the directory
of the project that owns them — project-scoped plugins are matched against your
current directory, so from anywhere else only `user`-scope orphans can be
removed. A normal run names any orphans it finds without removing them, so you
will know when this is worth doing.

### Narrowing a run

```bash
python update_claude.py --skip npm ext        # only marketplaces + plugins
python update_claude.py --skip plugins        # everything except the slow stage
python update_claude.py --add-npm typescript  # one more global, if present
```

The plugin stage is the slow one: it runs one `claude plugin update` per plugin,
sequentially, so a 60-plugin machine takes minutes. The `[ n/total]` counter is
there so you can tell it apart from a hang.

### Reading the result

The last line of a good run is `all stages completed cleanly`, and the exit code
is `0`. Otherwise you get a numbered list of problems and exit code `1`, which is
what makes this usable from cron or a scheduled task.

Per-plugin markers:

| Marker | Meaning |
|---|---|
| `>` | updated — the version moved |
| `=` | already at the latest version |
| `x` | orphaned — deleted from its marketplace, `--prune-orphans` removes it |
| `-` | no marketplace behind it, nothing to update against; not a failure |
| `!` | a real failure, and the only kind worth re-running |

### When something fails

- **`npm install failed: <pkg>`** — read the message on the following line; it is
  the *tail* of npm's output, where the real cause is. A package with native
  addons needs a working C++ toolchain, and that is a machine problem, not a tool
  problem. Every other package still updates: they are installed one at a time.
- **`marketplace update exit 1`** — one source failed to refresh, the rest
  succeeded. Usually transient; run it again before investigating.
- **`plugin update failed: <name>`** — the only failure `--yes` might fix, and
  only when the marketplace has changed the plugin's declared install command.
  Read [About `--yes`](#about---yes) before reaching for it.
- **`claude CLI not found`** — install it, or check it is where the tool looks.
  Marketplace and plugin stages are skipped, and this counts as a problem rather
  than passing silently.

## What it does

Four stages, run in dependency order — marketplaces refresh *before* plugins, so
plugin updates resolve against current catalogs.

| Stage | Action |
|---|---|
| `npm` | `npm install -g <pkg>@latest` for the packages in `NPM_PACKAGES` |
| `market` | `claude plugin marketplace update` |
| `plugins` | `claude plugin update <name> --scope <scope>` for every installed plugin |
| `ext` | `--install-extension anthropic.claude-code --force` in each detected editor |

Nothing is hardcoded to one machine:

- **npm packages are only updated if already installed globally.** The tool never
  installs a new one. Check `installed_globals()` output, not a wishlist. Each is
  installed by its own `npm install -g` call, so one package that cannot build
  does not hold the others back.
- **Plugins are discovered at runtime** by parsing `claude plugin list`, including
  each plugin's scope, so `local`- and `user`-scope plugins both update correctly.
  Plugins loaded from a bare directory have no marketplace to update against and
  are skipped rather than counted as failures.
- **Editors are discovered by probing PATH** for `code`, `code-insiders`, `cursor`
  and `windsurf`. Missing ones are skipped silently.
- **The CLI is found on PATH or in `~/.local/bin`**, so a native install that was
  never added to PATH still works. If it genuinely cannot be found, that is
  reported as a problem and the exit code is `1` — skipped stages never count as
  a clean run.

> **The CLI itself is not updated by any of the four stages** unless you
> installed it from npm. A native install updates itself: run `claude update`.

## Flags

| Flag | Effect |
|---|---|
| `--dry-run` | Print every planned command, change nothing |
| `--yes` | Pass `--yes` to plugin updates — see the warning below |
| `--prune-orphans` | Uninstall plugins their marketplace has deleted |
| `--skip STAGE...` | Skip any of `npm market plugins ext` |
| `--add-npm PKG...` | Update extra global packages, if present |

Exit code is `1` if any stage reported a problem, `0` otherwise — usable in CI or
a scheduled task.

### About `--yes`

`claude plugin update` requires confirmation when a marketplace has changed a
plugin's declared install command, and it *demands* `-y` when stdout isn't a TTY.
This tool always captures output, so its stdout is never a TTY.

That confirmation is a real trust boundary: accepting it runs whatever command
the marketplace now declares. So `--yes` is **opt-in**. Without it, a plugin
needing re-confirmation fails loudly and the summary tells you to review it by
hand. Use `--yes` for unattended runs where you already trust every marketplace
you've installed from.

## Configuration

Edit the constants at the top of the file:

```python
NPM_PACKAGES = ("@anthropic-ai/claude-code", "claude-flow",
                "@openai/codex", "@nanonets/graft")
EDITOR_COMMANDS = ("code", "code-insiders", "cursor", "windsurf")
EXTENSION_ID = "anthropic.claude-code"
```

Or leave them alone and pass `--add-npm` per run.

## Notes from building this

Things that cost debugging time, so you don't repeat them:

- **`claude plugin list` bullets each plugin with `❯` (U+276F), not `>`.** Parsing
  for ASCII `>` silently yields zero plugins and the tool reports "no plugins
  parsed" while 62 sit installed. `PLUGIN_NAME_RE` accepts both — and it has to,
  because a PowerShell host on a legacy code page really does emit ASCII `>`.
- **Status lines carry decorative glyphs** (`Status: ✔ enabled`, or `√`/`×` under
  that same PowerShell host). They're stripped before comparison, so `status`
  reads plain `enabled` / `disabled` / `loaded` / `failed to load`.
- **A batched `npm install -g a@latest b@latest` is one transaction.** One
  package that fails to compile rolls back the lot, silently. Install singly.
- **An orphan is not always visible in the status line.** A plugin deleted
  upstream *and* disabled reports plain `disabled`, so orphans are also detected
  from a `not found` verdict when the update is attempted.
- **`claude plugin update` exits `1` on failure** and prints `✘ Failed to update
  plugin "..."`. Both signals are checked — exit code alone is enough today, the
  text check is belt-and-braces.
- **`npm ls -g --json` exits non-zero whenever anything is outdated**, so its exit
  code is deliberately ignored. Its stderr must stay *unmerged* or npm's warnings
  corrupt the JSON.
- **On Windows, `shutil.which("npm")` finds `npm.ps1`**, which `CreateProcess`
  cannot execute. `resolve()` prefers `.cmd`/`.exe`/`.bat` shims.
- **Windows gives Python a cp1252 stdout whenever output is piped or redirected**,
  and cp1252 cannot encode the `✔`/`✘` the CLI prints in normal success output.
  Unfixed, that aborts the run mid-stage — after npm has already made changes.
  `configure_stdio()` forces UTF-8 with `errors="replace"` before anything runs.
- Updates run sequentially. With 60+ plugins that takes minutes, hence the
  `[ n/total]` progress counter.

## Verified

On Windows 10 / Python 3.14.2, against Claude Code 2.1.246 with 62 plugins:

- `--dry-run` across all four stages — clean, correct commands printed.
- Plugin parsing — 62/62 parsed, versions, scopes (61 `user`, 1 `local`) and
  statuses (50 enabled, 12 disabled) all correct.
- Failure path — a bogus plugin name is correctly classified as failed.

A full live run against Claude Code 2.1.260 then confirmed the rest: all four
npm globals updated, 4 marketplaces refreshed, 62 plugins processed (1 changed,
61 already current, 0 failed) and the Cursor extension updated. The
already-at-latest message is verbatim `✔ <name> is already at the latest version
(<v>).` at exit 0, which `classify()` reads correctly.

On Windows 11 / Python 3.11.9, against Claude Code 2.1.247 and 2.1.263:

- A live pass updated 28 plugins and left 31 already current; a later pass
  updated 12 and left 47 current.
- Once everything was current a run printed
  `changed 0, already current 59, orphaned 2, unmanaged 1, failed 0` — the
  three non-updatable plugins are classified, not miscounted as failures.
- **The orphan path is now exercised**, and both detection routes fired: one
  plugin via `Status: failed to load`, and one — invisible to that check
  because it was also disabled — via the `not found` verdict from its update.
  `--prune-orphans` removed the first; the second turned out to be unremovable
  by the CLI at any scope and is documented in CLAUDE.md.
- npm's per-package install confirmed: `@nanonets/graft` cannot build without a
  C++ toolchain, and `@openai/codex` updated anyway. Batched with graft it had
  been stuck 22 releases back.
- Ten plugins reported a version change that was **byte-identical** in content
  — marketplace-wide SHA churn, not a real update.

`--dry-run` still cannot predict an update-time orphan, since it never runs an
update. It plans an ordinary update for such a plugin; the live run reclassifies
it.

## License

MIT — see [LICENSE](LICENSE).

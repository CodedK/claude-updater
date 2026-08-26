# claude-updater

One command to bring a machine's whole Claude Code toolchain up to date — the
CLI, its plugin marketplaces, every installed plugin, and the editor extension.

```bash
python update_claude.py --dry-run   # show what would change
python update_claude.py             # do it
```

No dependencies beyond the standard library. Python 3.10+ (uses `X | None`
syntax), Windows/macOS/Linux.

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
  installs a new one. Check `installed_globals()` output, not a wishlist.
- **Plugins are discovered at runtime** by parsing `claude plugin list`, including
  each plugin's scope, so `local`- and `user`-scope plugins both update correctly.
- **Editors are discovered by probing PATH** for `code`, `code-insiders`, `cursor`
  and `windsurf`. Missing ones are skipped silently.

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
  parsed" while 62 sit installed. `PLUGIN_NAME_RE` accepts both.
- **Status lines carry decorative glyphs** (`Status: ✔ enabled`). They're stripped
  before comparison, so `status` reads plain `enabled` / `disabled`.
- **`claude plugin update` exits `1` on failure** and prints `✘ Failed to update
  plugin "..."`. Both signals are checked — exit code alone is enough today, the
  text check is belt-and-braces.
- **`npm ls -g --json` exits non-zero whenever anything is outdated**, so its exit
  code is deliberately ignored. Its stderr must stay *unmerged* or npm's warnings
  corrupt the JSON.
- **On Windows, `shutil.which("npm")` finds `npm.ps1`**, which `CreateProcess`
  cannot execute. `resolve()` prefers `.cmd`/`.exe`/`.bat` shims.
- Updates run sequentially. With 60+ plugins that takes minutes, hence the
  `[ n/total]` progress counter.

## Verified

On Windows 10 / Python 3.14.2, against Claude Code 2.1.246 with 62 plugins:

- `--dry-run` across all four stages — clean, correct commands printed.
- Plugin parsing — 62/62 parsed, versions, scopes (61 `user`, 1 `local`) and
  statuses (50 enabled, 12 disabled) all correct.
- Failure path — a bogus plugin name is correctly classified as failed.

Not yet exercised on a live run: the "already at the latest" detection string and
the orphan-pruning path (no orphaned plugins were installed to test against).
Both degrade safely — an unrecognized message is reported verbatim rather than
being silently swallowed.

## License

MIT — see [LICENSE](LICENSE).

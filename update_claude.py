#!/usr/bin/env python3
"""Update the whole Claude Code toolchain on any machine.

Stages, in dependency order:
  1. npm    - global packages (Claude Code CLI + adjacent tooling)
  2. market - plugin marketplaces (refresh catalogs first, so plugin
              updates resolve against current sources)
  3. plugins- every installed plugin, discovered dynamically, per-scope
  4. ext    - the Claude Code extension in VS Code / Cursor / Windsurf

Nothing is hardcoded to one machine: plugins and editors are discovered at
runtime, and npm packages are only touched if already installed globally.

Usage:
    python update_claude.py                     # update everything
    python update_claude.py --dry-run           # show what would happen
    python update_claude.py --yes               # auto-accept plugin prompts
    python update_claude.py --prune-orphans     # also uninstall dead plugins
    python update_claude.py --skip npm ext      # run a subset of stages
    python update_claude.py --add-npm foo bar   # also update extra globals
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, replace
from typing import Sequence

IS_WINDOWS = os.name == "nt"

# Only updated when already present globally - we never install new tools.
NPM_PACKAGES: tuple[str, ...] = (
    "@anthropic-ai/claude-code",
    "claude-flow",
    "@openai/codex",
    "@nanonets/graft",
)

EDITOR_COMMANDS: tuple[str, ...] = ("code", "code-insiders", "cursor", "windsurf")
EXTENSION_ID = "anthropic.claude-code"

STAGES: tuple[str, ...] = ("npm", "market", "plugins", "ext")

ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")

# `claude plugin list` bullets each plugin with U+276F. Older builds used ">".
PLUGIN_NAME_RE = re.compile(r"^\s*[❯>]\s*(\S+)\s*$")
VERSION_RE = re.compile(r"^\s*Version:\s*(\S+)")
SCOPE_RE = re.compile(r"^\s*Scope:\s*(\S+)")
STATUS_RE = re.compile(r"^\s*Status:\s*(.+?)\s*$")
# Decorative check/cross/chevron glyphs the CLI mixes into status text.
GLYPH_RE = re.compile(r"[✔✘❯]")

# Windows shims Python can spawn directly; .ps1 is not executable via CreateProcess.
WINDOWS_EXTS: tuple[str, ...] = (".cmd", ".exe", ".bat")


# --------------------------------------------------------------------------
# process helpers
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Result:
    code: int
    output: str

    @property
    def ok(self) -> bool:
        return self.code == 0


def resolve(program: str) -> str | None:
    """Locate an executable, preferring shims that subprocess can actually run."""
    if IS_WINDOWS:
        for ext in WINDOWS_EXTS:
            found = shutil.which(program + ext)
            if found:
                return found
    return shutil.which(program)


def run(argv: Sequence[str], timeout: int = 900, merge_stderr: bool = True) -> Result:
    """Run a command, returning its exit code and decoded, ANSI-stripped output."""
    try:
        proc = subprocess.run(
            list(argv), capture_output=True, timeout=timeout, check=False
        )
    except FileNotFoundError:
        return Result(127, f"executable not found: {argv[0]}")
    except OSError as exc:
        return Result(126, f"could not run {argv[0]}: {exc}")
    except subprocess.TimeoutExpired:
        return Result(124, f"timed out after {timeout}s")

    raw = proc.stdout or b""
    if merge_stderr:
        raw += proc.stderr or b""
    text = raw.decode("utf-8", errors="replace")
    return Result(proc.returncode, ANSI_RE.sub("", text).strip())


def log(message: str = "") -> None:
    print(message, flush=True)


def heading(title: str) -> None:
    log()
    log(f"=== {title} ".ljust(64, "="))


def summarize(output: str, limit: int = 160) -> str:
    """Collapse multi-line command output into one readable line."""
    flat = " ".join(output.split())
    return flat[:limit] + ("..." if len(flat) > limit else "") or "(no output)"


# --------------------------------------------------------------------------
# plugin model
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Plugin:
    name: str
    version: str = "?"
    scope: str = "user"
    status: str = "unknown"

    @property
    def is_orphan(self) -> bool:
        """Upstream deleted it from its marketplace - no update can repair it."""
        return "failed to load" in self.status.lower()


def parse_plugins(listing: str) -> tuple[Plugin, ...]:
    """Parse `claude plugin list` output into immutable Plugin records."""
    plugins: list[Plugin] = []
    current: Plugin | None = None

    for line in listing.splitlines():
        name_match = PLUGIN_NAME_RE.match(line)
        if name_match:
            if current is not None:
                plugins.append(current)
            current = Plugin(name=name_match.group(1))
            continue
        if current is None:
            continue
        version_match = VERSION_RE.match(line)
        if version_match:
            current = replace(current, version=version_match.group(1))
            continue
        scope_match = SCOPE_RE.match(line)
        if scope_match:
            current = replace(current, scope=scope_match.group(1))
            continue
        status_match = STATUS_RE.match(line)
        if status_match:
            clean = GLYPH_RE.sub("", status_match.group(1)).strip()
            current = replace(current, status=clean)

    if current is not None:
        plugins.append(current)
    return tuple(plugins)


def classify(result: Result) -> str:
    """Bucket one `claude plugin update` run: failed / current / changed."""
    text = result.output.lower()
    if not result.ok or "failed to update" in text:
        return "failed"
    if "already" in text and "latest" in text:
        return "current"
    return "changed"


# --------------------------------------------------------------------------
# stages
# --------------------------------------------------------------------------

def installed_globals(npm: str) -> dict[str, str]:
    """Map of globally installed npm package -> version."""
    # npm ls exits non-zero when anything is outdated, so ignore the code.
    # stderr must stay separate or npm warnings corrupt the JSON.
    result = run([npm, "ls", "-g", "--depth=0", "--json"], merge_stderr=False)
    try:
        data = json.loads(result.output)
    except json.JSONDecodeError:
        return {}
    deps = data.get("dependencies") or {}
    return {
        name: (info or {}).get("version", "?")
        for name, info in deps.items()
        if isinstance(info, dict) or info is None
    }


def stage_npm(packages: Sequence[str], dry_run: bool) -> list[str]:
    heading("npm global packages")
    npm = resolve("npm")
    if npm is None:
        log("! npm not on PATH - skipping")
        return ["npm not found"]

    present = installed_globals(npm)
    targets = [pkg for pkg in packages if pkg in present]
    absent = [pkg for pkg in packages if pkg not in present]

    for pkg in absent:
        log(f"- {pkg}: not installed globally, skipping")
    if not targets:
        log("nothing to update")
        return []

    for pkg in targets:
        log(f"* {pkg} (current {present[pkg]})")
    if dry_run:
        log(f"DRY RUN: npm install -g {' '.join(t + '@latest' for t in targets)}")
        return []

    result = run([npm, "install", "-g", *(f"{t}@latest" for t in targets)], timeout=1800)
    after = installed_globals(npm)
    for pkg in targets:
        before, now = present[pkg], after.get(pkg, "?")
        marker = "=" if before == now else ">"
        log(f"  {marker} {pkg}: {before} -> {now}")

    if not result.ok:
        log(f"! npm exited {result.code}: {summarize(result.output)}")
        return [f"npm exit {result.code}"]
    return []


def stage_marketplaces(claude: str, dry_run: bool) -> list[str]:
    heading("plugin marketplaces")
    if dry_run:
        log("DRY RUN: claude plugin marketplace update")
        return []
    result = run([claude, "plugin", "marketplace", "update"], timeout=900)
    log(summarize(result.output))
    return [] if result.ok else [f"marketplace update exit {result.code}"]


def stage_plugins(
    claude: str, dry_run: bool, prune_orphans: bool, assume_yes: bool
) -> list[str]:
    heading("plugins")
    listing = run([claude, "plugin", "list"], timeout=300)
    plugins = parse_plugins(listing.output)
    if not plugins:
        log("! no plugins parsed - is the Claude CLI on PATH and initialised?")
        return ["no plugins parsed"]

    total = len(plugins)
    log(f"discovered {total} plugin(s)")
    if dry_run:
        for plugin in plugins:
            extra = " --yes" if assume_yes else ""
            log(f"DRY RUN: claude plugin update {plugin.name}"
                f" --scope {plugin.scope}{extra}  (at {plugin.version})")
        return []

    changed, current, failed = [], 0, []
    for index, plugin in enumerate(plugins, start=1):
        command = [claude, "plugin", "update", plugin.name, "--scope", plugin.scope]
        if assume_yes:
            command.append("--yes")
        result = run(command, timeout=600)
        text = summarize(result.output)
        counter = f"[{index:>3}/{total}]"
        verdict = classify(result)

        if verdict == "failed":
            failed.append(plugin.name)
            log(f"  ! {counter} {plugin.name}: {text}")
        elif verdict == "current":
            current += 1
            log(f"  = {counter} {plugin.name} {plugin.version}")
        else:
            changed.append(plugin.name)
            log(f"  > {counter} {plugin.name}: {text}")

    log(f"changed {len(changed)}, already current {current}, failed {len(failed)}")

    orphans = tuple(p for p in parse_plugins(run([claude, "plugin", "list"]).output)
                    if p.is_orphan)
    if orphans:
        log()
        log(f"{len(orphans)} orphaned plugin(s) - deleted from their marketplace:")
        for plugin in orphans:
            log(f"  x {plugin.name}")
        if prune_orphans:
            for plugin in orphans:
                result = run(
                    [claude, "plugin", "uninstall", plugin.name,
                     "--scope", plugin.scope],
                    timeout=300,
                )
                log(f"  {'removed' if result.ok else 'FAILED'} {plugin.name}")
        else:
            log("  (re-run with --prune-orphans to uninstall them)")

    return [f"plugin update failed: {name}" for name in failed]


def stage_extensions(dry_run: bool) -> list[str]:
    heading("editor extensions")
    problems, found_any = [], False

    for command in EDITOR_COMMANDS:
        binary = resolve(command)
        if binary is None:
            continue
        listing = run([binary, "--list-extensions", "--show-versions"], timeout=300)
        matches = [
            line.strip()
            for line in listing.output.splitlines()
            if line.strip().startswith(f"{EXTENSION_ID}@")
        ]
        if not matches:
            continue

        found_any = True
        log(f"* {command}: {matches[0]}")
        if dry_run:
            log(f"DRY RUN: {command} --install-extension {EXTENSION_ID} --force")
            continue

        result = run(
            [binary, "--install-extension", EXTENSION_ID, "--force"], timeout=900
        )
        log(f"  {summarize(result.output)}")
        if not result.ok:
            problems.append(f"{command} extension exit {result.code}")

    if not found_any:
        log(f"- {EXTENSION_ID} not installed in any detected editor")
    return problems


# --------------------------------------------------------------------------
# entry point
# --------------------------------------------------------------------------

def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Update the Claude Code toolchain (CLI, plugins, extensions).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--dry-run", action="store_true",
                        help="print planned actions without changing anything")
    parser.add_argument("--yes", action="store_true",
                        help="pass --yes to plugin updates, auto-accepting a "
                             "marketplace-declared install command without review")
    parser.add_argument("--prune-orphans", action="store_true",
                        help="uninstall plugins deleted from their marketplace")
    parser.add_argument("--skip", nargs="+", choices=STAGES, default=[],
                        metavar="STAGE", help=f"stages to skip: {', '.join(STAGES)}")
    parser.add_argument("--add-npm", nargs="+", default=[], metavar="PKG",
                        help="extra global npm packages to update if present")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    skip = frozenset(args.skip)

    log("Claude toolchain updater")
    log(f"python {sys.version.split()[0]} on {sys.platform}")
    if args.dry_run:
        log("DRY RUN - nothing will be modified")

    claude = resolve("claude")
    needs_claude = bool({"market", "plugins"} - skip)
    if claude is None and needs_claude:
        log()
        log("! 'claude' not on PATH - skipping marketplace and plugin stages.")
        log("  Install with: npm install -g @anthropic-ai/claude-code")
        skip = skip | {"market", "plugins"}

    problems: list[str] = []
    if "npm" not in skip:
        problems += stage_npm(tuple(NPM_PACKAGES) + tuple(args.add_npm), args.dry_run)
    if "market" not in skip and claude:
        problems += stage_marketplaces(claude, args.dry_run)
    if "plugins" not in skip and claude:
        problems += stage_plugins(
            claude, args.dry_run, args.prune_orphans, args.yes
        )
    if "ext" not in skip:
        problems += stage_extensions(args.dry_run)

    heading("summary")
    if problems:
        log(f"{len(problems)} problem(s):")
        for problem in problems:
            log(f"  ! {problem}")
        if not args.yes and any("plugin update failed" in p for p in problems):
            log()
            log("  A plugin whose install command needs re-confirming cannot be")
            log("  updated from a pipe. Re-run with --yes to accept, or update that")
            log("  plugin by hand in a terminal to review the command first.")
    else:
        log("all stages completed cleanly")

    if not args.dry_run:
        log()
        log("Restart Claude Code to load the updated CLI and plugins.")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())

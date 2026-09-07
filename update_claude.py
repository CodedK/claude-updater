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
from pathlib import Path
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
# Only a plugin loaded straight off disk (a skills directory) reports a Path.
PATH_RE = re.compile(r"^\s*Path:\s*(\S.*?)\s*$")
# Decorative check/cross/chevron glyphs the CLI mixes into status text. A
# PowerShell host on a legacy code page transliterates them to "√" and "×".
GLYPH_RE = re.compile(r"[✔✘❯√×]")

# Windows shims Python can spawn directly; .ps1 is not executable via CreateProcess.
WINDOWS_EXTS: tuple[str, ...] = (".cmd", ".exe", ".bat")

# The native installer drops the CLI here and does not always put it on PATH.
NATIVE_CLAUDE_DIRS: tuple[Path, ...] = (Path.home() / ".local" / "bin",)


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


def find_claude() -> str | None:
    """Locate the Claude CLI: PATH first, then the native installer's directory.

    A native install sits outside PATH on plenty of machines. Without this
    fallback the marketplace and plugin stages silently do nothing.
    """
    found = resolve("claude")
    if found:
        return found
    names = ("claude.exe", "claude.cmd", "claude.bat") if IS_WINDOWS else ("claude",)
    for directory in NATIVE_CLAUDE_DIRS:
        for name in names:
            candidate = directory / name
            if candidate.is_file() and os.access(candidate, os.X_OK):
                return str(candidate)
    return None


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


def configure_stdio() -> None:
    """Make stdout/stderr survive the non-ASCII glyphs the CLI prints.

    Windows falls back to cp1252 for these streams whenever they are not a
    console - a pipe, a redirect, a scheduled-task log. cp1252 cannot encode
    the check/cross marks `claude` emits, so printing one raises
    UnicodeEncodeError and aborts the run mid-stage.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError, ValueError):
            pass  # exotic stream - log() degrades gracefully below


def log(message: str = "") -> None:
    try:
        print(message, flush=True)
    except UnicodeEncodeError:
        # Belt-and-braces: configure_stdio() could not reconfigure this stream.
        encoding = getattr(sys.stdout, "encoding", None) or "ascii"
        print(message.encode(encoding, "replace").decode(encoding), flush=True)


def heading(title: str) -> None:
    log()
    log(f"=== {title} ".ljust(64, "="))


def summarize(output: str, limit: int = 160) -> str:
    """Collapse multi-line command output into one readable line."""
    flat = " ".join(output.split())
    return flat[:limit] + ("..." if len(flat) > limit else "") or "(no output)"


def summarize_failure(output: str, limit: int = 400) -> str:
    """Like summarize, but keeps the tail - build tools print the cause last."""
    lines = [line for line in output.splitlines() if line.strip()]
    flat = " ".join(" ".join(lines[-12:]).split())
    if not flat:
        return "(no output)"
    return ("..." + flat[-limit:]) if len(flat) > limit else flat


# --------------------------------------------------------------------------
# plugin model
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Plugin:
    name: str
    version: str = "?"
    scope: str = "user"
    status: str = "unknown"
    path: str = ""

    @property
    def is_orphan(self) -> bool:
        """Upstream deleted it from its marketplace - no update can repair it.

        This only catches an orphan that still tries to load. A disabled plugin
        reports "disabled" whatever happened upstream, so the update attempt in
        stage_plugins is the other half of the check.
        """
        return "failed to load" in self.status.lower()

    @property
    def is_unmanaged(self) -> bool:
        """Loaded straight from a directory, with no marketplace behind it."""
        return bool(self.path)


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
            continue
        path_match = PATH_RE.match(line)
        if path_match:
            current = replace(current, path=path_match.group(1))

    if current is not None:
        plugins.append(current)
    return tuple(plugins)


def classify(result: Result) -> str:
    """Bucket one `claude plugin update` run.

    orphan and unmanaged are failures no re-run can fix, so they are kept apart
    from the failures where retrying (or --yes) is worth suggesting.
    """
    text = result.output.lower()
    if not result.ok or "failed to update" in text:
        if "no marketplace backing" in text:
            return "unmanaged"
        if "not found" in text:
            return "orphan"
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
        for pkg in targets:
            log(f"DRY RUN: npm install -g {pkg}@latest")
        return []

    # One package per call. Batched into a single install, a package that cannot
    # build - native addons wanting a C++ toolchain - aborts the whole
    # transaction and silently holds every other package at its old version.
    failures: dict[str, str] = {}
    for pkg in targets:
        result = run([npm, "install", "-g", f"{pkg}@latest"], timeout=1800)
        if not result.ok:
            failures[pkg] = summarize_failure(result.output)

    after = installed_globals(npm)
    for pkg in targets:
        before, now = present[pkg], after.get(pkg, "?")
        marker = "=" if before == now else ">"
        log(f"  {marker} {pkg}: {before} -> {now}")
        if pkg in failures:
            log(f"    ! {failures[pkg]}")

    return [f"npm install failed: {pkg}" for pkg in failures]


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
            if plugin.is_unmanaged:
                log(f"DRY RUN: skip {plugin.name} - loaded from {plugin.path},"
                    " no marketplace to update from")
                continue
            extra = " --yes" if assume_yes else ""
            log(f"DRY RUN: claude plugin update {plugin.name}"
                f" --scope {plugin.scope}{extra}  (at {plugin.version})")
        if prune_orphans:
            for plugin in plugins:
                if plugin.is_orphan:
                    log(f"DRY RUN: claude plugin uninstall {plugin.name}"
                        f" --scope {plugin.scope}")
        return []

    changed, current, failed, unmanaged = [], 0, [], []
    pruned_failures: list[str] = []
    # Status-line orphans announce themselves up front. The rest only show up
    # when their update comes back "not found", so both sources feed this dict.
    orphans: dict[str, Plugin] = {p.name: p for p in plugins if p.is_orphan}

    for index, plugin in enumerate(plugins, start=1):
        counter = f"[{index:>3}/{total}]"
        if plugin.is_unmanaged:
            unmanaged.append(plugin.name)
            log(f"  - {counter} {plugin.name}: no marketplace backing, skipped")
            continue

        command = [claude, "plugin", "update", plugin.name, "--scope", plugin.scope]
        if assume_yes:
            command.append("--yes")
        result = run(command, timeout=600)
        text = summarize(result.output)
        verdict = classify(result)

        if verdict == "orphan":
            orphans[plugin.name] = plugin
            log(f"  x {counter} {plugin.name}: gone from its marketplace")
        elif verdict == "unmanaged":
            unmanaged.append(plugin.name)
            log(f"  - {counter} {plugin.name}: no marketplace backing, skipped")
        elif verdict == "failed":
            failed.append(plugin.name)
            log(f"  ! {counter} {plugin.name}: {text}")
        elif verdict == "current":
            current += 1
            log(f"  = {counter} {plugin.name} {plugin.version}")
        else:
            changed.append(plugin.name)
            log(f"  > {counter} {plugin.name}: {text}")

    log(f"changed {len(changed)}, already current {current},"
        f" orphaned {len(orphans)}, unmanaged {len(unmanaged)},"
        f" failed {len(failed)}")

    if orphans:
        log()
        log(f"{len(orphans)} orphaned plugin(s) - deleted from their marketplace:")
        for plugin in orphans.values():
            log(f"  x {plugin.name}")
        if prune_orphans:
            for plugin in orphans.values():
                result = run(
                    [claude, "plugin", "uninstall", plugin.name,
                     "--scope", plugin.scope],
                    timeout=300,
                )
                if result.ok:
                    log(f"  removed {plugin.name}")
                else:
                    # Never just say FAILED. A refused uninstall is usually a
                    # scope problem, and the reason is the only way to tell.
                    pruned_failures.append(plugin.name)
                    log(f"  FAILED  {plugin.name}: {summarize(result.output)}")
        else:
            log("  (re-run with --prune-orphans to uninstall them)")

    if unmanaged:
        log()
        log(f"{len(unmanaged)} plugin(s) with no marketplace behind them:")
        for name in unmanaged:
            log(f"  - {name}")
        log("  Nothing to update against - edit or delete the directory by hand.")

    return ([f"plugin update failed: {name}" for name in failed]
            + [f"orphan uninstall failed: {name}" for name in pruned_failures])


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
    configure_stdio()
    skip = frozenset(args.skip)

    log("Claude toolchain updater")
    log(f"python {sys.version.split()[0]} on {sys.platform}")
    if args.dry_run:
        log("DRY RUN - nothing will be modified")

    problems: list[str] = []
    claude = find_claude()
    needs_claude = bool({"market", "plugins"} - skip)
    if claude is None and needs_claude:
        log()
        log("! Claude CLI not found - skipping marketplace and plugin stages.")
        log("  Searched PATH and: "
            + ", ".join(str(d) for d in NATIVE_CLAUDE_DIRS))
        log("  Native install: run `claude update`, or add its directory to PATH.")
        log("  npm install:    npm install -g @anthropic-ai/claude-code")
        skip = skip | {"market", "plugins"}
        # A stage that never ran is not a stage that passed.
        problems.append("claude CLI not found - marketplace and plugin stages skipped")
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

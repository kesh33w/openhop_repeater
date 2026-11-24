#!/usr/bin/env python3
"""Prune old hashed web UI assets from repeater/web/html/assets.

Each UI bundle adds new content-hashed files, and the old ones are kept so a
browser tab left open across an upgrade can still load its lazy chunks. Kept
forever, they pile up (hundreds of files, tens of MB per checkout and per Pi
release directory). This keeps what can still be asked for:

- every asset the current index.html reaches, directly or through other
  assets (lazy chunks, CSS fonts and images);
- every asset reached from index.html in the last --keep-releases earlier UI
  bundles, taken from hydrahop-v* tags newest first (tags that ship the same
  index.html as the current build or a newer tag count once), so tabs opened
  on those releases keep working;
- every asset without a content hash (e.g. cad-mode-control-v1.js), which is
  referenced by a fixed name.

Everything else under assets/ is deleted. Dry run by default; pass --apply.
Run from the repository root, after rebuilding and restoring older assets.
"""

from __future__ import annotations

import argparse
import re
import subprocess  # nosec B404
import sys
from pathlib import Path

HTML_DIR = Path("repeater/web/html")
ASSETS = HTML_DIR / "assets"
# Vite names hashed assets "<name>-<8 char hash>.<ext>".
HASHED = re.compile(r"-[A-Za-z0-9_-]{8}\.[A-Za-z0-9]+$")
TEXT_SUFFIXES = {".js", ".mjs", ".css", ".html", ".json", ".svg", ".map"}


def git(*args: str) -> str:
    # git from PATH with arguments built here (tag names come from git itself).
    return subprocess.run(  # nosec B603 B607
        ["git", *args], check=True, capture_output=True, text=True
    ).stdout


def release_tags(count: int, current_index: str | None) -> list[str]:
    """Newest hydrahop-v* tags whose index.html differs from the current one
    and from every newer tag already picked, at most count of them."""
    picked: list[str] = []
    seen = {current_index}
    for tag in git("tag", "--list", "hydrahop-v*", "--sort=-v:refname").split():
        if len(picked) >= count:
            break
        index = read_at(tag)(str(HTML_DIR / "index.html"))
        if index is None or index in seen:
            continue
        seen.add(index)
        picked.append(tag)
    return picked


def reachable(read, names: set[str]) -> set[str]:
    """Asset names reachable from index.html. read(path) returns a file's
    text, or None when it does not exist in that version."""
    seen: set[str] = set()
    queue = [str(HTML_DIR / "index.html")]
    while queue:
        text = read(queue.pop())
        if text is None:
            continue
        for name in names:
            if name not in seen and name in text:
                seen.add(name)
                if Path(name).suffix in TEXT_SUFFIXES:
                    queue.append(str(ASSETS / name))
    return seen


def read_worktree(path: str) -> str | None:
    try:
        return Path(path).read_text(errors="ignore")
    except FileNotFoundError:
        return None


def read_at(tag: str):
    def read(path: str) -> str | None:
        try:
            return git("show", f"{tag}:{path}")
        except subprocess.CalledProcessError:
            return None

    return read


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--keep-releases",
        type=int,
        default=2,
        help="also keep the assets of the N newest earlier UI bundles in hydrahop-v* tags (default 2)",
    )
    parser.add_argument("--apply", action="store_true", help="delete (default: dry run)")
    args = parser.parse_args()

    if not ASSETS.is_dir():
        print(f"{ASSETS} not found: run from the repository root", file=sys.stderr)
        return 2

    names = {p.name for p in ASSETS.iterdir() if p.is_file()}
    keep = {n for n in names if not HASHED.search(n)}
    current = reachable(read_worktree, names)
    if not current:
        print("index.html reaches no assets; refusing to prune", file=sys.stderr)
        return 1
    keep |= current
    tags = release_tags(args.keep_releases, read_worktree(str(HTML_DIR / "index.html")))
    for tag in tags:
        keep |= reachable(read_at(tag), names)

    prune = sorted(names - keep)
    size = sum((ASSETS / n).stat().st_size for n in prune)
    print(
        f"{len(names)} assets: keeping {len(keep)} (current build {len(current)}, "
        f"releases {', '.join(tags) or 'none'}), pruning {len(prune)} ({size / 1e6:.1f} MB)"
    )
    if not args.apply:
        for n in prune:
            print(f"  would delete {n}")
        return 0
    for n in prune:
        (ASSETS / n).unlink()
    return 0


if __name__ == "__main__":
    sys.exit(main())

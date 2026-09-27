"""qdiff command line.

    qdiff OLD [NEW] [options]

OLD and NEW are directories (taken as they sit, uncommitted changes
included) or git refs. NEW defaults to the current directory. Both sides are
staged in scratch space, rendered to typst by the same quarto, and compared
by typst-diff, which diffs the evaluated documents and writes the PDF.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile

from qdiff import __version__
from qdiff.render import find_entry, inline_footnotes, prepare, raw_latex_sources, rewrite
from qdiff.sides import QdiffError, Side, release, repo_for, stage

INSTALL_HINT = (
    "install it with\n"
    "  cargo install --locked --git https://github.com/alexanderkoller/typst-diff typst-diff"
)


def typst_diff_binary() -> str:
    binary = shutil.which("typst-diff") or os.path.expanduser("~/.cargo/bin/typst-diff")
    if not os.path.isfile(binary):
        raise QdiffError("typst-diff not found; " + INSTALL_HINT)
    return binary


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        prog="qdiff", description="track-changes PDF from two versions of a Quarto paper"
    )
    ap.add_argument("old", help="old side: a directory or a git ref")
    ap.add_argument("new", nargs="?", default=".",
                    help="new side: a directory or a git ref (default: cwd)")
    ap.add_argument("--entry", help="entry .qmd relative to each side, e.g. paper/index.qmd")
    ap.add_argument("--profile", help="quarto profile to render both sides with")
    ap.add_argument("-o", "--output",
                    help="output PDF (default: <new>/.qdiff/trackchanges.pdf)")
    ap.add_argument("--execute", action="store_true",
                    help="run code cells while rendering (default: --no-execute)")
    ap.add_argument("--compact", action="store_true",
                    help="show substitutions as blue new text without the struck old text")
    ap.add_argument("--no-line-numbers", action="store_true")
    ap.add_argument("--keep", action="store_true",
                    help="keep the staged sides and print where they are")
    ap.add_argument("--open", action="store_true", help="open the PDF when done")
    ap.add_argument("--version", action="version", version=f"qdiff {__version__}")
    return ap.parse_args(argv)


def default_output(new_spec: str) -> str:
    base = new_spec if os.path.isdir(new_spec) else "."
    return os.path.join(os.path.abspath(base), ".qdiff", "trackchanges.pdf")


def count_edits(log_path: str) -> dict[str, int]:
    """Edit counts by kind from typst-diff's change log, whose entries are
    headed `## <line>: <kind>` (insert, delete, modify)."""
    counts: dict[str, int] = {}
    if os.path.isfile(log_path):
        with open(log_path, encoding="utf-8") as f:
            for kind in re.findall(r"^## \d+: (\w+)", f.read(), re.M):
                counts[kind] = counts.get(kind, 0) + 1
    return counts


def has_footnotes(typ: str) -> bool:
    with open(typ, encoding="utf-8") as f:
        return "#footnote[" in f.read()


def run(args: argparse.Namespace) -> str:
    binary = typst_diff_binary()
    # refs resolve in the new side's repo when it is a directory, else in cwd's
    repo = repo_for(args.new) if os.path.isdir(args.new) else repo_for(".")
    repo = repo or repo_for(".")
    output = os.path.abspath(args.output or default_output(args.new))
    os.makedirs(os.path.dirname(output), exist_ok=True)

    scratch = tempfile.mkdtemp(prefix="qdiff-")
    sides: list[Side] = []
    try:
        for label, spec in (("old", args.old), ("new", args.new)):
            sides.append(stage(label, spec, repo, scratch))
        old, new = sides
        entry = find_entry(new.tree, args.entry)
        if find_entry(old.tree, args.entry or entry) != entry:
            raise QdiffError(f"entry {entry} missing on the old side; pass --entry")
        latex = raw_latex_sources(new.tree, entry)
        if latex:
            print("qdiff: raw LaTeX blocks are dropped from quarto's typst output and "
                  "will be missing from the diff: " + ", ".join(latex), file=sys.stderr)
        typs = [
            prepare(s.tree, entry, args.execute, args.profile, not args.no_line_numbers)
            for s in sides
        ]
        log = os.path.splitext(output)[0] + "-changes.txt"
        cmd = [binary, typs[0], typs[1], "-o", output, "-l", log]
        if args.compact:
            cmd.append("--compact-substitutions")
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode != 0 and any(has_footnotes(t) for t in typs):
            print("qdiff: typst-diff failed with footnotes in the documents; "
                  "retrying with footnotes set inline", file=sys.stderr)
            for typ in typs:
                rewrite(typ, inline_footnotes)
            proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode != 0:
            raise QdiffError(
                "typst-diff failed:\n" + (proc.stderr or proc.stdout).strip()
                + "\n(rerun with --keep to inspect the staged sides)"
            )
        for s, typ in zip(sides, typs):
            shutil.copy2(typ, os.path.join(os.path.dirname(output), f"{s.label}.typ"))
        counts = count_edits(log)
        summary = ", ".join(f"{counts.get(k, 0)} {k}" for k in ("insert", "delete", "modify"))
        print(f"qdiff: {summary}")
        print(f"qdiff: change log {log}")
        print(f"qdiff: {output}")
        return output
    finally:
        if args.keep:
            print(f"qdiff: staged sides kept in {scratch}", file=sys.stderr)
        else:
            for s in sides:
                release(s)
            shutil.rmtree(scratch, ignore_errors=True)


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    try:
        output = run(args)
    except QdiffError as err:
        sys.exit(f"qdiff: {err}")
    if args.open:
        subprocess.run(["open" if sys.platform == "darwin" else "xdg-open", output])

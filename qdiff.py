#!/usr/bin/env python3
"""qdiff — track-changes PDFs from two versions of a Quarto paper.

    qdiff [OLD] [NEW] [options]

OLD and NEW are either directories on disk (used as they sit, uncommitted
changes and all) or git refs (commit, branch, tag), materialized with
`git archive` into a temp tree. NEW defaults to the working directory.

Pipeline:
  1. materialize both sides
  2. render each side's entry document to bare typst with the same quarto
     in the same invocation — toolchain churn cancels
  3. diff the two intermediates with typdiff (block diff, word-level
     inside modified blocks) or --engine=text as a difflib fallback
  4. compile the diff to PDF with typst
"""

from __future__ import annotations

import argparse
import difflib
import os
import re
import shutil
import subprocess
import time
import sys
import tempfile

# typdiff emits its own diff-added/diff-deleted definitions in the diff
# output; DIFF_DEFS is kept for the text engine and as a defensive prepend
# when an older typdiff omits them.
DIFF_DEFS = (
    '#let diff-added(body) = {\n'
    '  set text(fill: rgb("#0000ff"))\n'
    '  underline(body)\n'
    '}\n'
    '#let diff-deleted(body) = {\n'
        '  set text(fill: rgb("#cc0000"))\n'
        '  strike(body)\n'
    '}\n'
)

LINE_NUMBERS = '#set par.line(numbering: "1")\n'

ENTRY_CANDIDATES = ("paper/index.qmd", "index.qmd", "paper/main.qmd", "main.qmd")


def sh(cmd: list[str], cwd: str | None = None) -> str:
    """Run a command, exit with its stderr on failure, return stdout."""
    proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    if proc.returncode != 0:
        sys.exit(f"qdiff: `{' '.join(cmd)}` failed:\n{proc.stderr.strip()}")
    return proc.stdout


def is_git_ref(spec: str, repo_hint: str) -> bool:
    """True when spec is not an existing path and resolves as a git ref."""
    if os.path.exists(spec):
        return False
    proc = subprocess.run(
        ["git", "-C", repo_hint, "rev-parse", "--verify", "--quiet", f"{spec}^{{commit}}"],
        capture_output=True,
    )
    return proc.returncode == 0


def materialize(spec: str, repo_hint: str) -> tuple[str, str | None]:
    """Return (tree_path, temp_root) for a directory or a git ref.

    Dirs pass through as they sit (uncommitted changes and all). Refs are
    materialized as detached git worktrees, so the old side is the
    committed state and nothing in the source repo is disturbed.
    """
    if os.path.isdir(spec):
        return os.path.abspath(spec), None
    if not is_git_ref(spec, repo_hint):
        sys.exit(
            f"qdiff: {spec!r} is neither a directory nor a git ref visible "
            f"to {repo_hint!r}"
        )
    tmp = tempfile.mkdtemp(prefix="qdiff-wt-")
    sh(["git", "-C", repo_hint, "worktree", "add", "--detach", tmp, spec])
    return tmp, tmp


def release_tree(tree: str, tmp: str | None, repo_hint: str, keep: bool) -> None:
    """Remove a materialized worktree (unless --keep)."""
    if not tmp or keep:
        return
    subprocess.run(
        ["git", "-C", repo_hint, "worktree", "remove", "--force", tmp],
        capture_output=True,
    )
    if os.path.isdir(tmp):
        shutil.rmtree(tmp, ignore_errors=True)
    subprocess.run(["git", "-C", repo_hint, "worktree", "prune"], capture_output=True)


def repo_for(path: str) -> str | None:
    """The git repo containing path, or None."""
    proc = subprocess.run(
        ["git", "-C", path, "rev-parse", "--show-toplevel"], capture_output=True, text=True
    )
    if proc.returncode != 0:
        return None
    return proc.stdout.strip() or None


def find_entry(tree: str, override: str | None) -> str:
    """Locate the document to render inside a materialized tree."""
    if override:
        path = os.path.join(tree, override)
        if not os.path.isfile(path):
            sys.exit(f"qdiff: --entry {override} not found in {tree}")
        return override
    for cand in ENTRY_CANDIDATES:
        if os.path.isfile(os.path.join(tree, cand)):
            return cand
    qmds: list[str] = []
    for sub in ("", "paper"):
        d = os.path.join(tree, sub)
        if os.path.isdir(d):
            qmds += [
                os.path.join(sub, name)
                for name in sorted(os.listdir(d))
                if name.endswith(".qmd")
            ]
    if len(qmds) == 1:
        return qmds[0]
    sys.exit(
        "qdiff: could not locate an entry document; "
        "pass --entry paper/index.qmd"
    )


def render_side(tree: str, entry: str, execute: bool) -> tuple[str, list[str]]:
    """Render one side in place and return (typ_path, artifacts_to_clean).

    Renders with -M keep-typ:true so quarto leaves the bare .typ next to
    the entry document. (With --output-dir, quarto 1.10 deletes the .typ
    regardless of keep-typ — so we render in place and clean up after.)
    """
    entry_abs = os.path.abspath(os.path.join(tree, entry))
    entry_dir = os.path.dirname(entry_abs)
    stem = os.path.splitext(os.path.basename(entry))[0]
    start = time.time() - 1
    cmd = [
        "quarto", "render", entry_abs,
        "--to", "typst",
        "-M", "keep-typ:true",
    ]
    if not execute:
        cmd.append("--no-execute")
    # qdiff only needs the .typ, and quarto leaves it behind even when its
    # own .typ->PDF compile fails, so a failed PDF step is a warning here,
    # not an exit.
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        sys.stderr.write(
            "qdiff: quarto's PDF compile of the typst intermediate failed "
            "(qdiff only needs the .typ); continuing\n"
            + (proc.stderr.strip()[-2000:] + "\n")
        )
    new_typs = []
    for dirpath, _dirnames, filenames in os.walk(entry_dir):
        for name in filenames:
            if name == stem + ".typ":
                path = os.path.join(dirpath, name)
                if os.path.getmtime(path) >= start:
                    new_typs.append(path)
    if not new_typs:
        sys.exit(
            f"qdiff: no {stem}.typ appeared under {entry_dir} after render; "
            "quarto may have placed output elsewhere"
        )
    typ = max(new_typs, key=os.path.getmtime)
    artifacts = [typ]
    for dirpath, _dirnames, filenames in os.walk(entry_dir):
        for name in filenames:
            if name == stem + ".pdf":
                path = os.path.join(dirpath, name)
                if os.path.getmtime(path) >= start:
                    artifacts.append(path)
    return typ, artifacts


SHOW_HEAD = re.compile(r"#show: doc => article\(")
FLATTEN_KEYS = ("title", "subtitle", "abstract", "keywords", "thanks", "date")


def metadata_block(key: str, value_line: str) -> str:
    """One `key: [content],` metadata line → a diffable body block."""
    content = value_line.split(":", 1)[1].strip()
    content = content.rstrip(",").strip()
    if content.startswith("[") and content.endswith("]"):
        content = content[1:-1]
    if key == "title":
        return "= " + content
    if key == "subtitle":
        return "== " + content
    if key == "date":
        return content
    if key == "abstract":
        return "== Abstract\n\n" + content
    if key == "keywords":
        return "*Keywords:* " + content
    return "*Thanks:* " + content


def flatten_metadata(text: str) -> str:
    """Move title/subtitle/abstract/date/keywords/thanks out of the
    article() show rule and into the body as plain blocks, so typdiff can
    word-diff them. The remaining show-rule args are identical furniture.

    Returns the restructured text; no-op when the shape is unexpected.
    """
    m = SHOW_HEAD.search(text)
    if not m:
        return text
    close = text.find("\n)", m.start())
    if close == -1:
        return text
    end = close + 2
    block = text[m.start():end]
    keep: list[str] = ["#show: doc => article("]
    meta: list[str] = []
    for line in block.splitlines()[1:-1]:
        ls = line.strip()
        km = re.match(r"(\w+): \[(.*)\],?$", ls)
        if km and km.group(1) in FLATTEN_KEYS:
            meta.append(metadata_block(km.group(1), ls))
            continue
        keep.append(line)
    show_rule = "\n".join(keep) + "\n)\n"
    meta_body = "\n".join(meta)
    return text[:m.start()] + show_rule + "\n" + meta_body + "\n" + text[end:].lstrip("\n")


def normalize(text: str, tokens: list[tuple[str, str]]) -> str:
    """Replace absolute tree/output paths with side tokens so the two
    intermediates compare by content, not by where they sat on disk."""
    for path, token in tokens:
        if path and path != "/":
            text = text.replace(path, token)
    return text


def placeholder_images(text: str) -> str:
    """Replace image() calls with text markers `[FIGURE: path]` so the final
    compile never resolves image paths."""
    return re.sub(r'image\("([^"]+)"\)', r"[FIGURE: \1]", text)


BIB_RE = re.compile(r'#bibliography\(\(([^)]*)\)\)')


# Typst reference syntax: `@` + label chars (label names extend over `-`,
# `_`, `.`, `:`). Pandoc's typst writer emits citations as `@bibkey`, and
# a citation directly followed by an em-dash glues into the label
# (`@key---because`), which then fails as an unknown label. Rewriting
# bib-key refs to `#cite(<key>)` closes the token before any trailing text.
CITATION_RE = re.compile(r'@([A-Za-z0-9][A-Za-z0-9_.:-]*)')


# Pandoc's typst writer emits citation supplements as a trailing content
# block (`#cite(<key>)[p. 22]`), which typst 0.13+ rejects — supplement is
# a named argument. Normalize to the named form.
CITE_SUPP_RE = re.compile(r'#cite\(<([^>]+)>\)\[([^\]]*)\]')

# A `#ref(<label>` call, optionally with a named supplement — the form
# pandoc's typst writer emits for cross-references.
REF_FULL_RE = re.compile(
    r'#ref\(<([A-Za-z0-9][\w.:-]*)>(?:, supplement: \[([^\]]*)\])?\)'
)


def read_bib_keys(paths: list[str]) -> set[str]:
    """Collect citation keys from .bib files (both sides, unioned)."""
    keys: set[str] = set()
    for path in paths:
        if not os.path.isfile(path):
            continue
        with open(path, encoding="utf-8", errors="replace") as f:
            keys |= set(re.findall(r'@\w+\{\s*([^,\s{}]+)\s*,', f.read()))
    return keys


def merge_bibs(paths: list[str], dest: str) -> None:
    """Write the union of BibTeX entries from several .bib files.

    A cross-version diff cites keys from both sides; the revision usually
    drops keys the original cited, so one side's bib alone fails the compile
    with "citation key not present in the bibliography". Entries are parsed
    by scanning `@type{` starts with brace-depth tracking; the first side to
    define a key wins.
    """
    merged: dict[str, str] = {}
    order: list[str] = []
    for path in paths:
        if not os.path.isfile(path):
            continue
        with open(path, encoding="utf-8", errors="replace") as f:
            src = f.read()
        i = 0
        while True:
            m = re.compile(r"@\w+\{").search(src, i)
            if not m:
                break
            depth = 0
            j = m.start()
            while j < len(src):
                if src[j] == "{":
                    depth += 1
                elif src[j] == "}":
                    depth -= 1
                    if depth == 0:
                        break
                j += 1
            entry = src[m.start():j + 1]
            km = re.match(r"@\w+\{\s*([^,\s{}]+)\s*,", entry)
            if km:
                key = km.group(1)
                if key not in merged:
                    merged[key] = entry
                    order.append(key)
            i = j + 1
    with open(dest, "w", encoding="utf-8") as f:
        f.write("\n\n".join(merged[k] for k in order) + "\n")


def cite_fix(text: str, bib_keys: set[str]) -> str:
    """Rewrite `@bibkey` references to `#cite(<bibkey>)` so trailing text
    like em-dashes cannot glue into the label. Cross-references (@eq-did,
    @tbl-...) are not bib keys and pass through untouched. After the
    rewrite, a citation supplement emitted as a trailing content block
    (`@key[supp]` -> `#cite(<key>)[supp]`) is normalized to the
    named-argument form typst 0.13+ accepts."""
    if not bib_keys:
        return text
    ordered = sorted(bib_keys, key=len, reverse=True)

    def repl(m: re.Match) -> str:
        cand = m.group(1)
        if cand in bib_keys:
            return f"#cite(<{cand}>)"
        for key in ordered:
            if cand.startswith(key) and cand[len(key):].startswith("-"):
                return f"#cite(<{key}>)" + cand[len(key):]
        return m.group(0)

    text = CITATION_RE.sub(repl, text)
    return CITE_SUPP_RE.sub(r'#cite(<\1>, supplement: [\2])', text)


CSL_STR_RE = re.compile(r'(#set bibliography\(style: ")([^"]*)(")')


def fix_csl_path(text: str) -> str:
    """Quarto escapes a leading `.` in the CSL path (`.pandoc/...` reaches the
    .typ as `\\.pandoc/...`), which typst 0.15 rejects as a path. Strip the
    escapes from the style string."""
    return CSL_STR_RE.sub(
        lambda m: m.group(1) + m.group(2).replace("\\", "") + m.group(3), text
    )


def fix_dangling_refs(text: str) -> tuple[str, int]:
    """Rewrite `#ref(<label>)` calls whose label the diff does not define.

    typdiff emits deleted code-mode blocks (figures, equations wrapped in
    calls) as typst comments, so any label defined on a deleted block
    vanishes; refs to it in deleted text then fail the compile with "label
    does not exist". Rewrite the ref as literal struck text (its supplement
    word plus the label name, kind prefix dropped) — the ref's *text* is what
    a track-changes reader needs, not a live pointer.
    """
    stripped = re.sub(
        r'#(?:ref|cite)\(<[^>]*>(?:, supplement: \[[^\]]*\])?\)', "", text
    )
    defined = set(re.findall(r'<([A-Za-z0-9][\w.:-]*)>', stripped))

    def repl(m):
        name, supp = m.group(1), (m.group(2) or "")
        if name in defined:
            return m.group(0)
        short = name
        if name[:4] in ("sec-", "fig-", "tbl-"):
            short = name.split("-", 1)[1]
        count[0] += 1
        return f"{supp} {short}".strip()

    count = [0]
    return REF_FULL_RE.sub(repl, text), count[0]


CSL_RE = re.compile(r'#set bibliography\(style: "([^"]+)"\)')


def copy_bibs(text: str, bases: list[str], outdir: str) -> None:
    """Copy the bibliography and CSL files the diff references next to the
    compile so typst resolves them. Prefers the new side's files."""
    rels: list[str] = []
    m = BIB_RE.search(text)
    if m:
        rels += re.findall(r'"([^"]+)"', m.group(1))
    csl = CSL_RE.search(text)
    if csl:
        rels.append(csl.group(1).replace("\\", ""))
    for rel in rels:
        existing = [os.path.join(base, rel) for base in bases
                    if os.path.isfile(os.path.join(base, rel))]
        if not existing:
            sys.stderr.write(f"qdiff: bibliography file not found: {rel}\n")
            continue
        dest = os.path.join(outdir, rel)
        if len(existing) > 1 and rel.endswith(".bib"):
            # a cross-version diff cites both sides' keys: union the .bib
            merge_bibs(existing, dest)
            continue
        src = existing[0]
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        shutil.copy2(src, dest)


def engine_typdiff(old: str, new: str, out: str) -> bool:
    """Block diff via typdiff. False when typdiff is absent or fails."""
    binary = shutil.which("typdiff") or os.path.expanduser("~/.cargo/bin/typdiff")
    if not binary or not os.path.isfile(binary):
        return False
    proc = subprocess.run([binary, old, new, "-o", out], capture_output=True, text=True)
    if proc.returncode != 0:
        sys.stderr.write(
            "qdiff: typdiff failed "
            f"({proc.stderr.strip() or proc.stdout.strip()}) — falling back to text engine\n"
        )
        return False
    return True


def word_diff(a: str, b: str) -> str:
    """Word-level diff of one replaced line pair, as typst diff markup."""
    sm = difflib.SequenceMatcher(None, a.split(), b.split(), autojunk=False)
    parts: list[str] = []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            parts.append(" ".join(a.split()[i1:i2]))
        elif tag == "delete":
            parts.append("#diff-deleted[" + " ".join(a.split()[i1:i2]) + "]")
        elif tag == "insert":
            parts.append("#diff-added[" + " ".join(b.split()[j1:j2]) + "]")
        elif tag == "replace":
            parts.append("#diff-deleted[" + " ".join(a.split()[i1:i2]) + "]")
            parts.append("#diff-added[" + " ".join(b.split()[j1:j2]) + "]")
    return " ".join(parts)


def engine_text(old: str, new: str, out: str) -> None:
    """difflib fallback: line diff, word-level markup inside replaced lines."""
    with open(old, encoding="utf-8") as f:
        old_lines = f.read().splitlines()
    with open(new, encoding="utf-8") as f:
        new_lines = f.read().splitlines()
    sm = difflib.SequenceMatcher(a=old_lines, b=new_lines, autojunk=False)
    emitted: list[str] = [DIFF_DEFS]
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            emitted.extend(old_lines[i1:i2])
        elif tag == "delete":
            emitted.extend(f"#diff-deleted[{line}]" for line in old_lines[i1:i2])
        elif tag == "insert":
            emitted.extend(f"#diff-added[{line}]" for line in new_lines[j1:j2])
        else:  # replace
            pairs = zip(old_lines[i1:i2], new_lines[j1:j2])
            for a, b in pairs:
                emitted.append(word_diff(a, b))
            if i2 - i1 > j2 - j1:
                tail = old_lines[i1 + (j2 - j1):i2]
                emitted.extend(f"#diff-deleted[{line}]" for line in tail)
            elif j2 - j1 > i2 - i1:
                tail = new_lines[j1 + (i2 - i1):j2]
                emitted.extend(f"#diff-added[{line}]" for line in tail)
    with open(out, "w", encoding="utf-8") as f:
        f.write("\n".join(emitted) + "\n")


def wrapup(diff_typ: str, out_pdf: str, line_numbers: bool) -> None:
    """Defensively prepend diff defs and line numbering, compile to PDF."""
    with open(diff_typ, encoding="utf-8") as f:
        content = f.read()
    if "#let diff-added" not in content:
        content = DIFF_DEFS + content
    if line_numbers:
        content = LINE_NUMBERS + content
    with open(diff_typ, "w", encoding="utf-8") as f:
        f.write(content)
    sh(["typst", "compile", diff_typ, out_pdf])


def main() -> None:
    ap = argparse.ArgumentParser(
        prog="qdiff",
        description="track-changes PDF from two versions of a Quarto paper",
    )
    ap.add_argument("old", help="old side: a directory or a git ref")
    ap.add_argument("new", nargs="?", default=".",
                    help="new side: a directory or a git ref (default: cwd)")
    ap.add_argument("--entry", help="entry .qmd, e.g. paper/index.qmd")
    ap.add_argument("--outdir", help="output directory (default: <new>/.qdiff)")
    ap.add_argument("--engine", choices=("typdiff", "text"), default="typdiff")
    ap.add_argument("--execute", action="store_true",
                    help="allow code execution (default: --no-execute)")
    ap.add_argument("--keep", action="store_true",
                    help="keep materialized git-ref trees for inspection")
    ap.add_argument("--open", action="store_true",
                    help="open the PDF when done")
    ap.add_argument("--no-line-numbers", action="store_true")
    ap.add_argument("--no-placeholders", action="store_true")
    args = ap.parse_args()

    new_tree, new_tmp = materialize(args.new, repo_for(args.new) or repo_for(".") or ".")
    repo_hint = repo_for(new_tree) or repo_for(".") or "."
    old_tree, old_tmp = materialize(args.old, repo_hint)

    entry_new = find_entry(new_tree, args.entry)
    entry_old = find_entry(old_tree, args.entry)
    if entry_new != entry_old:
        sys.exit(f"qdiff: entries differ ({entry_old!r} vs {entry_new!r}); pass --entry")

    outdir = os.path.abspath(args.outdir or os.path.join(new_tree, ".qdiff"))
    os.makedirs(outdir, exist_ok=True)

    typ_old, arts_old = render_side(old_tree, entry_old, args.execute)
    typ_new, arts_new = render_side(new_tree, entry_new, args.execute)

    tokens = [
        (old_tree, "__QDIFF_OLD__"),
        (new_tree, "__QDIFF_NEW__"),
        (outdir, "__QDIFF_OUT__"),
    ]
    with open(typ_old, encoding="utf-8") as f:
        raw_old = f.read()
    with open(typ_new, encoding="utf-8") as f:
        raw_new = f.read()

    bases = [
        os.path.dirname(os.path.abspath(os.path.join(new_tree, entry_new))),
        new_tree,
        os.path.dirname(os.path.abspath(os.path.join(old_tree, entry_old))),
        old_tree,
    ]
    bib_rels: list[str] = []
    for text in (raw_old, raw_new):
        m = BIB_RE.search(text)
        if m:
            bib_rels += re.findall(r'"([^"]+)"', m.group(1))
    bib_keys = read_bib_keys([os.path.join(b, rel) for rel in bib_rels for b in bases])

    def prep(text: str) -> str:
        out = flatten_metadata(fix_csl_path(cite_fix(normalize(text, tokens), bib_keys)))
        if args.no_placeholders:
            # keep image() calls live: copy each referenced file into the
            # outdir under its side token, where the tokenized relative
            # path resolves from the diff document
            for token, tree in (("__QDIFF_OLD__", old_tree), ("__QDIFF_NEW__", new_tree)):
                for rel in sorted(set(
                    re.findall(r'image\("' + re.escape(token) + r'/([^"]+)"\)', out)
                )):
                    src = os.path.join(tree, rel)
                    if os.path.isfile(src):
                        dest = os.path.join(outdir, token, rel)
                        os.makedirs(os.path.dirname(dest), exist_ok=True)
                        shutil.copy2(src, dest)
                    else:
                        sys.stderr.write(f"qdiff: image not found: {rel}\n")
        else:
            out = placeholder_images(out)
        return out

    text_old = prep(raw_old)
    text_new = prep(raw_new)

    for art in arts_old + arts_new:
        try:
            os.remove(art)
        except OSError:
            pass

    with open(os.path.join(outdir, "intermediate-old.typ"), "w", encoding="utf-8") as f:
        f.write(text_old)
    with open(os.path.join(outdir, "intermediate-new.typ"), "w", encoding="utf-8") as f:
        f.write(text_new)

    for text in (text_old, text_new):
        copy_bibs(text, bases, outdir)

    old_n = os.path.join(outdir, "intermediate-old.typ")
    new_n = os.path.join(outdir, "intermediate-new.typ")
    diff_typ = os.path.join(outdir, "index-trackchanges.typ")
    out_pdf = os.path.join(outdir, "index-trackchanges.pdf")

    if args.engine == "text":
        engine_text(old_n, new_n, diff_typ)
    elif not engine_typdiff(old_n, new_n, diff_typ):
        engine_text(old_n, new_n, diff_typ)

    with open(diff_typ, encoding="utf-8") as f:
        diff_content = f.read()
    diff_content, n_dangling = fix_dangling_refs(diff_content)
    if n_dangling:
        sys.stderr.write(
            f"qdiff: rewrote {n_dangling} dangling #ref(s) as literal text\n"
        )
    with open(diff_typ, "w", encoding="utf-8") as f:
        f.write(diff_content)

    line_numbers = not args.no_line_numbers
    wrapup(diff_typ, out_pdf, line_numbers)

    with open(diff_typ, encoding="utf-8") as f:
        diff_content = f.read()
    n_add = diff_content.count("#diff-added[")
    n_del = diff_content.count("#diff-deleted[")

    for tmp in (old_tmp, new_tmp):
        if tmp:
            release_tree(tmp, tmp, repo_hint, args.keep)

    print(f"qdiff: {n_del} deleted, {n_add} added fragments")
    print(f"qdiff: {out_pdf}")
    if args.open:
        subprocess.run(["open", out_pdf])

if __name__ == "__main__":
    main()

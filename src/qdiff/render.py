"""Render a staged side to bare typst and make it a clean diff input.

typst-diff evaluates each side as a full typst document, so each side's
`.typ` must compile standalone; quarto's typst output does not always
(quarto's own `.typ -> PDF` step can fail on the same file). qdiff repairs
the known toolchain faults here, strips what typst-diff cannot lay out, and
moves the title block into the body so it diffs word by word. The old
engine's other workarounds (image placeholders, bibliography merging,
dangling-ref rewriting) are gone: typst-diff evaluates each side against
its own files, bibliography and labels.
"""

from __future__ import annotations

import os
import re
import subprocess
import time

from qdiff.sides import QdiffError

ENTRY_CANDIDATES = ("paper/index.qmd", "index.qmd", "paper/main.qmd", "main.qmd")

# The file typst-diff reads, written next to quarto's own .typ so relative
# paths (images, bibliography, CSL) resolve from typst-diff's root, which is
# the directory of the file it is given.
DIFF_INPUT = "__qdiff__.typ"

LINE_NUMBERS = '#set par.line(numbering: "1")\n'


def find_entry(tree: str, override: str | None) -> str:
    """The entry document, relative to the tree."""
    if override:
        if not os.path.isfile(os.path.join(tree, override)):
            raise QdiffError(f"--entry {override} not found in {tree}")
        return override
    for cand in ENTRY_CANDIDATES:
        if os.path.isfile(os.path.join(tree, cand)):
            return cand
    qmds = [
        os.path.join(sub, name)
        for sub in ("", "paper")
        if os.path.isdir(os.path.join(tree, sub))
        for name in sorted(os.listdir(os.path.join(tree, sub)))
        if name.endswith(".qmd") and not name.startswith("_")
    ]
    if len(qmds) == 1:
        return qmds[0]
    raise QdiffError("could not locate an entry document; pass --entry paper/index.qmd")


def quarto_typst(tree: str, entry: str, execute: bool,
                 profile: str | None) -> tuple[str, str]:
    """Render the entry to typst inside the staged tree.

    Returns (directory quarto wrote the .typ to, the .typ text).

    `-M keep-typ:true` keeps the intermediate. A failed quarto PDF step is
    tolerated — qdiff needs only the .typ, which quarto leaves behind.
    """
    entry_abs = os.path.join(tree, entry)
    stem = os.path.splitext(os.path.basename(entry))[0]
    cmd = ["quarto", "render", entry_abs, "--to", "typst", "-M", "keep-typ:true"]
    if not execute:
        cmd.append("--no-execute")
    if profile:
        cmd += ["--profile", profile]
    start = time.time() - 1
    proc = subprocess.run(cmd, cwd=tree, capture_output=True, text=True)
    typs = [
        os.path.join(d, name)
        for d, _dirs, names in os.walk(tree)
        for name in names
        if name == stem + ".typ" and os.path.getmtime(os.path.join(d, name)) >= start
    ]
    if not typs:
        raise QdiffError(
            f"quarto produced no {stem}.typ for {entry}:\n{proc.stderr.strip()[-2000:]}"
        )
    typ = max(typs, key=os.path.getmtime)
    with open(typ, encoding="utf-8") as f:
        return os.path.dirname(typ), f.read()


# Typst reference syntax: `@` + label chars, which extend over `-`, `_`,
# `.`, `:`. Pandoc's typst writer emits citations as `@bibkey`, so a
# citation followed by an em-dash glues into the label (`@key---because`)
# and fails as an unknown label. `#cite(<key>)` closes the token.
CITATION_RE = re.compile(r"@([A-Za-z0-9][A-Za-z0-9_.:-]*)")

# Pandoc emits a supplement as a trailing content block (`#cite(<key>)[p.
# 22]`), which typst 0.13+ rejects; supplement is a named argument.
CITE_SUPP_RE = re.compile(r"#cite\(<([^>]+)>\)\[([^\]]*)\]")

# Quarto escapes a leading `.` in the CSL path (`.pandoc/x.csl` reaches the
# .typ as `\.pandoc/x.csl`), which typst rejects as a path.
CSL_STR_RE = re.compile(r'(#set bibliography\(style: ")([^"]*)(")')

BIB_RE = re.compile(r"#bibliography\(\(?([^)]*)\)")


def bib_keys(typ_text: str, typ_dir: str) -> set[str]:
    """Citation keys of the .bib files this side's .typ loads."""
    keys: set[str] = set()
    m = BIB_RE.search(typ_text)
    if not m:
        return keys
    for rel in re.findall(r'"([^"]+)"', m.group(1)):
        path = os.path.join(typ_dir, rel)
        if rel.endswith(".bib") and os.path.isfile(path):
            with open(path, encoding="utf-8", errors="replace") as f:
                keys |= set(re.findall(r"@\w+\{\s*([^,\s{}]+)\s*,", f.read()))
    return keys


def cite_fix(text: str, keys: set[str]) -> str:
    """`@bibkey` -> `#cite(<bibkey>)`, splitting off glued trailing text;
    then supplements to the named-argument form. Cross-references
    (`@fig-map`) are not bib keys and pass through."""
    if keys:
        ordered = sorted(keys, key=len, reverse=True)

        def repl(m: re.Match) -> str:
            cand = m.group(1)
            if cand in keys:
                return f"#cite(<{cand}>)"
            for key in ordered:
                if cand.startswith(key) and cand[len(key):].startswith("-"):
                    return f"#cite(<{key}>)" + cand[len(key):]
            return m.group(0)

        text = CITATION_RE.sub(repl, text)
    return CITE_SUPP_RE.sub(r"#cite(<\1>, supplement: [\2])", text)


def fix_csl_path(text: str) -> str:
    return CSL_STR_RE.sub(
        lambda m: m.group(1) + m.group(2).replace("\\", "") + m.group(3), text
    )


def strip_document_sets(text: str) -> str:
    """Remove `set document(...)` rules (PDF metadata only).

    Quarto's article template sets document metadata inside the function its
    `#show: doc => article(...)` rule applies. typst-diff lays the evaluated
    content out inside a container, where typst rejects document set rules
    ("document set rules are not allowed inside of containers"). The rules
    carry no visible content, so dropping them changes nothing on the page.
    """
    out: list[str] = []
    i = 0
    for m in re.finditer(r"set document\(", text):
        if m.start() < i:
            continue
        depth, j = 0, m.end() - 1
        while j < len(text):
            if text[j] == "(":
                depth += 1
            elif text[j] == ")":
                depth -= 1
                if depth == 0:
                    break
            j += 1
        # a conditional rule (`set document(...) if cond`) goes whole
        tail = re.match(r"[ \t]+if\b[^\n]*", text[j + 1:])
        if tail:
            j += tail.end()
        out.append(text[i:m.start()])
        out.append("none")  # keeps a `#set document(...)` line valid code
        i = j + 1
    out.append(text[i:])
    return "".join(out)


SHOW_HEAD = re.compile(r"^#show: doc => article\(\n", re.M)
FLATTEN = {
    "title": "#align(center, text(size: 1.5em, weight: \"bold\")[{}])",
    "subtitle": "#align(center, text(size: 1.2em)[{}])",
    "date": "#align(center)[{}]",
    "abstract": "*Abstract.* {}",
    "keywords": "*Keywords:* {}",
    "thanks": "*Acknowledgments:* {}",
}


def closing_bracket(text: str, start: int) -> int:
    """Index of the `]` matching the `[` at start, skipping escaped chars."""
    depth, j = 0, start
    while j < len(text):
        c = text[j]
        if c == "\\":
            j += 2
            continue
        if c == "[":
            depth += 1
        elif c == "]":
            depth -= 1
            if depth == 0:
                return j
        j += 1
    return -1


def flatten_metadata(text: str) -> str:
    """Move title, subtitle, date, abstract, keywords and thanks out of the
    `#show: doc => article(...)` call and into the body as plain blocks.

    typst-diff treats the template's title block as one opaque unit: it
    re-sets an edited title word by word as headings and then repeats the
    unmarked new block. As body paragraphs the same fields diff word by
    word like the rest of the paper. Values may span lines; brackets are
    matched, not line-parsed. No-op when the call is not found.
    """
    m = SHOW_HEAD.search(text)
    if not m:
        return text
    blocks: list[str] = []
    i = m.end()
    while True:
        km = re.compile(r"[ \t]*(\w[\w-]*): \[").match(text, i)
        if not km or km.group(1) not in FLATTEN:
            nl = text.find("\n", i)
            if nl == -1 or text[i:nl].strip() == ")":
                break
            i = nl + 1
            continue
        end = closing_bracket(text, km.end() - 1)
        if end == -1:
            return text
        blocks.append(FLATTEN[km.group(1)].format(text[km.end():end]))
        stop = end + 1
        if text[stop:stop + 1] == ",":
            stop += 1
        if text[stop:stop + 1] == "\n":
            stop += 1
        text = text[:km.start()] + text[stop:]
    close = text.find("\n)", m.end())
    if close == -1:
        return text
    insert_at = close + 2
    return text[:insert_at] + "\n" + "\n\n".join(blocks) + "\n" + text[insert_at:]


def inline_footnotes(text: str) -> str:
    """`#footnote[body]` -> a small parenthetical note in the running text.

    The fallback for a typst-diff fault: on some documents with footnotes it
    renders old-side content live, so labels and the bibliography appear
    twice and the render fails ("page configuration is not allowed inside
    of containers", "multiple bibliographies are not yet supported").
    Inline, the note text still diffs word by word.
    """
    out: list[str] = []
    i = 0
    for m in re.finditer(r"#footnote\[", text):
        if m.start() < i:
            continue
        end = closing_bracket(text, m.end() - 1)
        if end == -1:
            return text
        out.append(text[i:m.start()])
        out.append("#text(size: 0.85em)[ (Note: " + text[m.end():end] + ")]")
        i = end + 1
    out.append(text[i:])
    return "".join(out)


def rewrite(path: str, fn) -> None:
    with open(path, encoding="utf-8") as f:
        text = f.read()
    with open(path, "w", encoding="utf-8") as f:
        f.write(fn(text))


def raw_latex_sources(tree: str, entry: str) -> list[str]:
    """Sources beside the entry carrying raw LaTeX blocks (`{=latex}`) alone.

    Quarto's typst writer drops raw LaTeX, so whatever those blocks hold
    (typically tables) is absent from both sides and cannot appear in the
    diff. A source that also carries a `{=typst}` block, as every table
    `ssus_papers.Table` writes does, reaches the diff through that block and
    is not reported.
    """
    hits = []
    for d, dirs, names in os.walk(os.path.join(tree, os.path.dirname(entry))):
        dirs[:] = [x for x in dirs if not x.startswith((".", "_freeze", "_manuscript"))]
        for name in names:
            if name.endswith((".qmd", ".md")):
                path = os.path.join(d, name)
                with open(path, encoding="utf-8", errors="replace") as f:
                    text = f.read()
                if "{=latex}" in text and "{=typst}" not in text:
                    hits.append(os.path.relpath(path, tree))
    return sorted(hits)


def prepare(tree: str, entry: str, execute: bool, profile: str | None,
            line_numbers: bool) -> str:
    """Render one staged side and write its standalone diff input.

    Returns the path of the .typ typst-diff should read.
    """
    typ_dir, text = quarto_typst(tree, entry, execute, profile)
    text = fix_csl_path(cite_fix(text, bib_keys(text, typ_dir)))
    text = flatten_metadata(strip_document_sets(text))
    if line_numbers:
        text = LINE_NUMBERS + text
    path = os.path.join(typ_dir, DIFF_INPUT)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    return path

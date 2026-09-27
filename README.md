# qdiff

Track-changes PDFs from two versions of a Quarto paper — the copy journals
ask for on resubmission, which quarto has no output for.

    qdiff OLD [NEW] [options]

`OLD` and `NEW` are directories (taken as they sit, uncommitted changes
included) or git refs (commit, branch, tag). `NEW` defaults to the current
directory.

## How it works

1. **Stage** both sides in scratch space: a directory is cloned (APFS
   copy-on-write on macOS, so large data trees cost nothing), a ref is
   checked out as a detached worktree. qdiff never renders in, writes to,
   or deletes from the author's tree.
2. **Render** each side to bare typst with the same quarto
   (`quarto render --to typst -M keep-typ:true`), so toolchain churn
   cancels and the diff shows what changed in the paper.
3. **Prepare** each `.typ` as a standalone diff input (see below).
4. **Diff** with [typst-diff](https://github.com/alexanderkoller/typst-diff),
   which evaluates both documents — show rules, includes, citations,
   cross-references — and diffs what typst actually typesets, word by word,
   with figures, tables, lists and equations diffed as units. Insertions are
   green, deletions red and struck.

Staged sides and worktrees are removed when qdiff exits, including on
failure (`--keep` keeps them for inspection).

## Install

    cargo install --locked --git https://github.com/alexanderkoller/typst-diff typst-diff
    pip install -e .            # provides the `qdiff` command

Requires quarto on PATH. typst-diff embeds its own typst (0.14.2 as of
2026-09) and finds system fonts only; a font the paper's template names
must be installed on the system to appear in the diff.

## Usage

    qdiff 10fde27 . --entry paper/index.qmd       # ref vs working tree
    qdiff submitted/ revised/ -o changes.pdf      # dir vs dir
    qdiff v1 v2 --profile blind --open            # two tags, blind profile

| flag | effect |
|---|---|
| `--entry` | entry `.qmd` when it is not `index.qmd` / `paper/index.qmd` |
| `--profile` | quarto profile to render both sides with |
| `-o`, `--output` | output PDF (default `<new>/.qdiff/trackchanges.pdf`) |
| `--execute` | run code cells while rendering (default `--no-execute`) |
| `--compact` | substitutions as blue new text, old text hidden |
| `--no-line-numbers` | omit line numbers |
| `--keep` | keep the staged sides and print where they are |
| `--open` | open the PDF when done |

Next to the PDF: `<name>-changes.txt` (typst-diff's log of every
insertion, deletion and modification) and `old.typ` / `new.typ` (the
prepared diff inputs).

## What qdiff does to each side

- **Citations**: pandoc's typst writer emits `@key`; typst label syntax
  extends over `-`, so `[@key]---because` glues into an unknown label.
  Bib-key refs become `#cite(<key>)`; `@key[supp]` becomes the named
  `supplement:` form typst 0.13+ requires. Cross-references pass through.
- **CSL path**: quarto escapes a leading `.` in the `csl:` path, which
  typst rejects; the escapes are stripped.
- **Document metadata**: `set document(...)` rules are removed — typst-diff
  lays content out inside a container, where typst rejects them. They carry
  PDF metadata only.
- **Title block**: title, subtitle, date, abstract, keywords and
  acknowledgments move out of the template's `article(...)` call into the
  body. typst-diff treats the title block as one opaque unit and mangles an
  edited title; as body paragraphs they diff word by word.
- **Line numbers**: `#set par.line(numbering: "1")` is prepended.

## Known limits

- **Raw LaTeX is dropped.** Quarto's typst writer drops `{=latex}` blocks,
  so tables written as raw LaTeX are missing from both sides and from the
  diff; qdiff warns and names the files. Write tables in a form that renders
  to typst too (pipe or grid tables, or a generator that emits both).
- **Footnotes can break typst-diff.** On some documents with footnotes it
  renders old-side content live and fails ("page configuration is not
  allowed inside of containers"). qdiff then retries with footnotes set
  inline as small parenthetical notes, and says so.
- **Deleted figures show their caption** struck, numbered from the new
  side's counters ("Figure 0"), since old-side counters do not run.
- **The bibliography** is shown as a whole old block and a whole new block.

## Tests

    pip install -e ".[test]"
    pytest

The end-to-end tests run real quarto and typst-diff (skipped when either
is missing) and cover the defects of the earlier typdiff-based qdiff: the
author's tree untouched, sized and deleted figures, math edits, deleted
citations and em-dash glue, and worktree cleanup for git-ref sides.

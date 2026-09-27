# qdiff

Track-changes PDFs from two versions of a Quarto paper.

    qdiff [OLD] [NEW] [options]

`OLD` and `NEW` are either directories on disk (used as they sit, uncommitted
changes and all) or git refs (commit, branch, tag), materialized as detached
git worktrees so the old side is the committed state and nothing in the
source repo is disturbed. `NEW` defaults to the working directory.

## Pipeline

1. materialize both sides (dirs pass through; git refs become worktrees)
2. render each side's entry document to bare typst with the same quarto —
   rendered in place with `-M keep-typ:true`, because quarto 1.10 deletes
   the `.typ` under `--output-dir` with `keep-typ` set
3. normalize the two intermediates and diff them with
   [typdiff](https://github.com/sou1118/typdiff) (block diff, word-level
   inside modified blocks) or `--engine=text` (difflib fallback)
4. compile the diff to PDF with `typst`

Because both sides are rendered by the same quarto in the same run,
toolchain churn cancels: the diff shows what changed in the paper, not in
the template.

## Requirements

- quarto and a `typst` on PATH for the final compile
- typdiff on PATH or at `~/.cargo/bin/typdiff` (optional but much better
  than the `--engine=text` fallback; see typdiff patches below)

## Usage

    qdiff main paper/                      # dir vs dir
    qdiff 10fde27 . --entry paper/index.qmd
    qdiff old-dir new-dir --outdir out --open

Options:

| flag | effect |
|---|---|
| `--entry` | entry `.qmd` when it is not `index.qmd`/`paper/index.qmd` |
| `--outdir` | where the diff and intermediates land (default `<new>/.qdiff`) |
| `--engine` | `typdiff` (default) or `text` (difflib fallback) |
| `--execute` | allow code execution during render (default `--no-execute`) |
| `--keep` | keep materialized git-ref trees for inspection |
| `--open` | open the PDF when done |
| `--no-line-numbers` | omit `#set par.line(numbering: "1")` |
| `--no-placeholders` | keep figures live instead of `[FIGURE: path]` markers |

Outputs in the outdir: `index-trackchanges.pdf` (the deliverable),
`index-trackchanges.typ` (the diff source), `intermediate-old.typ` /
`intermediate-new.typ` (the normalized bare-typst intermediates), plus the
bibliography, CSL and — with `--no-placeholders` — figure files the diff
compile resolves next to it.

## Normalizations

Between the quarto render and the diff, qdiff rewrites each intermediate so
the diff is by content, not by accident of the toolchain:

- absolute tree paths → `__QDIFF_OLD__` / `__QDIFF_NEW__` tokens
- `#show: doc => article(...)` metadata (title, subtitle, abstract, date,
  keywords, thanks) → flattened into diffable body blocks; typdiff treats
  the whole show rule as one atomic call and would drop those edits
- citations: pandoc's typst writer emits `@key` refs, where typst's label
  syntax extends over `-`, so `[@key]---because` glues into an unknown
  label and fails the compile. qdiff rewrites bib-key refs to
  `#cite(<key>)`, which closes the token before trailing text
  (cross-references like `@eq-did` are not bib keys and pass through)
- citation supplements: `@key[supp]` → `#cite(<key>, supplement: [supp])`,
  the named-argument form typst 0.13+ accepts
- the bibliography and CSL files referenced by the diff are copied next to
  it so the final compile resolves them — and `.bib` files present on both
  sides are **merged into the union of their entries** (a revision usually
  drops keys the original cited; deleted text still needs them)
- quarto escapes a leading `.` in the CSL path, so a side configured with
  `csl: .pandoc/csl/x.csl` reaches the `.typ` as `"\.pandoc/csl/x.csl"`,
  which typst 0.15 rejects; qdiff strips the escapes
- after the diff, `#ref(<label>)` calls whose label the diff does not
  define (typdiff comments out deleted code-mode blocks, taking their
  labels with them) are rewritten as literal struck text — supplement word
  plus label name — instead of failing the compile

## Converting a raw-pandoc side

A side that predates quarto (pandoc-include `!include` master, raw-LaTeX
tables) renders wrong or not at all through quarto. Convert it first, in
its worktree:

1. build an entry: the master's YAML frontmatter as-is, then one
   `{{< include section.md >}}` per `!include` line in the master's order,
   then `# References` and the appendix include (mirroring the quarto
   side's shape: sections, References heading, appendix, auto-bibliography)
2. rewrite `!include x.md` → `{{< include x.md >}}` everywhere (sections
   carry nested includes, e.g. tables)
3. rewrite pandoc-crossref colon syntax to quarto dash syntax —
   `{#eq:x}` → `{#eq-x}`, `@eq:x` → `@eq-x` (eq/fig/tbl/sec), labels and
   refs alike
4. convert raw-LaTeX tables: `pandoc -f latex -t markdown --wrap=none` per
   `tables/*.md` containing `\begin{`, then unwrap the `::: {#id}` div
   pandoc wraps them in, close up the blank line before the `: caption`
   line, and label the caption `: caption {#tbl-<label>}` — **quarto's
   crossref filter drops unlabeled table captions entirely**, and the
   label must be the `\label{...}` the LaTeX carried
5. prune `{{< include ... >}}` lines whose target file is not committed
   (main's appendix referenced tables that were never committed)

No `_quarto.yml` is needed for the old side: the master's YAML frontmatter
carries bibliography/csl/numbersections, and qdiff renders the entry file
directly.

## typdiff patches

qdiff's typdiff engine assumes a typdiff with two fixes beyond v0.1.3
(both worth reporting upstream; rebuild from a patched tree with
`cargo install --path <typdiff-src> --force`):

1. **math is atomic in word-level diffs** — `$...$` (no blank line inside)
   is one token, so the diff can no longer split an equation across diff
   markers and emit unbalanced delimiters; the token may cross lines for
   display math but never a blank line
2. **`#cite(<label>)` in deleted content stays bare** — the escaping rule
   that neutralizes labels in deleted content (`<` → `\<`) exempted only
   `#ref(...)`; `\<` is invalid in typst code, so a deleted `#cite(...)`
   failed the compile. A `#ref`/`#cite` call before the label is exempted

## Known behavior

- citation formatting is not itself diffed (citations render fresh against
  the new side's bibliography; a changed key or a changed entry shows up as
  changed rendered text only when the surrounding sentence changed)
- tables and figures diff coarsely: an edited table is a deleted table
  followed by an added table
- references render fresh from the new side's `.bib` (plus anything only on
  the old side, whose keys are still needed by deleted text)
- a crashed run can leave a worktree behind; clean up with
  `git worktree prune` (or list them first with `git worktree list`)
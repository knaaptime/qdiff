# typdiff upstream draft 2 — `<` escaped in deleted `#cite(<label>)`

Issue to file against [sou1118/typdiff](https://github.com/sou1118/typdiff)
(v0.1.3). Verified against stock v0.1.3 (clean clone, `cargo build --release`)
and against our patched build. Copy into a GitHub issue when filing; if filed,
update `pkm/wiki/software/qdiff/qdiff.md` and its AGENTS.md.

**Title:** Deleted content escapes `<` in `#cite(<label>)` label arguments —
`\<` is not valid typst code

**Environment:** typdiff 0.1.3, macOS (Apple Silicon), typst 0.15.1.

## Repro

Three files, contents below.

`old.typ`:

```typst
= Background

Prior work #cite(<quast2017>) documented the pattern.

#bibliography("refs.bib", title: none)
```

`new.typ`:

```typst
= Background

Prior work documented the pattern.

#bibliography("refs.bib", title: none)
```

`refs.bib`:

```bibtex
@article{quast2017,
  title = {A study},
  author = {One Author},
  year = {2017},
}
```

Command: `typdiff old.typ new.typ -o diff.typ`

## Observed (stock v0.1.3)

The diff line of the output:

`#diff-deleted[#cite(\<quast2017>) ]documented the pattern.`

typst 0.15.1 refuses the output:

```text
error: the character `\` is not valid in code
```

The escaping pass for deleted content escapes `<` (so deleted labels don't
survive as live refs), but the exemption for label arguments covers only
`#ref(...)` — not `#cite(...)`. A deleted citation therefore emits `\<`,
which is invalid in typst code mode.

## Expected

The label argument of `#cite(<...>)` is preserved bare in deleted content, the
same exemption `#ref(...)` already gets:

`#diff-deleted[#cite(<quast2017>) ]documented the pattern.`

The diff then compiles (the bibliography call is still in the document).

## Fix sketch

In `render.rs`, `is_ref_label_arg` generalizes: after finding a `<label>`,
inspect the text before it; if the prefix trimmed and stripped of `(` ends
with `#ref` **or** `#cite`, treat it as a label argument and skip the escaping.
One-line change; our local build with it produces the expected output above
and compiles clean under typst 0.15.1.
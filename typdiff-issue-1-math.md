# typdiff upstream draft 1 — word-level diff splits `$...$` math spans

Issue to file against [sou1118/typdiff](https://github.com/sou1118/typdiff)
(v0.1.3). Verified against stock v0.1.3 (clean clone, `cargo build --release`)
and against our patched build. Copy into a GitHub issue when filing; if filed,
update `pkm/wiki/software/qdiff/qdiff.md` and its AGENTS.md.

**Title:** Word-level diff does not treat `$...$` as atomic — diff markers land
inside math spans

**Environment:** typdiff 0.1.3, macOS (Apple Silicon), typst 0.15.1.

## Repro

`old.typ`:

```typst
= Results

The estimate is $hat(rho)_"DID"$ in column 3.
```

`new.typ`:

```typst
= Results

The estimate is $hat(rho)_"DID"^(1)$ in column 3.
```

Command: `typdiff old.typ new.typ -o diff.typ`

## Observed (stock v0.1.3)

The diff line of the output:

```typst
The estimate is $hat(rho)_"DID"#diff-added[^(1)]$ in column 3.
```

The diff marker is emitted **inside** the math span. In typst math, the body of
`#diff-added[...]` renders as markup, not math, so `^(1)` renders as literal
text rather than a superscript. Depending on how token alignment falls, the
split can also leave unbalanced `$` delimiters (a side's closing `$` inside a
marker body), which fails the compile outright — the outcome we hit on a real
~200-page quarto→typst diff.

## Expected

Diff markers wrap whole math spans and never land between the delimiters:

```typst
The estimate is #diff-deleted[$hat(rho)_"DID"$]#diff-added[$hat(rho)_"DID"^(1)$] in column 3.
```

## Fix sketch

In the mixed tokenizer, branch on `$` and scan for the closing unescaped `$`,
skipping escaped `\$` pairs; accept the token as atomic only if no blank line
occurs inside (inline math never crosses a blank line, and display math emitted
by pandoc crosses lines but not with a blank line inside). Two tokenizer gaps
from the same root cause on real input: identifiers in `#call.chains(...)`
stop at `.`, so `#math.equation(...)` is not one token; and the closing scan
must skip `\$` so escaped dollars don't pair as delimiters.

With this change, both spans are wrapped whole by the existing block diff; our
local build produces the expected output above and compiles clean under
typst 0.15.1.
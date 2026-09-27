#!/usr/bin/env python3
"""Convert a raw-pandoc paper tree (pandoc-include + pandoc-crossref) into a
quarto-renderable entry, in place, so it can serve as a qdiff side.

Handles the three things quarto cannot read from a raw-pandoc tree:
  1. `!include `file.md`` directives (pandoc-include) -> `{{< include file >}}`
   sources of truth: `draft.md` body order defines the section order.
  2. pandoc-crossref colon syntax `{#eq:x}` / `@eq:x` -> quarto dash syntax
     `{#eq-x}` / `@eq-x`  (eq, fig, tbl, sec)
  3. raw-LaTeX tables (pandoc drops raw latex under the typst writer, so the
     tables would vanish from the old side of the diff): each *.md under
     <paper>/tables/ that contains `\\begin{` is rewritten in place via
     `pandoc -f latex -t markdown`, and the bare id pandoc gives the table
     (`{#rent_desc}`) is prefixed to `{#tbl-rent_desc}`.
"""
import os
import re
 import shutil
import subprocess
import sys

# pandoc-include -> quarto include rewrite
INCLUDE_RE = re.compile(r"^!include\s+`?([^`\s]+)`?\s*$", re.M)
# pandoc crossref colon syntax -> quarto dash syntax (labels and refs)
XREF_RE = re.compile(r"(@|\{#)(eq|fig|tbl|sec):([A-Za-z0-9_.-]+)")
# bare table label emitted by pandoc's latex reader: {#rent_desc}
BARE_ID_RE = re.compile(r"\{#([A-Za-z0-9_.-]+)\}")

XREF_KINDS = ("eq", "fig", "tbl", "sec")


def sh(cmd, cwd=None):
    proc = convert helper
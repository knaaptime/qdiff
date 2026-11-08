"""End-to-end tests: real quarto, real typst-diff.

Each case is a regression test for a defect found in the typdiff-based
qdiff (audit, 2026-09-26) or for one of the two typdiff patches that engine
needed. Skipped when quarto or typst-diff is absent.
"""

from __future__ import annotations

import os
import shutil
import struct
import subprocess
import zlib

import pytest

from qdiff.cli import main, typst_diff_binary
from qdiff.render import flatten_metadata, inline_footnotes, raw_latex_sources, strip_document_sets
from qdiff.sides import QdiffError

TOY = os.path.join(os.path.dirname(__file__), "toy")

try:
    typst_diff_binary()
    HAVE_ENGINE = shutil.which("quarto") is not None
except QdiffError:
    HAVE_ENGINE = False

e2e = pytest.mark.skipif(not HAVE_ENGINE, reason="needs quarto and typst-diff")

HEADER = """---
title: "Probe"
bibliography: refs.bib
---

"""

BIB = """@article{knaap2026,
  title = {Space Is Not a Control Group},
  author = {Knaap, Elijah},
  journal = {Journal of Regional Science},
  year = {2026},
}
@article{tobler1970,
  title = {A Computer Movie Simulating Urban Growth},
  author = {Tobler, W. R.},
  journal = {Economic Geography},
  year = {1970},
}
"""


def png(path: str) -> None:
    """A 1x1 PNG, enough for typst to lay out an image."""
    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data)))
    with open(path, "wb") as f:
        f.write(b"\x89PNG\r\n\x1a\n"
                + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
                + chunk(b"IDAT", zlib.compress(b"\x00\xff\x00\x00"))
                + chunk(b"IEND", b""))


def paper(root, name: str, body: str) -> str:
    d = root / name
    d.mkdir()
    (d / "index.qmd").write_text(HEADER + body)
    (d / "refs.bib").write_text(BIB)
    png(str(d / "fig.png"))
    return str(d)


def qdiff(old: str, new: str, out, *extra: str) -> str:
    """Run qdiff; return the change log text."""
    pdf = str(out / "diff.pdf")
    main([old, new, "-o", pdf, *extra])
    assert os.path.getsize(pdf) > 0
    with open(str(out / "diff-changes.txt"), encoding="utf-8") as f:
        return f.read()


def edits(log: str) -> int:
    return sum(1 for line in log.splitlines() if line.startswith("## "))


# --- unit -----------------------------------------------------------------

def test_strip_document_sets_handles_multiline_and_conditional():
    src = ("  set document(title: title)\n"
           "  set document(\n    author: authors.map(a => a.name),\n  ) if authors != none\n"
           "  set par(justify: true)\n")
    out = strip_document_sets(src)
    assert "document" not in out
    assert "if authors" not in out
    assert "set par(justify: true)" in out


def test_flatten_metadata_moves_multiline_abstract():
    src = ("#show: doc => article(\n"
           "  title: [A [nested] title],\n"
           "  abstract: [First paragraph.\n\nSecond paragraph.],\n"
           "  toc_depth: 3,\n"
           "  doc,\n)\n\nBody.\n")
    out = flatten_metadata(src)
    head = out[:out.index("\n)")]
    assert "title:" not in head and "abstract:" not in head
    assert "toc_depth: 3" in head
    assert "A [nested] title" in out
    assert "Second paragraph." in out.split("\n)", 1)[1]


def test_inline_footnotes_keeps_nested_content():
    src = "Claim#footnote[See #emph[this] and #ref(<fig-a>, supplement: [Figure]).] ends."
    out = inline_footnotes(src)
    assert "#footnote" not in out
    assert "(Note: See #emph[this] and #ref(<fig-a>, supplement: [Figure]).)" in out
    assert out.endswith(" ends.")


def test_raw_latex_sources_scans_beside_the_entry(tmp_path):
    (tmp_path / "paper").mkdir()
    (tmp_path / "paper" / "index.qmd").write_text("Body.\n")
    (tmp_path / "paper" / "_tbl.md").write_text("```{=latex}\n\\begin{tabular}\n```\n")
    (tmp_path / "old").mkdir()
    (tmp_path / "old" / "x.md").write_text("```{=latex}\n```\n")
    # a table with a typst block beside the LaTeX reaches the diff
    (tmp_path / "paper" / "_tbl_both.md").write_text("```{=latex}\n```\n```{=typst}\n```\n")
    assert raw_latex_sources(str(tmp_path), "paper/index.qmd") == ["paper/_tbl.md"]


# --- end to end -------------------------------------------------------------

@e2e
def test_toy_pair_and_negative_control(tmp_path):
    assert edits(qdiff(os.path.join(TOY, "old"), os.path.join(TOY, "new"), tmp_path)) > 0
    neg = tmp_path / "neg"
    neg.mkdir()
    assert edits(qdiff(os.path.join(TOY, "new"), os.path.join(TOY, "new"), neg)) == 0


@e2e
def test_author_tree_is_never_written(tmp_path):
    """Audit: in-place rendering overwrote and then deleted index.pdf."""
    old = paper(tmp_path, "old", "Some text.\n")
    new = paper(tmp_path, "new", "Some revised text.\n")
    marker = os.path.join(new, "index.pdf")
    with open(marker, "w") as f:
        f.write("the author's rendered paper")
    before = sorted(os.listdir(new))
    out = tmp_path / "out"
    out.mkdir()
    qdiff(old, new, out)
    assert open(marker).read() == "the author's rendered paper"
    assert sorted(os.listdir(new)) == before


@e2e
def test_sized_figure_on_both_sides(tmp_path):
    """Audit: `image("fig.png", width: 50%)` escaped the placeholder regex."""
    fig = "![A map.](fig.png){#fig-map width=50%}\n\n"
    old = paper(tmp_path, "old", fig + "As @fig-map shows, it held.\n")
    new = paper(tmp_path, "new", fig + "As @fig-map shows, it fails.\n")
    assert edits(qdiff(old, new, tmp_path)) >= 1


@e2e
def test_deleted_figure_and_its_reference(tmp_path):
    """Audit: typdiff commented deleted figures out; refs to them dangled."""
    old = paper(tmp_path, "old",
                "![A map.](fig.png){#fig-map width=50%}\n\nAs @fig-map shows, it held.\n")
    new = paper(tmp_path, "new", "The figure was removed.\n")
    assert edits(qdiff(old, new, tmp_path)) >= 1


@e2e
def test_math_edit(tmp_path):
    """typdiff patch 1: diff markers landed inside `$...$`."""
    old = paper(tmp_path, "old", "The estimate is $\\hat{\\rho}_{DID}$ in column 3.\n")
    new = paper(tmp_path, "new", "The estimate is $\\hat{\\rho}_{DID}^{(1)}$ in column 3.\n")
    assert edits(qdiff(old, new, tmp_path)) >= 1


@e2e
def test_deleted_citation_and_emdash_glue(tmp_path):
    """typdiff patch 2 (deleted #cite) and the `@key---` label glue."""
    old = paper(tmp_path, "old",
                "Prior work [@tobler1970]---because of this---held [@knaap2026].\n")
    new = paper(tmp_path, "new", "Prior work held [@knaap2026].\n")
    assert edits(qdiff(old, new, tmp_path)) >= 1


@e2e
def test_git_ref_sides_leave_no_worktrees(tmp_path):
    """Audit: a crash or a two-ref run could leak worktrees."""
    repo = tmp_path / "repo"
    shutil.copytree(os.path.join(TOY, "old"), repo, ignore=shutil.ignore_patterns(".quarto"))
    git = ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t"]
    subprocess.run(git[:3] + ["init", "-q"], check=True)
    subprocess.run(git + ["add", "."], check=True)
    subprocess.run(git + ["commit", "-qm", "one"], check=True)
    shutil.copy(os.path.join(TOY, "new", "index.qmd"), repo / "index.qmd")
    subprocess.run(git + ["commit", "-qam", "two"], check=True)
    out = tmp_path / "out"
    out.mkdir()
    cwd = os.getcwd()
    os.chdir(repo)
    try:
        assert edits(qdiff("HEAD~1", "HEAD", out)) > 0
    finally:
        os.chdir(cwd)
    listing = subprocess.run(git[:3] + ["worktree", "list"], capture_output=True, text=True)
    assert len(listing.stdout.strip().splitlines()) == 1

"""REPORTS33 (TODO-0206): awf_brief «What's new» clips on clean boundaries.

The What's new section used to clip the latest CHANGELOG section with a
word counter mid-line: «Two fix waves born from a full day of running
awf on a real JS …» (report
awf-bug-20261012-awf-brief-what-s-new-obrezaet-changelog-posredi.md).
The cut must land on a paragraph (blank-line) or a sentence boundary,
with the "…" only after a complete paragraph or sentence. The word
budget is unchanged — only the cut point moved.

Invariants (TODO-0206):
1. a section within the budget is returned whole — no "…";
2. a section over the budget is cut on a blank-line boundary — whole
   paragraphs kept, the marker follows a complete paragraph, never a
   mid-letter word clip;
3. when even the first paragraph alone exceeds the budget — the cut is
   at a sentence boundary (". "! " " ?" + space) within the remaining
   words, and without a fitting sentence it falls back to the
   paragraph boundary.
"""
from __future__ import annotations

from awf import brief as _brief

MAX_WORDS = _brief._CHANGELOG_MAX_WORDS


def _words(text: str) -> int:
    return len(text.split())


def _last_char_before_marker(text: str) -> str:
    """The last content character of a clipped section (before " …")."""
    assert text.endswith(" …"), f"expected the trailing marker, got: {text!r}"
    body = text[: -len(" …")]
    assert body, "marker with an empty body"
    return body[-1]


class TestRealChangelog:
    def test_real_cut_not_mid_letter(self):
        """(a) the real repo CHANGELOG: the heading is kept, the cut —
        when present — is not a mid-letter word clip, and the word
        budget (19) holds."""
        section = _brief.latest_changelog()
        if not section:
            return  # installed wheel: no CHANGELOG.md, the card degrades
        assert section.startswith("## [")
        words = _words(section)
        if section.endswith(" …"):
            words -= 1  # the marker is not content
            ch = _last_char_before_marker(section)
            assert not ch.isalpha(), f"cut mid-letter at {ch!r}: {section!r}"
        assert words <= MAX_WORDS


class TestShortSection:
    def test_short_section_whole_no_marker(self):
        """(b) a section within the budget is returned whole, no "…"."""
        section = "## [1.0.0] — 2026-01-01\n\nFixed a thing."
        assert _words(section) <= MAX_WORDS
        out = _brief._clip_changelog_section(section, MAX_WORDS)
        assert out == section


class TestParagraphBoundary:
    P1 = "Alpha beta gamma delta epsilon zeta eta theta iota kappa."
    P2 = "lambda mu nu xi omicron pi rho sigma tau upsilon phi."

    def test_cut_on_blank_line_keeps_whole_paragraphs(self):
        """(a, mechanism) whole paragraphs are kept up to the budget; the
        cut lands on the blank line, the marker follows a complete
        paragraph."""
        # heading 4 words; P1 (10) fits, P1+P2 (20) does not.
        section = f"## [3.0.0] — 2026-03-03\n\n{self.P1}\n\n{self.P2}"
        assert _words(section) > MAX_WORDS
        out = _brief._clip_changelog_section(section, MAX_WORDS)
        assert out.endswith(" …")
        assert self.P1 in out
        assert self.P2 not in out
        assert _last_char_before_marker(out) in ".!?"
        assert _words(out) - 1 <= MAX_WORDS


class TestFirstParagraphOverBudget:
    S1 = "Alpha beta gamma delta epsilon zeta eta theta iota."
    S2 = (
        "kappa lambda mu nu xi omicron pi rho sigma tau upsilon "
        "phi chi psi omega alpha beta gamma delta epsilon."
    )

    def test_cut_at_sentence_boundary(self):
        """(c) the first paragraph alone exceeds the budget: the cut is
        on a sentence boundary — the marker follows a complete
        sentence, the rest is dropped."""
        # heading 4 words; S1 (10) fits, S1+S2 (30) does not.
        section = f"## [2.0.0] — 2026-02-02\n\n{self.S1} {self.S2}"
        assert _words(section) > MAX_WORDS
        out = _brief._clip_changelog_section(section, MAX_WORDS)
        assert out.endswith(" …")
        assert self.S1 in out
        assert self.S2 not in out
        assert _last_char_before_marker(out) in ".!?"
        assert _words(out) - 1 <= MAX_WORDS

    def test_no_fitting_sentence_falls_back_to_paragraph_boundary(self):
        """(c, degenerate) the first sentence alone exceeds the
        remaining budget — no sentence boundary fits: the cut falls back
        to the paragraph boundary (the marker after a complete
        paragraph, not mid-letter)."""
        long = " ".join(f"w{i}" for i in range(20)) + "."
        section = f"## [4.0.0] — 2026-04-04\n\n{long}. {long}"
        assert _words(section) > MAX_WORDS
        out = _brief._clip_changelog_section(section, MAX_WORDS)
        assert out.endswith(" …")
        assert long not in out
        assert not _last_char_before_marker(out).isalpha()
        assert _words(out) - 1 <= MAX_WORDS

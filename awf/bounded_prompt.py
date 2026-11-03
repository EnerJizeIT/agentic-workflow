"""A42: the one place where a prompt that is over budget gets cut.

Two builders hand a role text that can outgrow its budget: the replan prompt
(``rejection.build_replan_context``) and the verify prompt
(``verify_context.build_verify_prompt``). Both obey the same rules, so the
rules live here once instead of being re-typed next to each caller:

- sections are named pairs ``(label, text)`` and the caller keeps the
  formatting: ``render(items)`` produces the whole prompt, head and tail
  included, so the fixed instructions are part of every measurement;
- ``required`` sections always go in — they are the reason the prompt
  exists (a verdict, a reviewer report);
- each ``optional`` section enters only when the WHOLE render still fits,
  otherwise it is dropped by name;
- overflow is then taken off the sections named in ``truncate_order``, BY
  LABEL — never off the end of the prompt, because the tail carries the
  closing instruction (``replan_for:`` / the signal to write) and cutting
  it produces an answer no waiter recognizes;
- every cut is named in the returned list, and the caller logs that list;
- the last resort — the fixed text alone (instructions and section frames)
  is over the budget — truncates the tail and names itself differently, so
  a section cut is never mistaken for it.
"""
from __future__ import annotations

from collections.abc import Callable, Sequence

Section = tuple[str, str]
Renderer = Callable[[list[Section]], str]


def fit_prompt(
    render: Renderer,
    *,
    required: Sequence[Section],
    optional: Sequence[Section],
    budget: int,
    truncate_order: Sequence[str],
) -> tuple[str, list[str]]:
    """Render ``required`` + fitting ``optional`` sections inside ``budget``.

    Returns ``(prompt, dropped)``. Empty texts are skipped: a missing source
    is an absent section, never an empty heading.
    """
    dropped: list[str] = []
    kept: list[Section] = [(label, text) for label, text in required if text]

    for label, text in optional:
        if not text:
            continue
        if len(render([*kept, (label, text)])) <= budget:
            kept.append((label, text))
        else:
            dropped.append(label)

    prompt = render(kept)
    if len(prompt) > budget:
        overflow = len(prompt) - budget
        for wanted in truncate_order:
            if overflow <= 0:
                break
            idx = next(
                (i for i, (label, _text) in enumerate(kept) if label == wanted),
                None,
            )
            if idx is None:
                continue
            label, text = kept[idx]
            keep = max(0, len(text) - overflow)
            if keep < len(text):
                dropped.append(f"{label} truncated")
                overflow -= len(text) - keep
            kept[idx] = (label, text[:keep])
        prompt = render(kept)

    if len(prompt) > budget:
        # The fixed text (instructions and section frames) alone is over the
        # budget: a limit too small to carry this prompt at all. The tail
        # gives way and the fact is named differently from a section cut.
        dropped.append("fixed text truncated")
        prompt = prompt[:budget]
    return prompt, dropped


__all__ = ["fit_prompt"]

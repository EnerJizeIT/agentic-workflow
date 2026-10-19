"""Byte-faithful codec for the transfer LINK files (``BASELINE-*.include`` /
``REJECT-*.files``).

R2.1 keeps paths as git BYTES (``surrogateescape``); a legal git name may
contain a newline (``0x0a``), another ``splitlines`` boundary
(``0x0b/0x0c/0x1c-0x1e``) or an edge space. The links used to join paths with
``"\\n"`` and read them back with ``splitlines()`` + ``strip()`` — every one of
those bytes silently rewrote the path, the adopted exclusion was lost and the
commit gate dropped the file (the silent-loss class TODO-0221 exists to close).

The format is the same NUL machine protocol the R2.1 model speaks: a NUL byte
can never be part of a path, so each entry is terminated with a NUL and the
reader splits on NUL only — no ``strip``. Files written before this fix (the
newline-joined legacy form) carry no NUL and are read with the old rule; writes
are ALWAYS the new form. Empty content is the documented empty list; a NUL that
is not a terminator (a binary blob) degrades to ``None`` — the NEG-3 contract.
"""
from __future__ import annotations

#: The entry terminator. A NUL can never occur inside a git path.
NUL = "\x00"


def encode_path_link(paths: list[str]) -> str:
    """RAW paths → the link body (sorted, each entry NUL-terminated)."""
    return "".join(p + NUL for p in sorted(paths))


def decode_path_link(data: bytes) -> list[str] | None:
    """Link bytes → sorted RAW paths, or ``None`` when not a readable link.

    * empty → ``[]`` (the documented "nothing" form);
    * ends with NUL → the NUL form: split on NUL, drop the trailing empty
      entry, no strip — the raw identity survives a newline / control byte /
      edge space in a name;
    * contains a NUL that is NOT a terminator, or a binary blob → ``None``
      (NEG-3: the file is not a path list);
    * no NUL → the legacy newline-joined form: ``splitlines`` + ``strip``,
      the pre-fix reader, kept for projects written by an older awf.
    """
    if not data:
        return []
    if data.endswith(b"\x00"):
        entries = data.split(b"\x00")[:-1]
        if any(not e for e in entries):
            return None
        # F-A (review-fix 10): decode EXACTLY as the writer encodes
        # (``atomic_write_text``: utf-8 + surrogateescape) and as every other
        # consumer of the same path identity (``display_path``). ``os.fsdecode``
        # would use the host's filesystem encoding, so on a non-UTF-8 host the
        # decoded strings would not match the adopted set -> silent loss.
        return sorted(e.decode("utf-8", "surrogateescape") for e in entries)
    if b"\x00" in data:
        return None
    # The legacy form was written with a STRICT utf-8 writer, so a no-NUL blob
    # with an invalid byte is not a path list — degrade to None (NEG-3), never
    # a surrogate "path". ``surrogateescape`` here would never raise and would
    # return bogus paths, the very regression this branch guards against.
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return None
    return sorted(ln.strip() for ln in text.splitlines() if ln.strip())


__all__ = ["NUL", "decode_path_link", "encode_path_link"]

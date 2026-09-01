"""Normalising screenplay text so a quote can be matched against a line.

Two places in this codebase compare a quote to a line of pages, for the same
reason — a citation is only worth printing if the file backs it:

    services/extractor/extract.py   checks the model's citations at extraction
    services/gate/evidence.py       re-finds a citation in the current pages

They apply different POLICIES, deliberately. The Extractor searches inside one
scene and will accept a match in either direction, because it is correcting a
model's arithmetic. The Gate searches the whole file and demands a unique match,
because it is carrying a verified quote across a revision and has no scene
boundary to lean on.

What they must share is the normalisation, so "  She sold it.  " and
'"She sold it."' are the same line to both. That is all this module is.
"""

from __future__ import annotations

import re

_WHITESPACE = re.compile(r"\s+")
_EDGE_NOISE = re.compile(r"^[\"'“”\s.…]+|[\"'“”\s.…]+$")


def normalise(text: str) -> str:
    """Collapse whitespace, strip surrounding quotes and dots, casefold."""
    return _EDGE_NOISE.sub("", _WHITESPACE.sub(" ", text or "")).casefold()

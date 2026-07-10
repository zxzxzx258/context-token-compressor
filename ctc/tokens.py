from __future__ import annotations

import logging
from functools import lru_cache

LOGGER = logging.getLogger(__name__)


@lru_cache(maxsize=32)
def _encoding_for_model(model: str):
    import tiktoken

    try:
        return tiktoken.encoding_for_model(model)
    except Exception:
        return tiktoken.get_encoding("cl100k_base")


def estimate_tokens(text: str, model: str | None = None) -> int:
    if not text:
        return 0
    if model:
        try:
            return len(_encoding_for_model(model).encode(text))
        except Exception as exc:
            LOGGER.debug("token estimation fell back to character heuristic: %s", type(exc).__name__)
    return max(1, round(len(text) / 4))

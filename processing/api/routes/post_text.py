"""Shared helpers for owner-editable post text (title/description/caption).

Ownership rule (v1): first detector only.
- verified_potholes: the user_id of the row's original ride (rides_metadata.user_id).
- community_photos: the row's user_id.
The pipeline worker only regenerates `caption`, so user-owned title/description
columns are never clobbered by merges (merges are in-place row updates).
"""
from __future__ import annotations

import logging
from typing import Any

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

TITLE_MAX_LENGTH = 80
DESCRIPTION_MAX_LENGTH = 280
CAPTION_MAX_LENGTH = 280


class PostTextUpdate(BaseModel):
    caption: str | None = Field(default=None, max_length=CAPTION_MAX_LENGTH)
    title: str | None = Field(default=None, max_length=TITLE_MAX_LENGTH)
    description: str | None = Field(default=None, max_length=DESCRIPTION_MAX_LENGTH)


def normalize_text_update(body: PostTextUpdate) -> dict[str, str | None]:
    """Keep only explicitly-provided fields; trim; blank string clears to None."""
    data = body.model_dump(exclude_unset=True)
    updates: dict[str, str | None] = {}
    for field in ("caption", "title", "description"):
        if field not in data or data[field] is None:
            continue
        text = data[field].strip()
        updates[field] = text or None
    return updates


def log_text_edit(
    svc: Any,
    *,
    post_type: str,
    post_id: int,
    user_id: str | None,
    old: dict[str, Any],
    new_updates: dict[str, str | None],
) -> None:
    """Best-effort append of previous values to title_history. Never raises."""
    try:
        changed = {k: v for k, v in new_updates.items() if v != old.get(k)}
        if not changed:
            return
        entry = {
            "post_type": post_type,
            "post_id": post_id,
            "user_id": user_id,
            "old_title": old.get("title"),
            "old_description": old.get("description"),
            "new_title": new_updates.get("title", old.get("title")),
            "new_description": new_updates.get("description", old.get("description")),
            "reason": "edit",
        }
        svc.insert("title_history", entry)
    except Exception as exc:
        logger.warning("title_history insert failed (non-critical): %s", exc)

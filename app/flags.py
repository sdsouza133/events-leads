"""Switches an operator flips in the console (issue #9), kept in the database so they take effect at once, without
a deploy. A switch nobody has flipped yet is off."""

import uuid
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.models import Flag

NEW_RUNS_PAUSED = "new_runs_paused"
"""On: the start form takes no new requests. Runs already asked for carry on, and so does spending, which only
PAUSE_NEW_RUNS stops."""


def is_on(session: Session, name: str) -> bool:
    return bool(session.scalar(select(Flag.enabled).where(Flag.name == name)))


def turn(session: Session, name: str, *, on: bool, by: uuid.UUID, now: datetime) -> None:
    values = {"enabled": on, "changed_by": by, "changed_at": now}
    session.execute(
        insert(Flag).values(name=name, **values).on_conflict_do_update(index_elements=["name"], set_=values)
    )

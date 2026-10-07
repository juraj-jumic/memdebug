from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from memdebug.models import MemoryEvent, Op, Source

T = datetime(2026, 10, 4, tzinfo=timezone.utc)


def make(**kw):
    base = dict(backend="b", memory_id="m", op=Op.ADD, ts=T)
    base.update(kw)
    return MemoryEvent(**base)


def test_naive_timestamps_are_rejected():
    with pytest.raises(ValidationError):
        make(ts=datetime(2026, 10, 4))


def test_unknown_fields_are_rejected():
    with pytest.raises(ValidationError):
        MemoryEvent.model_validate({"backend": "b", "memory_id": "m", "op": "ADD", "ts": T.isoformat(), "x": 1})


def test_sizes_are_bounded():
    with pytest.raises(ValidationError):
        make(memory_id="m" * 300)
    with pytest.raises(ValidationError):
        make(memory_id="")
    with pytest.raises(ValidationError):
        make(scope={str(i): "v" for i in range(20)})
    with pytest.raises(ValidationError):
        make(after="x" * 200_000)
    with pytest.raises(ValidationError):
        Source(actor_id="a" * 300)

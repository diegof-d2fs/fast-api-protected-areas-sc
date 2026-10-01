from __future__ import annotations

import pytest

from app.core.errors import AppError
from app.domain.models import SubmissionStatus
from app.domain.state import ensure_transition


def test_accepts_valid_transition() -> None:
    ensure_transition(SubmissionStatus.RECEIVED, SubmissionStatus.VALIDATING)


def test_rejects_invalid_transition() -> None:
    with pytest.raises(AppError) as captured:
        ensure_transition(SubmissionStatus.REJECTED, SubmissionStatus.ACCEPTED)
    assert captured.value.error_code == "INVALID_SUBMISSION_TRANSITION"

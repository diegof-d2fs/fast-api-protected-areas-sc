from __future__ import annotations

from app.core.errors import conflict
from app.domain.models import SubmissionStatus

_ALLOWED_TRANSITIONS: dict[SubmissionStatus, set[SubmissionStatus]] = {
    SubmissionStatus.RECEIVED: {SubmissionStatus.VALIDATING, SubmissionStatus.FAILED},
    SubmissionStatus.VALIDATING: {
        SubmissionStatus.ACCEPTED,
        SubmissionStatus.REJECTED,
        SubmissionStatus.FAILED,
    },
    SubmissionStatus.ACCEPTED: {
        SubmissionStatus.PUBLISHED,
        SubmissionStatus.DUPLICATE,
        SubmissionStatus.FAILED,
    },
    SubmissionStatus.PUBLISHED: {SubmissionStatus.PROCESSING, SubmissionStatus.FAILED},
    SubmissionStatus.PROCESSING: {
        SubmissionStatus.SUCCEEDED,
        SubmissionStatus.DUPLICATE,
        SubmissionStatus.FAILED,
    },
    SubmissionStatus.REJECTED: set(),
    SubmissionStatus.DUPLICATE: set(),
    SubmissionStatus.SUCCEEDED: set(),
    SubmissionStatus.FAILED: set(),
}


def ensure_transition(current: SubmissionStatus, target: SubmissionStatus) -> None:
    if target not in _ALLOWED_TRANSITIONS[current]:
        raise conflict(
            "INVALID_SUBMISSION_TRANSITION",
            f"A submissão não pode mudar de {current.value} para {target.value}.",
        )

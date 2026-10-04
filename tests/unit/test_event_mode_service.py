from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.application.event_mode import EventModeService, EventModeSettings
from app.core.errors import AppError
from app.domain.auth import CurrentUser, UserRole
from app.domain.event_mode import ActivateEventRequest, EventStatus, event_login
from tests.fakes import FakeAutomation, FakeEventModeRepository, FakeEventSecrets, FakeReturnGuard

ADMIN = CurrentUser(id=1, nome="Admin", role=UserRole.ADMIN)
T0 = datetime(2026, 10, 20, 15, 0, tzinfo=UTC)


class Clock:
    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kwargs: float) -> None:
        self.now += timedelta(**kwargs)


@pytest.fixture
def env():
    clock = Clock()
    repository = FakeEventModeRepository()
    automation = FakeAutomation()
    guard = FakeReturnGuard()
    secrets = FakeEventSecrets()
    in_flight = {"count": 0}
    service = EventModeService(
        repository,
        EventModeSettings(
            normal_instance_type="t3a.small",
            event_instance_type="t3a.large",
            min_active_minutes=30,
            default_duration_hours=5,
            max_duration_hours=24,
            extra_cost_usd_per_hour=0.0564,
            region="us-east-1",
            db_host="db.example",
            db_name="protected_areas_sc",
            ogc_base_url="https://areasprotegidas-sc.com/geoserver/protected_areas_sc/",
        ),
        automation=automation,
        return_guard=guard,
        secrets=secrets,
        imports_in_flight=lambda: in_flight["count"],
        clock=clock,
    )
    return {
        "service": service,
        "clock": clock,
        "repo": repository,
        "automation": automation,
        "guard": guard,
        "secrets": secrets,
        "in_flight": in_flight,
    }


def activate(service: EventModeService, key: str = "k1", **kwargs) -> object:
    return service.activate(ADMIN, ActivateEventRequest(nome="Univali", confirmacao="ATIVAR", **kwargs), key)


def make_active(env) -> int:
    view = activate(env["service"])
    env["automation"].finish("exec-1")
    assert env["service"].state().current.status is EventStatus.ACTIVE
    return view.id


def test_immediate_activation_starts_automation_and_guard(env) -> None:
    view = activate(env["service"])

    assert view.status is EventStatus.ACTIVATING
    assert view.auto_return_at == T0 + timedelta(hours=5)
    assert env["automation"].started == [("activate", view.id, "ws_univali_20261020")]
    assert env["guard"].scheduled[view.id] == T0 + timedelta(hours=5, minutes=30)
    assert view.execution_url.endswith("exec-1?region=us-east-1")


def test_confirmation_is_required(env) -> None:
    with pytest.raises(AppError) as error:
        env["service"].activate(ADMIN, ActivateEventRequest(nome="Univali", confirmacao="sim"), "k")
    assert error.value.error_code == "EVENT_MODE_CONFIRMATION_REQUIRED"
    assert env["automation"].started == []


def test_success_turns_active_with_min_time_and_progress_is_reported(env) -> None:
    view = activate(env["service"])
    assert env["service"].state().current.current_step == "pararServidor"

    env["clock"].advance(minutes=5)
    env["automation"].finish("exec-1")
    current = env["service"].state().current

    assert current.status is EventStatus.ACTIVE
    assert current.activated_at == T0 + timedelta(minutes=5)
    assert current.deactivate_allowed_at == T0 + timedelta(minutes=35)
    assert ("ativo" in [audit[2] for audit in env["repo"].audits if audit[0] == view.id])


def test_deactivation_is_blocked_until_min_time_with_retry_after(env) -> None:
    event_id = make_active(env)
    env["clock"].advance(minutes=10)

    with pytest.raises(AppError) as error:
        env["service"].deactivate(ADMIN, event_id, "DESATIVAR")

    assert error.value.status_code == 409
    assert error.value.error_code == "EVENT_MODE_MIN_ACTIVE_TIME"
    assert error.value.headers["Retry-After"] == str(20 * 60)


def test_deactivation_after_min_time_returns_and_removes_guard(env) -> None:
    event_id = make_active(env)
    env["clock"].advance(minutes=31)

    view = env["service"].deactivate(ADMIN, event_id, "DESATIVAR")
    assert view.status is EventStatus.DEACTIVATING
    env["automation"].finish("exec-2")
    final = env["service"].state().current

    assert final.status is EventStatus.FINISHED
    assert env["guard"].scheduled == {}
    assert env["service"].state().current is None


def test_only_one_open_event(env) -> None:
    activate(env["service"])
    with pytest.raises(AppError) as error:
        activate(env["service"], key="k2")
    assert error.value.error_code == "EVENT_MODE_ALREADY_OPEN"


def test_same_idempotency_key_returns_the_same_event(env) -> None:
    first = activate(env["service"])
    again = activate(env["service"])
    assert again.id == first.id
    assert len(env["automation"].started) == 1


def test_imports_in_flight_block_immediate_activation(env) -> None:
    env["in_flight"]["count"] = 2
    with pytest.raises(AppError) as error:
        activate(env["service"])
    assert error.value.error_code == "EVENT_MODE_IMPORTS_IN_FLIGHT"
    assert env["repo"].records == {}


@pytest.mark.parametrize(
    ("hours", "code"),
    [(0.25, "EVENT_MODE_RETURN_TOO_SOON"), (25, "EVENT_MODE_RETURN_TOO_LATE")],
)
def test_return_must_respect_min_and_max(env, hours: float, code: str) -> None:
    with pytest.raises(AppError) as error:
        activate(env["service"], retorno_automatico_em=T0 + timedelta(hours=hours))
    assert error.value.error_code == code


def test_naive_datetimes_are_rejected(env) -> None:
    with pytest.raises(AppError) as error:
        activate(env["service"], inicio=datetime(2026, 10, 21, 14, 0))
    assert error.value.error_code == "EVENT_MODE_TIMEZONE_REQUIRED"


def test_scheduled_event_starts_on_tick_and_waits_for_imports(env) -> None:
    start = T0 + timedelta(hours=2)
    view = activate(env["service"], inicio=start)
    assert view.status is EventStatus.SCHEDULED
    assert env["automation"].started == []

    env["clock"].now = start
    env["in_flight"]["count"] = 1
    env["service"].tick()
    assert env["automation"].started == []

    env["in_flight"]["count"] = 0
    env["service"].tick()
    assert env["automation"].started[0][0] == "activate"
    assert env["service"].state().current.status is EventStatus.ACTIVATING


def test_tick_returns_automatically_at_the_return_time_ignoring_min_time(env) -> None:
    event_id = make_active(env)
    env["clock"].now = T0 + timedelta(hours=5)

    env["service"].tick()

    assert env["automation"].started[-1] == ("deactivate", event_id, "ws_univali_20261020")
    assert env["service"].state().current.status is EventStatus.DEACTIVATING


def test_extend_moves_return_and_guard_within_the_ceiling(env) -> None:
    event_id = make_active(env)
    new_return = T0 + timedelta(hours=8)

    view = env["service"].extend(ADMIN, event_id, new_return)

    assert view.auto_return_at == new_return
    assert env["guard"].scheduled[event_id] == new_return + timedelta(minutes=30)
    with pytest.raises(AppError):
        env["service"].extend(ADMIN, event_id, T0 + timedelta(hours=30))


def test_cancel_only_scheduled_events(env) -> None:
    view = activate(env["service"], inicio=T0 + timedelta(hours=3))
    cancelled = env["service"].cancel(ADMIN, view.id)
    assert cancelled.status is EventStatus.CANCELLED
    assert env["guard"].scheduled == {}


def test_failed_automation_marks_event_failed_and_allows_forced_return(env) -> None:
    view = activate(env["service"])
    env["automation"].finish("exec-1", success=False, message="Step aguardarSaude failed")

    failed = env["service"].state().current
    assert failed.status is EventStatus.FAILED
    assert failed.failure_message == "Step aguardarSaude failed"
    with pytest.raises(AppError) as error:
        activate(env["service"], key="k2")
    assert error.value.error_code == "EVENT_MODE_FAILED_PENDING"

    returned = env["service"].deactivate(ADMIN, view.id, "DESATIVAR")
    assert returned.status is EventStatus.DEACTIVATING


def test_automation_start_failure_is_reported_without_side_effects(env) -> None:
    env["automation"].fail_start = True
    with pytest.raises(AppError) as error:
        activate(env["service"])
    assert error.value.status_code == 502
    assert env["service"].history()[0].status is EventStatus.FAILED


def test_credentials_only_while_active_and_audited(env) -> None:
    event_id = make_active(env)
    env["secrets"].passwords[event_id] = "s3nha-do-evento"

    credentials = env["service"].credentials(ADMIN, event_id)

    assert credentials.db_login == "ws_univali_20261020"
    assert credentials.db_password == "s3nha-do-evento"
    assert ("credenciais_lidas" in [audit[2] for audit in env["repo"].audits])


def test_cost_estimate_uses_active_hours(env) -> None:
    event_id = make_active(env)
    env["clock"].advance(hours=2)
    view = env["service"].view(env["repo"].get(event_id))
    assert view.estimated_cost_usd == pytest.approx(0.11, abs=0.01)


def test_event_login_is_normalized_and_uses_brasilia_date() -> None:
    late_night_utc = datetime(2026, 10, 21, 1, 30, tzinfo=UTC)  # 20/10 às 22:30 em Brasília
    login = event_login("Universidade São José, Câmpus 2!", late_night_utc)
    assert login == "ws_universidade_sao_jose_campus_2_20261020"
    assert len(login) <= 63

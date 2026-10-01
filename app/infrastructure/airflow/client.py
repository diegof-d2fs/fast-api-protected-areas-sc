from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from app.core.errors import AppError


@dataclass(frozen=True)
class AirflowDagRun:
    dag_id: str
    dag_run_id: str
    state: str | None


class AirflowClient:
    def __init__(
        self,
        base_url: str,
        *,
        username: str,
        password: str,
        timeout_seconds: float,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.authorization = base64.b64encode(f"{username}:{password}".encode()).decode()
        self.timeout_seconds = timeout_seconds

    def trigger(self, dag_id: str, dag_run_id: str, conf: dict) -> AirflowDagRun:
        request = Request(
            f"{self.base_url}/api/v1/dags/{dag_id}/dagRuns",
            data=json.dumps({"dag_run_id": dag_run_id, "conf": conf}).encode("utf-8"),
            headers={
                "Authorization": f"Basic {self.authorization}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            method="POST",
        )
        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:
                payload = json.load(response)
        except HTTPError as exc:
            if exc.code == 409:
                return AirflowDagRun(dag_id=dag_id, dag_run_id=dag_run_id, state=None)
            raise self._unavailable(f"Airflow respondeu HTTP {exc.code} ao disparar {dag_id}.") from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise self._unavailable(f"Não foi possível alcançar o Airflow para disparar {dag_id}.") from exc
        return AirflowDagRun(
            dag_id=dag_id,
            dag_run_id=str(payload.get("dag_run_id", dag_run_id)),
            state=payload.get("state"),
        )

    def get_dag_run(self, dag_id: str, dag_run_id: str) -> AirflowDagRun:
        request = Request(
            f"{self.base_url}/api/v1/dags/{dag_id}/dagRuns/{dag_run_id}",
            headers={"Authorization": f"Basic {self.authorization}", "Accept": "application/json"},
        )
        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:
                payload = json.load(response)
        except HTTPError as exc:
            raise self._unavailable(
                f"Airflow respondeu HTTP {exc.code} ao consultar {dag_run_id}."
            ) from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise self._unavailable(f"Não foi possível consultar a execução {dag_run_id}.") from exc
        return AirflowDagRun(
            dag_id=dag_id,
            dag_run_id=str(payload.get("dag_run_id", dag_run_id)),
            state=payload.get("state"),
        )

    def ready(self) -> bool:
        request = Request(
            f"{self.base_url}/api/v1/health",
            headers={"Authorization": f"Basic {self.authorization}", "Accept": "application/json"},
        )
        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:
                return response.status == 200
        except Exception:
            return False

    @staticmethod
    def _unavailable(detail: str) -> AppError:
        return AppError(503, "AIRFLOW_UNAVAILABLE", "Airflow indisponível", detail)

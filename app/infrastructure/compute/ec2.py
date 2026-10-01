from __future__ import annotations

import logging
from typing import Any, Protocol


class EC2Client(Protocol):
    def describe_instances(self, **kwargs: Any) -> Any: ...

    def start_instances(self, **kwargs: Any) -> Any: ...


class ProcessingNodeWaker:
    """Start the on-demand processing node when there is pipeline work and Airflow is down.

    Never raises: a failed start only delays processing, because pending imports stay
    `PUBLISHED` and are dispatched as soon as Airflow answers.
    """

    def __init__(self, client: EC2Client, instance_id: str) -> None:
        self.client = client
        self.instance_id = instance_id
        self.logger = logging.getLogger(__name__)

    def wake(self) -> bool:
        try:
            response = self.client.describe_instances(InstanceIds=[self.instance_id])
            state = response["Reservations"][0]["Instances"][0]["State"]["Name"]
            if state == "stopped":
                self.client.start_instances(InstanceIds=[self.instance_id])
                self.logger.info("processing node start requested", extra={"instance_id": self.instance_id})
                return True
            self.logger.info(
                "processing node not stopped; nothing to start",
                extra={"instance_id": self.instance_id, "state": state},
            )
        except Exception:
            self.logger.exception("could not start processing node", extra={"instance_id": self.instance_id})
        return False

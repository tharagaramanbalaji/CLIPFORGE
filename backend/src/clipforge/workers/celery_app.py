"""Celery application. Queues per ADR-0004: gpu (c=1), cpu, render (c=1-2), llm.

Pipeline state lives in Postgres (ADR-0003); no result backend is relied upon.
Start one worker per queue, e.g.:
    celery -A clipforge.workers.celery_app worker -Q gpu -c 1 -n gpu@%h
"""

from __future__ import annotations

from celery import Celery
from kombu.entity import Queue

from clipforge.config import get_settings
from clipforge.log import configure_logging

QUEUES = ("gpu", "cpu", "render", "llm")

_settings = get_settings()
configure_logging(_settings.log_level, _settings.log_json)

app = Celery("clipforge", broker=_settings.redis_url, include=["clipforge.workers.tasks"])
app.conf.update(
    task_queues=[Queue(q) for q in QUEUES],
    task_default_queue="cpu",
    task_serializer="json",
    accept_content=["json"],
    task_ignore_result=True,
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,
    worker_hijack_root_logger=False,
    broker_connection_retry_on_startup=True,
    task_routes={
        "clipforge.system.environment_report": {"queue": "gpu"},
    },
)

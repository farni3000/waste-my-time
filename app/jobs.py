from __future__ import annotations

from collections.abc import Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from .db import SessionLocal
from .domain.profile import build_profile
from .domain.recommend import run_recommendation
from .models import Job, Source
from .sources.editorial import fetch_editorial
from .sources.rss import fetch_rss
from .sources.steam import enrich_library, sync_library
from .timeutil import utcnow

Progress = Callable[[int, str], None]


def enqueue(session: Session, kind: str, payload: dict | None = None) -> Job:
    job = Job(kind=kind, payload_json=payload or {}, status="queued")
    session.add(job)
    session.commit()
    return job


def _ingest_sources(session: Session, payload: dict, progress: Progress) -> dict:
    query = select(Source).where(Source.enabled.is_(True))
    if payload.get("source_id"):
        query = select(Source).where(Source.id == int(payload["source_id"]))
    sources = session.scalars(query).all()
    if not sources:
        raise ValueError("Keine aktiven Quellen vorhanden.")
    results = []
    for index, source in enumerate(sources, start=1):
        progress(int(100 * (index - 1) / len(sources)), f"Quelle: {source.name}")
        source_id = source.id
        source_name = source.name
        try:
            if source.kind == "html":
                result = fetch_editorial(session, source)
            else:
                result = fetch_rss(session, source)
        except Exception as exc:  # noqa: BLE001 - one broken source must not stop the run
            session.rollback()
            source = session.get(Source, source_id)
            source.last_error = str(exc)[:500]
            session.commit()
            result = {"source": source_name, "error": str(exc)[:200]}
        results.append(result)
    return {"results": results}


def _recommend(session: Session, payload: dict, progress: Progress) -> dict:
    run = run_recommendation(
        session,
        mode=payload.get("mode", "backlog"),
        filters=payload.get("filters") or {},
        progress=progress,
    )
    return {"run_id": run.id, "status": run.status, "model": run.model}


HANDLERS: dict[str, Callable[[Session, dict, Progress], dict]] = {
    "sync_steam": lambda session, payload, progress: sync_library(
        session, enrich=True, progress=progress
    ),
    "enrich_library": lambda session, payload, progress: enrich_library(
        session, limit=int(payload.get("limit") or 80), progress=progress
    ),
    "ingest_sources": _ingest_sources,
    "build_profile": lambda session, payload, progress: {
        "version": build_profile(session, force=bool(payload.get("force", True))).version
    },
    "recommend": _recommend,
}


def run_job(job_id: int) -> None:
    with SessionLocal() as session:
        job = session.get(Job, job_id)
        if job is None:
            return
        job.status = "running"
        job.started_at = utcnow()
        job.progress = 1
        job.message = "Gestartet"
        session.commit()
        kind = job.kind
        payload = job.payload_json or {}

        def progress(percent: int, message: str) -> None:
            job.progress = max(0, min(100, int(percent)))
            job.message = message[:500]
            session.commit()

        handler = HANDLERS.get(kind)
        try:
            if handler is None:
                raise ValueError(f"Unbekannter Job-Typ: {kind}")
            result = handler(session, payload, progress)
            job.status = "done"
            job.progress = 100
            job.message = "Fertig"
            job.result_json = result or {}
        except Exception as exc:  # noqa: BLE001 - report in UI
            session.rollback()
            job = session.get(Job, job_id)
            job.status = "failed"
            job.error = str(exc)[:2000]
            job.message = "Fehlgeschlagen"
        finally:
            job.finished_at = utcnow()
            session.commit()


def process_next() -> bool:
    with SessionLocal() as session:
        job = session.scalar(
            select(Job).where(Job.status == "queued").order_by(Job.id.asc()).limit(1)
        )
        if job is None:
            return False
        job_id = job.id
    run_job(job_id)
    return True


def run_now(kind: str, payload: dict | None = None) -> dict:
    """Run a handler without involving the queue (used by the CLI)."""
    with SessionLocal() as session:
        job = Job(kind=kind, payload_json=payload or {}, status="running", started_at=utcnow())
        session.add(job)
        session.commit()

        def progress(percent: int, message: str) -> None:
            job.progress = percent
            job.message = message
            session.commit()

        job_id = job.id
        try:
            result = HANDLERS[kind](session, payload or {}, progress)
            job = session.get(Job, job_id)
            job.status = "done"
            job.progress = 100
            job.message = "Fertig"
            job.result_json = result or {}
            job.finished_at = utcnow()
            session.commit()
            return result
        except Exception as exc:
            session.rollback()
            job = session.get(Job, job_id)
            job.status = "failed"
            job.error = str(exc)[:2000]
            job.finished_at = utcnow()
            session.commit()
            raise

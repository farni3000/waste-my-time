from __future__ import annotations

import argparse
import json
import sys

from sqlalchemy import func, select

from .config import BASE_DIR, get_settings
from .db import SessionLocal, engine, run_migrations
from .domain.profile import build_profile
from .domain.recommend import run_recommendation
from .jobs import run_now
from .models import Game, LibraryEntry, Recommendation, Source, SourceItem
from .seed import seed_default_sources
from .sources.resolve import resolve_pending
from .timeutil import utcnow


def _print(value) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, default=str))


def _progress(percent: int, message: str) -> None:
    print(f"[{percent:3d}%] {message}", flush=True)


def command_init_db(_args) -> None:
    run_migrations()
    with SessionLocal() as session:
        created = seed_default_sources(session)
    _print({"ok": True, "seeded_sources": created, "db": str(get_settings().db_file)})


def command_seed_sources(_args) -> None:
    run_migrations()
    with SessionLocal() as session:
        _print({"created": seed_default_sources(session)})


def command_sync_steam(args) -> None:
    _print(run_now("sync_steam", {"enrich": not args.no_enrich}))


def command_enrich(args) -> None:
    _print(run_now("enrich_library", {"limit": args.limit}))


def command_ingest(args) -> None:
    _print(run_now("ingest_sources", {"source_id": args.source_id}))


def command_profile(_args) -> None:
    run_migrations()
    with SessionLocal() as session:
        profile = build_profile(session, force=True)
        _print({"id": profile.id, "version": profile.version, "summary": profile.summary_json})


def command_recommend(args) -> None:
    run_migrations()
    filters = {
        "max_price": args.max_price,
        "max_hours": args.max_hours,
        "mood": args.mood or "",
        "exclude_tags": args.exclude_tags or "",
    }
    with SessionLocal() as session:
        run = run_recommendation(session, mode=args.mode, filters=filters, progress=_progress)
        _print(
            {
                "run_id": run.id,
                "status": run.status,
                "model": run.model,
                "candidates": run.candidate_count,
                "error": run.error,
            }
        )


def command_resolve(_args) -> None:
    run_migrations()
    with SessionLocal() as session:
        _print(resolve_pending(session))


def command_backup(_args) -> None:
    run_migrations()
    backup_dir = BASE_DIR / "data" / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    target = backup_dir / f"app-{utcnow().strftime('%Y%m%d-%H%M%S')}.db"
    with engine.connect() as connection:
        connection.exec_driver_sql(f"VACUUM INTO '{target}'")
    _print({"backup": str(target)})


def command_stats(_args) -> None:
    run_migrations()
    with SessionLocal() as session:
        _print(
            {
                "games": session.scalar(select(func.count(Game.id))),
                "library_entries": session.scalar(select(func.count(LibraryEntry.id))),
                "hours": round(
                    (session.scalar(select(func.sum(LibraryEntry.minutes_played))) or 0) / 60, 1
                ),
                "sources": session.scalar(select(func.count(Source.id))),
                "source_items": session.scalar(select(func.count(SourceItem.id))),
                "recommendations": session.scalar(select(func.count(Recommendation.id))),
            }
        )


def command_serve(args) -> None:
    import uvicorn

    settings = get_settings()
    uvicorn.run(
        "app.main:app",
        host=args.host or settings.app_host,
        port=args.port or settings.app_port,
        reload=args.reload,
    )


def command_worker(_args) -> None:
    from .worker import main as worker_main

    worker_main()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="wmt", description="waste-my-time local game recommender")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("init-db", help="Datenbank anlegen und Standardquellen eintragen").set_defaults(
        func=command_init_db
    )
    subparsers.add_parser("seed-sources", help="Standard-RSS-Quellen eintragen").set_defaults(
        func=command_seed_sources
    )

    sync = subparsers.add_parser("sync-steam", help="Steam-Bibliothek synchronisieren")
    sync.add_argument("--no-enrich", action="store_true", help="Keine Store-Metadaten laden")
    sync.set_defaults(func=command_sync_steam)

    enrich = subparsers.add_parser("enrich", help="Store-Metadaten für die Top-Spiele laden")
    enrich.add_argument("--limit", type=int, default=80)
    enrich.set_defaults(func=command_enrich)

    ingest = subparsers.add_parser("ingest", help="RSS-/Bestenlisten-Quellen einlesen")
    ingest.add_argument("--source-id", type=int)
    ingest.set_defaults(func=command_ingest)

    subparsers.add_parser("profile", help="Geschmacksprofil neu berechnen").set_defaults(
        func=command_profile
    )

    recommend = subparsers.add_parser("recommend", help="Empfehlungen erzeugen")
    recommend.add_argument("--mode", choices=["backlog", "new"], default="backlog")
    recommend.add_argument("--max-price", type=float)
    recommend.add_argument("--max-hours", type=float)
    recommend.add_argument("--mood", type=str)
    recommend.add_argument("--exclude-tags", type=str)
    recommend.set_defaults(func=command_recommend)

    subparsers.add_parser("resolve", help="Offene Quellen-Erwähnungen automatisch zuordnen").set_defaults(
        func=command_resolve
    )
    subparsers.add_parser("backup", help="SQLite-Datenbank sichern").set_defaults(func=command_backup)
    subparsers.add_parser("stats", help="Kurze Übersicht ausgeben").set_defaults(func=command_stats)

    serve = subparsers.add_parser("serve", help="Web-App starten")
    serve.add_argument("--host")
    serve.add_argument("--port", type=int)
    serve.add_argument("--reload", action="store_true")
    serve.set_defaults(func=command_serve)

    subparsers.add_parser("worker", help="Job-Worker starten").set_defaults(func=command_worker)
    return parser


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        args.func(args)
    except Exception as exc:  # noqa: BLE001 - CLI surface
        print(f"Fehler: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()

from __future__ import annotations

from collections import Counter, defaultdict
from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import desc, func, select
from sqlalchemy.orm import Session

from ..config import BASE_DIR, get_settings
from ..db import engine, get_db
from ..domain.normalize import format_hours, normalize_title, parse_hours, parse_tag_list
from ..domain.profile import active_profile
from ..domain.recommend import recommendation_games
from ..jobs import enqueue
from ..models import (
    ApiCache,
    Game,
    Interest,
    Job,
    LibraryEntry,
    Mention,
    PlaytimeSnapshot,
    Recommendation,
    RecommendationRun,
    Source,
    SourceItem,
    SteamAccount,
)
from ..sources.resolve import create_game_from_mention, ignore_mention, suggestions
from ..sources.steam import set_steam_id
from ..timeutil import local, utcnow

router = APIRouter()
templates = Jinja2Templates(directory=str(BASE_DIR / "app" / "web" / "templates"))


def _dt(value, fmt: str = "%d.%m.%Y %H:%M") -> str:
    converted = local(value)
    return converted.strftime(fmt) if converted else "–"


def _money(cents) -> str:
    if cents is None:
        return "–"
    return f"{cents / 100:.2f} €".replace(".", ",")


def _ago(value) -> str:
    if not value:
        return "–"
    days = max(0, (utcnow() - value).days)
    if days == 0:
        return "heute"
    if days == 1:
        return "gestern"
    return f"vor {days} Tagen"


templates.env.filters["hours"] = format_hours
templates.env.filters["dt"] = _dt
templates.env.filters["date"] = lambda value: _dt(value, "%d.%m.%Y")
templates.env.filters["money"] = _money
templates.env.filters["ago"] = _ago


def _ctx(request: Request, session: Session, **extra) -> dict:
    return {
        "request": request,
        "active_profile": active_profile(session),
        "settings": get_settings(),
        "nav": request.url.path,
        "status_labels": {
            "owned": "Besitz",
            "playing": "spielt",
            "completed": "abgeschlossen",
            "abandoned": "abgebrochen",
            "wishlist": "Wunschliste",
            "new": "neu",
            "liked": "gemerkt",
            "dismissed": "verworfen",
            "played": "gespielt",
        },
        **extra,
    }


def _redirect(path: str, message: str | None = None) -> RedirectResponse:
    if message:
        separator = "&" if "?" in path else "?"
        path = f"{path}{separator}msg={quote(message)}"
    return RedirectResponse(path, status_code=303)


def _rating(value: str | None) -> int | None:
    try:
        number = int(value or "")
    except (TypeError, ValueError):
        return None
    return number if 1 <= number <= 5 else None


def _clamp_weight(value: str | None) -> int:
    try:
        number = int(value or 0)
    except (TypeError, ValueError):
        return 0
    return max(-3, min(3, number))


def _enqueue_once(session: Session, kind: str, payload: dict | None = None) -> Job:
    existing = session.scalar(
        select(Job)
        .where(Job.kind == kind, Job.status.in_(["queued", "running"]))
        .order_by(Job.id.desc())
        .limit(1)
    )
    if existing is not None:
        return existing
    return enqueue(session, kind, payload)


def _job_panel(request: Request, job: Job) -> HTMLResponse:
    return templates.TemplateResponse(request, "partials/job_panel.html", {"job": job})


@router.get("/", response_class=HTMLResponse)
def dashboard(request: Request, session: Session = Depends(get_db)):
    latest_run = session.scalar(
        select(RecommendationRun)
        .where(RecommendationRun.status == "done")
        .order_by(desc(RecommendationRun.id))
        .limit(1)
    )
    cards = recommendation_games(session, latest_run.id)[:6] if latest_run else []
    jobs = session.scalars(select(Job).order_by(desc(Job.id)).limit(6)).all()
    account = session.scalar(select(SteamAccount).limit(1))
    stats = {
        "games": session.scalar(select(func.count(Game.id))) or 0,
        "entries": session.scalar(select(func.count(LibraryEntry.id))) or 0,
        "hours": round((session.scalar(select(func.sum(LibraryEntry.minutes_played))) or 0) / 60),
        "backlog": session.scalar(
            select(func.count(LibraryEntry.id)).where(LibraryEntry.minutes_played < 60)
        )
        or 0,
        "sources": session.scalar(select(func.count(Source.id))) or 0,
        "pending": session.scalar(
            select(func.count(Mention.id)).where(Mention.status == "pending")
        )
        or 0,
    }
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        _ctx(
            request,
            session,
            stats=stats,
            latest_run=latest_run,
            cards=cards,
            jobs=jobs,
            account=account,
        ),
    )


@router.get("/library", response_class=HTMLResponse)
def library(
    request: Request,
    q: str = "",
    status: str = "",
    source: str = "",
    session: Session = Depends(get_db),
):
    query = select(LibraryEntry, Game).join(Game, Game.id == LibraryEntry.game_id)
    if q.strip():
        query = query.where(Game.title.ilike(f"%{q.strip()}%"))
    if status:
        query = query.where(LibraryEntry.status == status)
    if source:
        query = query.where(LibraryEntry.source == source)
    rows = session.execute(
        query.order_by(desc(LibraryEntry.minutes_played), Game.title.asc())
    ).all()
    return templates.TemplateResponse(
        request,
        "library.html",
        _ctx(
            request,
            session,
            rows=rows,
            q=q,
            status=status,
            source=source,
            total=len(rows),
        ),
    )


@router.get("/library/new", response_class=HTMLResponse)
def library_new(request: Request, session: Session = Depends(get_db)):
    return templates.TemplateResponse(
        request, "game_form.html", _ctx(request, session, entry=None, game=None)
    )


@router.post("/library/new")
def library_create(
    request: Request,
    title: str = Form(...),
    platform: str = Form("pc"),
    hours: str = Form("0"),
    rating: str = Form(""),
    status: str = Form("owned"),
    notes: str = Form(""),
    tags: str = Form(""),
    favorite: bool = Form(False),
    session: Session = Depends(get_db),
):
    title = title.strip()
    if not title:
        return _redirect("/library/new", "Titel fehlt.")
    game = session.scalar(select(Game).where(Game.normalized_title == normalize_title(title)))
    if game is None:
        game = Game(
            title=title,
            normalized_title=normalize_title(title),
            source_primary="manual",
            tags=parse_tag_list(tags),
        )
        session.add(game)
        session.flush()
    else:
        game.tags = list(dict.fromkeys((game.tags or []) + parse_tag_list(tags)))
    entry = session.scalar(
        select(LibraryEntry).where(
            LibraryEntry.game_id == game.id, LibraryEntry.platform == platform
        )
    )
    if entry is None:
        entry = LibraryEntry(game_id=game.id, platform=platform, source="manual")
        session.add(entry)
    entry.minutes_played = parse_hours(hours)
    entry.rating = _rating(rating)
    entry.status = status
    entry.notes = notes.strip() or None
    entry.user_tags = parse_tag_list(tags)
    entry.favorite = favorite
    session.commit()
    return _redirect("/library", f"{game.title} gespeichert.")


@router.get("/library/{entry_id}/edit", response_class=HTMLResponse)
def library_edit(entry_id: int, request: Request, session: Session = Depends(get_db)):
    entry = session.get(LibraryEntry, entry_id)
    if entry is None:
        raise HTTPException(404, "Eintrag nicht gefunden")
    return templates.TemplateResponse(
        request,
        "game_form.html",
        _ctx(request, session, entry=entry, game=entry.game),
    )


@router.post("/library/{entry_id}/edit")
def library_update(
    entry_id: int,
    hours: str = Form("0"),
    rating: str = Form(""),
    status: str = Form("owned"),
    notes: str = Form(""),
    tags: str = Form(""),
    favorite: bool = Form(False),
    session: Session = Depends(get_db),
):
    entry = session.get(LibraryEntry, entry_id)
    if entry is None:
        raise HTTPException(404, "Eintrag nicht gefunden")
    entry.minutes_played = parse_hours(hours)
    entry.rating = _rating(rating)
    entry.status = status
    entry.notes = notes.strip() or None
    entry.user_tags = parse_tag_list(tags)
    entry.favorite = favorite
    session.commit()
    return _redirect("/library", f"{entry.game.title} aktualisiert.")


@router.post("/library/{entry_id}/delete")
def library_delete(entry_id: int, session: Session = Depends(get_db)):
    entry = session.get(LibraryEntry, entry_id)
    if entry is not None:
        session.delete(entry)
        session.commit()
    return _redirect("/library", "Eintrag entfernt.")


@router.get("/interests", response_class=HTMLResponse)
def interests(request: Request, session: Session = Depends(get_db)):
    items = session.scalars(select(Interest).order_by(Interest.kind, Interest.value)).all()
    return templates.TemplateResponse(
        request, "interests.html", _ctx(request, session, items=items)
    )


@router.post("/interests")
def interest_create(
    kind: str = Form("genre"),
    value: str = Form(...),
    weight: str = Form("0"),
    hard_filter: bool = Form(False),
    notes: str = Form(""),
    session: Session = Depends(get_db),
):
    value = value.strip()
    if not value:
        return _redirect("/interests", "Wert fehlt.")
    session.add(
        Interest(
            kind=kind,
            value=value,
            weight=_clamp_weight(weight),
            hard_filter=hard_filter,
            notes=notes.strip() or None,
        )
    )
    session.commit()
    return _redirect("/interests", f"Interesse '{value}' hinzugefügt.")


@router.post("/interests/{interest_id}/delete")
def interest_delete(interest_id: int, session: Session = Depends(get_db)):
    item = session.get(Interest, interest_id)
    if item is not None:
        session.delete(item)
        session.commit()
    return _redirect("/interests", "Interesse entfernt.")


@router.get("/sources", response_class=HTMLResponse)
def sources(request: Request, session: Session = Depends(get_db)):
    items = session.scalars(select(Source).order_by(Source.name)).all()
    pending_by_source = dict(
        session.execute(
            select(SourceItem.source_id, func.count(Mention.id))
            .join(Mention, Mention.source_item_id == SourceItem.id)
            .where(Mention.status == "pending")
            .group_by(SourceItem.source_id)
        ).all()
    )
    item_counts = dict(
        session.execute(
            select(SourceItem.source_id, func.count(SourceItem.id)).group_by(SourceItem.source_id)
        ).all()
    )
    return templates.TemplateResponse(
        request,
        "sources.html",
        _ctx(
            request,
            session,
            items=items,
            pending_by_source=pending_by_source,
            item_counts=item_counts,
        ),
    )


@router.post("/sources")
def source_create(
    name: str = Form(...),
    kind: str = Form("rss"),
    url: str = Form(...),
    item_selector: str = Form(""),
    title_selector: str = Form(""),
    ranked: bool = Form(True),
    session: Session = Depends(get_db),
):
    url = url.strip()
    if not url.startswith("http"):
        return _redirect("/sources", "URL muss mit http(s) beginnen.")
    config = {}
    if kind == "html":
        config = {
            "item_selector": item_selector.strip() or "article, li",
            "title_selector": title_selector.strip(),
            "ranked": ranked,
        }
    session.add(
        Source(name=name.strip(), kind=kind, url=url, config_json=config, enabled=True)
    )
    session.commit()
    return _redirect("/sources", f"Quelle '{name}' angelegt.")


@router.post("/sources/{source_id}/toggle")
def source_toggle(source_id: int, session: Session = Depends(get_db)):
    source = session.get(Source, source_id)
    if source is not None:
        source.enabled = not source.enabled
        session.commit()
    return _redirect("/sources")


@router.post("/sources/{source_id}/delete")
def source_delete(source_id: int, session: Session = Depends(get_db)):
    source = session.get(Source, source_id)
    if source is not None:
        session.delete(source)
        session.commit()
    return _redirect("/sources", "Quelle entfernt.")


@router.get("/mentions", response_class=HTMLResponse)
def mentions(request: Request, session: Session = Depends(get_db)):
    rows = session.execute(
        select(Mention, SourceItem, Source)
        .join(SourceItem, SourceItem.id == Mention.source_item_id)
        .join(Source, Source.id == SourceItem.source_id)
        .where(Mention.status == "pending")
        .order_by(desc(Mention.id))
        .limit(50)
    ).all()
    pending = [
        {
            "mention": mention,
            "item": item,
            "source": source,
            "suggestions": suggestions(session, mention.raw_title),
        }
        for mention, item, source in rows
    ]
    recent = session.execute(
        select(Mention, SourceItem)
        .join(SourceItem, SourceItem.id == Mention.source_item_id)
        .where(Mention.status.in_(["resolved", "ignored"]))
        .order_by(desc(Mention.id))
        .limit(12)
    ).all()
    return templates.TemplateResponse(
        request,
        "mentions.html",
        _ctx(request, session, pending=pending, recent=recent),
    )


@router.post("/mentions/{mention_id}/resolve")
def mention_resolve(
    mention_id: int,
    action: str = Form("existing"),
    game_id: str = Form(""),
    title: str = Form(""),
    session: Session = Depends(get_db),
):
    mention = session.get(Mention, mention_id)
    if mention is None:
        raise HTTPException(404, "Erwähnung nicht gefunden")
    if action == "ignore":
        ignore_mention(session, mention)
        return _redirect("/mentions", "Erwähnung ignoriert.")
    if action == "create":
        game = create_game_from_mention(session, mention, title=title or mention.raw_title)
        if game is None:
            return _redirect("/mentions", "Spiel konnte nicht angelegt werden.")
        return _redirect("/mentions", f"{game.title} angelegt und zugeordnet.")
    try:
        target = session.get(Game, int(game_id))
    except (TypeError, ValueError):
        target = None
    if target is None:
        return _redirect("/mentions", "Kein Zielspiel ausgewählt.")
    mention.game_id = target.id
    mention.status = "resolved"
    mention.resolved_by = "manual"
    session.commit()
    return _redirect("/mentions", f"Erwähnung '{mention.raw_title}' zugeordnet.")


@router.get("/recommendations", response_class=HTMLResponse)
def recommendations(
    request: Request,
    run_id: int | None = None,
    job_id: int | None = None,
    session: Session = Depends(get_db),
):
    if run_id:
        run = session.get(RecommendationRun, run_id)
    else:
        run = session.scalar(
            select(RecommendationRun).order_by(desc(RecommendationRun.id)).limit(1)
        )
    cards = recommendation_games(session, run.id) if run else []
    job = session.get(Job, job_id) if job_id else None
    return templates.TemplateResponse(
        request,
        "recommendations.html",
        _ctx(request, session, run=run, cards=cards, job=job),
    )


@router.post("/recommendations/start", response_class=HTMLResponse)
def recommendation_start(
    request: Request,
    mode: str = Form("backlog"),
    max_price: str = Form(""),
    max_hours: str = Form(""),
    mood: str = Form(""),
    exclude_tags: str = Form(""),
    session: Session = Depends(get_db),
):
    if mode not in {"backlog", "new"}:
        mode = "backlog"
    filters = {
        "max_price": max_price.strip() or None,
        "max_hours": max_hours.strip() or None,
        "mood": mood.strip(),
        "exclude_tags": exclude_tags.strip(),
    }
    job = _enqueue_once(session, "recommend", {"mode": mode, "filters": filters})
    return _job_panel(request, job)


@router.post("/recommendations/{rec_id}/feedback", response_class=HTMLResponse)
def recommendation_feedback(
    rec_id: int,
    request: Request,
    status: str = Form(...),
    note: str = Form(""),
    session: Session = Depends(get_db),
):
    rec = session.get(Recommendation, rec_id)
    if rec is None:
        raise HTTPException(404, "Vorschlag nicht gefunden")
    if status not in {"liked", "dismissed", "played", "new"}:
        raise HTTPException(400, "Unbekannter Status")
    rec.status = status
    rec.feedback_note = note.strip() or None
    if status == "played":
        entry = session.scalar(
            select(LibraryEntry).where(
                LibraryEntry.game_id == rec.game_id, LibraryEntry.platform == "pc"
            )
        )
        if entry is None:
            session.add(
                LibraryEntry(
                    game_id=rec.game_id,
                    platform="pc",
                    source="manual",
                    status="playing",
                )
            )
        elif entry.status in {"owned", "wishlist"}:
            entry.status = "playing"
    session.commit()
    if request.headers.get("HX-Request"):
        game = session.get(Game, rec.game_id)
        run = session.get(RecommendationRun, rec.run_id)
        return templates.TemplateResponse(
            request, "partials/rec_card.html", _ctx(request, session, rec=rec, game=game, run=run)
        )
    return _redirect(f"/recommendations?run_id={rec.run_id}", "Feedback gespeichert.")


@router.get("/stats", response_class=HTMLResponse)
def stats(request: Request, session: Session = Depends(get_db)):
    entries = session.execute(
        select(LibraryEntry, Game).join(Game, Game.id == LibraryEntry.game_id)
    ).all()
    total_minutes = sum(entry.minutes_played or 0 for entry, _game in entries)
    genre_hours: Counter[str] = Counter()
    for entry, game in entries:
        genres = game.genres or entry.user_tags or (game.tags or [])[:1] or ["Unbekannt"]
        for genre in genres:
            genre_hours[genre] += (entry.minutes_played or 0) / 60

    snapshots = session.scalars(
        select(PlaytimeSnapshot).order_by(
            PlaytimeSnapshot.steam_appid, PlaytimeSnapshot.captured_at
        )
    ).all()
    previous: dict[int, int] = {}
    monthly: defaultdict[str, float] = defaultdict(float)
    tracked = 0.0
    for snapshot in snapshots:
        old = previous.get(snapshot.steam_appid, 0)
        delta = max(0, snapshot.minutes - old)
        if old > 0 and delta > 0:
            monthly[snapshot.captured_at.strftime("%Y-%m")] += delta / 60
            tracked += delta / 60
        previous[snapshot.steam_appid] = snapshot.minutes
    months = sorted(monthly.items())[-12:]

    feedback = dict(
        session.execute(
            select(Recommendation.status, func.count(Recommendation.id)).group_by(
                Recommendation.status
            )
        ).all()
    )
    top_games = [
        (game, entry)
        for entry, game in sorted(entries, key=lambda row: row[0].minutes_played or 0, reverse=True)[:12]
    ]
    max_genre = max(genre_hours.values(), default=1) or 1
    max_month = max((value for _month, value in months), default=1) or 1
    return templates.TemplateResponse(
        request,
        "stats.html",
        _ctx(
            request,
            session,
            total_hours=round(total_minutes / 60, 1),
            tracked_hours=round(tracked, 1),
            genre_rows=genre_hours.most_common(10),
            months=months,
            top_games=top_games,
            feedback=feedback,
            max_genre=max_genre,
            max_month=max_month,
            count=len(entries),
        ),
    )


@router.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, session: Session = Depends(get_db)):
    from ..sources.steam import get_steam_id

    db_path = get_settings().db_file
    size = db_path.stat().st_size if db_path.exists() else 0
    cache_count = session.scalar(select(func.count(ApiCache.key))) or 0
    return templates.TemplateResponse(
        request,
        "settings.html",
        _ctx(
            request,
            session,
            steam_id=get_steam_id(session),
            db_path=db_path,
            db_size=size,
            cache_count=cache_count,
        ),
    )


@router.post("/settings/steamid")
def settings_steamid(steam_id: str = Form(...), session: Session = Depends(get_db)):
    set_steam_id(session, steam_id)
    return _redirect("/settings", "SteamID gespeichert.")


@router.post("/settings/backup")
def settings_backup():
    backup_dir = BASE_DIR / "data" / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    target = backup_dir / f"app-{utcnow().strftime('%Y%m%d-%H%M%S')}.db"
    with engine.connect() as connection:
        connection.exec_driver_sql(f"VACUUM INTO '{target}'")
    return _redirect("/settings", f"Backup erstellt: {target.name}")


@router.post("/settings/clear-cache")
def settings_clear_cache(session: Session = Depends(get_db)):
    session.query(ApiCache).delete()
    session.commit()
    return _redirect("/settings", "API-Cache geleert.")


@router.get("/jobs/{job_id}/panel", response_class=HTMLResponse)
def job_panel(job_id: int, request: Request, session: Session = Depends(get_db)):
    job = session.get(Job, job_id)
    if job is None:
        raise HTTPException(404, "Job nicht gefunden")
    response = _job_panel(request, job)
    if job.status == "done" and request.headers.get("HX-Request"):
        target = "/"
        if job.kind == "recommend":
            run_id = (job.result_json or {}).get("run_id")
            target = f"/recommendations?run_id={run_id}" if run_id else "/recommendations"
        elif job.kind == "ingest_sources":
            target = "/mentions"
        elif job.kind == "enrich_library":
            target = "/library"
        response.headers["HX-Redirect"] = target
    return response


@router.post("/jobs/sync-steam", response_class=HTMLResponse)
def job_sync_steam(request: Request, session: Session = Depends(get_db)):
    return _job_panel(request, _enqueue_once(session, "sync_steam"))


@router.post("/jobs/enrich", response_class=HTMLResponse)
def job_enrich(request: Request, session: Session = Depends(get_db)):
    return _job_panel(request, _enqueue_once(session, "enrich_library"))


@router.post("/jobs/ingest", response_class=HTMLResponse)
def job_ingest(
    request: Request,
    source_id: str = Form(""),
    session: Session = Depends(get_db),
):
    payload = {}
    if source_id.strip():
        try:
            payload["source_id"] = int(source_id)
        except ValueError:
            pass
    return _job_panel(request, _enqueue_once(session, "ingest_sources", payload))


@router.post("/jobs/profile", response_class=HTMLResponse)
def job_profile(request: Request, session: Session = Depends(get_db)):
    return _job_panel(request, _enqueue_once(session, "build_profile", {"force": True}))


@router.get("/healthz")
def healthz():
    return JSONResponse({"ok": True})

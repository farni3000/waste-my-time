from __future__ import annotations

from collections.abc import Callable
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..llm.deepseek import LLMError, rank_candidates
from ..llm.prompts import PROMPT_VERSION
from ..llm.schemas import LLMRecommendation
from ..models import Game, Recommendation, RecommendationRun
from ..timeutil import utcnow
from .candidates import Candidate, _weights, backlog_candidates, new_candidates
from .profile import build_profile

Progress = Callable[[int, str], None] | None


def _fallback_reason(profile, candidate: Candidate) -> str:
    weights = _weights(profile)
    positive = [
        tag
        for tag in (candidate.game.tags or []) + (candidate.game.genres or [])
        if weights.get(tag.lower(), 0) > 0.2
    ][:3]
    parts = []
    if positive:
        parts.append("Passt zu deinen Interessen an " + ", ".join(dict.fromkeys(positive)))
    else:
        parts.append("Hebt sich positiv vom Rest deiner Bibliothek ab")
    if candidate.game.review_score:
        parts.append(f"{candidate.game.review_score} % positive Steam-Bewertungen")
    if candidate.breakdown.get("editorial"):
        parts.append("wird aktuell in deinen Quellen empfohlen")
    return "; ".join(parts) + "."


def _filter_dismissed(session: Session, candidates: list[Candidate]) -> list[Candidate]:
    cutoff = utcnow() - timedelta(days=30)
    dismissed = set(
        session.scalars(
            select(Recommendation.game_id).where(
                Recommendation.status == "dismissed",
                Recommendation.created_at >= cutoff,
            )
        )
    )
    if not dismissed:
        return candidates
    filtered = [candidate for candidate in candidates if candidate.game.id not in dismissed]
    return filtered or candidates


def run_recommendation(
    session: Session,
    mode: str = "backlog",
    filters: dict | None = None,
    progress: Progress = None,
) -> RecommendationRun:
    filters = filters or {}
    run = RecommendationRun(
        mode=mode,
        filters_json=filters,
        prompt_version=PROMPT_VERSION,
        status="running",
    )
    session.add(run)
    session.commit()

    def report(percent: int, message: str) -> None:
        if progress:
            progress(percent, message)

    try:
        report(10, "Geschmacksprofil berechnen")
        profile = build_profile(session)
        run.profile_id = profile.id
        session.commit()

        report(25, "Kandidaten sammeln")
        if mode == "new":
            candidates = new_candidates(session, profile, filters)
        else:
            candidates = backlog_candidates(session, profile, filters)
        candidates = _filter_dismissed(session, candidates)
        run.candidate_count = len(candidates)
        session.commit()
        if not candidates:
            raise ValueError(
                "Keine passenden Kandidaten gefunden. Spiele zuerst deine Bibliothek, "
                "erfasse Interessen oder füge Quellen hinzu."
            )

        report(55, f"{len(candidates)} Kandidaten an DeepSeek senden")
        try:
            result = rank_candidates(session, profile, candidates, mode, filters)
            run.model = result.model
            run.input_tokens = result.input_tokens
            run.output_tokens = result.output_tokens
            run.raw_response = result.raw_response
            recommendations: list[LLMRecommendation] = result.recommendations
        except LLMError as exc:
            report(70, "DeepSeek nicht verfügbar, nutze lokale Rangliste")
            run.model = "heuristic-fallback"
            run.error = f"LLM-Fallback: {exc}"[:800]
            recommendations = [
                LLMRecommendation(
                    candidate_id=candidate.game.id,
                    rank=index,
                    reason=_fallback_reason(profile, candidate),
                    match_tags=[
                        tag
                        for tag in (candidate.game.tags or [])[:4]
                        if _weights(profile).get(tag.lower(), 0) > 0
                    ],
                    confidence=candidate.score,
                )
                for index, candidate in enumerate(candidates[:8], start=1)
            ]

        by_id = {candidate.game.id: candidate for candidate in candidates}
        created = 0
        for item in recommendations:
            candidate = by_id.get(item.candidate_id)
            if candidate is None:
                continue
            session.add(
                Recommendation(
                    run_id=run.id,
                    game_id=candidate.game.id,
                    rank=item.rank or created + 1,
                    score=round(candidate.score, 4),
                    reason=item.reason,
                    match_tags=item.match_tags[:8],
                )
            )
            created += 1
        run.status = "done"
        run.finished_at = utcnow()
        session.commit()
        report(100, f"{created} Vorschläge erstellt")
        return run
    except Exception as exc:
        run.status = "failed"
        run.error = str(exc)[:2000]
        run.finished_at = utcnow()
        session.commit()
        raise


def recommendation_games(session: Session, run_id: int) -> list[tuple[Recommendation, Game]]:
    rows = session.execute(
        select(Recommendation, Game)
        .join(Game, Game.id == Recommendation.game_id)
        .where(Recommendation.run_id == run_id)
        .order_by(Recommendation.rank.asc())
    ).all()
    return [(recommendation, game) for recommendation, game in rows]

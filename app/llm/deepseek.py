from __future__ import annotations

import json
from dataclasses import dataclass

import httpx
from sqlalchemy.orm import Session

from ..config import get_settings
from ..domain.candidates import Candidate
from ..domain.profile import TasteProfile
from .models import effective_effort, resolve_model
from .prompts import SYSTEM_PROMPT, USER_TEMPLATE
from .schemas import LLMRecommendation, LLMResponse


class LLMError(RuntimeError):
    pass


@dataclass
class LLMResult:
    recommendations: list[LLMRecommendation]
    raw_response: str
    input_tokens: int
    output_tokens: int
    model: str


def _candidate_payload(candidate: Candidate) -> dict:
    game = candidate.game
    return {
        "candidate_id": game.id,
        "title": game.title,
        "release": (game.release_date or "")[:10],
        "genres": (game.genres or [])[:4],
        "tags": (game.tags or [])[:8],
        "review_percent": game.review_score,
        "price_eur": round(game.price_cents / 100, 2) if game.price_cents is not None else None,
        "description": (game.short_description or "")[:240],
        "editorial_signal": candidate.breakdown.get("editorial", 0),
        "local_score": candidate.score,
    }


def _extract_json(content: str) -> dict:
    text = (content or "").strip()
    if text.startswith("```"):
        lines = text.splitlines()
        text = "\n".join(line for line in lines if not line.strip().startswith("```")).strip()
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("Keine JSON-Struktur in der Antwort gefunden.")
    return json.loads(text[start : end + 1])


def _call(
    messages: list[dict],
    temperature: float,
    model: str,
    effort: str = "",
) -> tuple[str, dict]:
    settings = get_settings()
    if not settings.deepseek_api_key:
        raise LLMError("DEEPSEEK_API_KEY fehlt in .env.")
    url = f"{settings.deepseek_base_url.rstrip('/')}/chat/completions"
    payload = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": settings.llm_max_tokens,
        "stream": False,
        "response_format": {"type": "json_object"},
    }
    if effort:
        payload["reasoning_effort"] = effort
    try:
        response = httpx.post(
            url,
            headers={
                "Authorization": f"Bearer {settings.deepseek_api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=120,
        )
    except httpx.HTTPError as exc:
        raise LLMError(f"DeepSeek nicht erreichbar: {exc}") from exc
    if response.status_code >= 400:
        raise LLMError(f"DeepSeek HTTP {response.status_code}: {response.text[:400]}")
    data = response.json()
    try:
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise LLMError(f"Unerwartete DeepSeek-Antwort: {str(data)[:400]}") from exc
    return content, data.get("usage") or {}


def rank_candidates(
    session: Session,
    profile: TasteProfile,
    candidates: list[Candidate],
    mode: str,
    filters: dict,
) -> LLMResult:
    settings = get_settings()
    if not candidates:
        raise LLMError("Keine Kandidaten für die KI-Auswahl vorhanden.")

    profile_payload = profile.summary_json or {}
    candidate_payload = [_candidate_payload(candidate) for candidate in candidates[: settings.llm_max_candidates]]
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {
            "role": "user",
            "content": USER_TEMPLATE.format(
                mode="Backlog (bereits besessen)" if mode == "backlog" else "Neu kaufen",
                filters=json.dumps(filters, ensure_ascii=False),
                profile=json.dumps(profile_payload, ensure_ascii=False),
                candidates=json.dumps(candidate_payload, ensure_ascii=False),
            ),
        },
    ]

    valid_ids = {candidate.game.id for candidate in candidates}
    model = resolve_model(session)
    effort = effective_effort(session, model)
    last_error: Exception | None = None
    content = ""
    usage: dict = {}
    for _attempt in range(2):
        content, usage = _call(messages, settings.llm_temperature, model, effort)
        try:
            parsed = LLMResponse.model_validate(_extract_json(content))
            valid = [
                item.model_copy(update={"confidence": max(0.0, min(1.0, item.confidence))})
                for item in parsed.recommendations
                if item.candidate_id in valid_ids
            ]
            seen: set[int] = set()
            unique = []
            for item in sorted(valid, key=lambda item: item.rank or 999):
                if item.candidate_id in seen:
                    continue
                seen.add(item.candidate_id)
                unique.append(item)
            if unique:
                return LLMResult(
                    recommendations=unique,
                    raw_response=content,
                    input_tokens=int(usage.get("prompt_tokens") or 0),
                    output_tokens=int(usage.get("completion_tokens") or 0),
                    model=model,
                )
            last_error = ValueError("Keine gültigen candidate_ids in der Antwort.")
        except Exception as exc:  # noqa: BLE001 - retry with feedback
            last_error = exc
        messages = messages[:2] + [
            {"role": "assistant", "content": content},
            {
                "role": "user",
                "content": (
                    "Die Antwort war ungültig. Antworte erneut ausschließlich mit JSON im "
                    "vorgegebenen Schema und nur mit candidate_id-Werten aus der Kandidatenliste."
                ),
            },
        ]
    raise LLMError(f"DeepSeek lieferte keine verwertbare Antwort: {last_error}")

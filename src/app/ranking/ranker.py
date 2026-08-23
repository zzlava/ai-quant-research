from __future__ import annotations

from app.models.scores import ScoreResult


def rank_scores(results: list[ScoreResult]) -> list[ScoreResult]:
    return sorted(results, key=lambda item: (-item.final_score, item.symbol))

"""Research-scope classifications and mandatory user-facing boundaries."""

from __future__ import annotations

PUBLIC_RECONSTRUCTION_SCOPE = "public_reconstruction"
PUBLIC_RECONSTRUCTION_CLASSIFICATION = "public_reconstructed_not_licensed_pit"
PUBLIC_RECONSTRUCTION_NOTICE = (
    "公开重建研究：成员关系来自本次查询取得的历史快照；source_date 不是已证明的 "
    "historical available_at。结果只能作非严格 PIT 的说明性模拟，不能与正式策略回测比较。"
)


def research_classification(scope: str) -> str:
    if scope == PUBLIC_RECONSTRUCTION_SCOPE:
        return PUBLIC_RECONSTRUCTION_CLASSIFICATION
    return "declared_research_scope"


def research_notice(scope: str) -> str | None:
    if scope == PUBLIC_RECONSTRUCTION_SCOPE:
        return PUBLIC_RECONSTRUCTION_NOTICE
    return None

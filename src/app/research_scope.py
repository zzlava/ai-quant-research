"""Research-scope classifications and mandatory user-facing boundaries."""

from __future__ import annotations

PUBLIC_RECONSTRUCTION_SCOPE = "public_reconstruction"
PUBLIC_RECONSTRUCTION_CLASSIFICATION = "public_reconstructed_not_licensed_pit"
PUBLIC_RECONSTRUCTION_NOTICE = (
    "公开重建研究：成员关系来自本次查询取得的历史快照；source_date 不是已证明的 "
    "historical available_at。结果只能作非严格 PIT 的说明性模拟，不能与正式策略回测比较。"
)
HISTORICAL_ALL_A_SHARE_SCOPE = "historical_all_a_share"
HISTORICAL_ALL_A_SHARE_CLASSIFICATION = "pit_derived_liquid_a_share"
HISTORICAL_ALL_A_SHARE_NOTICE = (
    "历史全 A 股派生研究：候选范围为当时已上市的沪深普通 A 股，成员资格仅使用当日收盘前可观察的 "
    "ST、停牌、上市天数和 20 日平均成交额派生；不依赖事后指数成分名单。"
)


def research_classification(scope: str) -> str:
    if scope == PUBLIC_RECONSTRUCTION_SCOPE:
        return PUBLIC_RECONSTRUCTION_CLASSIFICATION
    if scope == HISTORICAL_ALL_A_SHARE_SCOPE:
        return HISTORICAL_ALL_A_SHARE_CLASSIFICATION
    return "declared_research_scope"


def research_notice(scope: str) -> str | None:
    if scope == PUBLIC_RECONSTRUCTION_SCOPE:
        return PUBLIC_RECONSTRUCTION_NOTICE
    if scope == HISTORICAL_ALL_A_SHARE_SCOPE:
        return HISTORICAL_ALL_A_SHARE_NOTICE
    return None

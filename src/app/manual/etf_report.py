"""Self-contained HTML view of one ETF plan: weights vs targets and tolerance bands, plus the order ticket."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from html import escape
from pathlib import Path

from app.manual.etf_allocation import EtfPlan

ROLE_LABELS = {
    "a_share_broad_equity": "A股宽基",
    "overseas_equity_qdii": "海外股票（QDII）",
    "gold": "黄金",
    "cn_government_bond": "国债",
}

_CSS = """
.viz-root {
  color-scheme: light;
  --surface-0: #f5f5f3; --surface-1: #fcfcfb; --border: #e3e2de; --grid: #ecebe8;
  --band: #e6e5e1; --text-primary: #0b0b0b; --text-secondary: #52514e; --text-muted: #6f6e69;
  --series-1: #2a78d6; --series-2: #eb6834; --critical: #d03b3b; --good: #0ca30c;
}
@media (prefers-color-scheme: dark) {
  :root:where(:not([data-theme="light"])) .viz-root {
    color-scheme: dark;
    --surface-0: #121211; --surface-1: #1a1a19; --border: #33332f; --grid: #2a2a27;
    --band: #383835; --text-primary: #ffffff; --text-secondary: #c3c2b7; --text-muted: #9a9990;
    --series-1: #3987e5; --series-2: #d95926;
  }
}
:root[data-theme="dark"] .viz-root {
  color-scheme: dark;
  --surface-0: #121211; --surface-1: #1a1a19; --border: #33332f; --grid: #2a2a27;
  --band: #383835; --text-primary: #ffffff; --text-secondary: #c3c2b7; --text-muted: #9a9990;
  --series-1: #3987e5; --series-2: #d95926;
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--surface-0); }
.viz-root { background: var(--surface-0); color: var(--text-primary); min-height: 100vh;
  font: 14px/1.5 -apple-system, "PingFang SC", "Microsoft YaHei", system-ui, sans-serif; padding: 24px 16px; }
main { max-width: 960px; margin: 0 auto; display: grid; gap: 16px; }
main > * { min-width: 0; }
.chart-wrap { overflow-x: auto; }
.chart-wrap svg { min-width: 640px; }
h1 { font-size: 20px; margin: 0; } h2 { font-size: 16px; margin: 0 0 12px; }
.sub { color: var(--text-secondary); margin: 4px 0 0; font-size: 13px; }
.card { background: var(--surface-1); border: 1px solid var(--border); border-radius: 10px; padding: 16px; }
.tiles { display: grid; grid-template-columns: repeat(auto-fit, minmax(min(180px, 100%), 1fr)); gap: 12px; }
.tile .label { color: var(--text-secondary); font-size: 13px; }
.tile .value { font-size: 24px; font-weight: 600; font-variant-numeric: tabular-nums; }
.tile .note { color: var(--text-muted); font-size: 12px; }
.status { display: inline-flex; gap: 6px; align-items: center; font-weight: 600; }
.status.act::before { content: "●"; color: var(--critical); }
.status.hold::before { content: "●"; color: var(--good); }
.legend { display: flex; flex-wrap: wrap; gap: 16px; color: var(--text-secondary); font-size: 13px;
  margin-bottom: 8px; }
.legend span { display: inline-flex; align-items: center; gap: 6px; }
.sw { width: 12px; height: 12px; border-radius: 3px; display: inline-block; }
.sw.tick { width: 3px; height: 14px; border-radius: 1px; background: var(--text-primary); }
svg { width: 100%; height: auto; display: block; overflow: visible; }
svg text { fill: var(--text-secondary); font-size: 12px; }
svg .name { fill: var(--text-primary); font-size: 13px; }
svg .val { fill: var(--text-primary); font-variant-numeric: tabular-nums; }
svg .flag { fill: var(--critical); font-weight: 600; }
.row-hit { fill: transparent; cursor: default; }
.row-bg { fill: transparent; }
.row-bg.on { fill: var(--grid); }
.tip { position: fixed; pointer-events: none; background: var(--surface-1); color: var(--text-primary);
  border: 1px solid var(--border); border-radius: 8px; padding: 8px 10px; font-size: 12px; line-height: 1.6;
  box-shadow: 0 4px 16px rgb(0 0 0 / .15); display: none; z-index: 10; white-space: nowrap; }
.table-wrap { overflow-x: auto; }
table { width: 100%; border-collapse: collapse; font-variant-numeric: tabular-nums; }
th, td { text-align: right; padding: 6px 8px; border-bottom: 1px solid var(--grid); white-space: nowrap; }
th:first-child, td:first-child, th:nth-child(2), td:nth-child(2) { text-align: left; }
th { color: var(--text-secondary); font-weight: 500; font-size: 13px; }
.buy { color: var(--series-1); font-weight: 600; } .sell { color: var(--series-2); font-weight: 600; }
ul.checks { margin: 4px 0 0; padding-left: 18px; color: var(--text-secondary); font-size: 12px; text-align: left;
  white-space: normal; min-width: 240px; }
.warn { color: var(--text-secondary); font-size: 13px; margin: 4px 0; }
.warn b { color: var(--critical); }
"""

_JS = """
const tip = document.querySelector('.tip');
document.querySelectorAll('.row-hit').forEach((el) => {
  el.addEventListener('mousemove', (e) => {
    tip.innerHTML = el.dataset.tip;
    document.querySelectorAll('.row-bg').forEach((bg) => bg.classList.toggle('on', bg.dataset.row === el.dataset.row));
    tip.style.display = 'block';
    const x = Math.min(e.clientX + 14, window.innerWidth - tip.offsetWidth - 8);
    tip.style.left = x + 'px';
    tip.style.top = (e.clientY + 14) + 'px';
  });
  el.addEventListener('mouseleave', () => {
    tip.style.display = 'none';
    document.querySelectorAll('.row-bg.on').forEach((bg) => bg.classList.remove('on'));
  });
});
"""


def _pct(value: Decimal) -> str:
    return f"{value * 100:.1f}%"


def _money(value: Decimal) -> str:
    return f"{value:,.2f}"


@dataclass(frozen=True)
class _Row:
    name: str
    detail: str
    current: Decimal
    target: Decimal
    band: Decimal | None
    post: Decimal | None
    out: bool


def _weights_chart(plan: EtfPlan) -> str:
    has_orders = bool(plan.orders)
    post = {leg.symbol: leg.weight for leg in plan.post_trade}
    cash_target = Decimal("1") - sum((leg.target_weight for leg in plan.legs), Decimal("0"))
    rows = [
        _Row(
            name=f"{leg.symbol[:6]} {ROLE_LABELS.get(leg.role, leg.role)}",
            detail=f"持有 {leg.quantity} 份 · 市值 {_money(leg.value)} 元",
            current=leg.weight,
            target=leg.target_weight,
            band=leg.band,
            post=post.get(leg.symbol) if has_orders else None,
            out=leg.out_of_band,
        )
        for leg in plan.legs
    ]
    rows.append(
        _Row(
            name="现金",
            detail=f"{_money(plan.cash)} 元",
            current=plan.cash_weight,
            target=cash_target,
            band=None,
            post=plan.post_trade_cash_weight if has_orders else None,
            out=False,
        )
    )
    peak = max(
        max(float(r.current), float(r.target + (r.band or 0)), float(r.post or 0)) for r in rows
    )
    x_max = max(0.6, (int(peak * 10) + 1) / 10)
    left, right, width = 170, 130, 900
    plot = width - left - right
    row_h = 52 if has_orders else 40
    top = 24
    height = top + row_h * len(rows) + 8

    def x(value: Decimal | float) -> float:
        return left + plot * min(max(float(value), 0.0), x_max) / x_max

    parts = [f'<svg viewBox="0 0 {width} {height}" role="img" aria-label="各资产当前权重、目标权重与容忍带">']
    for step in range(int(x_max * 10) + 1):
        gx = x(step / 10)
        parts.append(f'<line x1="{gx:.1f}" x2="{gx:.1f}" y1="{top - 6}" y2="{height - 8}" stroke="var(--grid)"/>')
        parts.append(f'<text x="{gx:.1f}" y="{top - 10}" text-anchor="middle">{step * 10}%</text>')
    for index, row in enumerate(rows):
        y0 = top + index * row_h
        bar_y = y0 + 8
        parts.append(
            f'<rect class="row-bg" data-row="{index}" x="0" y="{y0}" width="{width}" height="{row_h}" rx="6"/>'
        )
        tip = [f"<b>{escape(row.name)}</b>", escape(row.detail)]
        tip += [f"当前 {_pct(row.current)}", f"目标 {_pct(row.target)}"]
        if row.band is not None:
            lo, hi = row.target - row.band, row.target + row.band
            tip.append(f"容忍带 {_pct(max(lo, Decimal('0')))} – {_pct(hi)}")
            parts.append(
                f'<rect x="{x(lo):.1f}" y="{y0 + 4}" width="{x(hi) - x(lo):.1f}" height="{row_h - 12}" '
                'rx="4" fill="var(--band)"/>'
            )
        parts.append(
            f'<rect x="{left}" y="{bar_y}" width="{max(x(row.current) - left, 0):.1f}" height="12" rx="4" '
            'fill="var(--series-1)"/>'
        )
        value_x = width - right + 12
        parts.append(f'<text class="val" x="{value_x}" y="{bar_y + 10}">{_pct(row.current)}</text>')
        if row.post is not None:
            tip.append(f"调仓后 {_pct(row.post)}")
            post_y = bar_y + 16
            parts.append(
                f'<rect x="{left}" y="{post_y}" width="{max(x(row.post) - left, 0):.1f}" height="12" rx="4" '
                'fill="var(--series-2)"/>'
            )
            parts.append(f'<text class="val" x="{value_x}" y="{post_y + 10}">→ {_pct(row.post)}</text>')
        parts.append(
            f'<line x1="{x(row.target):.1f}" x2="{x(row.target):.1f}" y1="{y0 + 2}" y2="{y0 + row_h - 6}" '
            'stroke="var(--text-primary)" stroke-width="2"/>'
        )
        parts.append(f'<text class="name" x="0" y="{bar_y + 10}">{escape(row.name)}</text>')
        if row.out:
            parts.append(f'<text class="flag" x="0" y="{bar_y + 28}">▲ 超出容忍带</text>')
            tip.append("<b>超出容忍带</b>")
        parts.append(
            f'<rect class="row-hit" data-row="{index}" x="0" y="{y0}" width="{width}" height="{row_h}" '
            f'data-tip="{escape("<br>".join(tip))}"/>'
        )
    parts.append("</svg>")
    return "".join(parts)


def render_plan_html(plan: EtfPlan) -> str:
    acting = bool(plan.orders)
    status = (
        f'<span class="status act">需要下单 {len(plan.orders)} 笔</span>'
        if acting
        else '<span class="status hold">今天不用交易</span>'
    )
    risk_now = sum((leg.weight for leg in plan.legs if leg.role != "cn_government_bond"), Decimal("0"))
    risk_target = sum((leg.target_weight for leg in plan.legs if leg.role != "cn_government_bond"), Decimal("0"))
    triggers = "、".join(plan.triggers) if plan.triggers else "无"
    legend = (
        '<div class="legend"><span><i class="sw" style="background:var(--series-1)"></i>当前权重</span>'
        + ('<span><i class="sw" style="background:var(--series-2)"></i>调仓后权重</span>' if acting else "")
        + '<span><i class="sw tick"></i>目标权重</span>'
        '<span><i class="sw" style="background:var(--band)"></i>容忍带（在带内不交易）</span></div>'
    )
    order_rows = "".join(
        f"<tr><td>{i}</td><td class=\"{o.side}\">{'卖出' if o.side == 'sell' else '买入'} "
        f"{escape(o.symbol[:6])} {escape(o.name)}<ul class=\"checks\">"
        + "".join(f"<li>{escape(c)}</li>" for c in o.manual_checks)
        + f"</ul></td><td>{o.quantity}</td><td>{o.limit_price}</td><td>{_money(o.notional)}</td>"
        f"<td>{o.est_commission}</td></tr>"
        for i, o in enumerate(plan.orders, start=1)
    )
    orders_html = (
        '<section class="card"><h2>调仓单（按顺序手动下限价单，先卖后买）</h2><div class="table-wrap"><table>'
        "<thead><tr><th>#</th><th>操作</th><th>份数</th><th>限价</th><th>金额（元）</th><th>预估佣金</th></tr></thead>"
        f"<tbody>{order_rows}</tbody></table></div>"
        f'<p class="sub">成交后记账：<code>etf record-fill … --plan-id {escape(plan.plan_id)}</code></p></section>'
        if acting
        else ""
    )
    leg_rows = "".join(
        f"<tr><td>{escape(leg.symbol)}</td><td>{escape(ROLE_LABELS.get(leg.role, leg.role))}</td>"
        f"<td>{leg.quantity}</td><td>{_money(leg.value)}</td><td>{_pct(leg.weight)}</td>"
        f"<td>{_pct(leg.target_weight)}</td><td>±{_pct(leg.band)}</td>"
        f"<td>{'超出' if leg.out_of_band else '带内'}</td></tr>"
        for leg in plan.legs
    )
    notes = "".join(f'<p class="warn"><b>⛔</b> {escape(item)}</p>' for item in plan.blocked) + "".join(
        f'<p class="warn">⚠️ {escape(item)}</p>' for item in plan.warnings
    )
    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>ETF 调仓单 {escape(str(plan.as_of))}</title>
<style>{_CSS}</style></head>
<body><div class="viz-root"><main>
<header><h1>多资产 ETF 调仓单 · {escape(str(plan.as_of))}</h1>
<p class="sub">策略 {escape(plan.policy_version)} · plan_id {escape(plan.plan_id)}
 · 只生成手动调仓单，不连接券商</p></header>
<section class="tiles">
  <div class="card tile"><div class="label">账户总值</div><div class="value">{_money(plan.total_value)}</div>
    <div class="note">元，含现金 {_money(plan.cash)}</div></div>
  <div class="card tile"><div class="label">风险资产合计</div><div class="value">{_pct(risk_now)}</div>
    <div class="note">目标 {_pct(risk_target)}（股票 + 黄金）</div></div>
  <div class="card tile"><div class="label">今天的结论</div><div class="value" style="font-size:18px">{status}</div>
    <div class="note">触发原因：{escape(triggers)}</div></div>
</section>
<section class="card"><h2>当前权重 vs 目标</h2>{legend}<div class="chart-wrap">{_weights_chart(plan)}</div></section>
{orders_html}
<section class="card"><h2>持仓明细</h2><div class="table-wrap"><table>
<thead><tr><th>代码</th><th>资产</th><th>份数</th><th>市值（元）</th><th>当前</th><th>目标</th><th>容忍带</th><th>状态</th></tr></thead>
<tbody>{leg_rows}<tr><td>现金</td><td>—</td><td>—</td><td>{_money(plan.cash)}</td><td>{_pct(plan.cash_weight)}</td>
<td>—</td><td>—</td><td>—</td></tr></tbody></table></div></section>
<section class="card">{notes}<p class="warn">权重是事先设定的，没有经过回测优化，不保证收益。</p></section>
</main><div class="tip" role="tooltip"></div></div>
<script>{_JS}</script></body></html>
"""


def save_plan_html(plan: EtfPlan, plan_json_path: Path) -> Path:
    path = plan_json_path.with_suffix(".html")
    path.write_text(render_plan_html(plan), encoding="utf-8")
    return path

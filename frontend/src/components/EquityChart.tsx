import type { EquityPoint } from "../api/types";

type Props = {
  points: EquityPoint[];
};

export function EquityChart({ points }: Props) {
  if (points.length === 0) {
    return <p className="empty">无权益曲线。后端未返回 equity_curve，界面不会补造收益。</p>;
  }

  const width = 640;
  const height = 180;
  const pad = 12;
  const values = points.map((point) => point.equity);
  const min = Math.min(...values);
  const max = Math.max(...values);
  const span = max - min || 1;
  const coords = points.map((point, index) => {
    const x = pad + (index / Math.max(points.length - 1, 1)) * (width - pad * 2);
    const y = height - pad - ((point.equity - min) / span) * (height - pad * 2);
    return `${x.toFixed(1)},${y.toFixed(1)}`;
  });

  return (
    <figure className="chart">
      <svg viewBox={`0 0 ${width} ${height}`} role="img" aria-label="权益曲线">
        <polyline fill="none" stroke="currentColor" strokeWidth="2" points={coords.join(" ")} />
      </svg>
      <figcaption>
        {points[0].date} → {points[points.length - 1].date} · 点位来自后端 equity_curve
      </figcaption>
    </figure>
  );
}

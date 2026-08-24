# CSI300 public reconstruction reversal V3 experiment

This is an explanatory experiment, not evidence of an investable strategy.
Membership remains `public_reconstruction`: `source_date` has not been proven as historical
`available_at`, so no result from this experiment is a strict point-in-time backtest.

## Frozen research protocol

- Data snapshot: record the runtime `data_snapshot_id` in every generated report.
- Training decisions: 2022-04-01 through 2023-06-30.
- Longest training label: 10 later trading days. Holdout begins 2023-07-17 to prevent label overlap.
- Holdout decisions/backtest: 2023-07-17 through 2024-12-31.
- Candidate count: two, both selected from training-only diagnostics.
- Candidate hypothesis: the original V2 blend triple-counts related 20-day trend inputs;
  training IC showed that blend and its medium-term components had negative 5/10-day IC.
- Candidate A frozen change: `alpha_style: medium_term_reversal`, implemented as
  `100 - momentum_alpha`.
- Candidate B frozen change: Candidate A plus `attention_penalty: 0.25`. This uses the same
  coefficient as the existing crowding/execution penalties and is not numerically optimized.
- Unchanged controls: universe, regime gate, risk penalties, score threshold, position sizing,
  ATR exits, holding period, cooldown, commissions, stamp tax, slippage, and execution prices.

Only a candidate with positive training net return after declared costs may be evaluated once on
the holdout. Do not change either V3 configuration after reading holdout results. Any later
hypothesis must use a new config ID and a new untouched evaluation window.

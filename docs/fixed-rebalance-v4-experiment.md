# Fixed-rebalance V4 training protocol

This remains a `public_reconstruction` experiment and is not strict-PIT evidence.

The V3 reversal score showed positive execution-aligned Top-3 average estimated net returns at
both 5 and 10 trading-day horizons, while daily Top-3 membership turnover was about 51%. The V3
path-dependent engine instead replenished positions after early exits and lost after costs.

V4 changes execution only:

- keep the V3 reversal score, risk penalties, threshold, universe, regime gate, costs and Top-3;
- score every 11 trading days from the explicit `2022-04-01` anchor;
- enter at the next trading-day open;
- hold for 10 complete eligible trading days;
- exit at the tenth eligible-day close without an earlier TP/SL;
- immediately score after the old basket exits, then enter the next basket on the following day.

Training remains `2022-04-01..2023-06-30`. The holdout remains untouched from `2023-07-17`.
V4 may enter the holdout once only if training net return after all declared costs is positive.

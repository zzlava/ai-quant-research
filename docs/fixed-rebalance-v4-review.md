# Fixed-rebalance V4 review

Decision: reject V4 after its single frozen holdout evaluation.

All results use data snapshot
`b56e5f3e78252f4a871e234ad945e728a96cc00259e712441b5c68e35427b9f9` and configuration hash
`a311e33948bf4c56`. Membership is `public_reconstruction`, not strict PIT.

## Training gate: completed cycles through 2023-06-26

- final equity `80241.34`, total return `+0.3017%`, with no open positions;
- 32 closed trades, all fixed-horizon exits;
- gross realized PnL `+2297.00`;
- explicit costs plus estimated slippage `2055.66`;
- maximum drawdown `-10.6335%` and Sharpe `0.0871`.

The result passed the predeclared minimum gate of positive net training return, but its margin over
cost was only `241.34`. An initial run through `2023-06-30` reported `+1.2438%` but retained two
marked-to-market positions and therefore was not used as the final gate measurement.

## Frozen holdout: 2023-07-17..2024-12-31

- final equity `68176.89`, total return `-14.7789%`;
- 43 closed trades, all fixed-horizon exits;
- gross realized PnL `-8611.00`;
- explicit costs plus estimated slippage `1929.40`;
- maximum drawdown `-19.9088%` and Sharpe `-0.5800`.

The holdout loss existed before costs, so it is not a fee-model artifact.

Post-hoc diagnostics, which must not be used to tune V4, found that daily 10-day Top-3 estimated
net return was approximately zero (`-0.000003`) and the top-minus-bottom decile spread was
negative (`-0.002734`). The scheduled subset used by V4 had negative gross PnL. This is consistent
with a weak and regime-unstable training effect rather than a robust cross-sectional edge.

V4 is frozen and rejected. Any new hypothesis requires a new config ID and a new evaluation
protocol; this holdout can no longer be treated as untouched for related reversal strategies.

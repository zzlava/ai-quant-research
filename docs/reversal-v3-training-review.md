# Reversal V3 training review

Decision: reject both experimental candidates before holdout evaluation.

This review uses the public-reconstruction CSI300 collection and therefore is not strict PIT
evidence. It is a reproducible diagnostic tied to:

- data snapshot `b56e5f3e78252f4a871e234ad945e728a96cc00259e712441b5c68e35427b9f9`;
- training decisions `2022-04-01..2023-06-30`;
- longest label horizon 10 later trading days;
- untouched holdout start `2023-07-17`.

## Direction audit

The IC label is `future adjusted close / decision-day adjusted close - 1`. Spearman correlation
uses ascending ranks for both factor and label, while production ranking sorts score descending.
There is no label or rank sign inversion in the diagnostic.

V2 `alpha_score` triple-counts closely related medium-term trend inputs: 20-day relative strength,
distance from MA20, and 20-day return. On the training period, its mean IC was `-0.032631` at five
days and `-0.047795` at ten days. The 10-day result was also negative in every 126-scoring-day
window that was available.

## Candidate A: medium-term reversal

Config: `csi300_bigquant_public_reconstruction_reversal_v3`, hash `3e8101157b44aff5`.

- Training final-score IC: `+0.031069` at five days and `+0.045494` at ten days.
- Initial capital: `80000.00`; final equity: `78068.16`; total return: `-2.4148%`.
- Gross realized PnL: `+2499.92`.
- Explicit costs plus estimated slippage: `5383.92`.
- Trades: `92`; maximum drawdown: `-16.6383%`.

The rank signal had positive gross evidence but did not survive declared transaction costs.

## Candidate B: reversal plus attention penalty

Config: `csi300_bigquant_public_reconstruction_reversal_attention_v3`, hash `2c6a2744503d97cd`.

- Training final-score IC: `+0.031351` at five days and `+0.045658` at ten days.
- Initial capital: `80000.00`; final equity: `64647.93`; total return: `-19.1901%`.
- Gross realized PnL: `-9840.10`.
- Explicit costs plus estimated slippage: `5814.43`.
- Trades: `97`; maximum drawdown: `-31.4469%`.

The small all-name IC improvement did not improve the selected Top-3 trading path.

## Gate outcome

The frozen protocol requires positive training net return after declared costs before one holdout
evaluation. Neither candidate passed. The holdout was not queried, so it remains available for a
future independently motivated hypothesis. Do not tune these candidates against the holdout.

# AI Trading Competition Rules

Source: local HTML export in the repository root.

All dates and times are British Summer Time (BST).

## Purpose

- Simulated trading competition using real market quotes and a real liquidity-style environment.
- Goal: reward reproducible AI, quant, hybrid, or human-assisted trading systems that generate return while managing risk.
- Strategy intent is not judged. Ranking, elimination, and review use objective, computable metrics.

## Account Rules

| Item | Rule |
| --- | --- |
| Account type | Simulated trading account |
| Initial funds | USD 1,000,000 |
| Maximum leverage | 30x |
| Stop-out level | 30% margin level; positions are force-liquidated |
| Environment | Unified market data, order matching, and account conditions |
| Ranking basis | Equity, return, max drawdown, Sharpe ratio, risk discipline |
| Principal risk | No real principal risk |

The platform must not individually adjust price feeds based on one participant's trading behavior.

## Tradable Assets

- Forex: `AUD/USD`, `EUR/CHF`, `EUR/GBP`, `EUR/USD`, `GBP/USD`, `USD/CAD`, `USD/CHF`, `USD/JPY`
- Metals: `XAG/USD`, `XAU/USD`
- Crypto: `BAR/USD`, `BTC/USD`, `ETH/USD`, `SOL/USD`, `XRP/USD`

## Schedule

| Date | Phase | Key Details |
| --- | --- | --- |
| 15 Jun | Opening / Rules Announcement | 17:00-20:00 portal, historical data, sponsor toolkits, and credentials available; trading disabled |
| 18 Jun | Registration Deadline | 22:00 second registration deadline |
| 21 Jun | Official Launch | 22:00 competition starts; accounts initialized equally |
| 22 Jun | Round 1 Conclusion | 22:00 ranking snapshot; 22:00-23:00 compliance review |
| 23 Jun | Round 2 Conclusion | 22:00 ranking snapshot; 22:00-23:00 compliance review |
| 24 Jun | Round 3 Conclusion | 22:00 ranking snapshot; 22:00-23:00 compliance review |
| 24-26 Jun | Finals | 24 Jun 22:00 to 26 Jun 22:00; top 100 compete |
| 26 Jun | Post-Finals Audit | 22:00-23:00 anomaly detection, ranking confirmation, log review |
| 27 Jun | Results / Awards | Final rankings, highlights, awards |

Qualification counts for Rounds 1-3 are TBC in the source rules.

## Data And Execution

- Historical market data is provided for backtesting, model training, tuning, and execution preparation.
- The platform may not include a complete built-in backtesting engine.
- Native AI Agents may provide basic evaluation/backtesting support with less flexibility than a self-built framework.
- Quotes aggregate multiple broker/liquidity sources with risk-pricing logic.
- Quotes are not skewed for individual participants.
- Market and pending orders may experience depth limits, partial fills, slippage, and market impact.

## Transparency

- During elimination phase, 21 Jun to 24 Jun, participants can view leaderboards, peer trading logs, positions, performance, and risk metrics with 5-minute latency.
- After each round, 22:00-23:00, snapshots freeze for ranking, trade/risk metrics compile, and anomaly detection runs.
- If anomalies are flagged, the organizer discloses anomaly type, criteria, Trade/Order IDs, and qualification impact on Discord.
- During finals, peer logs, positions, and live leaderboard are blinded. Participants only see their own account state and risk metrics.
- After competition closure, final standings, key metrics, verified logs, required Trade/Order IDs, penalties, and dispute rulings are published.
- PII remains undisclosed.

## Technology Prize Eligibility

After Round 3 elimination on 24 Jun, eligible participants should provide:

- GitHub repository link for project code.
- Partner technologies used and how they were applied.
- Data usage details.
- Demo of how the project works.

Participants retain project IP. Access is requested for judging fairness and integrity.

## Ranking

Final standings use a formula-based composite score. No subjective penalties or discretionary deductions apply.

```
Final Score = 70% * Return Rank
            + 15% * Drawdown Rank
            + 10% * Sharpe Rank
            + 5% * Risk Discipline
```

`Rank` means percentile or absolute rank among active participants for the metric.

## Metrics

Return:

```
Return_i = (Equity_final_i - Equity_initial) / Equity_initial
Equity_initial = 1,000,000 USD
```

Return Rank:

```
Return Rank_i = 100 * (N - Rank_i) / (N - 1)
```

- Rank by `Return_i` descending.
- If `N = 1`, Return Rank is 100.

Maximum Drawdown (MaxDD):

```
MaxDD_i = max_t((PeakEquity_i_t - Equity_i_t) / PeakEquity_i_t)
```

Drawdown Rank:

```
Drawdown Rank_i = 100 * (N - RankDD_i) / (N - 1)
```

- Rank by `MaxDD_i` ascending.

Sharpe Ratio:

```
r_i_t = (Equity_i_t - Equity_i_t-1) / Equity_i_t-1
Sharpe_i = Mean(r_i_t) / Std(r_i_t)
```

- Non-annualized.
- Uses 15-minute account-equity returns.
- If `Std(r_i_t) = 0`, Sharpe is 0.
- If fewer than 8 valid 15-minute return observations exist, final Sharpe Rank is capped at 50.

Sharpe Rank:

```
Sharpe Rank_i = 100 * (N - RankSharpe_i) / (N - 1)
```

- Rank by `Sharpe_i` descending.

## Risk Discipline

- Each participant starts each round with Risk Discipline = 100.
- Deductions are per round and floor at 0.
- Risk Discipline resets each round unless a red-line violation occurs.
- Forced liquidation, system vulnerability exploitation, API abuse, multi-accounting, or fairness manipulation can directly disqualify.

Margin Usage:

```
Margin Usage_i = Used Margin_i / Equity_i
```

| Violation | Penalty |
| --- | --- |
| `Margin Usage_i > 90%` for `>= 30 min` | -20 |
| `Margin Usage_i > 95%` for `>= 15 min` | -30 |
| `Margin Usage_i > 98%` for `>= 10 min` | Compliance review |

Leverage Usage:

```
Leverage_i = Gross Notional Exposure_i / Equity_i
```

| Violation | Penalty |
| --- | --- |
| `Leverage_i > 28x` for `>= 30 min` | -20 |
| `Leverage_i > 29x` for `>= 15 min` | -30 |
| `Leverage_i` approaching `30x` for `>= 10 min` | Compliance review |

Exposure Concentration:

```
Single Instrument Exposure_i = Notional Exposure_single / Gross Notional Exposure_i
```

| Violation | Penalty |
| --- | --- |
| Single-instrument exposure `> 90%` for `>= 30 min` | -10 |
| Net directional exposure `> 95%` for `>= 30 min` | -10 |

Directional trading is allowed. The rules target prolonged, extremely concentrated, near-full-leverage risk.

## Red-Line Rules

- Forced liquidation: immediate elimination; no advancement.
- Inactive account: eliminated if not activated by login within 8 hours after competition start.
- System vulnerability exploitation: immediate disqualification.
- API abuse: immediate disqualification.
- Multi-account participation by same user: immediate disqualification.
- Unauthorized collaboration or collusion to manipulate rankings: prohibited.

API abuse includes flooding endpoints, bypassing rate limits, attacking/interfering with services, unauthorized access, or high-frequency requests that cause anomalies.

Safe harbor: requests at or below 500 per second are not automatically abnormal, but may still be reviewed if they cause system anomalies, bypass limits, or affect fairness.

## Elimination And Qualification

At each round conclusion:

1. 22:00: snapshot equity, positions, trading logs, and risk metrics.
2. 22:00-23:00: run anomaly detection, verify red-line rules, purge disqualified or force-liquidated accounts.
3. Calculate metrics and Final Score for remaining active accounts.
4. Sort participants by Final Score descending.
5. Finalize qualification roster and disclose anomalies/rulings on Discord.

| Round | Trading Cutoff | Audit Window | Status |
| --- | --- | --- | --- |
| Round 1 | 22 Jun 22:00 | 22:00-23:00 | Qualifiers TBC |
| Round 2 | 23 Jun 22:00 | 22:00-23:00 | Qualifiers TBC |
| Round 3 | 24 Jun 22:00 | 22:00-23:00 | Qualifiers TBC |
| Finals | 26 Jun 22:00 | 22:00-23:00 | Final ranking |

## Tie-Breaking

If Final Scores tie, resolve in this order:

1. Higher `Return_i`.
2. Lower `MaxDD_i`.
3. Higher `Sharpe_i`.
4. Higher Risk Discipline.
5. More reasonable trading activity.

If still tied, the organizer reviews and publishes the basis.

## Best Sharpe Ratio Award

- Prize: USD 10,000.
- Winner: eligible participant with the highest non-annualized Sharpe Ratio.

Eligibility:

- Reach the Finals.
- Finish top 50 in final overall ranking.
- No red-line violations.
- Execute at least 30 trades.

Tie-breakers for this award:

1. Higher Final Return.
2. Lower Maximum Drawdown.

## Appeals And Disclosure

- Participants may file reasonable appeals during or after the competition.
- For ambiguous disputes, the organizer may disclose redacted facts, Trade/Order IDs, organize discussion, and where needed put the matter to participant vote.
- The organizer safeguards fairness and integrity; it does not judge strategy intent.

For any penalty, elimination, or disqualification, the organizer discloses:

- Reason for the penalty.
- Basis for the determination.
- Relevant Trade/Order IDs.
- Ranking impact.

Personal sensitive information is not disclosed.

## Reserved Rights

The organizer may suspend, adjust, review, or modify arrangements due to:

- System failures.
- Market-data or quote anomalies.
- Matching or settlement anomalies.
- API-service anomalies.
- Force majeure or technical issues affecting fairness.

The organizer will publish reasons, impact, and resolution as transparently as possible.

## Terms

The HTML rules are a summary. Participation is governed by the full Terms & Conditions:

https://docs.google.com/document/d/1v_YoSMluzZskZ7hD4ccnEu3m-SfD8GHF/edit
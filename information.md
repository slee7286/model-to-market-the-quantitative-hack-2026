# General Notes

Source: local `General Notes.html` Notion export.

This file is a compact Codex-readable summary. For official rule thresholds and scoring formulas, use `rules.md`.

## Event Snapshot

- Event: Model to Market: The Quantitative Hack.
- Positioning: UK's first live AI-native trading competition.
- Dates: 15-27 June 2026.
- Format: solo only; one-person teams.
- Prize pool: USD 100,000 cash, hardware, and AI credits.
- Starting capital: USD 1,000,000 virtual funds per participant.
- Markets: FX, gold, silver, and crypto.
- Prizes are real; trading funds are simulated.

## Schedule

| Date | Event |
| --- | --- |
| 15 Jun | Kick-off drinks; portal, historical data, toolkits, and credentials open |
| 15-21 Jun | Strategy development / prep |
| 18 Jun 22:00 BST | Registration deadline |
| 19 Jun 08:00 BST | Trading channel selection opens |
| 21 Jun 22:00 BST | Competition trading starts |
| 21-24 Jun | Live trading elimination rounds |
| 24 Jun 22:00 BST | Round 3 ends; top 100 advance |
| 24-26 Jun | Finals |
| 27 Jun | Results and awards in London |

## Trading Channels

- Two channels exist: MetaTrader 5 (`MT5`) and AI Native Interface.
- Channel selection is irreversible after confirmation.
- If API connectivity is required, choose `MT5`.
- `MT5` supports API integration and external systems.
- AI Native does not provide API access or API key creation.
- Trading method selection opens 19 Jun at 08:00 BST.

## API And Automation

- Programmatic trading uses standard MT5 tooling with Console -> Trading Setup credentials: login, password, server.
- No separate REST/WebSocket order endpoint or API key is issued by the organizers.
- A common Python path is the official `MetaTrader5` package.
- The MT5 Python integration appears to be Windows-only.
- External infrastructure is allowed. Bots do not need to run inside the platform environment.
- If running external AI/agent systems, actual execution must go through MT5.
- Permitted tooling includes Pydantic AI, Claude/LLM APIs, news/sentiment APIs, local Python, and other external infrastructure.
- Avoid abusive request patterns. Detailed platform rate/safe-harbor limits are in `rules.md`.

## Tradable Instruments

Forex:

- `AUD/USD`
- `EUR/CHF`
- `EUR/GBP`
- `EUR/USD`
- `GBP/USD`
- `USD/CAD`
- `USD/CHF`
- `USD/JPY`

Metals:

- `XAG/USD`
- `XAU/USD`

Crypto:

- `BAR/USD` means HBAR/Hedera.
- `BTC/USD`
- `ETH/USD`
- `SOL/USD`
- `XRP/USD`

Notes:

- Crypto is available in live competition.
- Initial backtest data may not include crypto history.
- Final symbol strings, contract sizes, tick sizes, volume steps, and margin details are available in MT5 at login.

## Execution Model

- Trading uses real market data and pricing, but orders are simulated and do not affect the live market order book.
- The environment is not an isolated single-player simulator; participants compete directly by performance and progression.
- Pricing aggregates liquidity from multiple brokers plus risk-pricing logic.
- All participants should see the same bid/ask prices at the same time.
- Orders are order-book based, not dealing-desk.
- Passive/pending limit orders can rest as liquidity.
- Marketable orders and resting orders fill by available liquidity and queue position.
- Partial fills can occur.
- Larger orders can consume multiple depth levels; fills are calculated across the available liquidity.
- Slippage, liquidity constraints, and market impact are part of the simulation.
- Typical top-of-book size varies. Gold was cited as roughly 100 oz in normal conditions.
- Participant-to-participant matching can occur, but it is not expected to dominate execution.
- Real-life market order book data is an input to the simulation.

## Costs And Constraints

- No commission.
- No swap, overnight financing, or borrow fees.
- Leverage is account-level, with a maximum of 30:1.
- There are no extra position limits beyond account-level leverage and margin constraints.
- Minimum order size was noted as 1 unit or 1 ounce; no maximum was stated in the notes.
- Long and short trading are both allowed across supported instruments.
- Options are not available, so options hedging is not supported.
- Stop-losses and standard MT5 order functionality are supported.
- If an MT5 terminal disconnects, open positions remain open. There is no client-side disconnect auto-flatten.

## Ranking And Rounds

- General notes say PnL is the primary ranking emphasis, with risk controls to prevent excessive directional bets.
- Official formulas and penalties are in `rules.md`; use those for implementation.
- Equity carries through the entire competition and does not reset between rounds.
- Performance carries through into finals.
- Round 1-3 qualifier counts were not finalized in the notes.
- There is no extra minimum-activity requirement for the main competition beyond the official rules.
- Best Sharpe has a separate trade-count eligibility gate in `rules.md`.

## Market Data And Backtesting

- Historical market data is provided in advance for backtesting and model preparation.
- Live API should stream the same 5-level depth.
- Historical dump is expected to be structured and machine-readable, likely CSV, with timestamped bid/ask levels, sizes, and instrument metadata.
- Exact schema is released after opening announcement.
- Test environment is for validating connectivity, market data access, and execution workflow. Live environment may differ.

## Technology And Prize Notes

- Top 25 present their technical architecture.
- Technology judging considers system design, AI integration, and execution methodology.
- Best Technology Setup prize: USD 10,000.
- NVIDIA award: RTX 5080, selected from best technology implementation, including possible use of Nemotron models.
- Best Sharpe Ratio prize: USD 10,000.

## Sponsor Perks

| Sponsor | Perk |
| --- | --- |
| Anthropic | USD 50 API credits; `platform.claude.com` |
| Doubleword | Inference API access via Pydantic AI gateway / Logfire |
| Pydantic | USD 50 Pydantic Logfire inference credits; `pydantic.dev/hackathon` |
| Northflank | USD 100 platform credit; `app.northflank.com/i/AIENGINE` |

Useful links:

- Doubleword docs prompt: `Use the documentation from <https://doubleword.ai/quanthack.md> for help building on Doubleword during the Quantitative Hack`
- Doubleword page: `https://doubleword.ai/quanthack`
- Pydantic hackathon page: `https://pydantic.dev/hackathon`
- Northflank skills: `https://github.com/northflank/skills`

## Discord Channels

Information:

- `#announcements`: official updates, rules, schedule, urgent notices.

Community:

- `#general`: casual contestant conversation.
- `#strategy-discussion`: market views and trading ideas; visible to all contestants.
- `#introductions`: introduce yourself.

Support:

- `#support`: account issues, platform questions, rule clarifications.

Live Updates:

- `#leaderboard-updates`: live ranking updates during Week 2.
- `#phase-announcements`: elimination results and phase transitions.

Community rules:

- Be respectful and professional.
- No spam, advertising, or self-promotion.
- Strategies shared publicly are visible to all contestants.
- No offensive or inappropriate content.
- Organizers' decisions are final.

## Access And Help

- To get verified, go to `#verify` and send the registered email address.
- An organizer verifies contestants and unlocks contestant channels.
- Verification usually completes within a few hours.
- For platform/account issues, use `#support`.
- For private concerns, DM an `@Organizer`.

## Public Links

- Website: `https://quanthack.syphonix.com`
- Registration: `https://lu.ma/4j44j9l0`

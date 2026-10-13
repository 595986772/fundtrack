# FundScope Product Truth

## Product

FundScope is a personal fund research workspace. It combines fund facts,
historical risk, portfolio exposure, market events, community signals, and an
AI research assistant into one traceable decision-support flow.

## Primary User

The primary user is an individual Chinese fund investor who cares about risk,
market sentiment, and understandable explanations more than raw indicator
dumps. They may use the product on desktop for focused research and on mobile
for quick checks before the fund trading cutoff.

## Core Job

Help the user answer: "What deserves my attention today, why, and what evidence
supports that view?"

## Product Principles

- Evidence before opinion: conclusions must stay connected to dates, sources,
  holdings, and calculation scope.
- Risk before prediction: explain volatility, drawdown, concentration, and
  uncertainty without pretending to predict exact future prices.
- Plain language after professional analysis: calculations and retrieval can be
  technical; the final explanation should be understandable.
- Human decision, assisted workflow: the product may provide conditional views
  when asked, but it does not execute trades automatically.
- Freshness is visible: distinguish database snapshots, current market events,
  disclosure dates, and model-generated interpretation.

## Main Workspaces

- Assets: holdings, performance and risk. Fund charts and disclosed holdings are
  detail views; bookkeeping, pressure tests and allocation editors are secondary tasks.
- Event Intelligence: clustered event lists and historical changes in attention.
  Sources and full interpretations live in a dedicated detail view.
- Community Topics: topic-level opinions, with raw samples, saved AI reviews and
  collection settings separated. Platform access restrictions do not erase saved evidence.
- Sector Radar: dated ETF-flow evidence and explicitly tagged events. Missing
  sector return series or forward-looking research are shown as missing, not fabricated.
- AI Research: one conversation surface with a subject picker, research reports
  and decision records. Contextual handoffs prefill questions without sending them.

The product direction is evidence-backed multi-agent analysis of allocation and
prospects. Navigation consolidation does not itself add new analyst agents or
new data providers. Manual collection and explicit model invocation remain distinct
from page navigation and database reads.

## Constraints

- Existing FastAPI and vanilla HTML/CSS/JavaScript architecture remains the
  delivery stack unless a separate migration is explicitly requested.
- The interface is a task-oriented financial application, not a marketing site.
- Product copy is Chinese-first. English is reserved for proper names, code,
  standards, or compact technical metadata.

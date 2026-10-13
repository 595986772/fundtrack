---
name: FundScope
description: A dense, evidence-first financial research terminal.
colors:
  canvas: "#111215"
  surface: "#191a1e"
  surface-soft: "#1e1f24"
  surface-muted: "#282930"
  ink: "#e9e9ee"
  muted: "#b0b1bb"
  faint: "#989aa7"
  border: "#303138"
  border-strong: "#484a55"
  accent: "#edc85b"
  accent-strong: "#f7d878"
  accent-soft: "#302b1c"
  on-accent: "#181818"
  info: "#8ab5f5"
  info-soft: "#202c3d"
  comparison-line: "#79aefa"
  market-up: "#fa828c"
  market-down: "#69cbb0"
  caution: "#edc85b"
  caution-soft: "#302b1c"
  danger: "#fa828c"
  danger-soft: "#37232a"
  sidebar: "#15161a"
  sidebar-ink: "#f7f7f4"
  sidebar-accent: "#f2c94c"
  sidebar-accent-hover: "#ffdb6e"
  sidebar-accent-soft: "#3b3722"
  sidebar-hover: "#303136"
  sidebar-info: "#9dbcf4"
  sidebar-info-soft: "#293342"
  sidebar-caution: "#f4a261"
  sidebar-caution-soft: "#3d3024"
typography:
  title:
    fontFamily: "-apple-system, BlinkMacSystemFont, PingFang SC, Microsoft YaHei, sans-serif"
    fontSize: "19px"
    lineHeight: 1.55
    letterSpacing: "0"
  metric:
    fontFamily: "-apple-system, BlinkMacSystemFont, PingFang SC, Microsoft YaHei, sans-serif"
    fontSize: "20px"
    fontWeight: 550
    letterSpacing: "0"
  body:
    fontFamily: "-apple-system, BlinkMacSystemFont, PingFang SC, Microsoft YaHei, sans-serif"
    fontSize: "14px"
    fontWeight: 400
    letterSpacing: "0"
  native-input:
    fontSize: "16px"
  compact:
    fontFamily: "-apple-system, BlinkMacSystemFont, PingFang SC, Microsoft YaHei, sans-serif"
    fontSize: "12px"
    lineHeight: 1.9
    letterSpacing: "0"
  chart-axis:
    fontFamily: "Arial, PingFang SC, sans-serif"
    fontSize: "11px"
    letterSpacing: "0"
  micro:
    fontFamily: "-apple-system, BlinkMacSystemFont, PingFang SC, Microsoft YaHei, sans-serif"
    fontSize: "10px"
    letterSpacing: "0"
rounded:
  indicator: "2px"
  compact: "3px"
  sm: "4px"
  field: "6px"
  surface: "7px"
  lg: "8px"
  pill: "999px"
spacing:
  xs: "4px"
  sm: "8px"
  md: "16px"
  lg: "24px"
  xl: "32px"
components:
  button-primary:
    backgroundColor: "{colors.accent}"
    textColor: "{colors.on-accent}"
    rounded: "{rounded.compact}"
    padding: "8px 12px"
  button-primary-hover:
    backgroundColor: "{colors.accent-strong}"
    textColor: "{colors.on-accent}"
  button-secondary:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.ink}"
    rounded: "{rounded.field}"
    height: "38px"
  input:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.ink}"
    rounded: "{rounded.field}"
    height: "38px"
---

# Design System: FundScope

## Overview

**Creative North Star: "The Research Terminal"**

FundScope is an Operate-mode financial workspace: select an instrument, inspect
its historical data, follow the evidence, and explicitly ask the Agent.
The interface is dense, flat and legible rather than decorative. Its dials remain
`DESIGN_VARIANCE: 3`, `MOTION_INTENSITY: 2`, and `VISUAL_DENSITY: 8`.

## Colors

Neutral graphite and charcoal cover the canvas, navigation and working surfaces.
Signal yellow identifies selection, primary actions and the main chart series;
blue differentiates comparison curves and source links. Chinese-market price
and return values use red for positive and green for negative, always retaining
the sign. Return contribution bars match their numeric labels. Risk alerts keep
their own semantic meaning; green returns do not mean a favorable risk assessment.

Holdings use champagne gold (`#e5cc87`), cool blue (`#a8c5ef`) and silver
(`#ccd1dc`) for broad-index, industry-theme and bond identities, respectively.
Each has an icon and category text. These categories never encode return direction.
Daily quote colors use their own `data-quote-tone` so legacy brand-status styles
cannot override market red/green. Quote date and publication status remain visible.

## Typography

Use system sans with Chinese fallbacks and tabular numerals for financial values.
The default body is 14px; terminal summaries use 12px, metadata 9-11px, and primary
values 20px. Discrete mobile breakpoints use 17-18px values and 16-17px instrument
headings. Do not interpolate type size with viewport width.

Grouped workspace navigation uses 13px link labels and 12px section labels.
Native mobile page and fund selectors use 16px to remain readable without focus zoom.

Quarterly-report evaluation selects and review textareas use the fixed
`native-input` role to avoid mobile focus zoom; body and metadata roles remain unchanged.

ECharts explicitly sets both axis labels to 11px in the muted text color.
Portfolio axis and date labels are HTML at fixed 11px, outside the SVG scaling
system, with a 48px axis column and 180px drawing height.

## Layout

Desktop uses a 236px navigation rail and a flexible, single-task content surface.
Five product entries remain visible: assets, event intelligence, community topics,
sector radar and AI research. Page-local links sit under the header, with two or
three primary views. Auxiliary tools are reached from their owning task.
Fund detail uses a full-width chart and fund risk; it no longer duplicates the
market ticker, news column, community widgets or a second AI composer.

At 1080px the rail becomes a drawer and the same five entries appear in bottom
navigation. Local navigation fits within three links. Wide financial tables scroll
inside their own region with a sticky identifying column. Event filters use named
grid areas on mobile. Saved asset summaries are read-only by default; editors and
bookkeeping live on separate pages. Detail views replace the list and preserve its
filters and scroll position on return. The primary fund chart is 275px high on small
phones. A disclosure retains the complete fund report.

## Elevation & Depth

Terminal surfaces are flat, separated by thin borders and tonal differences.
The existing soft/overlay shadows remain in the shared system for older business
surfaces and dialogs, not for decorating every terminal section. Exact shadow
values, motion and responsive rules are recorded in `.impeccable/design.json`.

## Shapes

Cards and major surfaces use at most 8px radius. Inputs, buttons, and list rows
use 4-6px. Pills are reserved for compact statuses and filter chips.

## Components

### Buttons

Primary actions use yellow with dark text. Secondary actions use dark or
transparent surfaces. Icon-only controls use Lucide and provide accessible names
and tooltips; terminal targets are 30px on desktop and 36px below 760px.
Tab groups have stable heights and arrow-key, Home and End navigation.

### Cards / Containers

Use a card only when an item is independently selectable or needs a genuinely
framed tool surface. Never put decorative cards inside cards. Prefer full-width
bands, dividers, and spacing for research sections.

### Inputs / Fields

Labels appear above fields. Placeholders supplement labels and never replace
them. Focus uses the gold accent ring; errors remain inline and name a recovery.

### Navigation

Desktop navigation is persistent and left aligned. Mobile uses the same five
workspaces. Canonical routes include `#assets/performance`, `#intelligence/events`,
`#community/topics`, `#sectors/overview` and `#research/chat`; legacy routes still resolve.
Selection, keyboard focus, and page titles stay aligned; switching functions
does not submit forms, start collection, or invoke a model. Portfolio pages mount
on first visit and retain their DOM while hidden. Each data owner invalidates only
its page. Risk, performance, and research have independent period controls.

### Charts and Research

The Web workspace is data-first: primary values and comparisons precede prose.
Assets pair the return curve with current allocation in one unframed overview.
Holdings follow immediately; signed floating-PnL contributions and accounting
details are disclosures below them. The primary configuration-analysis action
prefills a fresh conversation, retaining old conversations without starting a model.
Single-fund charts show the selected account's portfolio position alongside them:
market value, cumulative PnL, units, average cost and the ledger's NAV date.
Unknown, loading, failed, different-account and different-portfolio states never
reuse another position's amounts. This summary is not an intraday revaluation.
On narrow Web layouts the position context stacks above the chart;
review places benchmark returns beside drawdown, with concentration below.
Event rows form a compact dated timeline. Sector bars compare the same ETF-sample
flow window on a shared zero axis. Community stance distributions retain unknowns
and show distinct authors per topic, never a sum presented as global deduplication.
Research pairs opportunities and risks with dated metrics and evidence links.
Web research reports place the follow-up action in the report header and follow
one reading sequence: conclusion followed by a compact row of dated metrics,
all findings grouped by risk/support/neutral, disagreements and follow-ups,
then the optional multi-role process and report appendices. Grouping happens
before presentation limits; a finding's explanation, holdings and references
share one disclosure. Empty categories occupy a compact row, not an empty
column. Analyst viewpoints and cross-review are secondary disclosures after
the synthesis; degradation and uncertainty remain explicit. This presentation
does not rewrite model judgments, mutate frozen reports or invoke the model.
Calculation details, full source text and execution traces are disclosures;
staleness, insufficient observations, uncertainty and missing evidence stay visible.

`data-visuals.js` owns reusable display functions and ECharts lifecycle only.
Numeric nulls remain gaps, not zero; chart tables provide accessible daily values.
Page mounting defers chart initialization until the container has a real width,
and replacement releases chart instances and ResizeObservers. These Web rules do
not change the mini-program, backend financial calculations or manual refresh policy.

Single-fund holdings use `#assets/fund/holdings`, separate from the chart and full
report. The surface inherits Operate mode, graphite/yellow tokens, 20px headings
and metrics, 14px body/table text, and 12px provenance. Desktop shows disclosed
assets beside industry bars; mobile stacks them and contains wide tables in
their own scroll region. Disclosure changes are a separate local view. Missing
ETF look-through mapping is visible before the figures, not buried in a footer.
The old portfolio exposure page is removed; its backend services remain intact.

The main chart uses locally hosted ECharts 6.0.0 with a crosshair, zoom slider,
reset and explicit calendar-period controls. Fund total returns come from the
existing backend return engine; raw NAV remains a separate mode. Comparisons
align shared dates and rebase at the same start. Missing history is disclosed,
never filled with zeros or fabricated candles. Index summaries distinguish
trading-day periods from calendar periods.

Instrument selection changes context without a model call. A single AI workspace
owns the composer and SSE flow. Fund, event, topic and sector actions prefill a
question with dated context, without sending it. Changing the AI subject starts a
fresh conversation and retains the unsent question. Sector radar keeps its own
snapshots, isolated from event-page filters. Missing return series and forward
research are labeled as missing, not presented as zero values or recommendations.
Reading a snapshot and starting an external synchronization are separate actions.

Daily briefings occupy `#research/briefings`, beside chat and reports. Their
flat schedule row uses an explicit switch (off by default), a dated history,
source status disclosures, and the existing report/follow-up entry. A GET never
starts data collection. Enabled schedules visibly state Beijing time and model
usage; fallback reports remain distinguishable from AI synthesis. Decision
records retain their auxiliary route and are linked from this page.

Large-capital research leads with the nine tracked sector ETF themes, a fixed
1/5/20-day window selector, net-creation estimates, comparable-sample intensity,
coverage, and dated observations. Broad indices follow separately. Institutional
LHB is a total-only summary, not an individual-stock leaderboard. On mobile the
sector table scrolls within its own region with a sticky first column. Window
changes use loaded data without external collection or model calls.

### Developer Evaluations

Quarterly-report evaluation extends the existing developer evaluation panel in
Operate mode, reusing graphite surfaces and yellow actions and focus states.
It does not add a main workspace.

**The Separate Verdicts Rule.** Automatic checks and their scores remain distinct
from manual semantic review. Automatic scores are not answer accuracy; citation
support, interpretation and unanswerable questions still require human review.

**The Explicit Evaluation Actions Rule.** Background evaluations start only from
run controls; pending tasks expose progress, resume and cancellation controls.
Changing suites, retrieval configuration or saved runs does not start an evaluation.
Review status and notes remain drafts until the review form is explicitly submitted.

## Do's and Don'ts

### Do:

- **Do** make data freshness, source, and calculation scope visible.
- **Do** use progressive disclosure for secondary evidence while keeping key
  risks and conclusions visible.
- **Do** implement loading, empty, error, disabled, hover, active, and focus
  states for interactive components.
- **Do** verify desktop and mobile with real rendered data.

### Don't:

- **Don't** add marketing heroes, gradient text, glass decoration, or animated
  page-load choreography.
- **Don't** use English eyebrows above every section heading.
- **Don't** use color to imply a forecast or recommendation that the evidence
  does not support.
- **Don't** label stored close/NAV data as live intraday quotes or trigger paid
  model analysis merely by changing instruments or tabs.
- **Don't** remove domain complexity users need; organize it into a clearer
  sequence instead.

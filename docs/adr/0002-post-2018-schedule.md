# ADR 0002 — What the pipeline does after the data runs out

**Status:** Accepted · **Date:** 2026-09-11

## Context

The Olist extract covers **2016-09-04 to 2018-10-17**. The pipeline is
scheduled and backfills that window for real, but every scheduled run after
2018-10-17 has no source data to process.

A portfolio project has an obvious temptation here: map wall-clock time onto a
synthetic Olist date so the replay loops forever, and claim "runs daily in
production". That claim would be false in a way an interviewer can find in
about ninety seconds, and it would make every recency figure on the dashboard
("orders this week") fabricated.

## Decision

**The DAG no-ops, loudly and legibly, once the logical date passes the end of
the data.** No synthetic date mapping.

`orchestration/dags/olist_batch.py` begins with a short-circuit that compares
the run's logical date to `DATA_COVERAGE_END` and, when past it, logs the
reason and skips the downstream tasks:

```
Logical date 2026-09-11 is beyond the Olist coverage window
(2016-09-04 .. 2018-10-17). Nothing to ingest. This is expected:
the source is a static historical extract, not a live feed.
```

Skipped runs are a normal, documented state, not a failure — they stay green
and do not page.

The README says exactly this, and never claims live daily operation:

> The DAG backfills 2016-09 to 2018-10 for real — 25 monthly runs plus daily
> runs for the final 90 days. Scheduled runs after 2018-10-17 short-circuit
> with a logged reason, because the source is a static extract.

## Consequences

**Good.** Every number on the dashboard is real. "What happens after the data
ends?" has a one-sentence answer that improves the impression rather than
damaging it.

**Good.** Short-circuiting is itself worth showing: a pipeline that knows the
boundaries of its source and declines to invent work is a better artefact than
one that manufactures rows.

**Cost.** The repo cannot display a rolling "last successful run: today"
badge against fresh data. Accepted — the CI badge already proves the pipeline
builds and passes 67 tests on every commit, which is the claim that matters.

## Alternative rejected: synthetic date mapping

Map `today - 2018-10-17` onto an offset into the replay so the pipeline always
has a slice to process.

Rejected because it produces a dashboard whose "recent orders" are real rows
wearing fake timestamps. The moment anyone compares a dashboard date to the
`order_purchase_timestamp` in the warehouse, the project looks dishonest
rather than clever. The cost of the honest option is one absent badge; the
cost of this one is credibility.

If a live-data story is wanted later, the right answer is a second source with
an actual feed, not a costume on this one.

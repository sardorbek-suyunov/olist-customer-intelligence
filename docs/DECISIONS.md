# Decisions

Judgement calls and the reasoning behind them.

This is not the ADR set. `docs/adr/` records *architectural* decisions -- the shape
of the system, where a boundary sits, which materialisation a model uses. This file
records the calls that were arguable at the time: a threshold picked without data, a
sample design that cannot measure what you would want it to measure, a cheap model
chosen over a good one. The kind of thing that looks arbitrary six months later
unless the reasoning was written down while it was still fresh.

It is rationale, not state. Where a number would drift, this file points at whatever
generates it -- `docs/figures.json`, the cost log, `enrichment/taxonomy.py` -- rather
than keeping a second copy. Counts quoted from the labelled corpus are tagged with
the prompt version that produced them, so a v2 run does not silently falsify a v1
decision record.

---

## 1. Support thresholds were committed before the pilot ran

**The call.** `PILOT_MIN_OCCURRENCES = 20` and `EVAL_MIN_POSITIVES = 30` were written
into `enrichment/taxonomy.py` and committed before a single review was labelled.

**Why up front.** A threshold chosen after seeing the distribution is not a threshold,
it is a description of the distribution. The failure mode is specific and easy to fall
into without noticing: label the corpus, see that `split_shipment` came back 14 times,
decide 20 was always a bit strict, set it to 10, and report that every aspect cleared
the bar. Nothing in that sequence feels dishonest from the inside. Fixing the number
first makes the later decision a rule being *applied* rather than a rule being
*chosen*, and `scripts/pilot_report.py` exists to apply it rather than to pick it.

**Why those values.** 20 is 1% of the 2,000-review pilot -- below that an aspect is
either genuinely marginal or the model is confusing it with a neighbour, and the
remedy is the same either way. 30 comes off the binomial interval: the 95% half-width
on a proportion is roughly 1/sqrt(n), so n=30 already carries +/-18pp. Publishing a
per-aspect F1 under that floor prints a number with no information in it, which is
worse than printing nothing, because a reader cannot tell the difference.

**The cost of committing early, paid honestly.** Both numbers are defensible rather
than optimal, and they were guesses about a distribution nobody had seen. That is the
trade: a defensible number fixed in advance beats an optimal one chosen afterwards,
because only the first one is falsifiable.

## 2. `split_shipment` and `tracking_missing` are scored, and the threshold did not move

**The call.** Both aspects fell below `PILOT_MIN_OCCURRENCES` in the 2,000-review pilot
and the declared rule folded them into their parent, `delivery_late`. Both are scored
separately in the eval anyway. `PILOT_MIN_OCCURRENCES` is still 20.

**Why this is not moving the goalposts.** The threshold is a rule about *pilot-scale
evidence* -- it answers "given 2,000 labels, is there enough support to trust this
aspect standing alone?" It was never a claim about the aspect's real prevalence. The
full corpus is 35,616 labels, 17.8x the pilot, and under prompt v1 both aspects clear
20 on the actual count by a wide margin: `split_shipment` 210, `tracking_missing` 480.

So the rule was not relaxed; the evidence base changed. The merge was conditional on
pilot-scale support and that condition no longer holds. The distinction matters,
because the other way to reach the same outcome -- lowering the threshold to 10 until
they qualified -- would have produced the identical taxonomy from reasoning that does
not survive being written down.

**What it still bounds.** Clearing the extraction threshold is not the same as being
scorable. `split_shipment` sits at 0.59% prevalence, so a 300-review random stratum
holds roughly 1.8 of them: it has precision support from the targeted stratum and no
recall estimate at all. It is scored, and reported as precision-only. See decision 4.

**Why `split_shipment` was kept at all.** It is Olist-specific rather than generic
review-taxonomy furniture: one order spans several sellers, so it ships in pieces and
bills in pieces, and customers experience that as one complaint. An aspect that rare
would normally be merged without much thought. This one describes a real property of
the marketplace, which is why it was given a parent to fall back to rather than being
left out of the taxonomy.

## 3. Only `fct_orders` is incremental

**The call.** One incremental model. Every dimension is a full table rebuild; staging
is views throughout.

**Why not more.** A backfill is 25 monthly windows and each one rebuilt the whole mart.
At roughly 120 MB that costs nothing -- which is an honest reason not to care, and a
poor reason not to have decided. The decision is therefore made once, on the model
where it actually pays, and the reasoning is recorded for the models where it does not.

**Why `fct_orders` specifically.** It is the largest object here, it grows without
bound as orders accumulate, and a window of orders is genuinely additive. It is keyed
on `order_id` and merged, so reprocessing a window converges instead of appending --
which is what a late arrival needs, and what the torn-backfill incident showed the
cost of getting wrong.

**Why the dimensions stay tables.** `dim_customers` is SCD2 and a late-arriving change
can restate history *behind* the high-water mark, so an append-shaped incremental
strategy would be wrong rather than merely unnecessary. The others are small and fully
derived. Incremental on any of them would buy no measurable time and add a second code
path -- the `is_incremental()` branch -- that only runs sometimes, which is the shape
of bug that hides longest.

**The general form.** Incremental materialisation is a cost/complexity trade, and at
this data size the cost side is near zero. Adopting it everywhere would be cargo cult:
the complexity is real and the saving is not.

## 4. Recall is unmeasurable for 13 of the 16 aspects

**The call.** The eval reports precision for all scored aspects and recall for three:
`delivery_fast`, `product_quality`, `seller_recommended`. The other 13 report precision
with recall marked unavailable, rather than reporting a recall number.

**Why.** A false negative is a review the model *failed* to label. It can only be found
in a sample drawn without reference to the model's labels -- in a stratum selected from
what the model found, missed reviews are absent by construction. So only the 300-review
random stratum can support a recall estimate, and an aspect needs
`EVAL_MIN_POSITIVES` = 30 positives *inside that stratum* to clear the floor.

At 300 reviews that requires roughly 10% prevalence. Under prompt v1 exactly three
aspects clear it: `delivery_fast` (33.3%), `product_quality` (24.4%),
`seller_recommended` (19.4%). The next one down, `not_as_described` at 8.1%, yields
about 24 -- close, and still under. The targeted stratum fixes precision support for
the tail and can do nothing for recall, because it is drawn from the model's own
positives.

**Why not just enlarge the sample.** Recall for a 0.6% aspect at n=30 needs a random
stratum near 5,000 reviews. That is a hand-adjudication budget this project does not
have, and the honest response is to say which aspects the design cannot measure rather
than to quietly report a recall computed off three examples.

**What gets published.** The unmeasurable ones are named, with their achievable counts,
in the eval output. An absent number that says why it is absent is a stronger result
than a present number that cannot be trusted -- and it is the same standard the README
is already held to.

## 5. Eval ground truth is model-generated, and this bounds the result

**The call.** The 600-review eval sample is labelled by a stronger model, and the
production model (`gemini-3.1-flash-lite`) is scored against those labels. No human
adjudication.

**Why.** Hand-labelling 600 Portuguese reviews across 16 aspects is somewhere near 20
hours and would not get done, and a portfolio piece that blocks on it ships nothing.
A stronger model is the affordable substitute that still breaks the worst circularity
-- scoring a model against its own output measures self-consistency and calls it
accuracy.

**Exactly what it bounds, stated plainly.** This measures *agreement with a stronger
model*, not accuracy. Concretely:

- Where both models share a systematic misreading, the eval scores it correct. That is
  the irreducible limit, and it is likelier on the aspects whose boundaries are fuzzy
  in the first place -- `product_quality` against `not_as_described`, `delivery_late`
  against `not_received`.
- The ceiling is the stronger model's own accuracy, which is unmeasured here. A
  reported precision of 0.9 means "agrees with the reference 90% of the time", and the
  reference's own error rate is unknown.
- It is therefore usable for *comparison* -- v1 against v2, one aspect against another
  -- and not as an absolute quality claim. The prompt iteration in step 3 only ever
  needs the comparison, which is why this is enough.

**What would lift the bound.** Hand-adjudicating a subset -- even 50 reviews -- would
put an error bar on the reference itself and turn agreement into a calibrated estimate.
Not done, and not claimed to be done.

**Which model is the reference, and at what thinking budget.** See decision 9.

## 5a. The reference is `gemini-3.8-flash` with thinking off, and it is not Pro

**The call.** The 600-review reference pass runs on `gemini-3.8-flash` at
`thinking_budget = 0` -- the same thinking setting as the model under test. It cost
$0.1130 for 587 distinct texts, 0 quarantined.

**Why thinking is held constant.** Running the reference with thinking on and the
subject with thinking off confounds two variables: the reference would be both a
stronger model *and* running in a more expensive mode, and a disagreement could not be
attributed to either. Fixing the thinking budget makes the comparison a clean test of
model capability, which is the question the eval is actually asking. It also keeps the
v1-to-v2 prompt iteration interpretable, since the reference does not shift underneath
it.

**Why not Pro, which the plan named.** `gemini-3.1-pro-preview` cannot do it. The API
rejects `thinking_budget = 0` outright: *"Budget 0 is invalid. This model only works in
thinking mode."* So "Pro with thinking held constant" is not a configuration that
exists, and the choice was between keeping the model and keeping the method. The method
was kept.

`gemini-3.8-flash` is still a real step up from the model under test -- $0.75/$3.75
against $0.25/$1.50, roughly triple the input rate -- so the reference is stronger
without being differently configured.

**What the probes found, which is why they were run.** Three exploratory calls on one
batch of 20, $0.0863 all told, measured rather than projected:

- Pro at `thinking_budget = 0` -- refused, as above.
- Pro at `thinking_budget = 128` -- 1,284 thought tokens. **The budget is a hint, not a
  cap**; it overshot by 10x. Anything that treated it as a spend ceiling would have been
  wrong, and wrong in the expensive direction.
- Pro uncapped -- 3,550 thought tokens, projecting $1.715 for the 600.

The pre-probe projection had used the 5.2x thinking multiplier measured on
`gemini-3.1-flash-lite`'s pilot and put Pro-uncapped near $2.00. Using one model's
thinking ratio to predict another's is the same substitution of a self-report for the
thing itself that the cost log was already caught on four times, and the probe replaced
it with a measurement for about eight cents.

**What this costs the result.** A Pro reference would very likely be a better ground
truth, and decision 5's ceiling -- the reference's own unmeasured accuracy -- sits lower
than it otherwise would. That is a real weakening of the eval and is stated rather than
absorbed. The cheap way to bound it later is a Pro pass over roughly 100 of the same
reviews, about $0.12: reference-against-reference disagreement puts a number on exactly
what the cheaper reference gave up. Not done yet, and not claimed.

**Prices went in the table before the run, not after.** `gemini-3.1-pro-preview` was
added to `enrichment/pricing.py` from Google's published rates -- including the tiered
rate above a 200k-token prompt -- before any call was made. An unpriced model logs $0.00
and warns, so probing first would have produced a cost log that recorded the probes as
free.

**The probes are in the cost log.** They were made through `GeminiClient` directly
rather than through the pipeline, so nothing would have recorded them. Three rows were
written by hand with their measured token counts, `reviews = 0` since no labels were
kept. A total that omits real spend because it was spent exploratively is the same
understatement the log exists to prevent.

## 5b. The v1-to-v2 prompt change, and what it bought

**The call.** One change: the `product_quality` description was widened from "build
quality or materials, good or bad, short of an outright defect" to include a plain
verdict on the product, with an explicit note that it applies even when the review also
praises delivery or the seller. Nothing else in the prompt, the taxonomy or the schema
moved.

**Why that one.** It was diagnosed, not guessed. `product_quality` false negatives were
39 of 85 -- 46% of every miss in the v1 eval. Three measurements narrowed the cause:

- Not global under-labelling: aspects per review were 1.512 for the subject against
  1.508 for the reference. The subject labels the same *amount*, just not the same
  things.
- Not a polarity asymmetry: recall was 72% on negative text and 76% on positive, so it
  was not "misses praise, catches criticism".
- In 31 of the 39 misses the subject labelled some *other* aspect on the same review.
  `Os copos sao lindos e chegaram antes do prazo` got `delivery_fast` alone; the
  verdict on the cups was dropped.

That points at the wording rather than the model: "build quality or materials" reads as
a request for a technical judgement, and a plain `Bom produto` does not obviously match
it. So the fix is a description change, and it is the only change, so the re-score
attributes cleanly.

**What it bought.** Against the same unchanged reference, on the same 600:

- `product_quality` false negatives 39 -> 9, recall 0.825 -> 0.969, F1 0.884 -> 0.936.
- Overall false negatives 85 -> 61; micro recall 0.906 -> 0.933; micro F1 0.905 -> 0.916.
- Exact aspect-set match 77.8% -> 80.2%; sentiment agreement 94.5% -> 95.8%.
- Micro precision essentially flat, 0.904 -> 0.900.

**What it cost, which is the part worth keeping.** Precision on `product_quality` fell
0.953 -> 0.904 as it was applied more widely -- the expected trade, and a good one at
this ratio. The unexpected one was `seller_unresponsive`, whose precision fell
0.812 -> 0.702 on 8 extra false positives, from a change that did not mention it.

Reading those 17 false positives complicates the story rather than settling it. Several
are reviews where the customer plainly describes contacting the seller -- *"entrei em
contato com o vendedor"*, *"liguei pra reclamar"* -- and the reference did not label
them. Whether those are the subject over-applying the aspect or the reference missing
it cannot be settled from inside this eval, because the reference's own accuracy is
unmeasured. This is decision 5's stated ceiling turning up as a concrete number instead
of a caveat, and it is left as a disagreement rather than resolved in the subject's
favour.

**Why the mart still runs on v1 labels.** v2 is demonstrated on 600 reviews and is not
applied to the corpus. Re-labelling all 35,616 texts at v2 would cost about $3.19,
against roughly $2.39 remaining, so it is not affordable -- and `PROMPT_VERSION` is in
the cache key precisely so that a v2 corpus cannot be half-built and mistaken for a
whole one. The honest statement is that the iteration is a measured result about the
prompt, not a change to the shipped data.

## 6. A cheap model, measured honestly, over a capable one

**The call.** `gemini-3.1-flash-lite` with `--thinking-budget 0` at batch size 20, for
the full 35,616-text corpus.

**Why.** The budget is real and small, and the capable models are not a little more
expensive, they are multiples. Spending the whole ceiling on one pass of a better model
buys a higher number and forecloses everything downstream: no eval, no v1-to-v2
iteration, no embeddings. The cheaper pass leaves room for all three, and *those* are
the parts that demonstrate engineering judgement. A high score from an expensive model
with no eval behind it demonstrates a budget.

**Why thinking is off.** Thinking tokens bill at the output rate and dominated the
first pilot -- 3,774 thought tokens against 730 of answer on a batch of 20. Whether
turning it off costs accuracy is an eval question, not a cost question, and the eval is
the thing that will answer it.

**Why batch 20.** Batching is roughly a 10x lever on input tokens and wall time but
only about 2.7x on cost, because output tokens dominate and batching does not compress
them. It is worth doing and it is not the headline saving, and the A/B against batch
size 1 over the same seeded reviews exists to keep that claim measured rather than
assumed.

**The deliberate story.** "Cheap model, measured honestly" is the point, not an
apology. The interesting artefact is the per-aspect eval with its stated blind spots,
and that artefact is only affordable because the labelling pass was cheap.

## 7. Enrichment Parquet is committed; replay slices are not

**The call.** `data/slices/` is gitignored. `enrichment/data/*.parquet` is committed,
as an explicit un-ignore.

**Why they differ.** Not an exception to the rule -- a different answer to the same
question, which is *what does a fork lose if this is absent?*

Replay slices are regenerable for free: `make data && make backfill`, a few minutes,
no credentials. Committing roughly 29 MB of derived output would contradict the
clone-and-run claim rather than support it, and would redistribute a CC BY-NC-SA
dataset besides.

The enrichment output is not regenerable for free. It cost $3.19 and an API key, and a
fork has neither. Committing 3.3 MiB means a clone can build the aspect marts and run
the eval with no key and no spend. It doubles as the cache, so a fork that loads it and
re-runs the pipeline spends exactly $0 -- demonstrated by the second run in the cost
log recording zero calls and zero dollars, rather than asserted.

**The general form.** Derived data earns a place in the repo when regenerating it costs
money, credentials, or hours -- not merely when it is small. The committed dashboard
snapshot is in the repo for the same reason: the public demo has to survive a lapsed
service-account credential.

## 8. `gemini-embedding-2` over `gemini-embedding-001`, truncated to 1536

**The call.** Embed the 35,616 distinct texts with `gemini-embedding-2`, MRL-truncated
to 1536 dimensions, and run `VECTOR_SEARCH` without a vector index.

**Why not `001`, which the plan originally named.** It has no published price. It is
absent from Google's pricing page, including the canonical text version -- the only
rates available are third-party. `enrichment/pricing.py` exists precisely to refuse
that: an unpriced model logs 0.00 and warns, where a wrongly-priced one produces a
plausible number nobody can trace. `001` is also legacy: a 2,048-token input limit
against 8,192, text-only, and superseded in the documentation. The difference on this
corpus is a few cents against a rate that can be cited.

**Why 1536 and not the default 3072.** Storage is the smaller half of the argument. The
binding constraint is `DEFAULT_MAX_BYTES_PER_QUERY` in `analytics/bq_safety.py`. An
unindexed `VECTOR_SEARCH` scans the whole embedding column, and at 3072 dimensions
that column is about 835 MiB -- roughly 82% of the 1 GiB per-query ceiling, so the
first query that also joins a dimension trips a guardrail the project enforces on
itself. At 1536 it is about 417 MiB, which leaves the headroom the ceiling was chosen
to provide. Truncation is an MRL property of the model, not a lossy afterthought.

**Why no index.** `VECTOR_SEARCH` falls back to brute force and does not miss unindexed
rows. At 35,616 rows brute force is the right tool, and an index would add storage and
a maintenance surface for no measurable gain. Worth recording that the *reason* is
scale and not eligibility: the 10 MB floor below which an index silently fails to
populate is roughly 88x below this table, so the option is open and is being declined.

**How the token figure was reached.** Counted, not estimated: `countTokens` over an
800-text random sample, fitted against exact character counts, then reconciled against
the input tokens the labelling run was actually billed for. The planning-stage
chars/3.5 rule of thumb is superseded by measurement, which is the standing pattern --
`enrichment/client.py` records `usage_metadata` for the same reason.

## 9. The public demo's Gemini budget, decided before the demo was designed

**The call.** Per-session, per-day and lifetime dollar ceilings on the NL-to-SQL
agent's Gemini calls, an untouchable reserve, and a set of example questions whose
answers are computed at build time and served with no API call and no warehouse scan.

**Why before rather than after.** The agent calls the API on every visitor question,
against one key with a finite balance, behind a public URL. A portfolio link that is
dead because a few hundred people clicked it is the worst outcome this repository can
produce -- the link is already on the CV by then. And the decision is not additive: it
changes what the marts must answer, because the cached questions have to be answerable
from them. Bolting a budget on afterwards would have meant discovering that the
fallback path needed a mart that did not exist.

**The shape, which is `bq_safety`'s on purpose.** Same problem, so the same answer:

- *Price the call before making it.* There is no dry run for `generateContent`, but
  `countTokens` is free and exact and `max_output_tokens` bounds the other side, so the
  worst case is known before the call is issued. The budget object hands back the
  output cap as request config rather than trusting the caller to set it -- an
  unenforced limit is "prompt instructions are not a control" wearing a different hat.
- *Per-session ceiling.* One visitor cannot spend the month in one sitting.
- *Per-day and lifetime ceilings, shared across sessions.* A per-session cap cannot see
  a thousand visitors taking one turn each, which is exactly what a link going around
  looks like.
- *A reserve that is never spendable.* The key is shared with the enrichment work, and
  a demo that drains it takes away the ability to re-run a label pass.

**Reserve, then settle.** `check()` writes the worst case to the ledger *before* the
call; `record()` settles it against the actual `usage_metadata` afterwards. Checking
without reserving is a check-then-act race on the one number that must not go negative:
the API call sits between check and record, and two visitors arriving in that window
would both be told yes against the same balance. Settling against metadata rather than
the estimate is [[olist-cost-accounting-traps]] applied in advance instead of after.

**The control that actually matters is not a ceiling.** Caps stop a demo dying
expensively; they do not stop it being useless once they trip. So the default state is
free: six worked questions with their SQL and their answers precomputed, and a
degraded mode that names which ceiling was hit and when it resets. A visitor who never
asks their own question cannot tell the difference.

**Two of these exist because a test failed, not because they were designed in.** The
per-session lock was originally per-*instance*, which looks like mutual exclusion and
provides none, since the ledger is shared between instances and the locks were not. The
ledger's temp file was also a single shared name, which is its own race. A ten-thread
concurrency test caught both; without it they would have shipped, and the failure would
have looked like a budget that simply did not hold under exactly the traffic it existed
to survive.

**What it does not do.** The ledger is safe across threads in one process, which is
what a Streamlit app is. It is NOT safe across processes -- two instances on separate
hosts would each keep their own count. That needs a shared store, and until the demo
runs that way the honest thing is to say so rather than imply a guarantee the code does
not provide.

## 10. Frequency is not scored into quintiles

**The call.** `fct_segment_aspect` segments on Recency and Monetary. Frequency travels
as a raw count and an `is_repeat_customer` flag and is not quintiled.

**Why.** Textbook RFM quintiles all three. Measured first: 96.88% of customers have
exactly one order. `NTILE(5)` over a column that is 1 for nineteen customers in twenty
does not find five groups -- it cuts the ties into five arbitrary blocks, and which
block a customer lands in is decided by the sort's tiebreak rather than by anything
about the customer.

The failure mode is what makes this worth writing down: the score would look exactly
like a real one. Five populated buckets, a plausible distribution, and a third of the
segmentation driven by noise. Nothing downstream would have complained.

**The general form.** This is the same finding as the one the whole project opens with
-- `customer_id` is order-scoped, so `dbt snapshot` cannot work on it. In both cases
the idiomatic approach produces output rather than an error, and the only way to notice
is to check whether the input supports the technique before applying it.

**Recency is anchored to the extract, not to today.** The data ends 2018-10-17 and is
static, so measuring recency against `current_date` would collapse R to a constant that
drifts by one each night. Same reasoning as ADR 0002 on the schedule.

## 11. Coverage is a column, and both denominators are published

**The call.** `fct_segment_aspect` carries `review_text_coverage_pct` per segment, and
publishes every aspect rate twice -- once over orders with review text, once over all
orders.

**Why not one rate.** Only ~41% of orders carry review text, and the share differs by
segment: 37.4% to 43.9% across the six. So a segment showing few delivery complaints
might have few complaints, or might be a segment that does not write reviews. Those are
opposite conclusions from the same number, and a single rate makes them indistinguishable.

`recent_low_value` is the live example: it has both the lowest complaint rate and the
lowest coverage, so part of its apparent contentment is simply silence.

**Why both denominators rather than the better one.** There isn't a better one.
`aspect_rate_of_reviewed` is what people actually want and is the one that invites the
cross-segment mistake; `aspect_rate_of_all_orders` is safe to compare but understates
every aspect by the coverage factor, and read alone it looks like the aspect is rarer
than it is. Publishing both, with coverage beside them, is the only combination that
cannot be read wrongly by accident.

**The complete grid.** Every segment carries a row for every aspect, zero-filled. A
missing row and a zero are different claims -- "nobody in this segment mentioned it"
versus "this combination was never computed" -- and a chart cannot tell them apart. The
grid comes from a seed generated from `enrichment/taxonomy.py`, so the aspect list is
not written a second time in SQL.

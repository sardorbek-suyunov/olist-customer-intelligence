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

## 12. The embedding path needed its own guards, not the labelling ones

**The call.** `enrichment/embed.py` is a separate module with its own arity check,
normalisation check, rate limiter and cost accounting, rather than a call bolted onto
the labelling pipeline.

**Why, in three findings that all came from probing before running.**

**`contents=["a", "b", "c"]` returns ONE embedding, not three.** The SDK folds a list of
strings into a single Content and embeds the concatenation. It does not error and does
not warn; what comes back is a perfectly ordinary unit vector, for a text nobody wrote.
`list[types.Content]` returns one per text. Every batch is now checked for arity -- the
same guard `parse_batch` applies to a labelling response that silently covers 19 of 20
reviews. It is the same bug in a new place and it is worse here, because a merged
embedding is indistinguishable from a real one downstream: nothing fails, search just
quietly returns the wrong neighbours.

**The embed response carries no `usage_metadata`.** The labelling log records what the
API said it charged; there is nothing to record here, so tokens are counted with
`countTokens`. That is exact rather than estimated -- same tokenizer, free -- but it is
weaker provenance than usage metadata, and the module says so rather than letting the
two look equivalent in the cost log.

Batching that count introduced its own trap: `countTokens` over N Contents returns
exactly one token less per Content than counting them individually, because each item
carries framing. Measured at 1.00/text across batches of 5, 20 and 50. Since each text
is embedded on its own, the billed figure is the individual sum, so the batched call is
corrected by `+ len(texts)`. Without it the corpus undercounts by about 5%: small,
plausible, invisible. `verify_token_correction` re-checks the relationship on real texts
before each run relies on it, because a tokenizer change would otherwise shift every
cost figure while keeping the log internally consistent.

**The quota counts contents, not requests.** Runs kept dying on
`embed_content_paid_tier_requests`, limit 3,000. The obvious reading is 3,000 HTTP
requests a minute, and the job makes about 1,400 in total -- on that reading it could
not trip, and it tripped repeatedly. The first died at 3,187 vectors after roughly 64
calls. So the limit counts the items inside the batch, and the floor on wall time for
35,616 texts is about twelve minutes regardless of concurrency.

Retrying does not help against a per-minute quota; it spends attempts waiting for a
window that opens on a schedule. Pacing does. And a textbook token bucket was still not
enough: it starts FULL, so the first instant released a whole minute's allowance at once
and the burst crossed a sliding window that the average never would. The bucket now
holds a tenth of a minute and starts empty.

**What the failures exposed that was worth more than the run.** The retry logic lived
inside `GeminiClient.generate`, so the embedding path -- calling the SDK directly -- had
none. The retry existed the whole time; it was welded to the wrong method. It is now
`with_retries`, used by both.

## 13. Unlogged spend is found by comparing the log to the thing it describes

**The call.** `scripts/reconcile_embedding_cost.py` counts the tokens behind the vectors
that actually exist and compares that to what the cost log claims. `make embed-reconcile`.

**Why a reconciliation rather than more discipline.** This project has now lost spend
from its own cost log twice, in two different ways:

- Three exploratory labelling calls made straight through `GeminiClient`: $0.0863, no rows.
- A full-corpus embed run that died on a 429 with 2,800 vectors written and the cost row
  never reached, because it was written after the loop rather than in a `finally`.

Both are invisible from inside the log. The log was not wrong about anything it
contained; it simply did not know those calls had happened, and reading it more
carefully would never have revealed that. The only way to see it is to compare the log
against an independent record of the same events -- here the vectors themselves, whose
token count can be recovered exactly and for free.

It found the second one immediately: 51,799 tokens, $0.0104, recorded as a labelled
correction row rather than folded quietly into an existing one.

**Two structural changes alongside it.** The cost row is now written in a `finally`, so
a crashed run still reports what it spent before crashing -- proven on the next crash,
which recorded $0.0349 on its way out. And `GeminiClient` warns on construction when it
has no cost sink, naming the spend that will go unrecorded: a probe is still allowed, it
is just no longer silent.

**The general form.** Every control here is an instance of one rule, and this is that
rule applied to the log itself: a self-report is not evidence. The cost log is a
self-report. The vectors are the thing.

## 14. The SQL guard parses, and IAM is what actually stops a DELETE

**The call.** `analytics/sql_guard.py` parses agent SQL with sqlglot and decides against
the AST. `bq_safety`'s regex stays as a second opinion. Neither is the security
boundary: the service account holds `roles/bigquery.dataViewer` on the marts dataset and
`roles/bigquery.jobUser` at project level, and nothing else.

**Why a parser.** A regex reads text; SQL is a grammar. Two statements a
keyword-and-semicolon check cannot classify correctly:

    select 'delete from x' as note from t         -- a word inside quotes
    select * from t where c = 'a; drop table y'   -- a semicolon inside a literal

Both are harmless and both get rejected. That is wrong in the safe direction, which
sounds acceptable until it is priced: a false rejection costs a retry, and a retry costs
a slice of the visitor's budget to re-learn something the agent already did correctly.

**Why the allowlist is not the boundary.** `check()` takes `allowed_tables`, and it is
there for a clear error message rather than for safety. An allowlist in application code
protects nothing once the application is the thing that is wrong -- a bug in the guard,
or a future contributor adding a second query helper that forgets to call it. The grant
survives the code being wrong, which is the only property that matters. The setup script
grants at DATASET level rather than project level on purpose: one word's difference, and
a project-level `dataViewer` would hand the agent the raw extract where the identifying
columns live.

**LIMIT is injected, not required.** An agent told "always add a LIMIT" usually will.
Usually is not a control. Rather than rejecting the statement and spending a retry, the
limit is added to the AST and the rewritten SQL is what executes. An existing smaller
limit is kept; a larger one is lowered; a non-literal one is replaced, because it cannot
be compared and so does not get the benefit of the doubt.

The guard therefore returns SQL rather than a verdict. A caller that validates one
string and executes another has a guard in name only.

## 15. The agent is scored on execution accuracy, and the failures are the report

**The call.** 25 gold question/SQL pairs, scored by running both statements and
comparing result sets. Not string similarity.

**Why.** There are many correct SQL statements for any of these questions. `count(*)`
and `sum(1)`, a JOIN or a correlated subquery, a CTE instead of a nested select --
identical in result, arbitrarily different as text. Scoring on text rewards SQL that
looks like the answer key rather than SQL that answers the question, and penalises a
better query for being better.

**What counts as a match, and why each rule exists.** Values rather than column names:
the agent naming a column `complaint_rate` where the gold says `rate` is not a defect.
Row order significant only when the gold has an ORDER BY: a question that does not ask
for an ordering has no wrong ordering. Floats to a tolerance: a SUM over a different
join order differs in the last bit, and an eval that fails on that is measuring IEEE 754.

**The gold set is validated before it is used.** Gold SQL that is subtly wrong produces
an eval that reports a number and measures nothing -- worse than no eval, because it
carries authority. `--validate-gold` runs every statement and reports any that break or
return nothing, against DuckDB, so checking the answer key costs nothing.

**Trap questions are labelled.** Several of the 25 are questions where the obvious SQL
is wrong in a way that still returns plausible rows: the grain of `fct_segment_aspect`,
where a segment-level column repeats once per aspect, and the two denominators. They
score the same as the rest. The category exists so the failure analysis can say *where*
it failed rather than only how often, because those are the failures that would reach a
reader as a confident wrong number.

**The number ships with its failures.** 19 of 25 with six explained is a stronger claim
than 25 of 25, and it is the standard the aspect eval is already held to.

## 16. BigQuery loaded the vectors twice, wrongly, and the dry run approved both

**The call.** `scripts/measure_vector_search_bytes.py` lands the Parquet in a staging
table with an INFERRED schema, flattens it to `ARRAY<FLOAT64>` in SQL, verifies every
row is 1536-dimensional, and then measures by EXECUTING the queries rather than only
planning them.

**Why all four steps exist.** Each one is a failure that happened.

*Inferred schema* gives a `RECORD` column. The data is all there -- 420.8 MiB of it --
and `VECTOR_SEARCH` refuses the type outright. Loud, and easy to fix.

*Explicit `REPEATED FLOAT64`* gives the right type and silently drops every value.
35,616 rows load, the job succeeds, the table reports 3.4 MiB, and `array_length` is
**0 on every row**. Nothing failed. This is much worse than the first failure, and it
is the one that would have shipped.

*The dry run approved it.* Against that empty table, planning the search returned
"1.2 MiB, 0.1% of the per-query ceiling, OK". The query only fails when executed:
"Dimension of column embedding in the base table does not match the dimension of the
query data." A dry run validates the shape of a statement, not the contents of a
table -- so a byte measurement built on dry runs alone reported a comfortable pass on
a table that could not be searched at all.

It approved a second impossible query later, for a different reason: an all-zeros
probe vector plans fine and then fails with "Cannot compute cosine distance against
zero vector". Hence a real vector, read out of the table.

**And the script wrote its own version of the bug.** Against the empty table it printed
`Worst case 0.0 B, infx inside the per-query ceiling` -- a pass describing a
measurement that never happened, produced by the script written to measure one. It now
refuses to report when the table is empty or when every query failed to plan.

**What the measurement says.** The full demo shape -- search, then joins to the review
text, the orders fact and the aspect mart -- bills 435 MiB, 42.5% of the 1 GiB
per-query ceiling. It fits, 2.4x over.

The useful finding is which ceiling actually binds. The 5 GiB per-SESSION budget allows
**11 such searches**, not the 50 queries `DEFAULT_MAX_QUERIES_PER_SESSION` permits. At
3072 dimensions it would be five. The truncation to 1536 was chosen on arithmetic
before the run and the measurement confirms it -- but the session budget, not the
per-query one, is the number to watch when the demo is wired up.

## 17. The seller question is documented, not fixed

**The call.** The agent answers "which sellers have the longest average delivery
times?" with a cross join and 200 rows of the global average. It stays that way,
written up in the README with the SQL it produced.

**Why it happens.** `fct_orders` is at order grain with `seller_count`, a count.
It has no `seller_id`; `dim_sellers` has no order key. The relationship lives in
`order_items`, which is not a mart. The question is unanswerable from this
warehouse, the agent was not told that, and `ON o.seller_count > 0` was the only
predicate it could find.

**Why not add a mart.** A `seller_items` mart answers this question and not the
next one. The gap is structural -- a dimension with no path to the fact -- and
there are others like it. Building the one mart that closes the case someone
already found is how a demo ends up with a suspiciously good hit rate against
exactly the questions its author tried.

**Why not a prompt rule.** Naming this join in the prompt is tuning against a
case already seen. Decision 5b iterated the labelling prompt once, on a class of
error diagnosed across the eval, and said so; adding a rule for one question
found by hand is not that, and it would move the gold score without moving
capability. The eval's whole claim is that its number was not chosen after
seeing which cases it fails.

**What would actually fix it** is a guard that refuses a join whose predicate is
not a key relationship -- a real feature with a real design and a real false
positive rate, since `ON 1=1` is legitimate in a deliberate cross join. That is
a piece of work, not a patch, and pretending otherwise by special-casing this
query would leave the class of error untouched while making it invisible.

**What it is worth as it stands.** Every control passed: single SELECT, permitted
tables, allowlisted functions, injected LIMIT, priced before the call, inside the
timeout. The failure is orthogonal to all of them. That is the most useful thing
the demo demonstrates about LLM-written SQL, and removing it would remove the
demonstration. See the README's "A question it answers confidently and wrongly".

## 18. Terraform describes what exists, including the parts that are wrong

**The call.** `infra/terraform/` was written from an enumeration of the live
project, imported, and planned to zero changes before anything was committed. Two
resources that are demonstrably misconfigured are codified as they are, and one
control the documentation claimed was deleted from the documentation rather than
created in the project.

**Why enumerate first.** Infrastructure-as-code has a failure mode the rest of
this repository would recognise instantly: a `.tf` file looks like infrastructure
whether or not it matches any. Nothing compiles it against reality, and a reader
has no way to tell a reconciled config from an aspirational one. The first draft
here proved the point -- it gave each dataset a description, and the first plan
reported four in-place updates, because the live datasets have none. Config that
would have CHANGED the project on first apply while claiming to describe it.

**The quota was corrected, and the unit nearly caused a second failure.** The
intended control -- a daily ceiling on query bytes -- did not exist; two overrides
at 10 GB/day did, on `quota/extract/bytes` and on AlloyDB cross-region federated
query bytes. The real ceiling is now set at 10 GiB/day and verified.

The value is `10240`, not `10737418240`. The Service Usage API expresses this
metric in MEBIBYTES: the documented default is 200 TiB and the API reports
209715200, and 209715200 MiB is exactly 200 TiB. The first draft used the bytes
figure, which would have set ~10 PiB/day -- no limit at all, reading in the
config like a tight one, and reporting success. The console shows the same
number in TiB, a third unit for one value. Anything that looks like a units
question in this project deserves the arithmetic written out.

The AlloyDB override was DELETED: it constrained a product this project does not
use, so there was no reading under which it was a control. `quota/extract/bytes`
was KEPT, and thereby converted from an accident into a decision -- an extract
job is the one real egress path out of the warehouse, nothing here runs one, and
10 GB against a ~120 MB dataset cannot bite a legitimate use while capping a bad
one. Keeping an accident because it turned out useful is a bad habit; recording
why you are keeping it is what makes it a decision instead.

**The budget was the one asserted control that was true.** It could not be
verified before, because `billingbudgets.googleapis.com` was not enabled and
listing budgets without it fails with a permission error that reads like a
missing grant. Enabled and imported: $5 monthly, thresholds at 50/90/100/150%,
exactly as claimed. Two of three asserted controls turned out false and this one
did not, which is the argument for checking rather than against it. Its filter
carries no `projects` entry, so it covers the whole billing account -- the same
number today, a different number the moment a second project exists.

**Why the IAM row was deleted rather than implemented.** The README described a
service account holding `dataViewer` on the marts dataset as "the control that
actually stops a DELETE", with the parser, the budget and the byte ceiling
presented as defence in depth on top of it. Project IAM holds one binding, the
owner; every dataset carries only the four GCP defaults. Creating the service
account now would make the sentence true, and would also mean the repository
spent eight weeks citing a control it did not have and then quietly built it. The
sentence is struck through instead, with what it actually claimed and why it was
wrong, because that is the more useful artifact.

**And the dead code went with it.** The dashboard's "BigQuery (live)" toggle
could never run in production -- it needs `GCP_PROJECT_ID`, the deployment sets
only `GEMINI_API_KEY`, and a guard fell back to the snapshot. A control any
visitor could click that did nothing but print a red error. Removing it buys a
stronger claim than fixing it would: the demo runs entirely on committed Parquet
and no code path could use a warehouse credential. The record and the dead code
were separable; only the record was worth keeping.

**Why CI does not plan.** A plan needs credentials. This pipeline has none by
design, and a long-lived service-account key in repository secrets would trade
that for a badge. Workload Identity Federation is the correct fix and is
explicitly not-done rather than unknown. The local plan is committed as evidence.

**What is deliberately unmanaged**: the project (a stray destroy would take the
datasets, the billing link and the enrichment output), project IAM (a botched
apply could remove the only administrator), and the Google-managed service
account backing the Gemini API key.

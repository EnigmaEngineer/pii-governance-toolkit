# Limitations

Moved out of the README so the front page stays short. Back to the [README](../README.md).

## Known limitations

**Closed in v2. The grain is measured at classify time now.** `pii/grain.py` sends one
statement per table and the share of rows standing for one person scales the rescued
temporal signal. `analytics.encounter_daily.day` scores 0.4319 rather than 0.4500 and the
same mart regrouped to a month and a department falls out of the review band entirely. The
tool still does not decide for the reviewer, and that part was never the limitation.

**Closed in v2. Round two re-decides from round one's signals instead of re-running the
arms, and the grain is measured only for the tables it rescues.** On the shipped warehouse
that is 47 arm runs down to 42 and 6 statements down to 2. The repeat was never a round trip
cost, which is what I had assumed, and the saving is small here because the rescued set is
one table of five.

**The round two measurement runs on a synthetic fixture and the real saving on this
warehouse is four statements.** The 397 statements saved at 400 tables comes off tables this
repo generated for the purpose, with the rescued count held at 3 so the two costs could be
told apart. A warehouse where most tables are derived marts would sit between the two shapes
the probe prints and nothing here says where.

**The floor the discount creates was not chosen by anybody.** It is 0.7778, which is the
review band divided by the temporal rule's strength. A mart where half the rows are one
patient is ignored on that arithmetic. Raising the temporal strength would lower the floor
and would also move every date column in the warehouse, so the two cannot be tuned apart
while the discount is a multiply.

**The two coarse marts are mine.** They are regroupings of the same generated encounters,
so the shares they produce are facts about `pii/corpus.py`. Two things in that section are
not about the corpus. The floor falls out of two constants. The finding that k cannot
separate the bands is a property of `min`.

**The access report answers over a window and the window changes the answer.** There is no
policy application date anywhere in it, so it cannot answer the question an auditor
actually asks, which is who read the column while it was exposed rather than who read it
at all. Every figure in that section is "over this range" and none is "before the mask went
on".

**`did_read` is a floor and nothing closes the gap.** A `SELECT *` records no column, so
for 16 of 25 flagged columns the report says between nobody and somebody. Closing it needs
catalog history the warehouse does not keep.

**Grants are modelled at table level only.** Real warehouses also have column level
policies and row access policies. They have future grants and ownership and role
inheritance. None of that is here. Role inheritance is the one that would change the
reachable sets, because a role granted another role inherits its tables and this model has
no edge for that.

**The grants and the query log are generated, and no adapter reads a real one.** Snowflake
keeps both in `snowflake.account_usage`, on a different database with its own retention
window. `pii/access.py` takes sequences of `Grant` and `QueryEvent` and the code that would
fill them from an account does not exist. Writing a reader that has never run against an
account and presenting it as working is the thing this project does not do.

**The reviewer decisions in `scripts/review_probe.py` are mine.** Four of ten, stamped with
my name and a reason. They are there so the unblocking can be measured on something, and
they are not the tool's answers.

**The k target defaults to 2 and 2 is a weak bar.** It says one other person shares your
group. Expert determination work does not use that number. It is here because it is the
smallest integer that is not 1, which is arithmetic rather than privacy, and picking a
defensible one needs a population and a threat model this corpus does not have.

**The scaling result is a planner cost and not a round trip cost.** DuckDB runs in
process, so the 672 round trips the per column shape makes here are function calls. The
measurement shows that one statement with 6048 aggregates in it costs far more to plan than
672 small ones cost to plan and run, on this engine and this machine. It says nothing about
a warehouse across a network, where a round trip is milliseconds of latency rather than a
call. A Snowflake reading would be the one that settles it and there is no account to take
it on.

**No masked table is written.** Every masking expression is executed, inside the residual
measurement and inside the checks, and nothing materialises a masked copy of the warehouse.
Application here means the policy runs and its result is measured, not that a masked table
exists.

**The group size column is discovered now, and the discriminator is narrow.**
`mask.weight_for` reads it off the lineage graph. A `count(*)` names no field, so the
reader writes it as an `AGGREGATE` edge whose source column is `lineage.ROW_COUNT_SOURCE`.
That constant is a literal `*` and the star is the entire test. It picks `encounters` on
the mart and nothing on the four raw tables. `mean_length_of_stay_h` is also an `AGGREGATE` target and
its sources are named columns, so it is an average over rows rather than a count of them,
and without the star test it would be a second candidate. Two candidates refuse rather
than guess.

That star is a convention of this reader and not a property of SQL. A graph arriving from a
dbt manifest or an `information_schema` view would record nothing of the sort. The constant
is where that coupling is visible, which is the most the code can do about it without a
second reader to test against.

**A missing weight edge used to mean two different things and now it means one.**
This came up on the day the typed in dict was deleted, because deleting it narrowed the
blast radius rather than removing the defect. `weight_for` returned `None` both for a table
nothing derives, where rows really are people, and for a table whose derivation statement
the reader walked away from partway through. The second case reported a pre-aggregated
table as safer than it is by exactly the factor its `GROUP BY` deduplicated by, silently.

The fix is a refusal rather than a guess, and it needed the reader to say which table it
failed on. `lineage.Refusal` now carries the insert target it was raised under, empty when
the reader gave up before parsing one. `mask.derivation_of` reads that into three states.

```
analytics.encounter_daily      read           encounters
raw.claim                      not_derived    rows are people
raw.device_reading             not_derived    rows are people
raw.encounter                  not_derived    rows are people
raw.patient                    not_derived    rows are people
```

On this warehouse nothing changes, because the one derived statement parses with zero
refusals. The cost shows up the moment a warehouse holds a statement this reader cannot
read. Merge in a single `MERGE`, which is rejected on the keyword before the reader learns
what it writes to, and four of the five tables stop answering.

```
with one unreadable statement   refusals 1   unattributed 1
                                tables refused 4   tables answered 1
```

The mart keeps its answer, because a found count edge is evidence and a refusal elsewhere
does not unsay it. An unattributed refusal makes every other table unreadable, which looks
harsh and is the only honest reading. The reader knows one table's derivation is unread and
cannot say which, so calling the rest underived would be the same guess in a new place. The
figures are in `measurements/weight-refusal.json` and two checks re-derive them from a
database.

This trades a quiet wrong answer for a loud missing one. On a real warehouse with one
unparsed statement that means no k for anything, which is worse to use and better to
publish.

**The compliance report has no addressee, no date and no signature.** It is a document
generated from a scan. Whether the thing a regulator accepts also needs a named
determination, a retention statement and somebody's signature on it is a question about
compliance rather than about scanning, and the answer is almost certainly yes. What is here
is the evidence such a document would be built on.

**The masking policy blocks its own measurement everywhere now.** All five tables hold at
least one undecided column, so `residual_sql` refuses on all five. The only residual table
in the README with numbers in it is the upper bound pass, where a reviewer is assumed to
accept everything, and it is labelled a hypothesis rather than a result.

**`masking_expression` implements two of the seven granularities.** `YEAR` and `POSTAL_3`.
The rest raise rather than returning the column untouched. The taxonomy is wider than the
applier.

**The sampling arm reads an ordered sample and that is a limitation rather than a choice.**
`sample.sample_column` takes the first 500 rows. An ordered sample of a clustered table sees
one region of it, so a format used by one site only can be missed outright even when the
shape rule would have found it. A random sample costs a full scan on this warehouse and
nothing here has measured what that costs at width.

**The no values property is weaker than it was.** It used to be that the profile held
metadata and integers. With the sampling arm on it also holds signals whose detail carries a
shape, and a shape keeps the column's punctuation verbatim. The bound on that is an
allowlist of twelve separator characters and a check over it, which is a real bound and not
the same bound as before. The arm is off by default for this reason and not only for the
discovery tradeoff.

**The fixture that grades the sampling arm is mine, same as the planted labels.** Four
columns, four formats I chose. It says the mechanism separates a fixed shape code from
nothing without holding a rule for that kind of code. It does not say what share of real
unknown formats the arm would find, and nothing here could.

**The planted labels are mine.** Only the Safe Harbor clause list has another author. The
recall figures are a classifier I wrote graded on a schema I wrote against a scope somebody
else fixed, and those are three different strengths of evidence sitting in one table.

**Every row is generated.** The names come from two lists of twenty five. A real generator
pulls from census files and this one cannot.

**One labelling call of mine moves the headline.** `raw.patient.city` is planted as a postal
code at the coarse end, on the reading that clause B covers all geographic subdivisions
smaller than a state. That puts it in scope and makes it a miss. Drop it and recall goes
from 13/20 at 0.6500 to 13/19 at 0.6842, and the quasi figure from 4/9 to 4/8. Both readings
are here rather than only the one that reads better.

**Seven of the categories are never reached by any column, and six have no rule either.**
The tool reports this per category now rather than leaving it to a paragraph. `Regime.PCI`
is in the same position with zero columns under it, against 19 for HIPAA and 2 for GDPR
special.

**The classifier is graded against two answer keys and they are not interchangeable.**
Recall is counted over the Safe Harbor scope, because that list has another author. A false
alarm cannot be. A column outside Safe Harbor scope is not thereby harmless, since `sex` is
a quasi identifier this corpus re-identifies people with and a diagnosis is a GDPR special
category, and neither is on the Safe Harbor list. So the only thing that can say a column is
not personal is the planted label, which is mine. Using one key for both halves changes the
false alarm count from 2 to 5.

**Every weight and both thresholds are numbers I chose.** The token strengths. The match
floor at 0.60 and the conflict penalty at 0.5. Accept at 0.75 and review at 0.35. Nothing
here is fitted and nothing is calibrated. The probe sweeps the accept threshold and the table
context bar, and the rest are stated rather than defended.

**A single letter categorical column reads as a sex column on values alone.** The vocabulary
predicate matches `M` and `F` and `X`, so a column of letter grades under an unhelpful name
matches all of it. That predicate sits below the accept threshold for this reason, which
means the column reaches a reviewer rather than being masked, and the check asserting it is
in `tests/test_profile.py`.

**The sensitive axis contributes nothing to the measurement.** The recall split prints
`sensitive  nothing in scope`, because a diagnosis is not a Safe Harbor identifier. The
argument for that third of the taxonomy is reasoning rather than a number, and the table
above should not be read as covering it.

**The load uses `executemany`.** It moves about 7,000 rows in well under a second here. It
does not scale, and the answer at volume is a CSV and a `COPY`.

**The recovered nullability is a round trip and the crawl is what it proves.** `plant.py`
writes the DDL out of `pii/schema.py`, so a recovered `nullable=True` is this repo's own
declaration having gone through a database and come back. That the crawl reads it correctly
is a fact about the crawl. Whether the declaration is right about the column is a different
question, and only the null counts speak to it.

**The null rate is uniform across every column, which no real warehouse is.** A missing
discharge date and a missing postal code have different causes and different rates, and
modelling that would mean inventing a story per column. One rate is the honest version of
having no such story.

**A nulled discharge date does not retroactively change the claim derived from it.**
`raw.claim.submitted_on` is computed from `raw.encounter.discharged_at` before the nulls are
applied, so a claim can carry a date the warehouse no longer holds. The alternative is
nulling before the derived values exist, which would mean the generator handling missing
inputs everywhere.

**The crawler is DuckDB only.** `duckdb_columns()` and `duckdb_constraints()` are engine
specific catalog functions. `information_schema` would port further and does not carry the
primary key information in a form this needs. A Snowflake crawl is a second adapter and it
has not been written, let alone run.

**A reference can only be recovered inside one schema.** DuckDB refuses a foreign key across
schemas with a binder error, so `analytics.encounter_daily` cannot declare one back to
`raw.patient` even though every row in it came from there. The catalog also hands back the
referenced table as a bare name with no schema on it. Resolving that inside the referencing
schema is correct for this engine and is a guess on any engine that allows the cross schema
case.

**Lineage reaches five of the forty two columns.** The graph is built from the statement
that loads the mart, and every other column in this warehouse arrives by a path no SQL in
this repo describes. That is not a gap in the reader. It is what a lineage layer is on a
warehouse whose raw tables are loaded rather than derived, and it is the reason the
classifier is still the thing doing the work.

**The floor reads a fixed sample of fifty rows and needs a two thirds majority.** Both of
those are numbers I chose rather than measured. The classifier's value arm reads the whole
column instead, because a count is cheap and a sample is not obviously representative.

**The table context rule reads the classifier's own output.** Whether a table is about
people is decided by whether this classifier found a direct identifier in it, so a table
whose identifiers it misses becomes a table where every date is dropped. Following a column
back to its source is what would actually answer the question, and that is the lineage work
rather than a heuristic.

**DuckDB is the local stand in for Snowflake.** The DDL generated here is ANSI enough to
port and no Snowflake statement in this repo has ever executed. Anything that changes will
say so in this section rather than in a commit message.

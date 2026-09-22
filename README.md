# PII discovery and governance toolkit

Scan a warehouse and work out which columns hold personal data. Generate the masking
policy that follows and produce an audit trail somebody would accept. This is the
classification layer. A taxonomy with three fields rather than one label. A sample warehouse
with personal data planted in it on purpose. A classifier that returns a confidence with
every answer, and a measurement of what that confidence is actually worth.

```
python3 scripts/coverage_probe.py
```

That needs nothing but the standard library. Building the warehouse, crawling it back and
classifying it need DuckDB.

```
pip install -r requirements.txt
python3 scripts/plant.py --db /tmp/pii.duckdb --key
python3 scripts/crawl_probe.py --db /tmp/pii.duckdb
python3 scripts/classify_probe.py --db /tmp/pii.duckdb
```

## What is here

```
pii/taxonomy.py     categories, the three fields, the granularity ordering
pii/safeharbor.py   the HIPAA Safe Harbor clause list, as an external answer key
pii/schema.py       the sample warehouse and its planted labels
pii/corpus.py       generated rows for that schema, nulls included
pii/crawl.py        recovering the schema from a live catalog
pii/profile.py      counting things about a column without reading one
pii/classify.py     three arms, a confidence, and three bands
pii/lineage.py      column level edges, recovered from the statement that built the table
pii/mask.py         masking policy generation, application, and the k left afterwards
pii/review.py       the queue a person works, ordered by what a decision frees
pii/access.py       who could have read a column, and who did
pii/naive.py        the obvious name and regex scan, kept as a floor to measure against
pii/coverage.py     grading the floor against the clause list
pii/reidentify.py   uniqueness and k anonymity over quasi identifiers
pii/rng.py          named random streams
```

```
                     pii/safeharbor.py           pii/taxonomy.py
                   18 published clauses       categories, 3 fields each
                             |                          |
                             +------------+-------------+
                                          |
                                   pii/schema.py
                        5 tables, 42 columns, planted labels
                                          |
                         +----------------+----------------+
                         |                                 |
                   pii/corpus.py                      pii/naive.py
                  generated rows                  name and regex floor
                         |                                 |
                    scripts/plant.py                       |
                    the duckdb warehouse                   |
                         |                                 |
                    pii/crawl.py                           |
                    reads the catalog back                 |
                         |                                 |
                    pii/profile.py                         |
                    counts, never values                   |
                         |                                 |
                    pii/classify.py <--- pii/lineage.py    |
                    3 arms, confidence, bands              |
                         |           edges from the SQL    |
            +------------+------------+                    |
            |            |            |                    |
      pii/mask.py   pii/review.py  pii/access.py           |
      policy and k  the queue      grants and reads        |
            |            |            |                    |
            +------------+------------+---------------------+
                                          |
                          pii/coverage.py   pii/reidentify.py
                          what it finds     who it still exposes
```

The arrow back into `pii/classify.py` is the one edge worth pointing at. The classifier
reads the lineage graph, and it reads it to decide whether a derived table is describing
people, which is a question none of that table's own columns can answer.

## The schema is no longer its own witness

An earlier version of this repo shipped a problem here: `pii/schema.py` was a hand written description of
the warehouse `scripts/plant.py` then built, so any check that the two agreed was a check
that one of them had written the other. Nothing was learned by running it.

`pii/crawl.py` reads the catalog instead and rebuilds the same `Table` and `Column` objects
out of what it finds. The content hash over the result is the test, because it covers all
five fields including the nullability and key flags that nothing else consumes yet:

```
engine                     duckdb v1.5.5
tables recovered           5
columns recovered          42
key columns recovered      4
declared fingerprint       1501a19ca3d8
crawled fingerprint        1501a19ca3d8
recovered the schema       yes
differences                0
```

**Getting there needed a change to the warehouse and not to the crawler.** The emitted DDL
carried no primary key at all, so `is_key` existed only in this repo's own Python. Four of
the five fields in that hash were in the catalog and the fifth was nowhere, which made the
pinned value unreachable for a reason that had nothing to do with reading it. The keys are
declared now, and the crawler recovers them from `duckdb_constraints()` rather than from
column names, which matters because `raw.encounter.patient_id` ends in `_id` and is not a
key.

The crawl reads no rows. Names, types and flags all come out of the catalog, which is the
property a tool wants if it is going to be pointed at data somebody is not cleared to see.
`grade_nullability` is the one function that touches data and it reads `count(*) - count(c)`
per column, so it returns no values either.

**The fingerprint is asymmetric about order and it is worth knowing which way.** It sorts
the tables, so reversing them leaves the hash identical. It does not sort the columns, so
swapping two inside a table moves it. A check written on the opposite assumption failed and
that is how this was found. Column order is reported by the comparison as its own kind of
difference, because a reordered table and a changed one are not the same event.

## Nullability is a claim about the data now

The corpus writes nulls. It did not at first. While it did not, every nullable flag was a declaration nothing had
ever exercised. The suite could check that a NOT NULL column holds no null, which is the
easy direction. The reverse reading had no evidence at all.

From `scripts/plant.py`, about the four generated tables:

```
  nullable_columns             26
  nulls_total                  811
  nullable, never null         none
```

From `scripts/crawl_probe.py`, about all five tables in the database:

```
columns graded             42
declared not null          13
not null holding a null    none
nullable and exercised     29
nullable, never null       none
```

The rate is `pii.corpus.NULL_RATE`, which is 0.02.

The rate is a number chosen rather than measured. It is low enough that no aggregate moves
far and high enough that a thousand rows are very unlikely to miss a column by chance, and
the suite asserts the observed count rather than trusting that arithmetic.

**The nulls are applied in a post pass, after every value is drawn.** So `generate` with
the rate turned off reproduces the corpus as it stood before nulls, byte for byte. The suite
pins the four digests to prove it. That matters because it makes every figure the nulls
moved attributable to the nulls rather than to a shifted draw.

**The first null broke the load, and the schema was wrong rather than the corpus.**
`analytics.encounter_daily.department` was declared NOT NULL and the mart groups by
`raw.encounter.department`, which is nullable. A derived column cannot be stricter than the
column it comes from. Nothing could see it while the corpus had no null in it anywhere. The
flag moved, and moving it moved the published fingerprint from `dcff0aa3e7a5` to
`1501a19ca3d8`. Both values are in the suite rather than one quietly replacing the other.

## The decision this is built on

One label per column cannot carry a masking policy. A postal code is not personal data and
a postal code plus a birth date plus a sex is a person. A diagnosis is not an identifier
and it is the thing identification exposes. A timestamp identifies at second precision and
does not at year precision. None of that fits in a label.

So a category carries three things. What the column does on its own, which is direct or
quasi or sensitive or nothing. Which rulebooks claim it, as a set rather than a severity,
because HIPAA and GDPR disagree about what matters and neither contains the other. And the
granularity at which it stops being an identifier, where that is the deciding variable.

`docs/adr-0001-one-label-per-column-is-not-enough.md` carries the argument.

## What the obvious scan finds

`pii/naive.py` is a list of substrings matched against column names plus a handful of
regexes matched against values. It is what almost every in-house PII scan turns out to be
when you read it, and it is here so that any later classifier has a floor printed beside
it. The name list was written from memory, covering what came to mind, which is the point.

Graded against the Safe Harbor clause list over the sample schema:

```
in safe harbor scope       20 of 42 columns
recall, names only         10/20  0.5000
recall, names plus values  13/20  0.6500
  no value sample reached   analytics.encounter_daily, so 2 in scope columns there were graded
  on names alone. scripts/classify_probe.py reads its sample out of the
  database and sees all five tables, which is why its floor row is higher.
false alarms               0
flagged as wrong category  2

  direct     9/11  0.8182
  quasi      4/9  0.4444
```

Most of the way there on direct identifiers. A coin flip on quasi ones. Both halves are
Safe Harbor identifiers and only one half looks like one, so a single PII flag reports a
number in the middle and hides which half it came from.

What it walks past:

```
  raw.patient.city                         postal_code      quasi
  raw.encounter.admitted_at                event_date       quasi
  raw.encounter.discharged_at              event_date       quasi
  raw.claim.member_number                  health_plan_id   direct
  raw.device_reading.device_serial         device_id        direct
  raw.device_reading.taken_at              event_date       quasi
  analytics.encounter_daily.day            event_date       quasi
```

Four of seven are timestamps. Clause C says every element of a date tied to an individual is
an identifier except the year, and no column name heuristic is going to call `admitted_at`
personal data.

Two more columns are flagged as the wrong thing, and recall counts those as found because
the column did get flagged:

```
  raw.encounter.attending_npi              planted licence_number       naive phone
  raw.claim.submitted_on                   planted event_date           naive phone
```

Both are the same defect. The phone regex is `^\+?\d[\d\-\s]{7,}\d$`, so a ten digit
identifier matches it and so does an ISO date. `2026-03-14` is digits and dashes of about
the right length. It is left unfixed on purpose. Improving the floor is the one thing this
arm must not do, because the gap is the measurement.

## Who the quasi identifiers still expose

Every figure in this section is a figure about `pii/corpus.py`. There is no real patient
data here and there never will be, which is why it is planted. Regenerate with
`python3 scripts/coverage_probe.py`.

```
rows carrying all three    946 of 1000, 54 dropped

quasi identifiers                        k     cells   measured    uniform        gap
sex                                     21         3     0.0000     0.0000    -0.0000
sex + postal_code                        1       177     0.0296     0.0047    +0.0249
sex + postal_code + birth_date           1    164610     1.0000     0.9943    +0.0057
```

That table is about the 946 patients carrying all three columns. Which population a
uniqueness figure is about stopped being a detail the day the corpus started writing nulls,
and the section after this one is why.

`measured` counts rows whose combination is unique. `uniform` predicts the same quantity
assuming every combination is drawn independently and evenly across the product of the
column cardinalities, which is `(1 - 1/K) ** (N - 1)` with no approximation and no constant
in it. The first is a fact about the generator. The second is a fact about combinatorics
and knows nothing about the data.

Printing both is the point. If they agreed exactly I would have built a corpus that
satisfies the prediction by construction and measured nothing, so the generator draws
postal codes on a decaying weight and ages on a curve rather than flat. The busiest postal
code holds 22.5 percent of the population and the gap column is where that shows up.

Three columns, none of which a flat PII flag would mask, and every one of the 946 people is
alone in their cell.

Truncating the postal code to three digits and the birth date to a year:

```
sex + postal 3 + birth year              1      1365     0.2135     0.5003    -0.2868
```

**That row is not evidence about Safe Harbor and the probe says so out loud.** Clause B
permits the first three digits of a postal code only where the area those digits cover
holds more than 20,000 people. This corpus has a thousand patients across five three digit
areas, the smallest of them 10 people, so the allowance does not apply to it at all. The
number is correct arithmetic about the corpus and it is not a result about the regulation.
`pii/safeharbor.postal_3_floor` computes that verdict rather than leaving it to a reader.

Handed the whole corpus it reported six areas rather than five, because the 24 patients with
no postal code were being counted as a three digit area of their own. A null is not a place.
It reads the same population as the table above now.

## A null is not anonymity, and the metric disagrees

Every uniqueness figure here counts a null as a value, because that is what a `Counter`
does. Nobody is protected by this warehouse failing to record their postal code, since
somebody holding that postal code from elsewhere is not stopped by a gap in this table. So
the figure moves and nothing about the person has changed.

**Which way it moves was measured rather than argued, and the first version of this section
had it backwards.** `scripts/crawl_probe.py` sweeps the null rate and prints both
populations. The last column is the honest share minus the naive one, so a positive number
means treating nulls as values made the data read safer than it is.

```
sex + postal_code
  rate   complete  nulls as a    complete honest minus
             rows       value   rows only       naive
  0.00       1000      0.0290      0.0290     +0.0000
  0.02        964      0.0380      0.0280     -0.0100
  0.10        803      0.0430      0.0286     -0.0144
  0.20        624      0.0410      0.0401     -0.0009
  0.40        350      0.0600      0.1200     +0.0600
  0.60        154      0.0570      0.2273     +0.1703
```

A rare null is an uncommon value, so it isolates a row rather than pooling it. At the
shipped rate of 0.02 the naive figure reads 0.0380 against an honest 0.0280, which is less
safe and not more. The sign flips somewhere above 0.20, where the null cell finally holds
enough people to pool them.

```
sex + postal_code + birth_date
  0.02        946      0.9940      1.0000     +0.0060
  0.20        496      0.8400      1.0000     +0.1600
  0.40        220      0.6520      1.0000     +0.3480
```

On a combination that is already fully unique the honest figure is pinned at 1.0000 and
cannot rise, so pooling is the only thing left and the sign never flips. Two combinations
from the same corpus, moving in opposite directions, for a reason that is about the null
rate rather than about anybody's privacy.

**Dropping the incomplete rows is not a free fix either.** It shrinks the population, and
uniqueness depends on how many people are in it. That is why the complete column climbs to
0.2273 at a 0.60 rate off 154 survivors. Both answers move and for different reasons, so
both are printed and neither is offered as the number.

`pii/reidentify.generalise_postal` and `coarsen_date_to_year` now refuse a null by name
rather than raising whatever the language happens to raise on `None`. What a masking policy
should do with a missing value belongs with the masking work and is not something a generaliser gets
to make quietly.

## The confidence is the product, and the recall is not

Three arms feed one score. The column name read as tokens, the values counted by the
database, and the catalog metadata. Each arm produces weighted evidence and the strongest
category wins, so nothing is decided by whichever rule happened to be checked first.

Against the substring floor, from `scripts/classify_probe.py`:

```
                                   substring floor   classifier
in Safe Harbor scope, found               14/20        20/20
right category                            11/20        20/20
masked with no human in the loop    all of them        12/20
flagged and planted not personal              0            2
```

**Read the first row and then discount it.** I wrote the token rules with the answer key
open, so 20 of 20 is a report on my memory rather than on the method. Any classifier
written the same way gets the same number. It is printed because leaving it out would look
like hiding it.

It reads 19 of 20 in the history and this is the run that moved it, so the mechanism is
worth a sentence rather than a footnote. The twentieth column is
`analytics.encounter_daily.day` and nothing about the classifier improved. It stopped
deleting evidence it already had. See the lineage section below.

The third row is the one that is not circular. The floor has no confidence, so every answer
it gives carries the same authority: either all 14 columns it finds are masked without
anybody looking, or none of them are. This classifier masks 12 and sends the rest to a
person, and the question is whether it sends the right ones.

```
accept band  15 columns, 0 of them planted not personal
review band  10 columns, 2 of them planted not personal
ignored      17 columns, 0 of them in Safe Harbor scope

lowest column masked with no human    0.7500
highest column the classifier got wrong 0.7250
margin                                 0.0250
```

Both mistakes land in the band that goes to a human and nothing wrong is masked
automatically. That is the result the design is for and the margin is what it rests on.
Twenty five thousandths, on a scale from zero to one, over forty two columns. I picked the
accept threshold with both of those numbers on the screen, so the separation is a fact
about this warehouse rather than a property of the method.

The third line used to read `18 columns, 1 of them in Safe Harbor scope`, and that one
column was the worst outcome this three band design can produce. A column in scope,
scored 0.0000, therefore in the ignore band, and the ignore band does not queue. It
reached neither a mask nor a reviewer. It is fixed and the fix was not to make the ignore
band queue.

The sweep is in the probe. Below 0.73 a column that is not personal starts getting masked,
and the count of wrongly masked columns is 1 or 2 all the way down to 0.40.

## What the classifier gets wrong, and why neither is fixable here

```
raw.patient.created_at                         event_date               0.72 review    NOT PERSONAL
raw.claim.payer_name                           person_name              0.40 review    NOT PERSONAL
```

`created_at` is the row's load time. `admitted_at`, `discharged_at` and `taken_at` are
dates tied to a patient, and clause C makes every element of those an identifier except the
year. All four are timestamps, all four end in `_at`, and three of them have a distinct
count equal to the row count. **No property of the column separates them.** What separates
them is where the value came from, and this classifier cannot see that.

One of the two does have a table level answer. A date is tied to an individual when the
table is about individuals, so a temporal signal is dropped in any table where nothing else
names a person outright. That removes `analytics.encounter_daily.day`. It cannot remove
`created_at`, which sits in the patient table beside an email address and a national
identifier.

**This README used to call `analytics.encounter_daily.day` a reporting grain rather than
anybody's date, and the group sizes say otherwise.** The mart groups on
day and department and postal code, and 1,386 of its 1,444 rows are a group of one. In 96
percent of that table the day is one patient's single admission, sitting in the same row as
their postal code. The rule that drops the signal still fires and the reason it gave was
wrong. What it turns on is whether the grouping is coarse enough to hide anybody, and
nothing in the classifier measures that. **The planted label now reads `event_date` and the
recall figures above carry the cost.** The denominator went from 19 to 20, the classifier
still calls the column not personal, and that is a miss rather than a find. Keeping an
answer key the data contradicts in order to protect a published number is the trade this
repo is not going to make. See `scripts/lineage_probe.py` and `scripts/mask_probe.py`.

That rule has a threshold in it and the threshold bit immediately:

```
 bar     found   masked    wrong  tables treated as about people
0.35     20/20       12        2  4
0.60     18/20       12        2  3
0.70     18/20       12        2  3
0.75     17/20       12        2  2
0.90     17/20       12        2  2
```

At 0.75 `raw.claim` stops counting as a table about people. Its only direct identifier is a
membership number scoring 0.70, five hundredths under. `raw.claim.submitted_on` then loses
its only evidence and disappears from the results, so one column sitting in the review band
deleted a different column's answer. The bar is the review floor for that reason. Demanding
certainty that a table is about people before conceding that it is runs the wrong way for a
tool whose job is to find personal data.

## Two of the three arms decide almost nothing

Removing one arm at a time and counting how many band assignments move:

```
arms                       found  masked    wrong   bands moved
all three                  20/20      12        2             0
name only                  19/20      13        2             3
value only                  5/20       3        0            21
structure only              0/20       0        0            25
without name               10/20       3        1            16
without value              20/20      13        2             2
without structure          19/20      12        2             1
```

**The structure arm moves no band anywhere.** It changes the number printed beside five
columns and changes no decision about any of them. The value arm moves two. Take the name
arm away and the classifier finds 10 of 20 instead of 20.

So on this warehouse the confidence is mostly a restatement of how sure I was when I wrote
the token list. That is not an argument for deleting the other two arms. It is an argument
that a warehouse with meaningful column names is the easy case, and the honest way to show
what the other arms are worth is to take the names away:

```
in Safe Harbor scope, found        10/20
right category                     8/20
masked with no human in the loop   3/20
flagged and planted not personal   1

  raw.patient.c12              was ssn                    called national_id            0.89
  raw.patient.c05              was email                  called email                  0.88
  raw.device_reading.c07       was source_ip              called ip_address             0.83
  raw.encounter.c06            was attending_npi          called phone                  0.69  planted licence_number
  raw.patient.c06              was phone                  called phone                  0.68
  analytics.encounter_daily.c01 was day                    called event_date             0.45
  raw.device_reading.c04       was taken_at               called event_date             0.45
```

Same rows and same types and same nulls. The column names are replaced by position labels
and the whole thing is rebuilt as a real database rather than stubbed out in Python. Recall
falls from 19 to 9 and automatic masking from 12 to 3. Ten digits is ten digits, so a
clinician licence number and a telephone number come back as the same thing, and only the
name ever told them apart.

## Lineage answers a question the classifier cannot, and it is a smaller question than I thought

The classifier decides one column at a time on properties of that column. `pii/lineage.py`
builds the other view: nodes are columns and edges are derivations, recovered from the
statement that loads the mart rather than from an edge list written beside it. A hand
written edge list is the circularity the crawler was built to break.

```
raw.encounter.admitted_at              -> analytics.encounter_daily.day          cast       cast to DATE, grouping key
raw.encounter.department               -> analytics.encounter_daily.department   grouped    grouping key
raw.patient.postal_code                -> analytics.encounter_daily.postal_code  grouped    grouping key
raw.encounter.*                        -> analytics.encounter_daily.encounters   aggregate  row count over the join grain
raw.patient.*                          -> analytics.encounter_daily.encounters   aggregate  row count over the join grain
raw.encounter.admitted_at              -> analytics.encounter_daily.mean_length_of_stay_h aggregate  avg over the group
raw.encounter.discharged_at            -> analytics.encounter_daily.mean_length_of_stay_h aggregate  avg over the group
7 derivation edges, 0 refusals
```

**The kind on the edge is the whole design.** A grouping key is not an aggregate. A
`GROUP BY` drops duplicate rows and leaves the values in the key exactly as they were, so a
postal code that was a postal code in `raw.patient` is the same postal code in a table
whose name suggests it has been rolled up. Copies and casts and grouping keys carry the
label down. Aggregates do not.

**The reader refuses rather than guesses.** It refuses a star and a common table expression
and a set operation. It refuses a window function and a subquery in the FROM clause. It
refuses an unqualified column with two sources in scope. Each returns a refusal naming what
it could not read. A lineage edge that is wrong gets a column masked or unmasked on evidence
nobody checked. A refusal gets a human.

### It reaches five columns out of forty two

```
42 columns, 37 of them roots with nothing upstream
5 columns have an upstream, which is 11.9 percent
```

That is the number I did not expect and it is the honest headline for this layer. Every raw
table here is loaded rather than derived, so there is no statement to read and no upstream
to ask. A lineage graph on this warehouse describes one mart.

**It does not settle `created_at` against `admitted_at`, which is what it was supposed to
do.** The classifier leaves those two columns indistinguishable by any property either one
carries, and the obvious conclusion was that the thing separating them is where the value
came from. That is right and it does not help. Neither column came from anywhere this repo
can see.
Both are roots. Lineage propagates what somebody already knew at the source, and at the
source nobody knew.

### Where it does earn its place

Strip the mart's column names and the classifier has nothing left:

```
column                       on its own               from upstream           
scratch.rollup_v2.d1         not_personal (ignore)    event_date              
scratch.rollup_v2.grp        not_personal (ignore)    not_personal            
scratch.rollup_v2.geo        not_personal (ignore)    postal_code             
scratch.rollup_v2.n          not_personal (ignore)    not_personal            
scratch.rollup_v2.dur        not_personal (ignore)    not_personal            
2 columns that read as not personal on their own are personal upstream
```

Every one of those five scores 0.0000 with no signal of any kind. There is no value
predicate for a postal code in this repo, so `raw.patient.postal_code` is caught by its name
and by nothing else. Rename it to `geo` in a mart and the classifier goes silent. Upstream
still knows.

On the real mart, which kept its column names, propagation changes one answer out of forty
two. That one is `analytics.encounter_daily.day`.

### The aggregate that does not aggregate

```
1444 rows in the mart, encounters between 1 and 3
1386 of them are a group of one, which is 96.0 percent
```

The mart groups on day and department and postal code, and that grain is almost unique. In
96 percent of the table, a row is one patient's one admission with their postal code beside
it. Calling `encounters` an aggregate is true about the function and false about the result.

This is what settled the `day` column. The classifier drops its temporal signal because
nothing in the mart names a person outright, and the planted label used to agree with the
classifier. Lineage disagreed with both and the group sizes were on lineage's side, so the
label moved to `event_date`. The substring floor finds the column the classifier walked
past, because the phone regex matches an ISO date and the floor has no table context rule to
talk itself out of it. Being right for no reason still counts in the numerator, which is one
more thing a single recall figure hides.

### The classifier reads the lineage graph now, and the fix was to delete less

Moving the label exposed something worse than it settled. `analytics.encounter_daily.day`
was in Safe Harbor scope, the classifier scored it 0.0000, and 0.0000 is the ignore band.
The ignore band does not queue. So the column reached no mask and no reviewer, which is the
worst outcome a three band design can produce, and it was live in this repo for a day.

There were two obvious shapes for a fix and both are wrong.

Queue any column in Safe Harbor scope regardless of its band. That grades the tool against
my own planted answer key at runtime, which makes every figure downstream circular.

Feed a k measurement on the grain into the confidence so the column earns its score. That
is the deeper answer and it is a redesign of the band logic, and it needs a measurement
nothing takes at classify time.

The third option was in a docstring the whole time. `table_is_person_linked` asks whether
anything in a table names a person outright, and its own comment already said the honest
version is following the column back to its source. So `classify_warehouse` runs twice now.
Round one is every table on its own columns, unchanged. Round two re-runs any table that
came back naming nobody, if the tables it was derived from do name somebody, and the
lineage graph is what answers that.

**Round two adds no evidence. It stops deleting evidence.** The rescued score is exactly
the score the column earns with no table rule at all, and a check asserts that the two are
equal signal for signal, because a classifier that invented a number for a column on the
strength of its parentage would be the circular version wearing different clothes.

```
band counts {'accept': 15, 'review': 10, 'ignore': 17}
rescued by lineage: analytics.encounter_daily
```

One column moved. `day` scores 0.4500, which is the review band, so a person decides. That
is the right end state and not a consolation: nothing in the catalog says whether a group
of one survived the `GROUP BY`, so the tool asks rather than answers. The whole of its
evidence is that the catalog type is DATE, and what makes the question answerable for a
reviewer is the lineage trace printed beside it.

The closure is value preserving only. A `GROUP BY` key is value preserving and that is the
case that matters here. An aggregate is not, so a mart built only out of counts does not
answer for the table it counted.

**Recall went 19 of 20 to 20 of 20 and that number is worth less than it looks.** The
column that moved it is flagged on one structural signal and sits in the band that means
the tool declined to decide. Take the structure arm away and it drops back to 19 of 20,
which is the internal evidence that the mechanism is the one described and not a
coincidence.

It also made the residual report worse before better. The mart was the one table
`residual_sql` could measure, and it was measuring it over a set that was missing a column
in scope. Now the mart has an undecided column in it, so it refuses like the other four.
All five tables wait on a reviewer.

```
python3 scripts/lineage_probe.py --db /tmp/pii.duckdb
```

There is an interaction here worth naming rather than smoothing over. `lineage_probe.py`
measures what propagation adds on top of a column's own answer, and a column's own answer
can now be lineage informed. So that section deliberately classifies without the graph. A
comparison that passed the graph to both sides would be measuring a lineage informed answer
against a lineage informed answer and calling the difference the value of lineage.

## The policy is the easy half

`pii/mask.py` turns a classification into an action and the action into SQL. Then it applies
the SQL and measures how many people are still alone afterwards. The first three steps are a
lookup and a string. The fourth is the only part that can contradict anything, and it does.

```
python3 scripts/mask_probe.py --db /tmp/pii.duckdb
```

Over the forty two columns:

```
  redact       9
  generalise   3
  retain       20
  review       10
```

A direct identifier is redacted, because it names somebody by itself and has no coarser form
that stops doing so. A quasi identifier with a granularity threshold is generalised to what
the clause list permits. A sensitive attribute is retained, because a diagnosis is not an
identifier and redacting it protects nobody who has not already been picked out by the quasi
columns beside it. The review band gets a person, and asking for its masking expression
raises rather than quietly returning the raw value.

### Where the clause list is stricter than my own taxonomy

```
temporal: the taxonomy would allow month and Safe Harbor permits only year
```

The taxonomy says a temporal category identifies at day precision, which makes a month non
identifying by its own ordering. Clause C removes every element of a date except the year,
so a month does not survive either. Deriving the masking target from my own threshold would
have published a policy that keeps months and calls itself Safe Harbor. The target is written
down separately, the clause wins, and a check pins the disagreement so neither half can move
without the other being looked at.

### Most of the warehouse is kept because nothing fired

```
17 of 42 columns are kept because no rule fired, not because anything is known
```

That is what the no values rule costs, as a number rather than as a caveat. `pii/profile.py`
never lets a value reach the process, so the classifier can only count matches of patterns it
already holds, and a format nobody wrote a predicate for scores zero on all of them. The
column then lands in exactly the same bucket as one that was genuinely cleared. One of the
eighteen is planted as something personal.

### The residual risk report is mostly blank, and that is correct

```
5 of 5 tables cannot be measured at all until somebody works the review queue
that is the honest state of this report and it is not a bug. a k computed
around an undecided column is a k for a policy nobody has agreed to.
```

A k computed around an undecided column is a k for a policy nobody agreed to, so
`residual_sql` refuses rather than measuring around it. All five tables are waiting on a
reviewer. With a reviewer accepting every queued column, which nobody did and which is an
upper bound rather than a result:

```
  table                             k   groups      alone  quasi set
  analytics.encounter_daily        20        6    0.0000  day, postal_code
  raw.claim                        31        2    0.0000  submitted_on
  raw.device_reading             3043        1    0.0000  taken_at
  raw.encounter                    22        2    0.0000  admitted_at, discharged_at
  raw.patient                       1      788    0.6170  city, postal_code, birth_date, sex, created_at
```

`raw.patient` comes back at k of 1 with 61.7 percent of patients alone in their group, after
every generalisation the regulation permits.

**This line read four of five until the day column came into reach.** The mart used to be
the one table the report could measure, and it measured it over a set missing a column in
Safe Harbor scope. So the published k of 20 was a number about the wrong set, and making
the tool see the column turned a confident figure into a refusal. That is the report
getting better. A governance tool that answers where it should decline is worse than one
that declines everywhere, because the confident answer is the one somebody acts on.

### The number is a claim about a set somebody chose

The mart reports a comfortable k, and the set it was measured over has one column in it.

```
  quasi set                         k   groups      alone
  what the policy measured         20        6    0.0000
  plus the day, generalised        20        6    0.0000
  plus department                   1       35    0.0007
```

`department` is planted not personal. The classifier agrees. It is on no clause list
anywhere. It still takes the table from k of 20 to k of 1. The residual figure was
arithmetically correct and it was answering a question about the wrong set, because the set
came from a classifier that had already missed a column. This is the same shape as the
taxonomy's two axes: whether a column is personal is not a property of that column.

### What the masking bought on the table that can be measured

```
  columns                                     k   groups      alone
  postal_code, birth_date, sex                1      996    0.9940
  masked: postal3, birth year, sex            1      430    0.2380
```

Every permitted generalisation applied in full takes `raw.patient` from 99.4 percent of
people sitting alone to 23.8 percent, and k is still 1. **Satisfying Safe Harbor and
protecting the people in the table are different properties.** A tool that generates the
policy and stops has reported the first one and said nothing about the second.

## The review queue is the part a confidence score cannot do

The review band has been a count since the classifier existed. Ten columns score between
the two thresholds and the tool says so and stops, which is where most classifiers stop.
What a reviewer needs is not the list.

`pii/review.py` builds the queue, and the ordering is the design decision in it.

```
  pos   frees   blocks  distance  column
    1       1        1    0.0250  raw.device_reading.taken_at
    2       0        2    0.0250  raw.encounter.admitted_at
    3       0        1    0.0500  raw.claim.member_number
    4       0        1    0.2244  raw.encounter.attending_npi
    5       0        1    0.3500  raw.claim.payer_name
    6       0        1    0.0250  raw.claim.submitted_on
    7       0        1    0.0250  raw.encounter.discharged_at
    8       0        1    0.0250  raw.patient.created_at
    9       0        1    0.1500  raw.patient.city
   10       0        1    0.3000  analytics.encounter_daily.day
```

Sorting a review queue by confidence sorts it by how close the machine got, which is a
fact about the machine. `residual_sql` refuses to measure a table holding an undecided
column, so a review can be the only thing standing between a table and any residual figure
at all. That is what the queue is ordered on.

**The first version of that ordering did not work and the output is how I found out.** It
sorted on blocked tables, and a review blocks the table it lives in by definition, so eight
of the ten tied at one and the order was the alphabet with extra steps. `frees` is the
column that separates them: a table waiting on three reviews is freed by none of them
individually, and a table waiting on one is freed the moment somebody answers it. Exactly
one item in this queue frees a table on its own, and nothing about its confidence or its
category says so.

Each item carries the evidence, and for a derived column the trace is the part that makes
it answerable:

```
10. analytics.encounter_daily.day  candidate event_date at 0.4500
  question   is this column personal data
  short of   the accept threshold by 0.3000
  evidence   structure event_date             0.4500  catalog type DATE is temporal
  came from  raw.encounter.admitted_at
  waits with others on analytics.encounter_daily
```

A reviewer handed a mart column called `day` with a catalog type and nothing else would
reasonably call it a reporting grain and be wrong. The `came from` line is what changes the
answer.

Decisions are recorded rather than applied and forgotten. A reviewer, a date and a reason,
all three required by the constructor, because an unattributed decision with no reason is
the row an auditor asks about and nobody can answer six months later.

```
  2026-09-22  analytics.encounter_daily.day                personal     s.hussain  1386 of 1444 mart rows are a group of one, so the day is one admission
  2026-09-22  raw.device_reading.taken_at                  personal     s.hussain  a reading is tied to an encounter and through it to one patient
  2026-09-22  raw.patient.city                             personal     s.hussain  a geographic subdivision smaller than a state, which clause B covers
  2026-09-22  raw.patient.created_at                       not personal s.hussain  the row's load time, which is not tied to anything the patient did
```

Four decisions out of ten unblock two of the five tables:

```
  tables now measurable: raw.device_reading, raw.patient
  tables still blocked:  analytics.encounter_daily, raw.claim, raw.encounter
```

The mart needed two decisions rather than one, because `raw.encounter.admitted_at` is
carried into it and `day` is its own review. A table is reported unblocked only when every
review against it is answered. Anything looser would promise a residual number
`residual_sql` then refuses to produce.

`apply` refuses a decision for a column that is not in the queue, and refuses two decisions
for one column rather than taking the later one. Which of two conflicting reviews wins is a
policy question about an organisation, not something a function gets to assume.

```
python3 scripts/review_probe.py --db /tmp/pii.duckdb
```

## Who could have read this column, and did they

Two questions, two sources, and the gap between them is the report. Permission comes from
grants and is true whether or not anybody used it. Use comes from query history. An
auditor asks the first and a breach notification turns on the second.

`pii/access.py` answers both. The grants and the query log are generated in `pii/corpus.py`
and not in the module that measures them, for the same reason the schema stopped being its
own witness.

Here is what a table level access review produces, which is what most organisations have:

```
  analyst_bi           dpatel, rkhan, tovborg       analytics.encounter_daily
  analyst_clinical     jruiz, mchen                 analytics.encounter_daily, raw.device_reading, raw.encounter
  auditor_readonly     lokafor, rkhan               raw.claim
  engineer_etl         swhite                       analytics.encounter_daily, raw.claim, raw.device_reading, raw.encounter, raw.patient
```

`analyst_bi` holds the mart and nothing else. A least privilege review calls that a well
scoped role. It is the finding.

```
  raw.encounter.admitted_at: 3 direct, 3 more through analytics.encounter_daily.day
  raw.patient.postal_code: 1 direct, 5 more through analytics.encounter_daily.postal_code
```

**One user is granted `raw.patient` and six read a patient's postal code.** The mart's
grouping keys are copies, so a role with SELECT on the mart and nothing else reads the
value. A grant review says that role has no access to `raw.patient`, which is true, and
reading it as "cannot see patient postal codes" is wrong.

```
  raw.encounter.admitted_at                  dpatel, rkhan, tovborg
  raw.patient.postal_code                    dpatel, jruiz, mchen, rkhan, tovborg
```

Five of the six who read the postal code hold no grant on the table it lives in. Put the
two columns together and that is a postal code beside an admission date, on a grain where
96 percent of rows are one person.

The clearest version is two columns from the same table:

```
      1      0        0  raw.patient.mrn
      6      6        0  raw.patient.postal_code
```

Could, did, and granted but unused. Identical grants, identical role list, and one is
reachable by six people and the other by one. The difference is not in `raw.patient` at
all. `postal_code` is a grouping key in the mart and `mrn` is not, and no access review
that reads grants per table can see that.

The `unused` column is zero for every row over the full range, which is the number a
revocation would cost. It is not zero over a shorter one.

The closure has to stop somewhere or the report is noise. `encounters` is a row count
derived from every column in `raw.encounter`, and closing over aggregate edges would report
that reading a count exposes every patient identifier that was counted. A count is derived
from values it does not carry.

Where it stops is the one design decision in the module, and the first version got it
wrong. It used `VALUE_PRESERVING`, which is the set `pii/lineage.py` uses to decide whether
a *label* propagates, and that set excludes `TRANSFORM`. A transform is any call the reader
could not classify further. `upper(name)` is a transform and it still names the person.
`sha256(name)` is a transform and it does not. The reader cannot tell them apart, so one of
the two errors has to be chosen.

For label propagation the safe error is to claim less, because inheriting a granularity
through a transform is a claim about the value's shape. For an exposure report it runs the
other way. Saying a column might have been reachable and being wrong costs a reviewer some
time. Saying it was not reachable and being wrong is a sentence in a breach notification.
So `access.CARRIES_A_VALUE` is `VALUE_PRESERVING` plus `TRANSFORM`, and the two sets differ
by exactly that one kind.

**No figure in this section moved when that changed**, because the mart is built with no
transform edge in it, and a check asserts that so the next person to add one is told to
re-capture. The checks are the only evidence the fix works, which is worth knowing about a
fix whose whole justification is the case the sample data does not contain.

### The window decides the answer

```
  window   queries   stars   unused grants       star only
      3d         6       1               4               3
      7d        21       6               0              18
     14d        43      12               0              14
     30d       103      27               0              22
     60d       205      47               0              16
    113d       400      95               0              30
```

`unused grants` is the number a least privilege review acts on. Over three days it
recommends revoking from four users who have simply not run a query yet, and the
recommendation disappears entirely as the window grows. It is a property of the log's
length and it reads like a property of the users. Anyone running a quarterly access review
against a thirty day retention window is reading the first row and calling it the last.

### A query log cannot answer the question it is asked

```
  95 of 400 queries named a table and no column
  617 column references across the rest
```

A `SELECT *` records no column, so column level attribution is a floor rather than a count.

```
     did     at most    only by star  column
       0           1               1  raw.patient.ssn
       0           1               1  raw.patient.mrn
       0           3               3  raw.encounter.clinical_note
       6           6               0  raw.patient.postal_code
```

Four rows of the twenty five, picked to show the shape. For **sixteen** of the twenty five
flagged columns `did` is zero and `at most` is not, which means the honest answer to "did
anybody read the SSN column" is "between nobody and one person". That is the question a
breach notification turns on and this report cannot close it.

I wrote fourteen there first, by reading the table rather than counting it. The count is in
`scripts/access_probe.py` now for the same reason every other figure here is in a script.

Closing it needs the star expanded against the catalog as it stood on the query's date.
This warehouse keeps no catalog history, and expanding against today's catalog would
attribute a column to a query that ran before the column existed. So both ends of the
interval are printed and neither is called the number.

```
python3 scripts/access_probe.py --db /tmp/pii.duckdb
```

## The scanner never reads a value

`pii/profile.py` sends predicates down to the database and gets counts back. What crosses
the boundary is `976 of 983 non null values match this pattern` and never the 976 values.
`ColumnProfile` holds its address, its catalog metadata and integers, and a check walks the
dataclass fields rather than trusting the paragraph that says so.

The cost is real. A classifier that can only count matches of patterns it already holds
cannot find a pattern nobody wrote down, so a passport number in an unfamiliar format
returns zero on every predicate and falls back to the name arm. Sampling would find it.
Sampling would also mean a tool pulling personal data out of the warehouse it was pointed
at in order to decide whether that data is personal. The rows would then be in the process.
They would be in a traceback, and in whatever the caller did next.

The floor does sample, which is why `pii/naive.py` is the only thing in the repo that reads
a value. It stays that way because a comparison against a floor nobody ran is not a
comparison.

## What a missing value does to a claim about a column

The floor drops nulls before taking its majority and never says so, so a column that is
sixty percent empty and forty percent valid email addresses scores exactly like a complete
one. Here the match rate and the share of the column it was computed over are two fields.

```
 null rate  non null  match rate   support    weight   bands moved  accept band  emptiest
      0.00      1000      1.0000    1.0000    0.9000             0           15    0.0000
      0.02       983      1.0000    0.9830    0.8847             0           15    0.0332
      0.10       897      1.0000    0.8970    0.8073             0           15    0.1150
      0.30       713      1.0000    0.7130    0.6417             0           15    0.3205
      0.60       403      1.0000    0.4030    0.3627             1           14    0.6343
```

The rate is over the values that exist, because a missing value has no shape to match.
Counting a null as a failure instead would put a half empty column of perfectly formed
email addresses under the match floor, and it would never be flagged at all.

And then the honest end of it. Emptying sixty percent of every column in the warehouse
moves one band in forty two. The arm the missing values damage is the arm that was already
deciding almost nothing.

## Seven categories with no column, and six with no rule either

```
7 of 24 categories have no column in this warehouse
6 of those also have no rule in any arm, so nothing can return them
1 has a rule and no column, which is the one worth a fixture: payment_card
```

The taxonomy names seven things the sample warehouse has no example of. Account numbers and
biometrics and face photographs. Occupations and payment cards and vehicle identifiers and
web addresses. Six of the seven have no rule in any arm, so no input could ever produce
them. That is
fair for a taxonomy, whose job is to name what a governance tool has to be able to express
rather than what this one detects today, and it stops being fair the moment nobody says
which is which. `classify.rule_coverage` reports it per category, derived from the rule
tables rather than from a list maintained beside them.

`payment_card` is the one that was different. It had a token rule and a value predicate. Both were reachable and both looked reasonable,
and neither had ever run against anything. It has a fixture now. A rule that has never fired is the easiest kind of coverage to fake.

## Running the checks

```
python3 tests/run_all.py            466 checks, standard library only
python3 tests/run_with_duckdb.py    526 checks, needs the driver
```

The second one fails rather than skips when DuckDB is missing, and exits 2. A runner that
skips reports a different number depending on what happened to be installed, and the number
that gets published is whichever one somebody read.

Mutation, run from a copy of the tree with a suite oracle at both ends:

```
pii/corpus.py + pii/reidentify.py   182 sites, 173 killed, 9 survived
  control before: 194 passed, 0 failed
  control after:  194 passed, 0 failed

pii/crawl.py                        28 sites, 0 survivors
  control before: 222 passed, 0 failed
  control after:  222 passed, 0 failed

pii/classify.py                     87 sites, 86 killed, 1 survived
  control before: 238 passed, 0 failed
  control after:  238 passed, 0 failed

pii/profile.py                      10 sites, 10 killed, 0 survivors
  control before: 281 passed, 0 failed
  control after:  281 passed, 0 failed

pii/lineage.py                      161 sites, 153 killed, 7 survived, 1 ungraded
  control before: 295 passed, 0 failed
  control after:  295 passed, 0 failed

pii/mask.py                         54 sites, 54 killed, 0 survived, 0 ungraded
  control before: 342 passed, 0 failed
  control after:  342 passed, 0 failed

pii/access.py
control before: 466 passed, 0 failed, 466 checks in 0.85 s
56 killed, 0 survived, 0 ungraded, 56 graded
control after: 466 passed, 0 failed, 466 checks

pii/review.py
control before: 466 passed, 0 failed, 466 checks in 0.86 s
38 killed, 2 survived, 0 ungraded, 40 graded
control after: 466 passed, 0 failed, 466 checks

pii/classify.py again, 94 sites     93 killed, 1 survived, 0 ungraded
pii/lineage.py again, 170 sites    161 killed, 7 survived, 2 ungraded
  both over three slices with the control clean at every end
```

The figures above `pii/access.py` are from the run that first graded each module and the
control counts are what the suite stood at on that date. The five below are from the latest
run. Both are here because replacing the old ones would hide that two modules were graded
twice and one of them got worse before it got better.

`pii/access.py` opened at 46 of 54 and finished at 56 of 56. Four survivors were `frozen=True` on the four record
types, which is the same gap `pii/mask.py` had and which nothing had learned from. One was
a `SELECT *` holder test, invisible because every star query in the checks pointed at a
table that did hold the column. Two were the fallbacks that fill an omitted date range from
the log's own first and last query. The last one was not a missing check. `widening` carried
an `or "nothing"` for an empty carrier list, the guard above it makes that unreachable, and
flipping the operator broke nothing because nothing can reach it. It is deleted. A site
count going 54 to 53 is what that looks like.

`pii/review.py` opened at 31 of 40 and its two remaining survivors are equivalent rather
than gaps. Both move a value in the priority rank table. `_IDENTIFIABILITY_RANK` is total
over the identifiability enum and `not_personal` is the only category carrying NONE, which
`Classification.band` forces to the ignore band, so a NONE column cannot reach a review
queue and its rank is never read. Moving the sensitive entry from 2 to 3 lands it on the
unreachable one and changes no ordering.

`pii/classify.py` re-graded at 93 of 94 after the second round went in. The survivor is in
`two_readings_of_one_fact` and it is equivalent: the guard returns early when either
granularity is missing, and with the guard flipped to require both, `granularity_family`
of a missing one compares unequal to any real family and the function returns the same
answer by a different route.

`pii/lineage.py` re-graded at 161 of 170 with the same seven survivors day 4 left in the
parser. The two new methods added nine sites and eight are killed. The ninth is the cycle
guard in `upstream_tables`, reported ungraded because flipping its membership test makes
the walk run forever, which is precisely what a broken cycle guard does. The first version
of that method was recursive with no visited set, and a two table cycle put it into the
stack limit. A check caught it before anything else did.

`pii/mask.py` opened at 46 of 53. All seven survivors were coverage gaps rather than
equivalent mutants and all seven are closed. Two were the `frozen=True` on the two record
types, which nothing asserted, so a caller could edit a generated policy and the validation
would never run again. Four were `resolve_review`, which had checks only behind the database
runner and therefore none that this pass could see. The last was the default k target, which
no check could tell from any other number until a table sitting at exactly k of 2 was added.

`pii/lineage.py` opened at 106 of 163 and that is the worst a module has started here. The
reader is a parser and a parser is mostly branches nothing obvious exercises. Twenty nine
checks were written against named survivors and the second pass is 153 of 160 graded.

Three of those checks came out of one measurement rather than out of an argument. Six
survivors sat in the string scanner and all six looked equivalent, so both forms were run
over 200,001 generated inputs. One of the six agrees on every input and is equivalent.
The other five differ on an empty literal and on an escaped quote, which are ordinary SQL
and not edge cases, and they only survived because no statement in the suite contained
either. `SELECT '' AS blank, p.city` is the input that separates them.

**One site is ungraded and the reason is worth keeping.** Changing one offset in the cast
scanner produces a mutant that never terminates, and the mutation tool has no per mutant
timeout, so the pass hangs rather than reporting. The unmutated code does terminate. It
terminated for a reason that lived in the rewrite rather than in the loop, though, so the
scan now moves forward instead of restarting from the beginning of the string. That is a
termination guarantee the loop makes on its own.

The classifier started at 65 of 87 and twenty checks were written against named
survivors. Three dataclasses were mutable and nothing said otherwise, which matters for a
record a masking policy and an audit trail both read back. `rule_coverage` had no test at
all, which is what putting a measurement in the library and then not testing it looks
like. The free text rule has two thresholds in one condition and no fixture sat on either.

**One survivor is left and it is equivalent, checked rather than argued.**
`two_readings_of_one_fact` guards with `a is None or b is None` and the mutant makes it
`and`. Running both forms over all 576 ordered pairs of categories returns the same answer
on every one, because only three categories carry a granularity threshold and the
both-null case short circuits identically either way.

`pii/profile.py` runs under the DuckDB suite, so it was sliced at five sites per call with
the control at both ends of each. Its one survivor read `mean_length` off the wrong element
of the result row. That is not a spare field. The free text rule is a threshold on it, and
a mean length of zero turns that rule off for every column in the warehouse without
anything failing.

The crawler runs under the DuckDB suite, whose oracle is 15.8 seconds against 0.5 for the
standard library one, so it was sliced across calls with the control at both ends of each.

Those control lines read 222 and the runner above reports 223, and the gap is not a typo.
The identifier quoting check was written after the pass, when the audit asked what
`grade_nullability` does with a column called `order`. The control figure is what the pass
really saw and it stays that way rather than being updated to look tidy.

The nine survivors are all cases where the mutant produces identical output, and the
distinction between two kinds of that is worth keeping. Three are unconditional. A
`random() < rate` moved to `<=` cannot differ, because `random()` never returns 1.0. A
`% 8999999` inside the phone number cannot fire below about 243,000 patients. And a
`flattery > 0` moved to `>= 0` sits inside a comprehension that has already filtered the
zeroes out.

The other six are equivalent **for this seed** rather than in principle. They widen a
`randint` bound by one, and the extra value is only reachable if a particular draw lands on
it, which none does here. A different seed could kill them. They are listed as survivors
rather than dismissed, because calling a seed dependent result an equivalent mutant is how
a coverage figure gets rounded up.

**One mutation pass was thrown away.** The oracle had been timed by running the
suite inside the copied tree without `PYTHONDONTWRITEBYTECODE`, which left `.pyc` files
behind. The harness sets that variable for its own subprocesses, so it will not write a
cache, and it has no way to refuse one that was already there. The control was clean at both
ends of the discarded pass, which is the uncomfortable part: a clean control does not prove
a pass was valid against a stale cache. Redone from a copy that nothing had ever executed.

## Known limitations

**Nothing measures whether the grain deduplicated a person away, so the tool asks.**
`analytics.encounter_daily.day` is in Safe Harbor scope and it reaches a reviewer now
rather than nothing, which is the fix. What it still does not do is decide. Whether a mart
date is one person's admission or a reporting grain turns on the group size, and no k is
measured at classify time. `pii/mask.py` computes a k over a masked grain and
`pii/reidentify.py` computes one over rows, so the measurement exists in the repo and
nothing upstream of the bands consumes it. Feeding it in is the version where the column
earns a score instead of a question.

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

**No masked table is written.** Every masking expression is executed, inside the residual
measurement and inside the checks, and nothing materialises a masked copy of the warehouse.
Application here means the policy runs and its result is measured, not that a masked table
exists.

**The group size column is named in the probe rather than discovered.**
`analytics.encounter_daily` stores one row per group and `encounters` says how many
admissions it stands for. Counting rows instead of people there reports the table as safer by
exactly the factor it deduplicated by, which is the failure this module is about, and the
probe avoids it because the mapping is typed in.

**The masking policy blocks its own measurement everywhere now.** All five tables hold at
least one undecided column, so `residual_sql` refuses on all five. The upper bound pass,
where a reviewer is assumed to accept everything, is the only residual table in the README
with numbers in it, and it is labelled a hypothesis rather than a result.

**`masking_expression` implements two of the seven granularities.** `YEAR` and `POSTAL_3`.
The rest raise rather than returning the column untouched. The taxonomy is wider than the
applier.

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

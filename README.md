# PII discovery and governance toolkit

Scan a warehouse, work out which columns hold personal data, generate the masking policy
that follows, and produce an audit trail somebody would accept. This is the classification
layer: a taxonomy with three fields rather than one label, a sample warehouse with personal
data planted in it on purpose, a classifier that returns a confidence with every answer,
and a measurement of what that confidence is actually worth.

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
                    pii/classify.py                        |
                    3 arms, confidence, bands              |
                         |                                 |
                         +----------------+----------------+
                                          |
                          pii/coverage.py   pii/reidentify.py
                          what it finds     who it still exposes
```

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

The corpus writes nulls. It did not at first, and while it did not every nullable flag was a
declaration nothing had ever exercised: the suite could check that a NOT NULL column holds
no null, which is the easy direction, and the reverse reading had no evidence at all in
either direction.

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

**The nulls are applied in a post pass, after every value is drawn.** So `generate` with the
rate turned off reproduces the corpus as it stood before nulls, byte for byte, and the suite pins the four
digests to prove it. That matters because it makes every figure the nulls moved
attributable to the nulls rather than to a shifted draw.

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
in safe harbor scope       19 of 42 columns
recall, names only         10/19  0.5263
recall, names plus values  13/19  0.6842
false alarms               0
flagged as wrong category  2

  direct     9/11  0.8182
  quasi      4/8  0.5000
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
```

Three of six are timestamps. Clause C says every element of a date tied to an individual is
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
in Safe Harbor scope, found               13/19        19/19
right category                            11/19        19/19
masked with no human in the loop    all of them        12/19
flagged and planted not personal              1            2
```

**Read the first row and then discount it.** I wrote the token rules with the answer key
open, so 19 of 19 is a report on my memory rather than on the method. Any classifier
written the same way gets the same number. It is printed because leaving it out would look
like hiding it.

The third row is the one that is not circular. The floor has no confidence, so every answer
it gives carries the same authority: either all 13 columns it finds are masked without
anybody looking, or none of them are. This classifier masks 12 and sends the rest to a
person, and the question is whether it sends the right ones.

```
accept band  15 columns, 0 of them planted not personal
review band   9 columns, 2 of them planted not personal
ignored      18 columns, 0 of them in Safe Harbor scope

lowest column masked with no human      0.7500
highest column the classifier got wrong 0.7250
margin                                  0.0250
```

Both mistakes land in the band that goes to a human and nothing wrong is masked
automatically. That is the result the design is for and the margin is what it rests on.
Twenty five thousandths, on a scale from zero to one, over forty two columns. I picked the
accept threshold with both of those numbers on the screen, so the separation is a fact
about this warehouse rather than a property of the method.

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
names a person outright. That removes `analytics.encounter_daily.day`, which is a reporting
grain rather than anybody's date. It cannot remove `created_at`, which sits in the patient
table beside an email address and a national identifier.

That rule has a threshold in it and the threshold bit immediately:

```
 bar     found   masked    wrong  tables treated as about people
0.35     19/19       12        2  4
0.60     17/19       12        2  3
0.75     16/19       12        2  2
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
all three                  19/19      12        2             0
name only                  19/19      13        2             2
value only                  5/19       3        0            20
structure only              0/19       0        0            24
without name                9/19       3        1            16
without value              19/19      13        2             2
without structure          19/19      12        2             0
```

**The structure arm moves no band anywhere.** It changes the number printed beside five
columns and changes no decision about any of them. The value arm moves two. Take the name
arm away and the classifier finds 9 of 19 instead of 19.

So on this warehouse the confidence is mostly a restatement of how sure I was when I wrote
the token list. That is not an argument for deleting the other two arms. It is an argument
that a warehouse with meaningful column names is the easy case, and the honest way to show
what the other arms are worth is to take the names away:

```
in Safe Harbor scope, found        9/19
right category                     7/19
masked with no human in the loop   3/19

  raw.patient.c12              was ssn                    called national_id            0.89
  raw.patient.c05              was email                  called email                  0.88
  raw.device_reading.c07       was source_ip              called ip_address             0.83
  raw.encounter.c06            was attending_npi          called phone                  0.69  planted licence_number
  raw.patient.c06              was phone                  called phone                  0.68
  raw.device_reading.c04       was taken_at               called event_date             0.45
  raw.patient.c10              was birth_date             called event_date             0.45  planted birth_date
```

Same rows and same types and same nulls. The column names are replaced by position labels
and the whole thing is rebuilt as a real database rather than stubbed out in Python. Recall
falls from 19 to 9 and automatic masking from 12 to 3. Ten digits is ten digits, so a
clinician licence number and a telephone number come back as the same thing, and only the
name ever told them apart.

## The scanner never reads a value

`pii/profile.py` sends predicates down to the database and gets counts back. What crosses
the boundary is `976 of 983 non null values match this pattern` and never the 976 values.
`ColumnProfile` holds its address, its catalog metadata and integers, and a check walks the
dataclass fields rather than trusting the paragraph that says so.

The cost is real. A classifier that can only count matches of patterns it already holds
cannot find a pattern nobody wrote down, so a passport number in an unfamiliar format
returns zero on every predicate and falls back to the name arm. Sampling would find it.
Sampling would also mean a tool pulling personal data out of the warehouse it was pointed
at in order to decide whether that data is personal, and the rows would then be in the
process, in a traceback, and in whatever the caller did next.

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

`payment_card` is the one that was different. It had a token rule and a value predicate,
both reachable, both reasonable looking, and neither had ever run against anything. It has
a fixture now. A rule that has never fired is the easiest kind of coverage to fake.

## Running the checks

```
python3 tests/run_all.py            238 checks, standard library only
python3 tests/run_with_duckdb.py    281 checks, needs the driver
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
```

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

**The planted labels are mine.** Only the Safe Harbor clause list has another author. The
recall figures are a classifier I wrote graded on a schema I wrote against a scope somebody
else fixed, and those are three different strengths of evidence sitting in one table.

**Every row is generated.** The names come from two lists of twenty five. A real generator
pulls from census files and this one cannot.

**One labelling call of mine moves the headline.** `raw.patient.city` is planted as a postal
code at the coarse end, on the reading that clause B covers all geographic subdivisions
smaller than a state. That puts it in scope and makes it a miss. Drop it and recall goes
from 13/19 at 0.6842 to 13/18 at 0.7222, and the quasi figure from 4/8 to 4/7. Both readings
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

**Every weight and both thresholds are numbers I chose.** The token strengths, the match
floor at 0.60, the conflict penalty at 0.5, accept at 0.75 and review at 0.35. Nothing here
is fitted and nothing is calibrated. The probe sweeps the accept threshold and the table
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

**Only primary keys are recovered, not foreign keys.** `raw.encounter.patient_id` points at
`raw.patient` and nothing in the crawl knows that. Column level lineage is the next piece
and that is where the reference matters.

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

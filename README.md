# PII discovery and governance toolkit

Scan a warehouse, work out which columns hold personal data, generate the masking policy
that follows, and produce an audit trail somebody would accept. This is the classification
layer: a taxonomy with three fields rather than one label, a sample warehouse with personal
data planted in it on purpose, and a measurement of how much an obvious scan walks past.

```
python3 scripts/coverage_probe.py
```

That needs nothing but the standard library. Building the warehouse needs DuckDB.

```
pip install -r requirements.txt
python3 scripts/plant.py --db /tmp/pii.duckdb --key
```

## What is here

```
pii/taxonomy.py     categories, the three fields, the granularity ordering
pii/safeharbor.py   the HIPAA Safe Harbor clause list, as an external answer key
pii/schema.py       the sample warehouse and its planted labels
pii/corpus.py       generated rows for that schema
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
                         +----------------+----------------+
                                          |
                          pii/coverage.py   pii/reidentify.py
                          what it finds     who it still exposes
```

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
quasi identifiers                        k     cells   measured    uniform        gap
sex                                     22         3     0.0000     0.0000    -0.0000
sex + postal_code                        1       180     0.0290     0.0038    +0.0252
sex + postal_code + birth_date           1    176760     1.0000     0.9944    +0.0056
```

`measured` counts rows whose combination is unique. `uniform` predicts the same quantity
assuming every combination is drawn independently and evenly across the product of the
column cardinalities, which is `(1 - 1/K) ** (N - 1)` with no approximation and no constant
in it. The first is a fact about the generator. The second is a fact about combinatorics
and knows nothing about the data.

Printing both is the point. If they agreed exactly I would have built a corpus that
satisfies the prediction by construction and measured nothing, so the generator draws
postal codes on a decaying weight and ages on a curve rather than flat. The busiest postal
code holds 22.5 percent of the population and the gap column is where that shows up.

Three columns, none of which a flat PII flag would mask, and every one of a thousand people
is alone in their cell.

Truncating the postal code to three digits and the birth date to a year:

```
sex + postal 3 + birth year              1      1365     0.2070     0.4809    -0.2739
```

**That row is not evidence about Safe Harbor and the probe says so out loud.** Clause B
permits the first three digits of a postal code only where the area those digits cover
holds more than 20,000 people. This corpus has a thousand patients across five three digit
areas, the smallest of them 11 people, so the allowance does not apply to it at all. The
number is correct arithmetic about the corpus and it is not a result about the regulation.
`pii/safeharbor.postal_3_floor` computes that verdict rather than leaving it to a reader.

## Running the checks

```
python3 tests/run_all.py            166 checks, standard library only
python3 tests/run_with_duckdb.py    174 checks, needs the driver
```

The second one fails rather than skips when DuckDB is missing, and exits 2. A runner that
skips reports a different number depending on what happened to be installed, and the number
that gets published is whichever one somebody read.

Mutation, run from a copy of the tree with a suite oracle at both ends:

```
181 sites over 7 modules, 180 killed, 1 survived
control before: 166 passed, 0 failed
control after:  166 passed, 0 failed
```

The survivor turns `sort_keys=True` off in the taxonomy fingerprint. The payload is built
in dataclass field order, which is stable, so the two produce identical bytes today. It
stays because without it the published fingerprint would depend on the order fields happen
to be declared in.

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

**Seven of the categories are never reached by any column.** Account numbers and biometrics
and face photographs. Occupations and payment cards and vehicle and web identifiers. They
exist because Safe Harbor names them and the sample schema has no example of any of them, so
nothing has ever classified one. `Regime.PCI` is in the same position with zero columns
under it, against 19 for HIPAA and 2 for GDPR special.

**The sensitive axis contributes nothing to the measurement.** The recall split prints
`sensitive  nothing in scope`, because a diagnosis is not a Safe Harbor identifier. The
argument for that third of the taxonomy is reasoning rather than a number, and the table
above should not be read as covering it.

**The load uses `executemany`.** It moves about 7,000 rows in well under a second here. It
does not scale, and the answer at volume is a CSV and a `COPY`.

**Nullability is declared and almost unverified.** The suite checks that every column
declared NOT NULL really holds no null in the sample data. The reverse reading is worse and
is not fixed: the generator never writes a null anywhere, so every column declared nullable
carries a declaration nothing has ever exercised. A mutation pass flipped ten of those flags
and the suite stayed green until the schema got a content hash pinned to a literal.

**Nothing crawls anything yet.** `pii/schema.py` is a hand written description of the
warehouse `scripts/plant.py` then builds. Discovering that description from a live catalog
is the next piece, and until it exists the schema and the database agree because one wrote
the other.

**The value arm reads a fixed sample of fifty rows and needs a two thirds majority.** Both
of those are numbers I chose rather than measured.

**DuckDB is the local stand in for Snowflake.** The DDL generated here is ANSI enough to
port and no Snowflake statement in this repo has ever executed. Anything that changes will
say so in this section rather than in a commit message.

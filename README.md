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
python3 scripts/compliance_report.py --db /tmp/pii.duckdb
```

The last one is the artefact. Everything else is a probe I run to find out whether I was
wrong about something.

```
python3 tests/run_all.py            674 checks, standard library only
python3 tests/run_with_duckdb.py    798 checks, needs the driver
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
pii/ablation.py     remove one arm at a time and count the decisions that move
pii/lineage.py      column level edges, recovered from the statement that built the table
pii/mask.py         masking policy generation, application, and the k left afterwards
pii/review.py       the queue a person works, ordered by what a decision frees
pii/access.py       who could have read a column, and who did
pii/compliance.py   the report, and the questions it refuses to answer
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
                                          |
                                  pii/compliance.py
                            the document, answers and refusals
```

The arrow back into `pii/classify.py` is the one edge worth pointing at. The classifier
reads the lineage graph, and it reads it to decide whether a derived table is describing
people, which is a question none of that table's own columns can answer.

## Results

The warehouse is generated, five tables and 42 columns with personal data planted on
purpose, and graded against the HIPAA Safe Harbor clause list. Every figure below comes off a
probe in `scripts/`, and the ones a README could drift on are committed under
`measurements/` with a check that re-derives them.

**A fully compliant masking policy still leaves people alone in their group.** Every
generalisation Safe Harbor permits, applied in full to `raw.patient`:

```
  columns                                     k   groups      alone
  postal_code, birth_date, sex                1      996    0.9940
  masked: postal3, birth year, sex            1      430    0.2380
```

That takes the share of people alone from 99.4 percent to 23.8 percent, and k is still 1.
Satisfying the clause list and protecting the people in the table are different properties.

**Remove one arm of the classifier at a time and count the decisions that move.**

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

**The name arm is the one this warehouse turns on.** Sixteen bands move without it and
recall falls from 20 of 20 to 10 of 20. The value arm moves two. The structure arm moves
one.

**Take the column names away and most of the evidence goes with them.** Rebuilt with every
column renamed to a position label, recall falls from 20 of 20 to 10 of 20 and automatic
masking from 12 to 3. A clinician licence number and a telephone number are both ten digits, and only the
name ever told them apart.

**An opt in arm that reads values changed no decision on this warehouse.** Four of the 42
columns produce a sampled signal and all four agree with something the other arms already
had. Its cost and what it can find are measured against a separate fixture of columns
formatted outside every rule.

**One query per column is the right shape, and the reason I first gave for it was wrong.**
Both query shapes grow linearly with table width, at an exponent of 1.005 each. Batching
pays for planning once instead of once per column and is still slower, and here is where it
goes. Each shape under DuckDB's detailed profile, as a share of the wall time around it:

```
  cols  shape        stmts  exprs  planning  executing  unprofiled
    42  per column      42    420    0.2336     0.7548      0.0116
    42  batched          1    379    0.0923     0.9019      0.0058
   336  per column     336   3360    0.2520     0.7282      0.0169
   336  batched          1   3025    0.0369     0.9610      0.0021
```

One statement spends a third of the planning share that 336 statements do, which is the
saving batching was supposed to be for. Execution is where it loses, and by more than
planning saves. The earlier draft of this section blamed binding and planning and had
nothing behind it.

## Known limitations

- Every row is generated, and the planted labels and the reviewer decisions are mine.
- DuckDB is the local stand in for Snowflake. The crawler is DuckDB only.
- Lineage reaches five of the 42 columns.
- Every weight and both thresholds are numbers I chose.
- `masking_expression` implements two of the seven granularities.
- Grants are modelled at table level only, and the grants and query log are generated.
- The k target defaults to 2, and 2 is a weak bar.
- One unreadable statement in a warehouse costs every table its weight verdict.
- Nothing bounds how far one wrong answer from the opt in sampling arm spreads.

The full list is in [docs/limitations.md](docs/limitations.md).

## More detail

**[Design notes](docs/design-notes.md)**

- [The schema is no longer its own witness](docs/design-notes.md#the-schema-is-no-longer-its-own-witness)
- [Nullability is a claim about the data now](docs/design-notes.md#nullability-is-a-claim-about-the-data-now)
- [The decision this is built on](docs/design-notes.md#the-decision-this-is-built-on)
- [What the obvious scan finds](docs/design-notes.md#what-the-obvious-scan-finds)
- [Who the quasi identifiers still expose](docs/design-notes.md#who-the-quasi-identifiers-still-expose)
- [A null is not anonymity, and the metric disagrees](docs/design-notes.md#a-null-is-not-anonymity-and-the-metric-disagrees)
- [The confidence is the product, and the recall is not](docs/design-notes.md#the-confidence-is-the-product-and-the-recall-is-not)
- [What the classifier gets wrong, and why neither is fixable here](docs/design-notes.md#what-the-classifier-gets-wrong-and-why-neither-is-fixable-here)
- [Two of the three arms decide almost nothing, and none of them decides nothing](docs/design-notes.md#two-of-the-three-arms-decide-almost-nothing-and-none-of-them-decides-nothing)
- [Lineage answers a question the classifier cannot, and it is a smaller question than I thought](docs/design-notes.md#lineage-answers-a-question-the-classifier-cannot-and-it-is-a-smaller-question-than-i-thought)
  - [It reaches five columns out of forty two](docs/design-notes.md#it-reaches-five-columns-out-of-forty-two)
  - [Where it does earn its place](docs/design-notes.md#where-it-does-earn-its-place)
  - [The aggregate that does not aggregate](docs/design-notes.md#the-aggregate-that-does-not-aggregate)
  - [The classifier reads the lineage graph now, and the fix was to delete less](docs/design-notes.md#the-classifier-reads-the-lineage-graph-now-and-the-fix-was-to-delete-less)
- [The rescue was a necessary condition spending a sufficient one](docs/design-notes.md#the-rescue-was-a-necessary-condition-spending-a-sufficient-one)
- [What round two costs, and my guess was wrong about where](docs/design-notes.md#what-round-two-costs-and-my-guess-was-wrong-about-where)
  - [The repeat does not grow with the warehouse, which is why the saving is small](docs/design-notes.md#the-repeat-does-not-grow-with-the-warehouse-which-is-why-the-saving-is-small)
  - [The cost that does grow was the grain map, and it was opened as the smaller half](docs/design-notes.md#the-cost-that-does-grow-was-the-grain-map-and-it-was-opened-as-the-smaller-half)
  - [The fixture's key flags decide what the rescue finds, not what it costs](docs/design-notes.md#the-fixtures-key-flags-decide-what-the-rescue-finds-not-what-it-costs)
  - [What this does not measure](docs/design-notes.md#what-this-does-not-measure)
- [The policy is the easy half](docs/design-notes.md#the-policy-is-the-easy-half)
  - [Where the clause list is stricter than my own taxonomy](docs/design-notes.md#where-the-clause-list-is-stricter-than-my-own-taxonomy)
  - [Most of the warehouse is kept because nothing fired](docs/design-notes.md#most-of-the-warehouse-is-kept-because-nothing-fired)
  - [The residual risk report is mostly blank, and that is correct](docs/design-notes.md#the-residual-risk-report-is-mostly-blank-and-that-is-correct)
  - [The number is a claim about a set somebody chose](docs/design-notes.md#the-number-is-a-claim-about-a-set-somebody-chose)
  - [What the masking bought on the table that can be measured](docs/design-notes.md#what-the-masking-bought-on-the-table-that-can-be-measured)
- [The review queue is the part a confidence score cannot do](docs/design-notes.md#the-review-queue-is-the-part-a-confidence-score-cannot-do)
- [Who could have read this column, and did they](docs/design-notes.md#who-could-have-read-this-column-and-did-they)
- [The report an auditor gets, and the twenty five answers in it](docs/design-notes.md#the-report-an-auditor-gets-and-the-twenty-five-answers-in-it)
- [The scanner never reads a value](docs/design-notes.md#the-scanner-never-reads-a-value)
- [What the refusal costs, measured twice](docs/design-notes.md#what-the-refusal-costs-measured-twice)
- [The threshold was never the thing](docs/design-notes.md#the-threshold-was-never-the-thing)
- [One query per column is the right shape, and the reason I gave for it was wrong](docs/design-notes.md#one-query-per-column-is-the-right-shape-and-the-reason-i-gave-for-it-was-wrong)
  - [Which part of the engine spends it](docs/design-notes.md#which-part-of-the-engine-spends-it)
- [What a missing value does to a claim about a column](docs/design-notes.md#what-a-missing-value-does-to-a-claim-about-a-column)
- [Seven categories with no column, and six with no rule either](docs/design-notes.md#seven-categories-with-no-column-and-six-with-no-rule-either)

**[Running the checks](docs/running-the-checks.md)**

**Other docs**

- [adr-0001-one-label-per-column-is-not-enough](docs/adr-0001-one-label-per-column-is-not-enough.md)
- [sample-compliance-report](docs/sample-compliance-report.md)

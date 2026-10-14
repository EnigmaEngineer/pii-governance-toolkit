# Running the checks

Moved out of the README so the front page stays short. Back to the [README](../README.md).

## Running the checks

```
python3 tests/run_all.py
python3 tests/run_with_duckdb.py
```

The check counts are on the front page, in the [README](../README.md), where a test
reads them.

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

pii/compliance.py                   29 sites, 29 killed, 0 survived, 0 ungraded
pii/mask.py again, 61 sites         61 killed, 0 survived, 0 ungraded
control before: 505 passed, 0 failed, 505 checks in 1.01 s
control after: 505 passed, 0 failed, 505 checks
```

The figures above `pii/access.py` are from the run that first graded each module and the
control counts are what the suite stood at on that date. The rest are from the latest
run. Both are here because replacing the old ones would hide that two modules were graded
twice and one of them got worse before it got better.

Every figure from `pii/access.py` down was re-run in one session against the final tree,
one module per call from a freshly extracted copy, and all five reproduced exactly. A kill
count is a count and a count reproduces or it is broken, so there is no tolerance band on
that pass. The control moved from 466 to 496 because the suite grew, and the site and kill
counts did not move at all.

`pii/compliance.py` opened at 24 of 29. Three survivors were `frozen=True` on the three
record types, which is the fourth module in this repo to ship that gap. The other two were
worse. One was the recall numerator in the report's own coverage row and the other was the
subtraction counting clauses that reach a category, so both sat in figures the document
publishes and neither had anything asserting it. A mutant inside a printed number is the
expensive kind, because the number is the part somebody quotes.

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

`pii/lineage.py` re-graded at 161 of 170 with the same seven survivors still sitting in the
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
The identifier quoting check was written after the pass, when a review asked what
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

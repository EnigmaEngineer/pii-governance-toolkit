# One label per column cannot carry a masking policy

## Status

Accepted. This is the decision the rest of the toolkit is built on.

## Context

The obvious design for a PII classifier is one label per column. Something like `EMAIL` or
`SSN` or `PHONE`, and `NONE` for everything else. Every in-house scanner I have read works
this way, and the masking policy hangs off the label.

It falls apart when you try to write the policy down.

A postal code is not personal data and a postal code plus a birth date plus a sex is a
person. A flat label has to answer yes or no about the postal code and both answers are
wrong. Answer yes and you mask a column analytics needs, and a guardrail that refuses what
it should not gets switched off. Answer no and the mart is re-identifiable.

A diagnosis code is not an identifier at all. Nobody is found by it. It is the thing
somebody is looking for once they have found you, so it needs protecting for a completely
different reason and with a different treatment. A flat label puts it in the same bucket as
an email address, and then a policy keyed on the bucket does the same thing to both.

A timestamp is an identifier at second precision and is not one at year precision. That
distinction is not expressible in a label at all.

## Decision

Three fields, not one.

**Identifiability.** One of `DIRECT` or `QUASI` or `SENSITIVE` or `NONE`. What the column
does on its own. Direct names a person. Quasi names nobody alone and names somebody in
combination. Sensitive is not an identifier and is what identification exposes.

**Regime.** A set, not a level. A column can be a HIPAA identifier and not a GDPR special
category, or both, or neither. There is no honest ordering between them, so the taxonomy
stores membership and refuses to rank.

**Granularity.** The unit a value is recorded in, where that decides the answer. Categories
carrying a threshold are identifiers at or below it and are not identifiers above it. A
birth date identifies at day precision. A birth year does not. A postal code identifies at
five digits. Three digits do not.

The three combine into one question, `Category.identifies(observed_granularity)`, so a
caller asks once and the taxonomy holds the reasoning.

## Why the second axis is not free

It would be decoration if every HIPAA category were also direct, in which case the
regime field would be the identifiability field wearing a second name. `tests/test_taxonomy.py`
asserts the two sets differ in both directions, so a taxonomy that collapses to one axis
fails rather than passing quietly.

The measurement is what settles it. `scripts/coverage_probe.py` runs a deliberately naive
name and regex scan over the sample schema and grades it against the HIPAA Safe Harbor
clause list. Nineteen of the forty two columns are in scope under that list:

```
recall, names only         10/19  0.5263
recall, names plus values  13/19  0.6842

  direct     9/11  0.8182
  quasi      4/8  0.5000
```

The obvious scan is most of the way there on direct identifiers and a coin flip on quasi
ones. Both halves are Safe Harbor identifiers and only one of them looks like one. A single
PII flag reports one number for that, somewhere in the middle, and the number hides which
half it came from.

The columns it walks past are the argument in one block:

```
  raw.patient.city                         postal_code      quasi
  raw.encounter.admitted_at                event_date       quasi
  raw.encounter.discharged_at              event_date       quasi
  raw.claim.member_number                  health_plan_id   direct
  raw.device_reading.device_serial         device_id        direct
  raw.device_reading.taken_at              event_date       quasi
```

Three of the six are timestamps. No column name heuristic is ever going to call
`admitted_at` personal data, and clause C of Safe Harbor says every element of a date tied
to an individual is an identifier except the year.

## The answer key, and why it is not mine

Grading a classifier I wrote against labels I wrote measures nothing. The Safe Harbor list
is in `pii/safeharbor.py` because it is the one input here that somebody else decided. The
planted labels on the sample schema are still mine and the README says so. What the list
buys is that the set of things counting as identifiers was fixed before I opened the editor
and I cannot quietly shrink it when the scan does badly.

Running the mapping in the reverse direction is what caught the one real defect in that
file. The natural reading is "does every clause have a category", which came back clean.
The other reading is "does every category claiming this regime have a clause behind it",
and it did not. Clause B names street address and postal code in one sentence and clause C
names birth dates and admission dates in another, and the mapping carried one key each, so
`street_address` and `birth_date` were tagged HIPAA on the strength of nothing. The field is
a tuple now and `check_every_hipaa_tagged_category_is_reachable_from_a_clause` fails if it
happens again.

## Consequences

The classifier has more to produce than a label. It has to score three fields rather than
one, and the confidence score has to be about the whole verdict rather than about the
category alone.

Masking policy generation gets easier rather than harder. Direct identifiers are replaced.
Quasi identifiers are generalised, which is a different operation and needs the granularity
field to know what to generalise to. Sensitive attributes are usually kept, because the
analysis needs them, and they are the reason the quasi identifiers have to be handled
properly.

A column with no category is a category. `not_personal` is an entry in the taxonomy rather
than a `None`, so no caller has to remember the null case.

Two categories in the Safe Harbor list have no home and both say why in the source. Fax
numbers are folded into telephone numbers because nothing here can tell them apart, and
clause R is a catch all. Its words are "any other unique identifying number, characteristic
or code" and its membership depends on the data rather than on the column. Nothing driven
by metadata can represent it. Naming the gap is the only honest option available.

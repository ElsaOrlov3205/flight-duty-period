# flight-duty-period

A small, standard-library-only Python module that calculates flight crew Flight Duty Period (FDP) limits and minimum rest requirements under a deliberately simplified reading of EASA ORO.FTL.225 / .235. It answers two questions: "is this duty within the FDP limit?" and "how much rest must follow before the next report?".

## Usage

```python
from datetime import datetime
from flight_duty_period import (
    CrewMember, Duty, calculate_fdp, calculate_rest, parse_time
)

duty = Duty(
    report=parse_time("2024-06-10T06:00"),
    debrief_end=parse_time("2024-06-10T18:00"),
    sectors=2,
)

result = calculate_fdp(duty)
print(result.within_limit, result.margin)  # True 1:30:00

rest = calculate_rest(duty)
print(rest.min_rest)  # 10:00:00 (10h floor applies; FDP was 11h30m)
```

`CrewMember` is optional and only affects the `acclimatised` flag on `FDPResult`:

```python
crew = CrewMember(name="PW", acclimatised_as_of=parse_time("2024-06-08T00:00").date())
result = calculate_fdp(duty, crew=crew)
print(result.acclimatised)  # True
```

Exported names: `CrewMember`, `Duty`, `FDPResult`, `RestRequirement`, `calculate_fdp`, `calculate_rest`, `parse_time`.

## Why this exists

Crew planners and individual crew members often need a quick, offline sanity check on a pairing without pulling up the full operator compliance tool. EASA ORO.FTL is a 40-page document with dozens of modifiers (augmented crew, in-flight rest, split duty, timezone differences, commander's discretion). This library models **none of those**. It implements the unaugmented, un-split, non-discretionary, local-clock-time case, which covers the bulk of short-haul pairings. The trade-off is deliberately limited scope in exchange for code small enough to audit by reading.

Key simplifications, stated so no one is misled:

- All times are local clock time. No UTC conversion, no timezone offsets accepted. The caller handles base-local time.
- One duty = one FDP. No extension by in-flight rest, no augmented tables, no split-duty breaks.
- A 30-minute debrief is assumed included in `debrief_end` and excluded from FDP. This is the common European short-haul convention; if your operation uses a different debrief length, the caller must account for it.
- Max FDP comes from a collapsed table keyed on report-time band and sector count (1, 2, or 3+).
- The daily rest floor is 10 hours, and where the preceding FDP exceeds 10 hours the rest must match the FDP duration. This is a conservative reading of ORO.FTL.235(e).
- A duty that touches the Window of Circadian Low (02:00-04:59 local) takes a 30-minute reduction on max FDP.
- `CrewMember.is_acclimatised` flags whether the report date is within 2 days of the acclimatisation date. **The flag does not change the numeric limit.** An unacclimatised result is not a go-ahead; separate unacclimatised tables are out of scope.

## The awkward edge

The WOCL (Window of Circadian Low) penalty is the main place readers will trip. The library checks whether the duty interval overlaps 02:00-04:59 on the report date **or** the same window on the following day. A duty that starts at 23:00 and ends at 06:00 therefore counts as WOCL-spanning and loses 30 minutes of max FDP. A duty starting at exactly 05:00 does not, because the WOCL window ends at 04:59. If your operation defines the WOCL differently, expect different results.

## Running the tests

```
PYTHONPATH=src python -m unittest discover -s tests
```

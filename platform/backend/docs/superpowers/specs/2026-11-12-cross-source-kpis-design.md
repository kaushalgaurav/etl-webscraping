# KPIs that analyse the whole merge, not one source

Date: 2026-11-12 · Status: draft for review · Branch: feature/website-warehouse

## 1. Why

The user's point (2026-11-12): the KPIs and the Dashboard show fixed labels that
come from HMIS or Census alone. A survey's merged data is more than one source.
For an HMIS + NFHS survey, cards like "BCG Coverage" (HMIS alone) may not be what
matters. What matters is what you can only see with both together, for example
"facilities report 94% institutional births, households say 88%".

So: **the KPI set should be designed from what the merge actually holds, and
should look across sources, not just inside one.**

## 2. What exists today

| Surface | What it does | The gap |
|---|---|---|
| 28 Dashboard cards (`constants/dashboard_kpis.py`, `GET /kpi/dashboard`) | Fixed recipes over Census + HMIS columns | Same 28 for every survey; can't use NFHS or any Website table; shows "unavailable" cards instead of better ones |
| 227 template indicators (`kpi_catalog`) | The report's fixed list | ~50 ask for NFHS-5 but use an HMIS/Census stand-in |
| Generate / Ask (`kpi/dynamic.py`, `POST /kpi/generate`) | Claude proposes ~10 KPIs from the merged columns; backend checks columns and does the maths | (a) one headline number per KPI, no per-district or per-year values; (b) only single-source maths (sum, mean, ratio of sums): no "compare A with B"; (c) a one-off answer stored in `kpi_requests`, never becomes the survey's Dashboard; (d) Website columns reach Claude as `web_raw_githubusercontent_com_…_42_in_688124`, so it can't tell what they are |
| `POST /kpi/aggregate` | Any column, by state/district/time, overall and per group, with the per-source counting rule | The right engine, but the caller has to know which columns to ask for |

The numbers are already live (recomputed every call). What is fixed is the
**list**, and none of the list's recipes look across sources.

## 3. Goals, non-goals, anti-goals

**Goals**
1. Each survey gets its **own KPI set**, built from the sources in its merge.
2. The set includes **cross-source KPIs**: compare, combine and relate measures
   from different sources.
3. Every KPI works **overall, by state, by district and by year** — the shapes the
   Dashboard draws.
4. When the merge changes (a source added or removed), the set is **re-proposed**,
   and KPIs whose columns disappeared are marked, not silently dropped.
5. Every indicator has a **readable name, source, year and unit**, everywhere.

**Non-goals**
- Changing the nimbli portal's Dashboard screen (another team). We provide the
  API; they decide when to switch.
- Changing how sources are fetched, cleaned or merged.
- Forecasting, or statistics beyond what is listed in §5.

**Anti-goals** (would be a failure even if it "works")
- An AI-produced number. Claude may suggest; the backend computes.
- A set that silently changes between two views of the same survey. Once saved,
  a survey's set only changes when someone changes it.
- Comparing two things that aren't comparable (different units, different
  district, different year) without saying so.
- Breaking the 28 cards for the portal before the portal team has moved.

## 4. Part 1 — An indicator list: "what can this merge measure?"

Before KPIs can be chosen, the system has to know what every merged column means.
Today that knowledge sits in four places (HMIS label TSV, Census column names,
`web_tables` + `web_raw.column_name`, the merge's column naming).

Build one **indicator list per survey** at merge time, one entry per measure column:

| Field | Example (HMIS) | Example (NFHS) |
|---|---|---|
| column | `pct_inst_del_hmis` | `web_raw_githubusercontent_…_42_in_688124` |
| name | % Institutional deliveries to Total Reported Deliveries | 42. Institutional births (%) |
| source | HMIS | Website table "NFHS-5-Districts.csv" |
| varies by | district × period | district × year (2019-2020) |
| unit | % | % |
| concept | `institutional_births` | `institutional_births` |
| districts with values | 640 | 341 |

- Names come from what we already hold: the HMIS label TSV, Census column
  names, and the indicator text saved for each Website table. No guessing.
- **Unit** is read from the name ("(%)", "(Rs.)", "per 1,000") and otherwise
  left blank.
- **Concept** is what lets two sources be compared (§6).
- This also fixes the unreadable Website column names wherever a person or
  Claude sees them, without renaming database columns.

## 5. Part 2 — KPI kinds, including cross-source ones

Each KPI in a set is one of these kinds. All are computed by the backend through
`kpi/aggregation.py` / `explorer.aggregate`, so the per-source counting rule still
holds (Census once per district, HMIS per period, Website once per district per year).

| Kind | What it says | Example (HMIS + NFHS + Census) |
|---|---|---|
| **Single** | one measure from one source | NFHS #73 children stunted (%) |
| **Rate across sources** | a count from one source over a population from another | HMIS live births per 1,000 Census population |
| **Gap** | same concept, two sources, same district and year: A − B in points | HMIS institutional deliveries % − NFHS institutional births % |
| **Disagreement list** | districts where the two sources differ most | "Top 10 districts where facility reports exceed household reports for ANC 4+" |
| **Relationship** | across districts, does X move with Y (correlation, plus a scatter) | NFHS women's literacy (#14) vs HMIS ANC 4+ |
| **Change over time** | same measure, two years | HMIS institutional deliveries 2018-19 → 2019-20 |

Rules the backend enforces, not the prompt:
- **Gap** only between two indicators with the same concept and unit, on rows
  where both have a value for the same district and year. It reports how many
  districts were compared.
- **Relationship** needs at least 30 districts with both values; otherwise it is
  "not enough data", not a number.
- Each KPI carries a **caution** where definitions differ, e.g. "HMIS counts
  facility reports; NFHS asks households", as fixed text attached to the concept.

## 6. Part 3 — Knowing two indicators measure the same thing

A small hand-kept file, like `canonical_fields.py` and `hmis_indicators.tsv`
already are: `constants/indicator_concepts.tsv`, mapping each known indicator to a
concept key, plus the caution text.

Starting pairs (HMIS code ↔ NFHS-5 district fact-sheet number):

| Concept | HMIS | NFHS-5 |
|---|---|---|
| institutional_births | pct_inst_del | 42 |
| anc_4plus | pct_anc_4plus | 33 |
| anc_first_trimester | pct_anc_1st_tri | 32 |
| csection_all / public / private | pct_csec / pct_csec_public / pct_csec_pvt | 46 / 48 / 47 |
| ifa_180_days | pct_ifa_180 | 36 |
| breastfed_1hr | pct_bf_1hr | 67 |
| sex_ratio_at_birth | sex_ratio_birth | 4 |
| skilled_birth_attendance | pct_safe_del (close, not identical) | 45 |

- A Website indicator is matched by its **leading number and source table**
  (the "42." in "42. Institutional births (%)"), so the NFHS-5 match works for any
  copy of the fact sheet. The text after the number is checked with the same
  difflib matching as `merging/semantic.py`, as a guard.
- Claude may **suggest** new pairs (§7); a suggested pair is shown as
  "suggested", and is only used for Gap KPIs once someone confirms it.
- Census has no matching concepts with NFHS in the starting set; it takes part
  through "rate across sources" (as denominators) and "relationship".

## 7. Part 4 — How a survey's KPI set is chosen

When a merge finishes (and on a "Re-propose" button):

1. **Rule-based proposals (free, instant, always on).** Recipes that fire when
   their inputs are present:
   - each concept present in two sources → a Gap KPI and a Disagreement list;
   - each of the 28 existing cards → proposed **only if** its columns are in the
     merge (so a BCG card isn't proposed for a merge with no BCG data);
   - Census population present + an HMIS count → the per-1,000 rate;
   - template indicators that ask for NFHS-5 → use the NFHS indicator when its
     concept is present, instead of the HMIS stand-in.
2. **Claude proposals (optional, costs money).** `dynamic.propose`, now given the
   readable indicator list (§4) instead of raw column names, and allowed to return
   the new kinds (§5). Every column is still checked; the backend still computes.
3. **Review.** The proposals are shown as a list with live values; an admin ticks
   the ones to keep, may rename them, and saves. That saved list is **the survey's
   KPI set**.
4. **Stability.** The saved set doesn't change by itself. If a later merge drops a
   column a saved KPI needs, that KPI shows "source no longer in this merge"; new
   proposals appear as "new since last review".

## 8. Part 5 — Showing it

- New endpoint, e.g. `GET /kpi/survey-set?survey_id=…&time=…&level=state|district`:
  the survey's saved KPIs, each with its value overall and per place, its kind,
  sources, unit and caution. One call gives the Dashboard everything it needs.
- The **local test portal** (`/ui`) gets a KPI-set screen (review + tick + save) and
  its Dashboard draws from that endpoint. This is where we prove it.
- The **nimbli portal** can switch from the 28 cards to this endpoint when the
  portal team is ready; until then `GET /kpi/dashboard` keeps working unchanged.

## 9. Worked example: survey = HMIS + NFHS (no Census)

Proposed set, all rule-based (no Claude):

| # | KPI | Kind | Sources |
|---|---|---|---|
| 1 | Institutional births: facility vs household gap | Gap | HMIS pct_inst_del − NFHS #42 |
| 2 | ANC 4+: facility vs household gap | Gap | HMIS pct_anc_4plus − NFHS #33 |
| 3 | C-section rate: facility vs household gap | Gap | HMIS pct_csec − NFHS #46 |
| 4 | Districts where ANC 4+ reports disagree most | Disagreement list | as #2 |
| 5–10 | Institutional Delivery Rate, Safe Delivery Rate, ANC 4+, ANC 1st trimester … | Single (existing cards whose HMIS columns are present) | HMIS |
| — | Total Population, Literacy … | not proposed | no Census in this merge |

On 10 Nov data, KPI 2 for Bihar reads "HMIS 74.1% vs NFHS 25.9% — gap 48.2 points",
the finding that today has to be found by hand in the Dashboard picker.

## 10. Constraints and known issues

- `dashboard_master` is shared across surveys and capped at 1,400 columns
  (`merging/web_join.py`); this design reads it, doesn't add columns.
- Known NFHS district-id collisions (9 districts land on a same-named district in
  another state; see `docs/test-reports/2026-11-10-nfhs-hmis-end-to-end.md`) would
  show up as false gaps. Gap and Disagreement KPIs should list collided districts
  under a caution until the crosswalk is fixed.
- The saved KPI set needs a new table (e.g. `survey_kpi_sets` / `survey_kpis`);
  generated as an Alembic migration, applied by the user.

## 11. Open decisions (the user's)

1. **Approval step:** must an admin review and save the set (as above), or should
   the rule-based set go live automatically and the review be optional?
2. **Claude in the default path:** run Claude proposals on every merge, or only
   when someone presses "Suggest more"? (It costs money per call.)
3. **The 28 cards on the portal:** keep them as they are until the portal team
   switches, or ask them to switch early?

## 12. Calls made in this draft (check these)

- Rule-based proposals come first and are free; Claude is an add-on.
- Concept pairing is a hand-kept file, not AI matching; Claude may only suggest.
- NFHS indicators are matched by their fact-sheet number, not their text.
- The 28 cards become proposals filtered by what's present, not deleted.
- Correlation needs ≥ 30 districts; below that, no number.
- Phase 1 stops at our API + the local test portal; the nimbli portal is phase 2.

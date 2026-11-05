# NFHS + HMIS: end to end through the Website flow

Date: 10 November 2026. Branch `feature/website-warehouse`, plus the uncommitted `indicator_column` change below.
Run on a **throwaway SQLite database** and API server (port 8766), driven through the same endpoints the portal calls. The real Postgres was never written. Migration `29f1437fff2d` was generated, not applied.

## Steps and results

| Step | What happened | Result |
|---|---|---|
| Search: official NFHS site | rchiips.org now redirects to **www.nfhsiips.in**. Our Search: "Couldn't connect to the site." | ❌ Blocked by the site's certificate setup (see below) |
| Search: NFHS-5 district file | `raw.githubusercontent.com/pratapvardhan/NFHS-5/master/NFHS-5-Districts.csv`, compiled from the official NFHS-5 phase-1 district fact sheets (2019-20 fieldwork, 21 states/UTs) | ✅ 35,464 rows × 8 columns in 4 s, suggested Keep, district + state columns guessed right |
| Save | State column `State`, district `District`, **indicator column `Indicator`**, kept column `NFHS-5`, year 2019-2020 | ✅ 35,464 values |
| Clean | — | ✅ 286 of 340 districts matched |
| Master | — | ✅ **27,604 values** (was 530 before the fix: ~104 indicators per district collapsed into 1) |
| HMIS fetch | F.1 key indicators, ALL.xls, 2018-19 + 2019-20 | ✅ 3,108 rows each. A first attempt lost 65 of 148 chunks to a local DNS outage; the second run had none |
| HMIS clean + master | `/warehouse/hmis/rebuild` | ✅ 6,216 clean rows, 5,904 master rows |
| Merge (HMIS + NFHS) | `/datasets/merge`, connectors `hmis`, 1 Website table | ✅ 4,879 rows × 510 columns, all 104 NFHS indicators as columns |
| KPIs: `/kpi/aggregate` | NFHS and HMIS side by side, per state, 2019-20 full year | ✅ 200, 0.4 s |
| KPIs: `/kpi/dimensions` | — | ✅ 8 time slices, 512 columns |
| KPIs: `/kpi/dashboard` (28 fixed cards) | — | ✅ 17 of 28 computed. The 11 others need Census, which this survey doesn't have |

## Correctness checks

- **Values are exact.** Institutional births, file vs merged: Patna 89.1 = 89.1, Pune 98.0 = 98.0, Wayanad 100.0 = 100.0, Kamrup Metropolitan 89.1 = 89.1.
- **The year join works.** NFHS values sit only on 2019-2020 rows. Each district-year has one value, repeated on its 4 HMIS quarterly rows. 15 NFHS districts have no HMIS row and get their own row.
- **No double counting.** Bihar's mean institutional births from `/kpi/aggregate` is 77.8368, exactly the file's mean over its 38 districts.
- **All-India 2019-20, NFHS vs HMIS** (mean of districts): institutional births 87.8 vs 93.8, 4+ ANC visits 61.6 vs 78.8, ANC in 1st trimester 71.3 vs 75.0, C-section 24.9 vs 20.0. HMIS counts facility reports; NFHS asks households. The gap, largest in Bihar (ANC4 25.9 vs 74.1), is the comparison the dashboard can now show.

## Problems found

1. **The official NFHS site can't be read by our Search.** Two separate reasons:
   - **Certificate (our side, fixable).** www.nfhsiips.in has a valid certificate that chains to a root Python trusts (emSign Root CA - G1). The server also sends an older path through a root that has been removed (Comodo "AAA Certificate Services"). Python's checker takes that path first and fails ("self-signed certificate in certificate chain"). Browsers and curl try the other path and succeed. A fix that keeps full checking is to verify with the operating system's checker (the `truststore` package, which pip itself uses). Not done; it needs a decision.
   - **Layout (the site's).** District data is behind the Publications form (choose phase, publication, state, district), and comes as one PDF per district with indicators as rows. Even with the certificate fixed, that is "needs clicks".
   - Also: the error says "Couldn't connect to the site." for a certificate failure, and the Search took 188 s to give up. The message should name the certificate.
2. **District id collisions put NFHS values on the wrong district (existing problem, not from this branch).** 9 NFHS districts landed on another state's district with the same `unique_id`, e.g.:
   - Sikkim North District → J&K Ganderbal (111)
   - Jammu → Arunachal Tawang (121)
   - Shimla → Odisha Bargarh (211)
   - Kasaragod → Punjab Pathankot (321)

   This is why "Arunachal Pradesh" shows NFHS values. It needs the crosswalk ids fixed.
3. **Split districts collapse onto one 2011 district.** Warangal Rural (100.0) and Warangal Urban (99.7) both match "Warangal"; master keeps the last (99.7). Bangalore and Bangalore Rural share one id, so Bangalore's 99.3 is labelled "Bangalore (Rural)".
4. **54 of 340 districts unmatched.** Most were created after 2011 (Gujarat's Aravali, Botad, Morbi…; Assam's Biswanath, Majuli…). Some are spellings: Ahmednagar/Ahmadnagar, Nicobar/Nicobars, Y.S.R., Leh (Ladakh).

## What changed in code for this test

New optional Save field `indicator_column` (`web_tables.indicator_column`, migration `29f1437fff2d`). With it:
- each value of a long table is filed under its row's indicator, not under the value column's heading;
- Clean folds the table to one row per district + state + year.

6 new tests; 317 pass.

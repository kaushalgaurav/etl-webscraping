# Website flow: live tests

Date: 5 November 2026. Branch `feature/website-warehouse` (after the review fixes, commit `b7adb00`).

Two tests, both run on a **throwaway SQLite database** (built the way the test suite builds its own). The owner's Postgres was only read (Census rows), never written, and no migration was applied.

1. **Breadth:** the real Search (`scan.run_scan`, browser mode on) on 40 real pages, 3 at a time.
2. **End to end:** a real API server driven like the portal: Search → Save → Clean → Master → Merge with live Census + HMIS → KPIs and Dashboard.

## 1. Breadth: 40 pages

| Outcome | Pages | Meaning |
|---|---|---|
| ✅ Worked | 12 | Useful tables found and suggested Keep |
| 🟡 Partly | 3 | Something useful, but incomplete or noisy |
| 🐞 Our bug / gap | 4 | The data was there; our code missed or mis-judged it |
| ⛔ Blocked by the site | 7 | Bot protection, CAPTCHA, paywall (reported, never worked around) |
| 📡 Unreachable | 6 | Site didn't answer, or answered too slowly |
| 🖱️ Needs clicks | 4 | Data appears only after choosing something on the page |
| ➖ No data on the page | 1 | Correctly found nothing worth keeping |
| ↪️ Refused by design | 3 | Census, HMIS, data.gov.in: use the URL option |

Of the 31 pages that answered and weren't refused, 19 had data our scraper could reach without a click or login: 15 gave data (12 fully, 3 partly) and 4 failed because of our code.

| # | Outcome | Page | Time (s) | Tables | Keep | Browser | Why |
|---|---|---|---|---|---|---|---|
| 1 | ✅ Worked | https://en.wikipedia.org/wiki/Demographics_of_India | 180.0 | 116 | 53 | no | 53 Keep tables; hit the 3-minute limit on linked files |
| 2 | ✅ Worked | https://en.wikipedia.org/wiki/List_of_districts_of_Bihar | 1.9 | 11 | 2 | no | 38-district table found and suggested Keep; 36/38 districts matched |
| 3 | ✅ Worked | https://en.wikipedia.org/wiki/List_of_districts_of_Karnataka | 95.0 | 135 | 101 | no | 31-district table, Keep, 17/31 matched; slow (95 s) reading linked PDFs |
| 4 | ✅ Worked | https://en.wikipedia.org/wiki/List_of_districts_of_Madhya_Pradesh | 8.6 | 18 | 2 | no | 55-district table, Keep, 48/55 matched |
| 5 | ✅ Worked | https://en.wikipedia.org/wiki/List_of_districts_of_Rajasthan | 1.7 | 14 | 1 | no | 41-district table, Keep, 28/41 matched (newer districts unmatched) |
| 6 | ✅ Worked | https://en.wikipedia.org/wiki/List_of_districts_of_Tamil_Nadu | 180.1 | 164 | 4 | no | 38-district table, Keep, 26/38 matched; hit the 3-minute limit on linked files |
| 7 | ✅ Worked | https://en.wikipedia.org/wiki/List_of_districts_of_Uttar_Pradesh | 72.4 | 21 | 2 | no | 75-district table, Keep, 63/75 matched (renamed and post-2011 districts unmatched) |
| 8 | ✅ Worked | https://en.wikipedia.org/wiki/List_of_states_and_union_territories_of_India_by_population | 204.0 | 50 | 25 | no | state population tables, 25 Keep; hit the 3-minute limit on linked files |
| 9 | ✅ Worked | https://en.wikipedia.org/wiki/Literacy_in_India | 180.2 | 571 | 472 | no | page tables Keep, plus 549 tables from a linked 572-page PLFS PDF; hit the 3-minute limit |
| 10 | ✅ Worked | https://ourworldindata.org/grapher/life-expectancy | 31.4 | 22 | 3 | yes | JavaScript page: browser clicked the download button and read the CSV (21,565 rows) |
| 11 | ✅ Worked | https://raw.githubusercontent.com/datasets/covid-19/main/data/countries-aggregated.csv | 4.4 | 1 | 1 | no | direct CSV read in 4 s, Keep |
| 12 | ✅ Worked | https://statisticstimes.com/demographics/india/indian-states-population.php | 3.8 | 3 | 2 | no | 2 state population tables, Keep |
| 13 | 🟡 Partly | https://nhm.gov.in/ | 192.8 | 37 | 18 | yes | tables come from guideline PDFs (checklists), not statistics; hit the 3-minute limit |
| 14 | 🟡 Partly | https://www.census2011.co.in/district.php | 1.3 | 1 | 1 | no | only the first 30 of 640 districts: the site splits the list over pages |
| 15 | 🟡 Partly | https://www.mospi.gov.in/ | 111.2 | 820 | 277 | yes | browser found real statistical tables in 12 linked PDFs, but the page's own feeds are menus; noisy |
| 16 | 🐞 Our bug | https://api.worldbank.org/v2/en/indicator/SP.POP.TOTL?downloadformat=csv | 1.1 | 2 | 0 | no | ZIP opened, but the main CSV (4 title lines above the header) wasn't read; only metadata |
| 17 | 🐞 Our bug | https://en.wikipedia.org/wiki/List_of_districts_of_Maharashtra | 1.8 | 5 | 0 | no | real 37-district table found but marked Skip: a column headed 'Name' is mistaken for personal names, and for a state column |
| 18 | 🐞 Our bug | https://raw.githubusercontent.com/datasets/population/main/data/population.csv | 1.6 | 1 | 0 | no | clean 17,394-row CSV marked Skip: header 'Country Name' mistaken for personal names |
| 19 | 🐞 Our gap | https://covid19.who.int/ | 2.3 | 3 | 0 | no | plain read found 2 glossary tables, so the browser wasn't opened and the dashboard data was missed |
| 20 | ⛔ Blocked | https://mohfw.gov.in/ | 2.3 | 0 | 0 | no | bot protection (firewall refuses automated tools) |
| 21 | ⛔ Blocked | https://pib.gov.in/ | 2.1 | 0 | 0 | no | bot protection |
| 22 | ⛔ Blocked | https://www.education.gov.in/ | 1.1 | 0 | 0 | no | bot protection |
| 23 | ⛔ Blocked | https://www.india.gov.in/ | 1.1 | 0 | 0 | no | bot protection |
| 24 | ⛔ Blocked | https://www.indiastat.com/ | 116.0 | 153 | 27 | yes | paywall: no Indiastat data, only 153 tables from unrelated third-party PDFs it links to |
| 25 | ⛔ Blocked | https://www.niti.gov.in/ | 1.1 | 0 | 0 | no | bot protection |
| 26 | ⛔ Blocked | https://www.rbi.org.in/Scripts/AnnualPublications.aspx | 3.4 | 1 | 0 | no | CAPTCHA on the publications |
| 27 | 📡 Unreachable | https://cdsco.gov.in/ | 2.1 | 0 | 0 | no | connection failed from our fetcher (likely the site's TLS certificate) |
| 28 | 📡 Unreachable | https://dbie.rbi.org.in/ | 1.1 | 0 | 0 | no | connection failed (also unreachable with curl) |
| 29 | 📡 Unreachable | https://rchiips.org/nfhs/NFHS-5_FCTS/India.pdf | 60.0 | 0 | 0 | no | no answer within 30 s (rchiips.org unreachable from here) |
| 30 | 📡 Unreachable | https://rchiips.org/nfhs/factsheet_NFHS-5.shtml | 60.0 | 0 | 0 | no | no answer within 30 s (rchiips.org unreachable from here) |
| 31 | 📡 Unreachable | https://www.nhp.gov.in/ | 1.0 | 0 | 0 | no | connection failed (site appears offline) |
| 32 | 📡 Unreachable | https://www.who.int/data/gho | 31.0 | 0 | 0 | no | no answer within 30 s |
| 33 | 🖱️ Needs clicks | https://data.worldbank.org/country/india | 19.3 | 6 | 0 | yes | data behind a form / country selector |
| 34 | 🖱️ Needs clicks | https://ndap.niti.gov.in/ | 8.1 | 4 | 0 | yes | browser opened; found only sector and ministry lists, statistics appear after choosing a dataset |
| 35 | 🖱️ Needs clicks | https://sdgindiaindex.niti.gov.in/ | 7.2 | 0 | 0 | yes | browser opened; data appears only after 'Explore Index' |
| 36 | 🖱️ Needs clicks | https://udiseplus.gov.in/ | 8.1 | 0 | 0 | yes | data behind a search form |
| 37 | ➖ No data | https://nhsrcindia.org/ | 9.9 | 3 | 0 | no | home page has menus and a 1-page PDF, no statistics; correctly all Skip |
| 38 | ↪️ Refused | https://censusindia.gov.in/census.website/data/census-tables | 0.0 | 0 | 0 | no | has its own connector: use the URL option |
| 39 | ↪️ Refused | https://data.gov.in/ | 0.0 | 0 | 0 | no | has its own connector: use the URL option |
| 40 | ↪️ Refused | https://hmis.mohfw.gov.in/ | 0.0 | 0 | 0 | no | has its own connector: use the URL option |

### What the failures teach

**Our bugs (fixable):**
1. **"Name" headers are mistaken for personal data.** A column headed `Name` or `Country Name` trips the personal-data check, so the table is marked Skip (Maharashtra's 37 districts, a 17,394-row population CSV). The same `Name` header is also read as a *state* column, so Maharashtra matched 1 of 37 districts. Fix: don't treat a name column as personal when the other columns are numbers or places; let a district guess beat a state guess.
2. **CSV with title lines above the header** (World Bank downloads have 4) isn't read. Fix: skip leading lines until a header-like row.
3. **The browser isn't opened when the plain read finds only junk tables** (WHO COVID dashboard: 2 glossary tables). Fix: also open the browser when every table found is suggested Skip.

**Speed and noise:**
- **8 of 40 Searches hit the 3-minute limit**, all on pages linking many files on *other* sites (Wikipedia references, Indiastat). Tables on the page itself were already found; the time went on third-party PDFs. Suggestion: read only files on the same site by default.
- PDF tables from manuals and checklists (NHM) are suggested Keep. The Keep suggestion is weak for PDFs.
- Wikipedia footnote marks end up in column names (`Population [6]` → `population_6`).

**Sites, not us:**
- **5 of 11 central government sites block automated tools** (MoHFW, PIB, Education, india.gov.in, NITI). The same pages answer a browser. By design we report it and don't work around it; the options are asking the site to allow the bot, or using an official API/connector.
- Dashboards that show data after a click (NDAP, SDG India Index, UDISE+, World Bank) stay out of reach, as expected.

## 2. End to end: Website → Census + HMIS → KPIs

Survey 101 on the throwaway server:
- **Census** fetched live (640 districts, 93 columns).
- **HMIS** fetched live (key-indicators report, last 2 years: 2018-2019 and 2019-2020, ALL.xls; 598 districts × 4 quarterly snapshots).
- Both rebuilt (clean + master) through `/warehouse/{connector}/rebuild`.

| Website source | Saved as | Search → table | Districts matched after Clean | Master rows |
|---|---|---|---|---|
| Wikipedia, Bihar districts | Bihar, 2010-2011 (Census 2011 reference date) | 38 × 9, Keep | 36 / 38 (East and West Champaran: list spells them Purba/Pashchim) | 144 |
| Wikipedia, Uttar Pradesh districts | Uttar Pradesh, 2010-2011 | 75 × 8, Keep | 63 / 75 (renamed: Prayagraj, Ayodhya, Amroha…; post-2011: Hapur, Shamli…) | 189 |
| NFHS-5 district CSV (GitHub) | state column `State`, 2019-2020 | 35,464 × 8 | 286 / 340 | 530 |

**Merge** (`/datasets/merge` with Census, HMIS and the 3 Website tables): succeeded, **5,143 rows × 510 columns**.

**Correctness checks:**
- **Populations match the official Census.** Bihar: 34 of 36 districts exactly equal (the 2 others differ by under 0.3% on Wikipedia). UP: 54 of 63 exactly equal; the 9 differences are Wikipedia showing today's smaller districts (e.g. Muzaffarnagar without Shamli).
- **Years line up.** NFHS-5 2019-2020 values sit on the 2019-2020 HMIS rows (2,808 rows share a year). The Wikipedia 2010-2011 values sit on their own rows. Each Website value appears once per district and year (766 district-years).
- **No double counting.** `/kpi/aggregate`, Bihar:
  - Census population = **104,099,452**, exactly the official 2011 total.
  - Wikipedia population (2010-2011) = 95,061,781 (36 districts, 91.3% of Census; the gap is the two unmatched Champarans).
  - HMIS institutional deliveries: 1,709,330 in 2019-20 against 1,550,673 in 2018-19, or 16.4 per 1,000 people. The previous-year comparison works.
- **Dashboard filters.** `/kpi/dimensions` lists 2010-2011 (a Website-only year) as a full-year slice, alongside 2018-2019 and 2019-2020.
- **Fixed 28 cards** (`/kpi/dashboard`): all 28 compute.

**Not tested here:**
- **Generate KPIs / Ask with a prompt.** They need `ANTHROPIC_API_KEY`, which isn't set locally.
- **The portal's screens.** Only the backend was driven.

### Problems found

1. **Long-shaped tables are silently collapsed (Website flow).** NFHS-5's CSV has one row per district *per indicator*. Clean keeps each value, but master keeps one value per district and year per column, so ~100 indicators per district become 1 (530 master rows from ~35,000 values). The Merge result looks fine and is wrong. Fix: at Save, let the admin name the column holding the indicator names (as for year), or refuse a table whose districts repeat.
2. **The district list's `unique_id` isn't unique (existing problem, not from this branch).** 10 ids belong to two districts in different states, e.g. 111 = Ganderbal (J&K) and North District (Sikkim); 2929 = Bangalore and Bangalore Rural. It looks like state and district numbers glued together ("1"+"11" = "11"+"1"). Effects in this run:
   - 14 districts' Census population lost (Dashboard Total Population 115.85 crore against 121.09 official; 14 more districts have no id at all);
   - 144 HMIS rows doubled (about +1.3% on HMIS totals);
   - a Website value for one of those 20 districts can land on the wrong district.
3. **Fixed count cards add up every HMIS snapshot (existing problem).** Live Births shows 94.4 million: the sum of 4 cumulative quarterly snapshots × 2 years. The 2019-20 full-year figure is 19.2 million. Deliveries, infant deaths, OPD and similar cards are inflated the same way. These cards should take one time slice.
4. **District aliases are missing**, so renamed districts and spellings don't match: Champaran (Bihar); Prayagraj, Ayodhya and others (UP). Fix: extend the alias list; districts created after the list was drawn stay unmatched by design.

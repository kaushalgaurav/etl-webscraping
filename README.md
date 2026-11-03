# Health and Family Welfare ETL

Extract from many sources, transform, then load.

| Stage | What it does |
| --- | --- |
| **Extract** | URLs (data.gov.in catalogs), CSV/Excel (API + `data/incoming`), PDF (NFHS factsheets + incoming) |
| **Transform** | Merge, clean, translate column names (Hindi/aliases → English) |
| **Load** | SQLite warehouse `data/warehouse/health_etl.db` plus CSV copies and a report |

Primary catalog: [Health and Family Welfare — data.gov.in](https://www.data.gov.in/sector/Health%20and%20Family%20welfare).

## Run

```bash
pip install -r requirements.txt
python run_pipeline.py --from-cache
```

Drop extra files into `data/incoming/` (`.csv`, `.xlsx`, `.pdf`) and rerun. Extra source URLs are in `etl/config.py` (`SOURCE_URLS`, `PDF_SOURCE_URLS`).

Set `DATA_GOV_IN_API_KEY` if the sample data.gov.in key is rate-limited. Direct `/files/` CSV links on the portal return HTTP 403; API resources are used instead.

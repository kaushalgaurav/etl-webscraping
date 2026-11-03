"""Output generation: Excel export (dataset + profile) and PDF export
(KPI/insights report) — the "Dashboard / PDF / Excel" box in the diagram.
The "Dashboard" itself is just the frontend rendering the JSON endpoints
(/datasets/{id}/aggregate, /kpi/{id}) as charts.
"""
import io
from pathlib import Path

import pandas as pd
from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from app.modules.reports.naming import CATEGORY_LABELS, category_for, export_filename


def build_kpi_pdf(dataset_name: str, prompt: str, narrative: str, kpis: list[dict]) -> bytes:
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=letter, topMargin=0.75 * inch, bottomMargin=0.75 * inch)
    styles = getSampleStyleSheet()
    story = [
        Paragraph(f"KPI / Insights Report — {dataset_name}", styles["Title"]),
        Spacer(1, 0.15 * inch),
        Paragraph(f"<b>Request:</b> {prompt}", styles["Normal"]),
        Spacer(1, 0.15 * inch),
        Paragraph("<b>Summary</b>", styles["Heading2"]),
        Paragraph(narrative or "(no narrative)", styles["Normal"]),
        Spacer(1, 0.2 * inch),
        Paragraph("<b>KPIs</b>", styles["Heading2"]),
    ]

    for kpi in kpis:
        story.append(Paragraph(f"<b>{kpi.get('name', '(unnamed)')}</b>", styles["Heading3"]))
        story.append(Paragraph(kpi.get("description", ""), styles["Normal"]))

        result = kpi.get("result")
        if isinstance(result, list) and result:
            headers = list(result[0].keys())
            rows = [headers] + [[str(r.get(h, "")) for h in headers] for r in result[:25]]
            table = Table(rows, repeatRows=1)
            table.setStyle(TableStyle([
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#2d3748")),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
                ("FONTSIZE", (0, 0), (-1, -1), 8),
            ]))
            story.append(Spacer(1, 0.08 * inch))
            story.append(table)
        elif result is not None:
            story.append(Paragraph(f"Result: {result}", styles["Normal"]))
        elif kpi.get("error"):
            story.append(Paragraph(f"<font color='red'>Error: {kpi['error']}</font>", styles["Normal"]))

        story.append(Spacer(1, 0.15 * inch))

    doc.build(story)
    buffer.seek(0)
    return buffer.getvalue()


def build_csv_export(df: pd.DataFrame) -> bytes:
    """Dataset as CSV bytes. UTF-8 BOM so Excel opens Indian names correctly."""
    return df.to_csv(index=False).encode("utf-8-sig")


def write_csv_exports(datasets_with_frames, base_dir: Path) -> list[dict]:
    """Write one CSV per dataset, foldered by acquisition method.

    Each method gets its own file and its own subdirectory (api/, web/, file/,
    merged/) so an API pull is never silently blended with a scraped page.
    """
    written = []
    for dataset, df in datasets_with_frames:
        category = category_for(dataset.connector)
        folder = base_dir / category
        folder.mkdir(parents=True, exist_ok=True)

        path = folder / export_filename(dataset)
        path.write_bytes(build_csv_export(df))

        written.append({
            "dataset_id": dataset.id,
            "name": dataset.name,
            "connector": dataset.connector,
            "category": category,
            "category_label": CATEGORY_LABELS.get(category, category),
            "stage": dataset.stage,
            "rows": int(df.shape[0]),
            "columns": int(df.shape[1]),
            "path": str(path),
            "filename": path.name,
        })
    return written

import io
from datetime import datetime
from database import get_conn
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle

import config as app_config
from billing import effective_tariff


def _usage_between(conn, unit_id, start_iso, end_iso):
    meter = conn.execute("SELECT id FROM meters WHERE unit_id=? AND status='active'", (unit_id,)).fetchone()
    if not meter:
        return None, None, None
    start_row = conn.execute(
        """SELECT positive_cumulative_flow_m3 FROM readings WHERE meter_id=? AND ts<=?
           AND positive_cumulative_flow_m3 IS NOT NULL ORDER BY ts DESC LIMIT 1""",
        (meter["id"], start_iso),
    ).fetchone()
    end_row = conn.execute(
        """SELECT positive_cumulative_flow_m3 FROM readings WHERE meter_id=? AND ts<=?
           AND positive_cumulative_flow_m3 IS NOT NULL ORDER BY ts DESC LIMIT 1""",
        (meter["id"], end_iso),
    ).fetchone()
    if not start_row or not end_row:
        return None, None, None
    return start_row["positive_cumulative_flow_m3"], end_row["positive_cumulative_flow_m3"], meter["id"]


def build_unit_usage_pdf(unit_id: str, start_iso: str, end_iso: str, period_label: str) -> bytes:
    with get_conn() as conn:
        unit = conn.execute("SELECT * FROM units WHERE id=?", (unit_id,)).fetchone()
        start_val, end_val, _ = _usage_between(conn, unit_id, start_iso, end_iso)

    tariff = effective_tariff(unit_id)
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, topMargin=20 * mm, bottomMargin=20 * mm)
    styles = getSampleStyleSheet()
    elements = [
        Paragraph(f"{app_config.BUILDING_NAME or app_config.PRODUCT_NAME} - Water Usage Report",
                  ParagraphStyle("t", parent=styles["Title"], fontSize=18)),
        Spacer(1, 4 * mm),
        Paragraph(f"Unit: <b>{unit['unit_number']}</b>", styles["Normal"]),
        Paragraph(f"Period: {period_label} ({start_iso[:10]} to {end_iso[:10]})", styles["Normal"]),
        Paragraph(f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M')}", styles["Normal"]),
        Spacer(1, 8 * mm),
    ]

    if start_val is None or end_val is None:
        elements.append(Paragraph("Insufficient meter data for this period.", styles["Normal"]))
    else:
        usage = round(max(end_val - start_val, 0), 3)
        cost = round(usage * tariff, 2)
        data = [
            ["Description", "Value"],
            ["Start reading (m3)", f"{start_val:.3f}"],
            ["End reading (m3)", f"{end_val:.3f}"],
            ["Usage (m3)", f"{usage:.3f}"],
            ["Tariff rate", f"R {tariff:.2f} / kL"],
            ["Total cost", f"R {cost:,.2f}"],
        ]
        table = Table(data, colWidths=[80 * mm, 60 * mm])
        table.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#2dd4a7")),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.HexColor("#051a13")),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
            ("BACKGROUND", (0, -1), (-1, -1), colors.HexColor("#eafbf5")),
            ("FONTNAME", (0, -1), (-1, -1), "Helvetica-Bold"),
            ("PADDING", (0, 0), (-1, -1), 6),
        ]))
        elements.append(table)

    doc.build(elements)
    return buf.getvalue()


def build_building_summary_pdf(start_iso: str, end_iso: str, period_label: str) -> bytes:
    with get_conn() as conn:
        units = conn.execute("SELECT * FROM units ORDER BY unit_number").fetchall()
        rows = [["Unit", "Usage (m3)", "Cost (R)"]]
        total_usage, total_cost = 0.0, 0.0
        for u in units:
            start_val, end_val, _ = _usage_between(conn, u["id"], start_iso, end_iso)
            usage = round(max((end_val or 0) - (start_val or 0), 0), 3) if start_val is not None else 0
            tariff = effective_tariff(u["id"])
            cost = round(usage * tariff, 2)
            total_usage += usage
            total_cost += cost
            rows.append([u["unit_number"], f"{usage:.3f}", f"{cost:,.2f}"])
        rows.append(["TOTAL", f"{total_usage:.3f}", f"{total_cost:,.2f}"])

    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, topMargin=20 * mm, bottomMargin=20 * mm)
    styles = getSampleStyleSheet()
    elements = [
        Paragraph(f"{app_config.BUILDING_NAME or app_config.PRODUCT_NAME} - Building Water Usage Summary",
                  styles["Title"]),
        Paragraph(f"Period: {period_label} ({start_iso[:10]} to {end_iso[:10]})", styles["Normal"]),
        Paragraph(f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M')}", styles["Normal"]),
        Spacer(1, 8 * mm),
    ]
    table = Table(rows, colWidths=[60 * mm, 40 * mm, 40 * mm])
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#2dd4a7")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.HexColor("#051a13")),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
        ("BACKGROUND", (0, -1), (-1, -1), colors.HexColor("#eafbf5")),
        ("FONTNAME", (0, -1), (-1, -1), "Helvetica-Bold"),
        ("PADDING", (0, 0), (-1, -1), 6),
    ]))
    elements.append(table)
    doc.build(elements)
    return buf.getvalue()


def build_statement_pdf(period_id: str) -> bytes:
    with get_conn() as conn:
        period = conn.execute("SELECT * FROM billing_periods WHERE id=?", (period_id,)).fetchone()
        if not period:
            raise ValueError("Statement not found")
        unit = conn.execute("SELECT * FROM units WHERE id=?", (period["unit_id"],)).fetchone()

    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, topMargin=20 * mm, bottomMargin=20 * mm)
    styles = getSampleStyleSheet()
    elements = [
        Paragraph(f"{app_config.BUILDING_NAME or app_config.PRODUCT_NAME} - Water Statement",
                  ParagraphStyle("t", parent=styles["Title"], fontSize=18)),
        Spacer(1, 4 * mm),
        Paragraph(f"Unit: <b>{unit['unit_number']}</b>", styles["Normal"]),
        Paragraph(f"Billing period: {period['period_start'][:10]} to {period['period_end'][:10]}", styles["Normal"]),
        Spacer(1, 8 * mm),
    ]
    data = [
        ["Description", "Value"],
        ["Start reading (m3)", f"{period['start_reading_m3']:.3f}"],
        ["End reading (m3)", f"{period['end_reading_m3']:.3f}"],
        ["Consumption (m3)", f"{period['consumption_m3']:.3f}"],
        ["Tariff", f"R {period['tariff_used']:.2f} / kL"],
        ["Amount due", f"R {period['amount_due_rand']:,.2f}"],
        ["Status", period["status"].upper()],
    ]
    table = Table(data, colWidths=[80 * mm, 60 * mm])
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#2dd4a7")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.HexColor("#051a13")),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
        ("BACKGROUND", (0, -1), (-1, -1), colors.HexColor("#eafbf5")),
        ("FONTNAME", (0, -1), (-1, -1), "Helvetica-Bold"),
        ("PADDING", (0, 0), (-1, -1), 6),
    ]))
    elements.append(table)
    doc.build(elements)
    return buf.getvalue()

from __future__ import annotations

from datetime import date, datetime
from html import escape
from io import BytesIO
from zoneinfo import ZoneInfo

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.pdfgen import canvas
from reportlab.platypus import (
    KeepTogether,
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

from src.formatters import format_brl_cents, format_brl_currency, format_date_br
from src.models import ReconciliationReport


NAVY = colors.HexColor("#243447")
SLATE = colors.HexColor("#5C6B7A")
LIGHT = colors.HexColor("#F3F5F7")
LINE = colors.HexColor("#D8DEE4")
GREEN = colors.HexColor("#2E7D32")
GREEN_BG = colors.HexColor("#EAF4EA")
RED = colors.HexColor("#B42318")
RED_BG = colors.HexColor("#FDECEC")
ORANGE = colors.HexColor("#B54708")
ORANGE_BG = colors.HexColor("#FFF3E0")


class NumberedCanvas(canvas.Canvas):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._saved_page_states: list[dict] = []

    def showPage(self):
        self._saved_page_states.append(dict(self.__dict__))
        self._startPage()

    def save(self):
        page_count = len(self._saved_page_states)
        for state in self._saved_page_states:
            self.__dict__.update(state)
            self._draw_footer(page_count)
            super().showPage()
        super().save()

    def _draw_footer(self, page_count: int):
        width, _ = A4
        self.saveState()
        self.setStrokeColor(LINE)
        self.setLineWidth(0.5)
        self.line(16 * mm, 13 * mm, width - 16 * mm, 13 * mm)
        self.setFont("Helvetica", 8)
        self.setFillColor(SLATE)
        label = f"Página {self._pageNumber} de {page_count}"
        self.drawRightString(width - 16 * mm, 8.5 * mm, label)
        self.restoreState()


def _styles():
    base = getSampleStyleSheet()
    return {
        "title": ParagraphStyle(
            "ReportTitle",
            parent=base["Title"],
            fontName="Helvetica-Bold",
            fontSize=20,
            leading=24,
            textColor=NAVY,
            alignment=TA_LEFT,
            spaceAfter=7,
        ),
        "meta": ParagraphStyle(
            "Meta",
            parent=base["Normal"],
            fontName="Helvetica",
            fontSize=9.5,
            leading=14,
            textColor=SLATE,
        ),
        "section": ParagraphStyle(
            "Section",
            parent=base["Heading2"],
            fontName="Helvetica-Bold",
            fontSize=12,
            leading=15,
            textColor=NAVY,
            spaceBefore=7,
            spaceAfter=6,
        ),
        "body": ParagraphStyle(
            "Body",
            parent=base["BodyText"],
            fontName="Helvetica",
            fontSize=9,
            leading=13,
            textColor=NAVY,
        ),
        "small": ParagraphStyle(
            "Small",
            parent=base["BodyText"],
            fontName="Helvetica",
            fontSize=7.5,
            leading=9,
            textColor=NAVY,
        ),
        "table_header": ParagraphStyle(
            "TableHeader",
            parent=base["Normal"],
            fontName="Helvetica-Bold",
            fontSize=7.2,
            leading=8.4,
            textColor=colors.white,
            alignment=TA_CENTER,
        ),
        "right": ParagraphStyle(
            "Right",
            parent=base["Normal"],
            fontName="Helvetica",
            fontSize=7.4,
            leading=9,
            textColor=NAVY,
            alignment=TA_RIGHT,
        ),
        "center": ParagraphStyle(
            "Center",
            parent=base["Normal"],
            fontName="Helvetica",
            fontSize=7.4,
            leading=9,
            textColor=NAVY,
            alignment=TA_CENTER,
        ),
    }


def _summary_table(report: ReconciliationReport, styles) -> Table:
    items = [
        ("Total CRM", report.totals["total_crm_cents"]),
        ("PIX do caixa (recebidos)", report.totals["pix_cash_cents"]),
        ("Dinheiro (recebidos)", report.totals["cash_cents"]),
        ("Esperado (dias completos)", report.totals["expected_card_cents"]),
        ("Aprovado na Rede", report.totals["rede_cents"]),
        ("Diferença (dias completos)", report.totals["difference_cents"]),
    ]
    data = []
    for index in range(0, len(items), 2):
        left = items[index]
        right = items[index + 1]
        data.append(
            [
                Paragraph(escape(left[0]), styles["small"]),
                Paragraph(f"<b>{escape(format_brl_currency(left[1]))}</b>", styles["right"]),
                Paragraph(escape(right[0]), styles["small"]),
                Paragraph(f"<b>{escape(format_brl_currency(right[1]))}</b>", styles["right"]),
            ]
        )
    table = Table(data, colWidths=[34 * mm, 43 * mm, 38 * mm, 43 * mm], rowHeights=12 * mm)
    table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, -1), LIGHT),
                ("BOX", (0, 0), (-1, -1), 0.6, LINE),
                ("INNERGRID", (0, 0), (-1, -1), 0.35, LINE),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("LEFTPADDING", (0, 0), (-1, -1), 7),
                ("RIGHTPADDING", (0, 0), (-1, -1), 7),
            ]
        )
    )
    return table


def _daily_table(report: ReconciliationReport, styles) -> Table:
    headers = [
        "Data",
        "Total<br/>CRM",
        "PIX<br/>caixa",
        "Dinheiro<br/>caixa",
        "Total Sistema<br/>C/D",
        "Total<br/>Rede",
        "Diferença",
        "Status",
    ]
    data = [[Paragraph(header, styles["table_header"]) for header in headers]]
    for row in report.rows:
        data.append(
            [
                Paragraph(format_date_br(row.date), styles["center"]),
                Paragraph(format_brl_cents(row.total_crm_cents), styles["right"]),
                Paragraph("N/I" if row.pix_cash_cents is None else format_brl_cents(row.pix_cash_cents), styles["right"]),
                Paragraph("N/I" if row.cash_cents is None else format_brl_cents(row.cash_cents), styles["right"]),
                Paragraph("N/C" if row.expected_card_cents is None else format_brl_cents(row.expected_card_cents), styles["right"]),
                Paragraph(format_brl_cents(row.rede_cents), styles["right"]),
                Paragraph("N/C" if row.difference_cents is None else format_brl_cents(row.difference_cents), styles["right"]),
                Paragraph(row.status, styles["center"]),
            ]
        )
    total_status = report.overall_status
    data.append(
        [
            Paragraph("<b>TOTAL</b>", styles["center"]),
            Paragraph(f"<b>{format_brl_cents(report.totals['total_crm_cents'])}</b>", styles["right"]),
            Paragraph(f"<b>{format_brl_cents(report.totals['pix_cash_cents'])}</b>", styles["right"]),
            Paragraph(f"<b>{format_brl_cents(report.totals['cash_cents'])}</b>", styles["right"]),
            Paragraph(f"<b>{format_brl_cents(report.totals['expected_card_cents'])}</b>", styles["right"]),
            Paragraph(f"<b>{format_brl_cents(report.totals['rede_cents'])}</b>", styles["right"]),
            Paragraph(f"<b>{format_brl_cents(report.totals['difference_cents'])}</b>", styles["right"]),
            Paragraph(f"<b>{total_status}</b>", styles["center"]),
        ]
    )
    widths = [21 * mm, 22 * mm, 19 * mm, 19 * mm, 25 * mm, 21 * mm, 21 * mm, 30 * mm]
    table = Table(data, colWidths=widths, repeatRows=1, hAlign="LEFT")
    commands = [
        ("BACKGROUND", (0, 0), (-1, 0), NAVY),
        ("BOX", (0, 0), (-1, -1), 0.6, LINE),
        ("INNERGRID", (0, 0), (-1, -1), 0.3, LINE),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("BACKGROUND", (0, -1), (-1, -1), LIGHT),
        ("LINEABOVE", (0, -1), (-1, -1), 0.9, NAVY),
    ]
    for index, row in enumerate(report.rows, 1):
        if row.status == "OK":
            commands.extend(
                [
                    ("BACKGROUND", (-1, index), (-1, index), GREEN_BG),
                    ("TEXTCOLOR", (-1, index), (-1, index), GREEN),
                ]
            )
        else:
            color = ORANGE if row.status == "PENDENTE" else RED
            background = ORANGE_BG if row.status == "PENDENTE" else RED_BG
            commands.extend(
                [
                    ("BACKGROUND", (-1, index), (-1, index), background),
                    ("TEXTCOLOR", (-1, index), (-1, index), color),
                ]
            )
    table.setStyle(TableStyle(commands))
    return table


def _seller_tables(report: ReconciliationReport, styles):
    sellers = list(report.sellers)
    chunks = [sellers[index : index + 5] for index in range(0, len(sellers), 5)]
    flowables = []
    for chunk_index, chunk in enumerate(chunks, 1):
        if chunk_index > 1:
            flowables.append(PageBreak())
        suffix = f" ({chunk_index}/{len(chunks)})" if len(chunks) > 1 else ""
        flowables.append(Paragraph("Detalhamento por vendedora" + suffix, styles["section"]))
        headers = ["Data", *chunk, "Total CRM"]
        data = [[Paragraph(escape(header), styles["table_header"]) for header in headers]]
        for row in report.rows:
            data.append(
                [
                    Paragraph(format_date_br(row.date), styles["center"]),
                    *[
                        Paragraph(format_brl_cents(row.sellers_cents.get(seller, 0)), styles["right"])
                        for seller in chunk
                    ],
                    Paragraph(format_brl_cents(row.total_crm_cents), styles["right"]),
                ]
            )
        totals = [sum(row.sellers_cents.get(seller, 0) for row in report.rows) for seller in chunk]
        data.append(
            [
                Paragraph("<b>TOTAL</b>", styles["center"]),
                *[Paragraph(f"<b>{format_brl_cents(value)}</b>", styles["right"]) for value in totals],
                Paragraph(f"<b>{format_brl_cents(report.totals['total_crm_cents'])}</b>", styles["right"]),
            ]
        )
        count = len(headers)
        widths = [24 * mm] + [(156 * mm - 24 * mm) / (count - 1)] * (count - 1)
        table = Table(data, colWidths=widths, repeatRows=1, hAlign="LEFT")
        table.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, 0), NAVY),
                    ("BOX", (0, 0), (-1, -1), 0.6, LINE),
                    ("INNERGRID", (0, 0), (-1, -1), 0.3, LINE),
                    ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                    ("TOPPADDING", (0, 0), (-1, -1), 4),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
                    ("BACKGROUND", (0, -1), (-1, -1), LIGHT),
                    ("LINEABOVE", (0, -1), (-1, -1), 0.9, NAVY),
                ]
            )
        )
        flowables.append(table)
    return flowables


def generate_pdf_report(
    report: ReconciliationReport,
    observations: dict[date, str] | None = None,
    issued_at: datetime | None = None,
) -> bytes:
    observations = observations or {}
    issued_at = issued_at or datetime.now(ZoneInfo("America/Sao_Paulo"))
    styles = _styles()
    buffer = BytesIO()
    document = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        rightMargin=16 * mm,
        leftMargin=16 * mm,
        topMargin=18 * mm,
        bottomMargin=18 * mm,
        title="Conferência de Fechamento de Caixa",
        author="Conferência Rezende/L2H",
        subject=f"{report.company} - {format_date_br(report.start_date)} a {format_date_br(report.end_date)}",
    )

    if report.overall_status == "OK":
        status_color = GREEN
        status_background = GREEN_BG
    elif report.overall_status == "PENDENTE":
        status_color = ORANGE
        status_background = ORANGE_BG
    else:
        status_color = RED
        status_background = RED_BG
    story = [
        Spacer(1, 5 * mm),
        Paragraph("Conferência de Fechamento de Caixa", styles["title"]),
        Paragraph(
            f"<b>Empresa:</b> {escape(report.company)}<br/>"
            f"<b>Período:</b> {format_date_br(report.start_date)} a {format_date_br(report.end_date)}<br/>"
            f"<b>Data de emissão:</b> {issued_at.strftime('%d/%m/%Y %H:%M')}",
            styles["meta"],
        ),
        Spacer(1, 5 * mm),
        Paragraph("Resumo do período", styles["section"]),
        _summary_table(report, styles),
        *(
            [
                Spacer(1, 2 * mm),
                Paragraph(
                    "Há datas sem fechamento de caixa. Totais de PIX, dinheiro, esperado em cartão e diferença "
                    "consideram somente os dias com fechamento recebido; nenhum valor ausente foi tratado como zero.",
                    styles["small"],
                ),
            ]
            if report.pending_days
            else []
        ),
        Spacer(1, 3 * mm),
        Table(
            [[
                Paragraph(f"<b>Dias OK:</b> {report.ok_days}", styles["body"]),
                Paragraph(f"<b>Dias com divergência:</b> {report.divergent_days}", styles["body"]),
                Paragraph(f"<b>Dias pendentes:</b> {report.pending_days}", styles["body"]),
                Paragraph(
                    f"<b>Status geral:</b> <font color='{status_color.hexval()}'>{report.overall_status}</font>",
                    styles["body"],
                ),
            ]],
            colWidths=[37 * mm, 48 * mm, 45 * mm, 48 * mm],
            style=TableStyle(
                [
                    ("BOX", (0, 0), (-1, -1), 0.6, LINE),
                    ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                    ("LEFTPADDING", (0, 0), (-1, -1), 7),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 7),
                    ("TOPPADDING", (0, 0), (-1, -1), 6),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
                ]
            ),
        ),
        Spacer(1, 4 * mm),
        Paragraph("Conferência diária", styles["section"]),
        _daily_table(report, styles),
    ]

    if report.sellers:
        story.append(PageBreak())
        story.append(Spacer(1, 5 * mm))
        story.extend(_seller_tables(report, styles))

    story.extend([PageBreak(), Spacer(1, 5 * mm), Paragraph("Observações", styles["section"])])
    observation_rows = [
        (day, text.strip()) for day, text in sorted(observations.items()) if text and text.strip()
    ]
    if observation_rows:
        for day, text in observation_rows:
            story.append(
                KeepTogether(
                    [
                        Paragraph(f"<b>{format_date_br(day)}</b>", styles["body"]),
                        Paragraph(escape(text).replace("\n", "<br/>"), styles["body"]),
                        Spacer(1, 3 * mm),
                    ]
                )
            )
    else:
        story.append(Paragraph("Nenhuma observação registrada para o período.", styles["body"]))

    story.extend(
        [
            Spacer(1, 8 * mm),
            Table(
                [[
                    Paragraph("<b>Status geral do período</b>", styles["body"]),
                    Paragraph(
                        f"<b><font color='{status_color.hexval()}'>{report.overall_status}</font></b>",
                        styles["body"],
                    ),
                ]],
                colWidths=[120 * mm, 58 * mm],
                style=TableStyle(
                    [
                        ("BACKGROUND", (0, 0), (-1, -1), status_background),
                        ("BOX", (0, 0), (-1, -1), 0.8, status_color),
                        ("ALIGN", (1, 0), (1, 0), "RIGHT"),
                        ("TOPPADDING", (0, 0), (-1, -1), 9),
                        ("BOTTOMPADDING", (0, 0), (-1, -1), 9),
                        ("LEFTPADDING", (0, 0), (-1, -1), 8),
                        ("RIGHTPADDING", (0, 0), (-1, -1), 8),
                    ]
                ),
            ),
        ]
    )
    document.build(story, canvasmaker=NumberedCanvas)
    return buffer.getvalue()

from datetime import date, datetime
from io import BytesIO

from pypdf import PdfReader

from src.pdf_report import generate_pdf_report
from tests.test_reconciliation import _cash, _crm, _rede
from src.reconciliation import reconcile


def test_pdf_generation_is_portrait_and_contains_report():
    first = date(2026, 7, 2)
    second = date(2026, 7, 3)
    report = reconcile(
        "Rezende",
        first,
        second,
        _crm({first: 10_000, second: 20_000}),
        _rede({first: 8_500, second: 16_999}),
        _cash({first: (1_000, 500), second: (2_000, 1_000)}),
    )
    pdf_bytes = generate_pdf_report(
        report,
        {second: "Conferir a diferença objetiva de R$ 0,01."},
        issued_at=datetime(2026, 7, 20, 10, 30),
    )
    assert pdf_bytes.startswith(b"%PDF")
    reader = PdfReader(BytesIO(pdf_bytes))
    assert len(reader.pages) >= 3
    assert all(float(page.mediabox.height) > float(page.mediabox.width) for page in reader.pages)
    text = "\n".join(page.extract_text() or "" for page in reader.pages)
    assert "Conferência de Fechamento de Caixa" in text
    assert "02/07/2026 a 03/07/2026" in text
    assert "Página 1 de" in text
    assert "-0,00" not in text


def test_pdf_marks_missing_cash_day_as_pending_without_zero():
    first = date(2026, 7, 2)
    second = date(2026, 7, 3)
    report = reconcile(
        "Rezende",
        first,
        second,
        _crm({first: 10_000, second: 20_000}),
        _rede({first: 8_500, second: 17_000}),
        _cash({first: (1_000, 500)}),
    )
    pdf_bytes = generate_pdf_report(
        report,
        {second: "Fechamento de caixa não enviado para esta data. Solicitar o reenvio do relatório de caixa."},
        issued_at=datetime(2026, 7, 20, 10, 30),
    )
    text = "\n".join(
        page.extract_text() or "" for page in PdfReader(BytesIO(pdf_bytes)).pages
    )
    assert "PENDENTE" in text
    assert "N/I" in text
    assert "nenhum valor ausente foi tratado como zero" in text
    assert "Solicitar o reenvio do relatório de caixa" in text

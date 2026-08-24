from datetime import date

import pytest

from src.parsers.caixa_pdf import CashPdfParseError, parse_cash_pdf, parse_cash_pdfs
from src.parsers.crm import parse_crm
from src.parsers.rede import parse_rede
from tests.conftest import build_cash_pdf


def test_crm_parser_with_provided_sample(crm_path):
    crm = parse_crm(crm_path)
    assert crm.record_count == 1255
    assert crm.branch_codes == {"00353"}
    assert crm.company_candidates == {"Rezende"}
    assert crm.sellers == ("DANIELY", "JOANA", "LADY")
    assert min(crm.all_dates) == date(2026, 7, 2)
    assert max(crm.all_dates) == date(2026, 7, 18)
    assert sum(crm.total_on(day) for day in crm.all_dates) == 8_298_508


def test_rede_parser_with_provided_sample(rede_path):
    rede = parse_rede(rede_path)
    assert rede.record_count == 561
    assert rede.cnpjs == {"18547721000181"}
    assert rede.company_candidates == {"Rezende"}
    assert sum(rede.daily_approved_card.values()) == 7_673_281
    assert sum(rede.daily_paid_pix.values()) == 552_785
    assert min(rede.all_dates) == date(2026, 7, 2)
    assert max(rede.all_dates) == date(2026, 7, 18)


def test_cash_pdf_parser_with_provided_sample(caixa_path):
    closing = parse_cash_pdf(caixa_path)
    assert closing.company == "L2H"
    assert closing.date == date(2026, 7, 18)
    assert closing.pix_cents == 14_480
    assert closing.cash_cents == 2_990
    assert closing.store_total_cents == 408_040
    assert closing.branch == "MORANA JARDIM BOTANICO SHOPPING"


def test_rezende_cash_pdf_is_identified_by_branch_and_proves_zero_cash():
    source = build_cash_pdf(
        company="",
        day="16/07/2026",
        branch="MORANA ASA NORTE BSB",
        include_cash=False,
        pix="575,11",
        total="3.323,64",
        extra_sales_lines=(
            "CARTÃO DE CRÉDITO 2.668,73 0,00 0,00 2.668,73",
            "CARTÃO DE DÉBITO 79,80 0,00 0,00 79,80",
        ),
        name="caixa-rezende.pdf",
    )
    closing = parse_cash_pdf(source)
    assert closing.company == "Rezende"
    assert closing.date == date(2026, 7, 16)
    assert closing.pix_cents == 57_511
    assert closing.cash_cents == 0
    assert closing.store_total_cents == 332_364
    assert closing.cash_zero_verified_from_total

    batch = parse_cash_pdfs([source])
    assert any("saldo R$ 0,00 comprovado" in warning for warning in batch.warnings)


def test_missing_cash_line_without_total_proof_is_blocking():
    source = build_cash_pdf(
        company="",
        branch="MORANA ASA NORTE BSB",
        include_cash=False,
        pix="10,00",
        total="100,00",
        name="caixa-sem-prova.pdf",
    )
    with pytest.raises(CashPdfParseError, match="não foi possível comprovar saldo zero"):
        parse_cash_pdf(source)


def test_equal_duplicate_cash_pdf_is_ignored(caixa_path):
    batch = parse_cash_pdfs([caixa_path, caixa_path])
    assert len(batch.closings) == 1
    assert len(batch.warnings) == 1
    assert "Duplicidade ignorada" in batch.warnings[0]


def test_conflicting_duplicate_cash_pdf_blocks():
    first = build_cash_pdf(pix="144,80", name="primeiro.pdf")
    second = build_cash_pdf(pix="144,81", name="segundo.pdf")
    with pytest.raises(CashPdfParseError, match="Conflito de fechamentos"):
        parse_cash_pdfs([first, second])


def test_pdf_without_extractable_text_has_clear_error():
    from io import BytesIO
    from reportlab.pdfgen import canvas
    from tests.conftest import NamedBytesIO

    buffer = BytesIO()
    pdf = canvas.Canvas(buffer)
    pdf.showPage()
    pdf.save()
    with pytest.raises(CashPdfParseError, match="não possui texto extraível"):
        parse_cash_pdf(NamedBytesIO(buffer.getvalue(), "sem-texto.pdf"))

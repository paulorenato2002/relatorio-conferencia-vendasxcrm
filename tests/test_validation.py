from datetime import date

from src.models import CashBatchData
from src.parsers.caixa_pdf import parse_cash_pdfs
from src.parsers.crm import parse_crm
from src.parsers.rede import parse_rede
from src.validation import validate_inputs


def test_files_from_different_companies_are_blocked(crm_path, rede_path, caixa_path):
    result = validate_inputs(
        "Rezende",
        date(2026, 7, 2),
        date(2026, 7, 18),
        parse_crm(crm_path),
        parse_rede(rede_path),
        parse_cash_pdfs([caixa_path]),
    )
    assert not result.is_valid
    assert any("empresa diferente" in error.lower() for error in result.errors)


def test_missing_cash_pdf_is_allowed_as_pending_without_assuming_zero(crm_path, rede_path):
    result = validate_inputs(
        "Rezende",
        date(2026, 7, 2),
        date(2026, 7, 18),
        parse_crm(crm_path),
        parse_rede(rede_path),
        CashBatchData(closings={}),
    )
    assert result.is_valid
    assert any("ficarão pendentes" in warning.lower() for warning in result.warnings)
    assert any("não serão assumidos como zero" in warning.lower() for warning in result.warnings)

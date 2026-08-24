from datetime import date

from src.diagnostics import suggest_observations
from src.models import RedeData, RedeTransaction
from src.reconciliation import reconcile
from tests.test_reconciliation import _cash, _crm


def test_diagnostics_use_pix_and_denied_transaction_evidence():
    day = date(2026, 7, 10)
    approved = RedeTransaction(day, "aprovada", "credito", 79_510)
    paid_pix = RedeTransaction(day, "pago", "pix", 5_000)
    denied = RedeTransaction(day, "negada", "credito", 10_490)
    rede = RedeData(
        file_name="rede.xlsx",
        daily_approved_card={day: 79_510},
        daily_paid_pix={day: 5_000},
        all_dates={day},
        transactions=(approved, paid_pix, denied),
        exception_transactions=(denied,),
        company_candidates={"Rezende"},
    )
    crm = _crm({day: 100_000})
    report = reconcile(
        "Rezende", day, day, crm, rede, _cash({day: (10_000, 0)})
    )
    note = suggest_observations(report, crm, rede)[day]
    assert "R$ 50,00 acima" in note
    assert "transação negada de R$ 104,90" in note


def test_diagnostics_find_matching_transaction_next_day():
    day = date(2026, 7, 10)
    next_day = date(2026, 7, 11)
    approved = RedeTransaction(day, "aprovada", "credito", 89_900)
    shifted = RedeTransaction(next_day, "aprovada", "credito", 100)
    rede = RedeData(
        file_name="rede.xlsx",
        daily_approved_card={day: 89_900},
        daily_paid_pix={day: 10_000},
        all_dates={day, next_day},
        transactions=(approved, shifted),
        exception_transactions=(),
        company_candidates={"Rezende"},
    )
    crm = _crm({day: 100_000})
    report = reconcile(
        "Rezende", day, day, crm, rede, _cash({day: (10_000, 0)})
    )
    note = suggest_observations(report, crm, rede)[day]
    assert "no dia seguinte" in note
    assert "R$ 1,00" in note


def test_pending_day_requests_cash_report_resend():
    day = date(2026, 7, 10)
    rede = RedeData(
        file_name="rede.xlsx",
        daily_approved_card={day: 90_000},
        daily_paid_pix={},
        all_dates={day},
        transactions=(RedeTransaction(day, "aprovada", "credito", 90_000),),
        exception_transactions=(),
        company_candidates={"Rezende"},
    )
    crm = _crm({day: 100_000})
    report = reconcile("Rezende", day, day, crm, rede, _cash({}))
    note = suggest_observations(report, crm, rede)[day]
    assert report.rows[0].status == "PENDENTE"
    assert "Fechamento de caixa não enviado" in note
    assert "Solicitar o reenvio" in note

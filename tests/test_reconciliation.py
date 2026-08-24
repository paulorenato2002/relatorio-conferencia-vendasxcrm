from datetime import date

from src.models import CashBatchData, CashClosing, CrmData, RedeData, RedeTransaction
from src.reconciliation import reconcile


def _crm(values_by_day):
    daily = {day: {"VENDEDORA": cents} for day, cents in values_by_day.items()}
    return CrmData(
        file_name="crm.xlsx",
        daily_by_seller=daily,
        sellers=("VENDEDORA",),
        all_dates=set(daily),
        company_candidates={"Rezende"},
    )


def _rede(values_by_day):
    transactions = tuple(
        RedeTransaction(day, "aprovada", "credito", cents)
        for day, cents in values_by_day.items()
    )
    return RedeData(
        file_name="rede.xlsx",
        daily_approved_card=dict(values_by_day),
        daily_paid_pix={},
        all_dates=set(values_by_day),
        transactions=transactions,
        exception_transactions=(),
        company_candidates={"Rezende"},
    )


def _cash(values_by_day):
    closings = {}
    for day, (pix, cash) in values_by_day.items():
        closing = CashClosing(
            file_name=f"{day}.pdf",
            company="Rezende",
            legal_name="Rezende",
            branch=None,
            date=day,
            pix_cents=pix,
            cash_cents=cash,
            store_total_cents=0,
        )
        closings[("Rezende", day)] = closing
    return CashBatchData(closings)


def test_one_cent_difference_is_divergence():
    day = date(2026, 7, 2)
    report = reconcile(
        "Rezende",
        day,
        day,
        _crm({day: 10_000}),
        _rede({day: 8_499}),
        _cash({day: (1_000, 500)}),
    )
    assert report.rows[0].difference_cents == 1
    assert report.rows[0].status == "DIVERGÊNCIA"
    assert report.divergent_days == 1
    assert report.pending_days == 0


def test_report_totals_are_sum_of_daily_rows():
    first = date(2026, 7, 2)
    second = date(2026, 7, 3)
    report = reconcile(
        "Rezende",
        first,
        second,
        _crm({first: 10_000, second: 20_000}),
        _rede({first: 8_500, second: 17_000}),
        _cash({first: (1_000, 500), second: (2_000, 1_000)}),
    )
    assert report.totals == {
        "total_crm_cents": 30_000,
        "pix_cash_cents": 3_000,
        "cash_cents": 1_500,
        "expected_card_cents": 25_500,
        "rede_cents": 25_500,
        "difference_cents": 0,
    }
    assert report.ok_days == 2
    assert report.divergent_days == 0
    assert report.pending_days == 0


def test_missing_cash_closing_creates_pending_row_without_zero_values():
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
    pending = report.rows[1]
    assert pending.date == second
    assert pending.status == "PENDENTE"
    assert pending.pix_cash_cents is None
    assert pending.cash_cents is None
    assert pending.expected_card_cents is None
    assert pending.difference_cents is None
    assert report.pending_days == 1
    assert report.overall_status == "PENDENTE"
    assert report.totals == {
        "total_crm_cents": 30_000,
        "pix_cash_cents": 1_000,
        "cash_cents": 500,
        "expected_card_cents": 8_500,
        "rede_cents": 25_500,
        "difference_cents": 0,
    }

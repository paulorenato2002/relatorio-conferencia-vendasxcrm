from __future__ import annotations

from datetime import date

from src.models import (
    CashBatchData,
    CrmData,
    ReconciliationReport,
    ReconciliationRow,
    RedeData,
    ValidationResult,
)


def reconcile(
    company: str,
    start_date: date,
    end_date: date,
    crm: CrmData,
    rede: RedeData,
    cash: CashBatchData,
    validation: ValidationResult | None = None,
) -> ReconciliationReport:
    if validation is not None and not validation.is_valid:
        raise ValueError("A conferência não pode ser processada enquanto houver erros de validação.")
    if start_date > end_date:
        raise ValueError("Período inválido.")

    company_cash = cash.for_company(company)
    movement_dates = {
        day
        for day in (crm.all_dates | rede.all_dates | set(company_cash))
        if start_date <= day <= end_date
        and (
            crm.total_on(day) != 0
            or day in rede.all_dates
            or (
                day in company_cash
                and (
                    company_cash[day].pix_cents != 0
                    or company_cash[day].cash_cents != 0
                    or company_cash[day].store_total_cents != 0
                )
            )
        )
    }

    rows: list[ReconciliationRow] = []
    for day in sorted(movement_dates):
        closing = company_cash.get(day)
        sellers = {seller: crm.daily_by_seller.get(day, {}).get(seller, 0) for seller in crm.sellers}
        total_crm = sum(sellers.values())
        rede_cents = rede.daily_approved_card.get(day, 0)
        if closing is None:
            pix_cents = None
            cash_cents = None
            expected_card = None
            difference = None
            status = "PENDENTE"
        else:
            pix_cents = closing.pix_cents
            cash_cents = closing.cash_cents
            expected_card = total_crm - pix_cents - cash_cents
            difference = expected_card - rede_cents
            status = "OK" if difference == 0 else "DIVERGÊNCIA"
        rows.append(
            ReconciliationRow(
                date=day,
                sellers_cents=sellers,
                total_crm_cents=total_crm,
                pix_cash_cents=pix_cents,
                cash_cents=cash_cents,
                expected_card_cents=expected_card,
                rede_cents=rede_cents,
                difference_cents=difference,
                status=status,
            )
        )

    totals = {
        "total_crm_cents": sum(row.total_crm_cents for row in rows),
        "pix_cash_cents": sum(row.pix_cash_cents for row in rows if row.pix_cash_cents is not None),
        "cash_cents": sum(row.cash_cents for row in rows if row.cash_cents is not None),
        "expected_card_cents": sum(row.expected_card_cents for row in rows if row.expected_card_cents is not None),
        "rede_cents": sum(row.rede_cents for row in rows),
        "difference_cents": sum(row.difference_cents for row in rows if row.difference_cents is not None),
    }
    ok_days = sum(row.status == "OK" for row in rows)
    divergent_days = sum(row.status == "DIVERGÊNCIA" for row in rows)
    pending_days = sum(row.status == "PENDENTE" for row in rows)
    return ReconciliationReport(
        company=company,
        start_date=start_date,
        end_date=end_date,
        sellers=crm.sellers,
        rows=rows,
        totals=totals,
        ok_days=ok_days,
        divergent_days=divergent_days,
        pending_days=pending_days,
    )

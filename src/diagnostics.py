from __future__ import annotations

from collections import defaultdict
from datetime import timedelta

from src.formatters import format_brl_currency
from src.models import CrmData, ReconciliationReport, RedeData


_STATUS_LABELS = {
    "negada": "negada",
    "negado": "negada",
    "expirada": "expirada",
    "expirado": "expirada",
    "devolvida": "devolvida",
    "devolvido": "devolvida",
    "cancelada": "cancelada",
    "cancelado": "cancelada",
}


def _pix_evidence(cash_pix: int, rede_pix: int) -> str | None:
    delta = cash_pix - rede_pix
    if delta == 0:
        return None
    direction = "acima" if delta > 0 else "abaixo"
    return (
        f"O PIX do fechamento de caixa está {format_brl_currency(abs(delta))} "
        f"{direction} do PIX pago registrado na Rede."
    )


def suggest_observations(
    report: ReconciliationReport,
    crm: CrmData,
    rede: RedeData,
) -> dict:
    transactions_by_date = defaultdict(list)
    exceptions_by_date = defaultdict(list)
    for transaction in rede.transactions:
        transactions_by_date[transaction.date].append(transaction)
    for transaction in rede.exception_transactions:
        exceptions_by_date[transaction.date].append(transaction)

    suggestions = {}
    for row in report.rows:
        if row.status == "PENDENTE":
            suggestions[row.date] = (
                "Fechamento de caixa não enviado para esta data. "
                "Solicitar o reenvio do relatório de caixa."
            )
            continue
        if row.status == "OK":
            suggestions[row.date] = ""
            continue
        evidence: list[str] = []
        if row.date not in crm.all_dates:
            evidence.append("Não há movimentação do CRM nesta data.")
        if row.date not in rede.all_dates:
            evidence.append("Não há transações da Rede nesta data.")

        pix_note = _pix_evidence(
            row.pix_cash_cents, rede.daily_paid_pix.get(row.date, 0)
        )
        if pix_note:
            evidence.append(pix_note)

        target = abs(row.difference_cents or 0)
        matching_exceptions = [
            transaction
            for transaction in exceptions_by_date.get(row.date, [])
            if transaction.value_cents == target
        ]
        if matching_exceptions:
            transaction = matching_exceptions[0]
            status = _STATUS_LABELS.get(transaction.status, transaction.status)
            evidence.append(
                f"Foi localizada uma transação {status} de "
                f"{format_brl_currency(transaction.value_cents)} na Rede, valor "
                "correspondente à diferença do dia."
            )

        for offset, label in ((-1, "anterior"), (1, "seguinte")):
            adjacent_date = row.date + timedelta(days=offset)
            matches = [
                transaction
                for transaction in transactions_by_date.get(adjacent_date, [])
                if transaction.value_cents == target
            ]
            if matches:
                evidence.append(
                    f"Foi localizada uma transação de {format_brl_currency(target)} "
                    f"no dia {label}, com valor correspondente à divergência."
                )
                break

        if not evidence:
            evidence.append(
                "Não foi possível identificar automaticamente a causa. "
                "Conferir as boletas e transações do dia."
            )
        suggestions[row.date] = " ".join(evidence)
    return suggestions

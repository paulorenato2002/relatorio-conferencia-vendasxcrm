"""Contrato do JSON devolvido pelo n8n e auto-conferência aritmética.

A leitura de manuscrito erra. O que torna o resultado utilizável não é confiar
no modelo, é conferir a boleta contra ela mesma: a soma dos itens precisa bater
com o SUB TOTAL, o SUB TOTAL menos o DESCONTO precisa bater com o TOTAL, e o
número de peças precisa bater com a quantidade de itens lidos. Boleta que não
fecha sozinha vai para revisão manual e não entra no cruzamento em silêncio.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import replace
from datetime import date
import re
from typing import Sequence
import unicodedata

from src.formatters import format_brl_currency, parse_money_cents
from src.models import Boleta, BoletaItem


class BoletaSchemaError(ValueError):
    pass


PAYMENT_METHODS = ("pix", "dinheiro", "credito", "debito")

_FLAG_FIELDS = ("brinde", "presente", "whats", "cashback", "aniver", "outros")

# Quantas boletas do mesmo arquivo precisam concordar numa data para que uma
# data solitária seja tratada como suspeita.
_DATE_CONSENSUS_MIN = 3

_PAYMENT_ALIASES = {
    "pix": "pix",
    "dinheiro": "dinheiro",
    "especie": "dinheiro",
    "credito": "credito",
    "cartao de credito": "credito",
    "debito": "debito",
    "cartao de debito": "debito",
}


def normalize_person(value: object) -> str | None:
    """Maiúsculas sem acento, para casar com o `nome_vendedor` do CRM."""
    text = str(value or "").strip()
    if not text:
        return None
    decomposed = unicodedata.normalize("NFKD", text)
    stripped = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return re.sub(r"\s+", " ", stripped).upper() or None


def normalize_barcode(value: object) -> str:
    """Código da etiqueta em 10 dígitos, sem os zeros à esquerda do CRM."""
    digits = re.sub(r"\D", "", str(value or ""))
    return digits.lstrip("0") or digits


def barcode_to_crm_code(barcode: str) -> str:
    """Forma do CRM: 13 dígitos com zeros à esquerda."""
    return re.sub(r"\D", "", barcode).zfill(13)


def resolve_boleta_date(value: object, start: date, end: date) -> date | None:
    """Resolve a data da boleta, que vem escrita sem o ano (`22/09`).

    O ano sai do período informado na tela. Quando o período cruza a virada do
    ano, escolhe-se o ano que faz a data cair dentro do intervalo.
    """
    text = str(value or "").strip()
    if not text:
        return None

    iso = re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})", text)
    if iso:
        year, month, day = (int(part) for part in iso.groups())
        try:
            return date(year, month, day)
        except ValueError as exc:
            raise BoletaSchemaError(f"Data inválida na boleta: {text!r}") from exc

    br = re.fullmatch(r"(\d{1,2})\s*[/\-.]\s*(\d{1,2})(?:\s*[/\-.]\s*(\d{2}|\d{4}))?", text)
    if not br:
        raise BoletaSchemaError(f"Formato de data não reconhecido na boleta: {text!r}")

    day, month = int(br.group(1)), int(br.group(2))
    raw_year = br.group(3)
    if raw_year:
        year = int(raw_year)
        candidates = [year + 2000 if year < 100 else year]
    else:
        candidates = sorted({start.year, end.year})

    resolved: date | None = None
    for year in candidates:
        try:
            candidate = date(year, month, day)
        except ValueError:
            continue
        if start <= candidate <= end:
            return candidate
        resolved = resolved or candidate
    if resolved is None:
        raise BoletaSchemaError(f"Data inválida na boleta: {text!r}")
    return resolved


def _money(value: object) -> int | None:
    if value is None or value == "":
        return None
    try:
        return parse_money_cents(value)
    except ValueError as exc:
        raise BoletaSchemaError(f"Valor monetário inválido na boleta: {value!r}") from exc


def _integer(value: object) -> int | None:
    if value is None or value == "":
        return None
    digits = re.sub(r"\D", "", str(value))
    return int(digits) if digits else None


def _payment_method(value: object) -> str | None:
    key = normalize_person(value)
    if not key:
        return None
    return _PAYMENT_ALIASES.get(key.lower())


def _items(raw: object, field: str) -> tuple[list[BoletaItem], list[str]]:
    if raw in (None, ""):
        return [], []
    if not isinstance(raw, list):
        raise BoletaSchemaError(f"O campo `{field}` deve ser uma lista.")
    items: list[BoletaItem] = []
    problems: list[str] = []
    for entry in raw:
        if not isinstance(entry, dict):
            raise BoletaSchemaError(f"Cada entrada de `{field}` deve ser um objeto.")
        barcode = normalize_barcode(entry.get("codigo"))
        value_cents = _money(entry.get("valor"))
        if value_cents is None:
            problems.append(f"item {barcode or '(sem código)'} sem valor legível")
            continue
        if barcode and len(barcode) != 10:
            problems.append(
                f"código {barcode} tem {len(barcode)} dígitos; a etiqueta usa 10"
            )
        items.append(
            BoletaItem(
                codigo=barcode,
                value_cents=value_cents,
                handwritten=bool(entry.get("manuscrito")),
            )
        )
    return items, problems


def _run_checks(
    items: list[BoletaItem],
    returns: list[BoletaItem],
    sub_total: int | None,
    discount: int | None,
    total: int | None,
    piece_count: int | None,
) -> list[str]:
    checks: list[str] = []
    items_sum = sum(item.value_cents for item in items)

    if not items:
        checks.append("nenhum item foi lido na boleta")
    if total is None:
        checks.append("TOTAL não foi lido")
    if piece_count is not None and items and piece_count != len(items):
        checks.append(
            f"Nº PEÇAS informa {piece_count}, mas foram lidos {len(items)} itens"
        )
    if sub_total is not None and items and items_sum != sub_total:
        checks.append(
            f"soma dos itens ({format_brl_currency(items_sum)}) difere do "
            f"SUB TOTAL ({format_brl_currency(sub_total)})"
        )
    if sub_total is not None and total is not None:
        expected = sub_total - (discount or 0)
        if expected != total:
            checks.append(
                f"SUB TOTAL - DESCONTO ({format_brl_currency(expected)}) difere do "
                f"TOTAL ({format_brl_currency(total)})"
            )
    elif total is not None and discount is None and items and items_sum != total:
        checks.append(
            f"soma dos itens ({format_brl_currency(items_sum)}) difere do "
            f"TOTAL ({format_brl_currency(total)})"
        )
    if returns:
        returns_sum = sum(item.value_cents for item in returns)
        if discount is not None and returns_sum != discount:
            checks.append(
                f"soma das trocas ({format_brl_currency(returns_sum)}) difere do "
                f"DESCONTO ({format_brl_currency(discount)})"
            )
    return checks


def flag_date_outliers(boletas: Sequence[Boleta]) -> list[Boleta]:
    """Marca boletas cuja data destoa das vizinhas do mesmo arquivo.

    A data é o único campo importante que não tem como ser conferido dentro da
    própria boleta: está escrito uma vez e não cruza com mais nada no papel. Um
    dígito lido errado aí inventa um dia inteiro no relatório em silêncio.

    A conferência possível é entre boletas: um lote escaneado junto costuma ser
    do mesmo dia. A regra é conservadora de propósito — só acusa quando a data
    aparece uma única vez no arquivo e outra data domina o mesmo lote. Um scan
    que legitimamente cobre vários dias não dispara nada.
    """
    by_file: dict[str, list[int]] = {}
    for index, boleta in enumerate(boletas):
        by_file.setdefault(boleta.source_file, []).append(index)

    result = list(boletas)
    for indexes in by_file.values():
        counts = Counter(
            result[index].date for index in indexes if result[index].date is not None
        )
        if len(counts) < 2:
            continue
        dominant, dominant_count = counts.most_common(1)[0]
        if dominant_count < _DATE_CONSENSUS_MIN:
            continue
        for index in indexes:
            boleta = result[index]
            if boleta.date is None or counts[boleta.date] > 1:
                continue
            result[index] = replace(
                boleta,
                checks=boleta.checks
                + (
                    f"data {boleta.date:%d/%m} aparece só nesta boleta; as outras "
                    f"{dominant_count} do mesmo arquivo são de {dominant:%d/%m}",
                ),
            )
    return result


def build_boleta(
    payload: dict,
    *,
    source_file: str,
    page: int,
    position: int,
    start: date,
    end: date,
) -> Boleta:
    """Converte um objeto JSON do n8n em `Boleta`, já auto-conferida."""
    if not isinstance(payload, dict):
        raise BoletaSchemaError("A boleta devolvida pelo n8n não é um objeto JSON.")

    items, item_problems = _items(payload.get("itens"), "itens")
    returns, return_problems = _items(payload.get("trocas"), "trocas")

    sub_total = _money(payload.get("sub_total"))
    discount = _money(payload.get("desconto"))
    total = _money(payload.get("total"))
    piece_count = _integer(payload.get("num_pecas"))

    unreadable = payload.get("campos_ilegiveis") or []
    if not isinstance(unreadable, list):
        raise BoletaSchemaError("O campo `campos_ilegiveis` deve ser uma lista.")

    checks = _run_checks(items, returns, sub_total, discount, total, piece_count)
    checks.extend(item_problems)
    checks.extend(return_problems)

    flags = frozenset(field for field in _FLAG_FIELDS if bool(payload.get(field)))

    return Boleta(
        source_file=source_file,
        page=page,
        position=position,
        numero=str(payload.get("numero") or "").strip() or None,
        numero_controle=str(payload.get("numero_controle") or "").strip() or None,
        date=resolve_boleta_date(payload.get("data"), start, end),
        seller=normalize_person(payload.get("vendedora")),
        client=str(payload.get("cliente") or "").strip() or None,
        phone=re.sub(r"\D", "", str(payload.get("telefone") or "")) or None,
        items=tuple(items),
        returns=tuple(returns),
        sub_total_cents=sub_total,
        discount_cents=discount,
        total_cents=total,
        piece_count=piece_count,
        payment_method=_payment_method(payload.get("pagamento")),
        installments=_integer(payload.get("parcelas")),
        card_brand=str(payload.get("bandeira") or "").strip() or None,
        flags=flags,
        unreadable_fields=tuple(str(field) for field in unreadable),
        checks=tuple(checks),
    )

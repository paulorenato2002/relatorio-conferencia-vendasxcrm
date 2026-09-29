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
from decimal import ROUND_HALF_UP, Decimal
import re
from typing import Sequence
import unicodedata

from src.formatters import (
    barcode_to_crm_code,
    format_brl_currency,
    normalize_barcode,
    parse_money_cents,
)
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


_MONTHS = {
    "jan": 1, "fev": 2, "mar": 3, "abr": 4, "mai": 5, "jun": 6,
    "jul": 7, "ago": 8, "set": 9, "out": 10, "nov": 11, "dez": 12,
}

# `22/09`, `22-9`, `22.09.2026`
_NUMERIC_DATE = re.compile(r"(\d{1,2})\s*[/\-.]\s*(\d{1,2})(?:\s*[/\-.]\s*(\d{2}|\d{4}))?")
# `05/SET`, `19 SET. 2026`, `9 SET`, `22/set.2026`, `5 de setembro` — como boa
# parte das vendedoras escreve. Visto no lote de setembro/2026: 208 de 783
# boletas estavam assim e eram descartadas por "formato não reconhecido".
_NAMED_DATE = re.compile(
    r"(\d{1,2})\s*(?:[/\-.]|\s+de\s+)?\s*([a-zç]{3,})\.?\s*[,/\-.]?\s*(\d{2}|\d{4})?",
    re.IGNORECASE,
)
# Só o dia (`30`): o mês vem do nome do arquivo ou do período.
_DAY_ONLY = re.compile(r"(\d{1,2})")
# Data no nome do arquivo, como a loja salva: "Boletas Leide 05.09.pdf".
_FILENAME_DATE = re.compile(r"(?<!\d)(\d{1,2})[.\-_](\d{1,2})(?:[.\-_](\d{2}|\d{4}))?(?!\d)")


def _month_from_name(token: str) -> int | None:
    key = unicodedata.normalize("NFKD", token.lower())
    key = "".join(ch for ch in key if not unicodedata.combining(ch))[:3]
    return _MONTHS.get(key)


def date_from_filename(file_name: str, start: date, end: date) -> date | None:
    """Data do lote pelo nome do arquivo (`Boletas Leide 05.09.pdf`), ou `None`.

    No lote de setembro/2026 o nome bateu com a data escrita em 648 boletas e
    divergiu em 33 — quase todas o modelo comendo um dígito (`1 SET` num
    arquivo de 21/09, `28/09` num período que acabava em 27/09).
    """
    for match in _FILENAME_DATE.finditer(file_name or ""):
        day, month = int(match.group(1)), int(match.group(2))
        raw_year = match.group(3)
        years = [int(raw_year) + (2000 if int(raw_year) < 100 else 0)] if raw_year else sorted({start.year, end.year})
        for year in years:
            try:
                candidate = date(year, month, day)
            except ValueError:
                continue
            if start <= candidate <= end:
                return candidate
    return None


def resolve_boleta_date(
    value: object, start: date, end: date, month_hint: int | None = None
) -> date | None:
    """Resolve a data da boleta, que vem escrita sem o ano (`22/09`, `05/SET`).

    O ano sai do período informado na tela. Quando o período cruza a virada do
    ano, escolhe-se o ano que faz a data cair dentro do intervalo. Dia sozinho
    (`30`) usa `month_hint` — o mês do nome do arquivo —, ou o mês do período
    quando ele cabe num mês só.
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

    raw_year = None
    numeric = _NUMERIC_DATE.fullmatch(text)
    named = _NAMED_DATE.fullmatch(text)
    day_only = _DAY_ONLY.fullmatch(text)
    if numeric:
        day, month, raw_year = int(numeric.group(1)), int(numeric.group(2)), numeric.group(3)
    elif named and _month_from_name(named.group(2)):
        day, month, raw_year = int(named.group(1)), _month_from_name(named.group(2)), named.group(3)
    elif day_only:
        day = int(day_only.group(1))
        month = month_hint or (start.month if (start.year, start.month) == (end.year, end.month) else None)
        if month is None:
            raise BoletaSchemaError(f"Data sem mês na boleta: {text!r}")
    else:
        raise BoletaSchemaError(f"Formato de data não reconhecido na boleta: {text!r}")

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


def _is_blank(value: object) -> bool:
    """Vazio, inclusive o texto "null" que o modelo às vezes devolve no lugar do
    nulo do JSON — no lote de setembro/2026, 3 boletas caíam por isso."""
    return value is None or str(value).strip().lower() in {"", "null", "none", "-", "--"}


def _money(value: object) -> int | None:
    if _is_blank(value):
        return None
    try:
        return parse_money_cents(value)
    except ValueError as exc:
        raise BoletaSchemaError(f"Valor monetário inválido na boleta: {value!r}") from exc


_PERCENT = re.compile(r"\s*(\d+(?:[.,]\d+)?)\s*%\s*")


def _discount(value: object, base_cents: int | None) -> tuple[int | None, bool, str | None]:
    """DESCONTO em reais ou em porcentagem: (centavos, era porcentagem, problema).

    No lote de setembro/2026, 48 boletas traziam o desconto como `10%`, `15%`,
    `20%`. Tratado como dinheiro, `10%` virava R$ 10,00 sem aviso e a conta da
    boleta deixava de fechar. A porcentagem incide sobre o SUB TOTAL, ou sobre a
    soma dos itens quando o SUB TOTAL está em branco.
    """
    if _is_blank(value):
        return None, False, None
    percent = _PERCENT.fullmatch(str(value))
    if not percent:
        return _money(value), False, None
    if base_cents is None:
        return None, True, f"DESCONTO de {value} sem SUB TOTAL nem itens para calcular o valor"
    rate = Decimal(percent.group(1).replace(",", "."))
    cents = (Decimal(base_cents) * rate / 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    return int(cents), True, None


def _integer(value: object) -> int | None:
    if _is_blank(value):
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
        # Código com dígito a mais ou a menos não vai para revisão: não há o que
        # corrigir na tela (código de barras não é editável), e o cruzamento com
        # o CRM reconhece o código a um dígito de distância como erro de leitura.
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
    discount_is_percent: bool = False,
) -> list[str]:
    checks: list[str] = []
    items_sum = sum(item.value_cents for item in items)
    # Desconto em porcentagem é arredondado no caixa; um centavo para cima ou
    # para baixo é o arredondamento, não erro de leitura.
    tolerance = 1 if discount_is_percent else 0

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
        if abs(expected - total) > tolerance:
            checks.append(
                f"SUB TOTAL - DESCONTO ({format_brl_currency(expected)}) difere do "
                f"TOTAL ({format_brl_currency(total)})"
            )
    elif total is not None and items:
        expected = items_sum - (discount or 0)
        if abs(expected - total) > tolerance:
            rotulo = "soma dos itens - DESCONTO" if discount else "soma dos itens"
            checks.append(
                f"{rotulo} ({format_brl_currency(expected)}) difere do "
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
        # Data do nome do arquivo ou digitada na tela já tem fonte melhor que a
        # comparação com as vizinhas.
        if boleta.date_source != "boleta":
            continue
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
                date_suspect=True,
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
    items_sum = sum(item.value_cents for item in items) if items else None
    discount, discount_is_percent, discount_problem = _discount(
        payload.get("desconto"), sub_total if sub_total is not None else items_sum
    )
    total = _money(payload.get("total"))
    piece_count = _integer(payload.get("num_pecas"))

    unreadable = payload.get("campos_ilegiveis") or []
    if not isinstance(unreadable, list):
        raise BoletaSchemaError("O campo `campos_ilegiveis` deve ser uma lista.")
    unreadable = [str(field) for field in unreadable]

    checks = _run_checks(
        items, returns, sub_total, discount, total, piece_count, discount_is_percent
    )
    checks.extend(item_problems)
    checks.extend(return_problems)
    if discount_problem:
        checks.append(discount_problem)

    effective_date, date_source, date_check = _effective_date(
        payload, source_file, start, end
    )
    if date_check:
        checks.append(date_check)
    if date_source == "arquivo" and "data" in unreadable:
        # A data em branco ou ilegível no papel foi coberta pelo nome do
        # arquivo; não há mais o que conferir nela.
        unreadable = [field for field in unreadable if field != "data"]

    flags = frozenset(field for field in _FLAG_FIELDS if bool(payload.get(field)))

    return Boleta(
        source_file=source_file,
        page=page,
        position=position,
        numero=str(payload.get("numero") or "").strip() or None,
        numero_controle=str(payload.get("numero_controle") or "").strip() or None,
        date=effective_date,
        date_source=date_source,
        items_uncertain=(
            not items
            or bool(item_problems)
            or (piece_count is not None and piece_count != len(items))
        ),
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
        unreadable_fields=tuple(unreadable),
        checks=tuple(checks),
    )


def _effective_date(
    payload: dict, source_file: str, start: date, end: date
) -> tuple[date | None, str, str | None]:
    """Data usada para a boleta: (data, de onde veio, aviso para revisão).

    Ordem de prioridade:

    1. Data digitada na tela (`data_confirmada`): o operador tem o papel na mão.
    2. Data no nome do arquivo: é o dia do lote. Se a boleta disser outro dia,
       vale a do arquivo e a boleta vai para revisão mostrando as duas — no lote
       de setembro/2026 foram 33 casos, quase todos um dígito comido pelo
       modelo. Se a boleta for mesmo de outro dia, basta digitar a data.
    3. Data lida na boleta, sem arquivo datado para comparar.

    Data que não dá para interpretar nunca derruba a boleta: vira `None`, e o
    nome do arquivo ou a correção na tela resolvem.
    """
    file_date = date_from_filename(source_file, start, end)
    try:
        read_date = resolve_boleta_date(
            payload.get("data"), start, end, month_hint=file_date.month if file_date else None
        )
        read_problem = None
    except BoletaSchemaError:
        read_date = None
        read_problem = f"data escrita como {payload.get('data')!r} não pôde ser interpretada"

    if payload.get("data_confirmada") and read_date is not None:
        return read_date, "confirmada", None
    if file_date is not None:
        if read_date is not None and read_date != file_date:
            return file_date, "arquivo", (
                f"a boleta diz {read_date:%d/%m}, o arquivo é de {file_date:%d/%m}; "
                f"foi considerado {file_date:%d/%m}. Se a boleta é mesmo de outro "
                "dia, digite a data na tabela"
            )
        return file_date, "arquivo", None
    return read_date, "boleta", read_problem

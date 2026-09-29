"""Cruzamento das boletas manuscritas com os itens do CRM, peça a peça.

A conferência de caixa compara totais: CRM, dinheiro/PIX do fechamento e cartão
aprovado na Rede. Ela responde se o dia fecha, mas não onde furou.

A boleta é o registro físico do balcão e traz o código de barras de cada peça
vendida. Como esse código é o mesmo `codigo` do CRM sem os zeros à esquerda, dá
para casar item a item e apontar a peça exata que está na boleta e não no
sistema — ou o contrário.

Um cuidado: a leitura das boletas é feita por um modelo de visão e erra dígito.
Um código lido errado é indistinguível de uma venda não registrada, se olhado
sozinho. A saída é comparar com o próprio CRM: quando o código sem par tem um
vizinho a um dígito de distância, com o mesmo valor e no mesmo dia, é erro de
leitura, não venda faltando. O cruzamento separa os dois casos em vez de
despejar tudo como divergência.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import date

from src.models import Boleta, BoletaData, CrmData, CrmItem


STATUS_OK = "OK"
STATUS_DIVERGENT = "DIVERGÊNCIA"
STATUS_REVIEW = "REVISAR"
STATUS_NO_BOLETA = "SEM BOLETA"

KIND_MISSING_IN_CRM = "venda_sem_registro"
KIND_MISSING_IN_BOLETA = "registro_sem_boleta"
KIND_SUSPECT_READ = "leitura_suspeita"


@dataclass(frozen=True, slots=True)
class ItemDiscrepancy:
    date: date
    kind: str
    codigo: str
    value_cents: int
    is_return: bool
    seller: str | None = None
    boleta_id: str | None = None
    boleta_numero: str | None = None
    sale_number: str | None = None
    product: str | None = None
    note: str = ""

    @property
    def label(self) -> str:
        return {
            KIND_MISSING_IN_CRM: "Na boleta, fora do CRM",
            KIND_MISSING_IN_BOLETA: "No CRM, fora da boleta",
            KIND_SUSPECT_READ: "Provável erro de leitura",
        }.get(self.kind, self.kind)


@dataclass(frozen=True, slots=True)
class CrosscheckRow:
    date: date
    boleta_count: int
    boleta_gross_cents: int
    boleta_net_cents: int
    crm_gross_cents: int
    crm_net_cents: int
    matched_items: int
    only_boleta: int
    only_crm: int
    suspect_reads: int
    review_boletas: int
    status: str
    excluded_boletas: int = 0

    @property
    def gross_difference_cents(self) -> int:
        return self.boleta_gross_cents - self.crm_gross_cents


@dataclass(slots=True)
class CrosscheckReport:
    start_date: date
    end_date: date
    rows: list[CrosscheckRow]
    discrepancies: list[ItemDiscrepancy]
    totals: dict[str, int] = field(default_factory=dict)

    @property
    def days_ok(self) -> int:
        return sum(row.status == STATUS_OK for row in self.rows)

    @property
    def days_divergent(self) -> int:
        return sum(row.status == STATUS_DIVERGENT for row in self.rows)

    @property
    def days_review(self) -> int:
        return sum(row.status == STATUS_REVIEW for row in self.rows)

    @property
    def days_without_boleta(self) -> int:
        return sum(row.status == STATUS_NO_BOLETA for row in self.rows)

    def by_kind(self, kind: str) -> list[ItemDiscrepancy]:
        return [item for item in self.discrepancies if item.kind == kind]


def _one_digit_apart(left: str, right: str) -> bool:
    """Os dois códigos diferem por um dígito trocado, sobrando ou faltando.

    Trocado foi o primeiro erro medido (`3698637912` por `...992`). Faltando
    apareceu no lote de setembro/2026: 58 códigos lidos com 9 dígitos, nenhum
    existente no CRM, onde todos têm 10 — o modelo come um dígito da etiqueta.
    """
    if left == right:
        return False
    if len(left) == len(right):
        return sum(1 for a, b in zip(left, right) if a != b) == 1
    if abs(len(left) - len(right)) != 1:
        return False
    shorter, longer = sorted((left, right), key=len)
    return any(longer[:i] + longer[i + 1:] == shorter for i in range(len(longer)))


def _boleta_units(boleta: Boleta) -> list[tuple[str, int, bool]]:
    """Peças da boleta como (código, valor absoluto, é devolução)."""
    units = [(item.codigo, abs(item.value_cents), False) for item in boleta.items]
    units += [(item.codigo, abs(item.value_cents), True) for item in boleta.returns]
    return units


def _crm_units(item: CrmItem) -> list[tuple[str, int, bool]]:
    """Linha do CRM expandida pela quantidade: duas peças iguais, dois pares."""
    unit = (item.codigo, abs(item.gross_cents), item.is_return)
    return [unit] * max(1, item.quantity)


def _match_day(
    day: date,
    boletas: list[Boleta],
    crm_items: list[CrmItem],
) -> tuple[int, list[ItemDiscrepancy]]:
    """Casa as peças do dia e devolve (casadas, discrepâncias)."""
    boleta_origin: dict[tuple[str, int, bool], list[Boleta]] = {}
    boleta_counts: Counter = Counter()
    for boleta in boletas:
        for unit in _boleta_units(boleta):
            boleta_counts[unit] += 1
            boleta_origin.setdefault(unit, []).append(boleta)

    crm_origin: dict[tuple[str, int, bool], list[CrmItem]] = {}
    crm_counts: Counter = Counter()
    for item in crm_items:
        for unit in _crm_units(item):
            crm_counts[unit] += 1
            crm_origin.setdefault(unit, []).append(item)

    matched = sum((boleta_counts & crm_counts).values())
    boleta_left = boleta_counts - crm_counts
    crm_left = crm_counts - boleta_counts

    discrepancies: list[ItemDiscrepancy] = []

    # Antes de acusar venda sem registro, procurar no que sobrou do CRM uma peça
    # de mesmo valor cujo código difere por um dígito: isso é o modelo tendo
    # lido errado, não peça vendida fora do sistema.
    for unit in list(boleta_left):
        codigo, value, is_return = unit
        while boleta_left[unit] > 0:
            twin = next(
                (
                    other
                    for other in crm_left
                    if crm_left[other] > 0
                    and other[1] == value
                    and other[2] == is_return
                    and _one_digit_apart(codigo, other[0])
                ),
                None,
            )
            if twin is None:
                break
            source = boleta_origin.get(unit, [None])[0]
            crm_line = crm_origin.get(twin, [None])[0]
            discrepancies.append(
                ItemDiscrepancy(
                    date=day,
                    kind=KIND_SUSPECT_READ,
                    codigo=codigo,
                    value_cents=value,
                    is_return=is_return,
                    seller=source.seller if source else None,
                    boleta_id=source.image_id if source else None,
                    boleta_numero=source.numero if source else None,
                    sale_number=crm_line.sale_number if crm_line else None,
                    product=crm_line.product if crm_line else None,
                    note=(
                        f"a boleta traz {codigo} e o CRM traz {twin[0]} no mesmo dia, "
                        "mesmo valor e um dígito de diferença"
                    ),
                )
            )
            boleta_left[unit] -= 1
            crm_left[twin] -= 1

    for (codigo, value, is_return), count in boleta_left.items():
        source = boleta_origin.get((codigo, value, is_return), [None])[0]
        for _ in range(count):
            discrepancies.append(
                ItemDiscrepancy(
                    date=day,
                    kind=KIND_MISSING_IN_CRM,
                    codigo=codigo,
                    value_cents=value,
                    is_return=is_return,
                    seller=source.seller if source else None,
                    boleta_id=source.image_id if source else None,
                    boleta_numero=source.numero if source else None,
                    note="peça lançada na boleta sem linha correspondente no CRM",
                )
            )

    for (codigo, value, is_return), count in crm_left.items():
        crm_line = crm_origin.get((codigo, value, is_return), [None])[0]
        for _ in range(count):
            discrepancies.append(
                ItemDiscrepancy(
                    date=day,
                    kind=KIND_MISSING_IN_BOLETA,
                    codigo=codigo,
                    value_cents=value,
                    is_return=is_return,
                    seller=crm_line.seller if crm_line else None,
                    sale_number=crm_line.sale_number if crm_line else None,
                    product=crm_line.product if crm_line else None,
                    note="venda registrada no CRM sem peça correspondente nas boletas",
                )
            )

    return matched, discrepancies


def _status(
    boleta_count: int, only_boleta: int, only_crm: int, suspects: int, uncertain: int
) -> str:
    if boleta_count == 0:
        return STATUS_NO_BOLETA
    # Leitura suspeita não conta: é código a um dígito do CRM, mesmo dia e mesmo
    # valor, que o cruzamento já tratou como a mesma peça. Ela fica listada para
    # conferência, mas não explica nenhuma divergência que sobrou.
    if not (only_boleta or only_crm):
        return STATUS_OK
    # Boleta com dúvida nas próprias peças (Nº PEÇAS que não bate, peça sem
    # valor) ou fora do cruzamento pode estar escondendo peça: a divergência é
    # inconclusiva, pode ser da leitura e não da loja.
    #
    # Qualquer outro motivo de revisão — total manuscrito que não fecha, nome
    # ilegível — não entra: o cruzamento compara as etiquetas impressas. Na
    # primeira versão entrava, junto com as leituras suspeitas, e no lote de
    # setembro/2026 os 22 dias saíram REVISAR, sem um OK ou DIVERGÊNCIA sequer.
    if uncertain:
        return STATUS_REVIEW
    return STATUS_DIVERGENT


def crosscheck(
    start_date: date,
    end_date: date,
    crm: CrmData,
    boletas: BoletaData,
) -> CrosscheckReport:
    """Cruza boletas e CRM peça a peça, dia a dia."""
    if start_date > end_date:
        raise ValueError("Período inválido.")

    by_day: dict[date, list[Boleta]] = {}
    for boleta in boletas.boletas:
        if boleta.date is None or not (start_date <= boleta.date <= end_date):
            continue
        by_day.setdefault(boleta.date, []).append(boleta)

    crm_by_day: dict[date, list[CrmItem]] = {}
    for item in crm.items:
        if start_date <= item.date <= end_date:
            crm_by_day.setdefault(item.date, []).append(item)

    rows: list[CrosscheckRow] = []
    discrepancies: list[ItemDiscrepancy] = []
    for day in sorted(set(by_day) | set(crm_by_day)):
        day_boletas = by_day.get(day, [])
        day_items = crm_by_day.get(day, [])

        # Boleta com data sob suspeita está no balde errado: suas peças não
        # acham par aqui e as peças verdadeiras do dia certo ficam órfãs. Uma
        # data lida errado geraria quatro acusações falsas — inclusive "venda
        # não registrada", que manda auditar a loja por erro nosso. Fora do
        # cruzamento até a data ser corrigida.
        matchable = [boleta for boleta in day_boletas if not boleta.date_suspect]
        excluded = len(day_boletas) - len(matchable)

        matched, day_discrepancies = _match_day(day, matchable, day_items)
        discrepancies.extend(day_discrepancies)

        only_boleta = sum(d.kind == KIND_MISSING_IN_CRM for d in day_discrepancies)
        only_crm = sum(d.kind == KIND_MISSING_IN_BOLETA for d in day_discrepancies)
        suspects = sum(d.kind == KIND_SUSPECT_READ for d in day_discrepancies)
        reviews = sum(boleta.needs_review for boleta in day_boletas)
        # Boleta fora do cruzamento também deixa o dia inconclusivo: as peças
        # dela aparecem como "no CRM, fora da boleta" sem serem.
        uncertain = excluded + sum(boleta.items_uncertain for boleta in matchable)

        rows.append(
            CrosscheckRow(
                date=day,
                boleta_count=len(day_boletas),
                excluded_boletas=excluded,
                boleta_gross_cents=sum(
                    boleta.items_total_cents for boleta in matchable
                ),
                boleta_net_cents=sum(
                    boleta.total_cents or 0 for boleta in matchable
                ),
                crm_gross_cents=sum(
                    item.gross_cents * item.quantity
                    for item in day_items
                    if not item.is_return
                ),
                crm_net_cents=crm.total_on(day),
                matched_items=matched,
                only_boleta=only_boleta,
                only_crm=only_crm,
                suspect_reads=suspects,
                review_boletas=reviews,
                status=_status(
                    len(day_boletas), only_boleta, only_crm, suspects, uncertain
                ),
            )
        )

    # Um dia que só existe porque uma boleta caiu nele com data errada não é um
    # dia de movimento: sem a boleta excluída não sobra nada para conferir. A
    # boleta em si continua visível na fila de revisão.
    excluded_total = sum(row.excluded_boletas for row in rows)
    rows = [
        row
        for row in rows
        if row.boleta_count > row.excluded_boletas or row.crm_gross_cents or row.only_crm
    ]

    totals = {
        "boletas": sum(row.boleta_count for row in rows),
        "excluded_boletas": excluded_total,
        "matched_items": sum(row.matched_items for row in rows),
        "only_boleta": sum(row.only_boleta for row in rows),
        "only_crm": sum(row.only_crm for row in rows),
        "suspect_reads": sum(row.suspect_reads for row in rows),
        "boleta_gross_cents": sum(row.boleta_gross_cents for row in rows),
        "crm_gross_cents": sum(row.crm_gross_cents for row in rows),
    }
    return CrosscheckReport(
        start_date=start_date,
        end_date=end_date,
        rows=rows,
        discrepancies=discrepancies,
        totals=totals,
    )

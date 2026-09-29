"""Cruzamento das boletas com as vendas do CRM: boleta a venda, depois peça a peça.

Como um auditor faz: primeiro acha, para cada boleta, a venda do CRM a que ela
corresponde; só depois confere as peças dentro da venda.

Por que não casar peça a peça direto no dia, como na primeira versão: a leitura
do código de barras pelo modelo erra dígito — no lote de setembro/2026, cerca de
um código em cada cinco —, e cada código errado virava duas divergências, "na
boleta, fora do CRM" e "no CRM, fora da boleta", para uma peça que estava nos
dois. No dia modelo de 01/09 as 17 boletas lidas tinham, todas, venda
correspondente no CRM, e o relatório acusava divergência no dia.

Casando pela venda, a peça é achada pelo valor dentro da venda certa, e o código
lido errado vira observação, não divergência.

O que identifica a venda de uma boleta:

- o número manuscrito no canto superior direito é o final do `nrovenda` do CRM
  (a boleta 28856 traz "056"; a venda é a 17056);
- as peças: mesmo valor, com código igual ou parecido;
- o total: o TOTAL da boleta bate com o líquido da venda.

Divergência de verdade, depois disso, é o que sobra: venda do CRM sem boleta,
boleta sem venda no CRM, e peça que não fecha dentro de uma venda casada.

O TOTAL manuscrito é o árbitro dentro da venda. Se ele fecha com a venda do
CRM, a peça que sobrou é erro de leitura — valor lido errado, troca que o
modelo não transcreveu —, e vira observação. Se não fecha, é divergência, com o
valor em reais.

Boleta que sobra num dia e venda que sobra em outro, próximos, ainda são
tentadas entre si antes de virar divergência: a boleta digitalizada no lote do
dia seguinte cai na data do arquivo.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
import re

from src.formatters import format_brl_currency
from src.models import Boleta, BoletaData, CrmData, CrmItem


STATUS_OK = "OK"
STATUS_DIVERGENT = "DIVERGÊNCIA"
STATUS_REVIEW = "REVISAR"
STATUS_NO_BOLETA = "SEM BOLETA"

KIND_SALE_WITHOUT_BOLETA = "venda_sem_boleta"
KIND_BOLETA_WITHOUT_SALE = "boleta_sem_venda"
KIND_PIECE_ONLY_BOLETA = "peca_so_na_boleta"
KIND_PIECE_ONLY_CRM = "peca_so_no_crm"
KIND_PIECE_VALUE = "valor_diferente"
KIND_CODE_MISREAD = "codigo_lido_diferente"
KIND_READING_ONLY = "diferenca_de_leitura"
KIND_OTHER_DAY = "boleta_de_outro_dia"
KIND_TOTAL_MATCH = "casada_pelo_total"

DIVERGENCE_KINDS = (
    KIND_SALE_WITHOUT_BOLETA,
    KIND_BOLETA_WITHOUT_SALE,
    KIND_PIECE_ONLY_BOLETA,
    KIND_PIECE_ONLY_CRM,
    KIND_PIECE_VALUE,
)
PIECE_KINDS = (KIND_PIECE_ONLY_BOLETA, KIND_PIECE_ONLY_CRM, KIND_PIECE_VALUE)
# Observações: explicadas pela leitura, não entram na diferença nem no status.
NOTE_KINDS = (KIND_CODE_MISREAD, KIND_READING_ONLY, KIND_OTHER_DAY, KIND_TOTAL_MATCH)

# Pontuação de um par boleta x venda. Uma peça com mesmo valor e código igual
# ou parecido é a evidência mais forte; o número de controle ajuda quando o
# modelo o leu inteiro; valor igual com código diferente é fraco sozinho —
# R$ 59,90 é preço de metade da loja.
_SCORE_EXACT = 10
_SCORE_SIMILAR = 7
_SCORE_SAME_VALUE = 3
_SCORE_CODE_ONLY_EXACT = 7
_SCORE_CODE_ONLY_SIMILAR = 4
_SCORE_CONTROL = 8
_SCORE_TOTAL = 5
_MIN_PAIR_SCORE = 7
# Boleta e venda de dias diferentes precisam de duas evidências — controle e
# peça parecida, ou código exato e total: o mesmo produto é vendido em dias
# diferentes.
_MIN_OTHER_DAY_SCORE = 15
_MAX_OTHER_DAY_DISTANCE = 3

# Até quantas edições (dígito trocado, sobrando, faltando ou dois vizinhos
# invertidos) o código lido ainda é "parecido" com o do CRM.
_SIMILAR_CODE_DISTANCE = 2


# --- estruturas -----------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Piece:
    codigo: str
    value_cents: int
    is_return: bool
    product: str | None = None
    # Valor pago, depois do desconto (só nas peças do CRM).
    paid_cents: int | None = None


@dataclass(frozen=True, slots=True)
class Sale:
    date: date
    number: str
    seller: str
    pieces: tuple[Piece, ...]

    @property
    def gross_cents(self) -> int:
        return sum(-p.value_cents if p.is_return else p.value_cents for p in self.pieces)

    @property
    def net_cents(self) -> int:
        """O que o cliente pagou: bruto menos desconto, quando o CRM informa."""
        total = 0
        for p in self.pieces:
            value = p.value_cents if p.paid_cents is None else p.paid_cents
            total += -value if p.is_return else value
        return total


@dataclass(frozen=True, slots=True)
class Discrepancy:
    """Uma divergência ou observação do cruzamento.

    `value_cents` é o valor da peça ou da venda; `impact_cents`, o efeito na
    diferença boletas − CRM (negativo quando falta na boleta o que o CRM tem).
    """

    date: date
    kind: str
    value_cents: int
    detail: str
    codigo: str | None = None
    is_return: bool = False
    seller: str | None = None
    boleta_numero: str | None = None
    boleta_id: str | None = None
    sale_number: str | None = None
    product: str | None = None
    impact_cents: int = 0

    @property
    def label(self) -> str:
        return KIND_LABELS.get(self.kind, self.kind)

    @property
    def is_divergence(self) -> bool:
        return self.kind in DIVERGENCE_KINDS


KIND_LABELS = {
    KIND_SALE_WITHOUT_BOLETA: "Venda no CRM sem boleta",
    KIND_BOLETA_WITHOUT_SALE: "Boleta sem venda no CRM",
    KIND_PIECE_ONLY_BOLETA: "Peça na boleta que não está na venda",
    KIND_PIECE_ONLY_CRM: "Peça da venda que não está na boleta",
    KIND_PIECE_VALUE: "Valor da peça diferente do CRM",
    KIND_CODE_MISREAD: "Código lido diferente do CRM",
    KIND_READING_ONLY: "Diferença de leitura — o total da boleta fecha",
    KIND_OTHER_DAY: "Boleta casada com venda de outro dia",
    KIND_TOTAL_MATCH: "Boleta casada com a venda de mesmo total",
}


@dataclass(frozen=True, slots=True)
class CrosscheckRow:
    date: date
    boleta_count: int
    sale_count: int
    matched_sales: int
    sales_without_boleta: int
    boletas_without_sale: int
    piece_divergences: int
    reading_notes: int
    review_boletas: int
    boleta_value_cents: int
    crm_value_cents: int
    difference_cents: int
    status: str
    excluded_boletas: int = 0


@dataclass(slots=True)
class CrosscheckReport:
    start_date: date
    end_date: date
    rows: list[CrosscheckRow]
    discrepancies: list[Discrepancy]
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

    def by_kind(self, kind: str) -> list[Discrepancy]:
        return [item for item in self.discrepancies if item.kind == kind]


# --- comparação de códigos --------------------------------------------------------


def code_distance(left: str, right: str) -> int:
    """Edições entre dois códigos, contando a troca de dois vizinhos como uma.

    Inversão é erro típico de leitura: a boleta 28851 trazia `2293977971` onde
    o CRM tem `2239377971` — dois vizinhos invertidos e mais um dígito trocado.
    """
    a, b = left or "", right or ""
    rows, cols = len(a) + 1, len(b) + 1
    d = [[0] * cols for _ in range(rows)]
    for i in range(rows):
        d[i][0] = i
    for j in range(cols):
        d[0][j] = j
    for i in range(1, rows):
        for j in range(1, cols):
            cost = 0 if a[i - 1] == b[j - 1] else 1
            d[i][j] = min(d[i - 1][j] + 1, d[i][j - 1] + 1, d[i - 1][j - 1] + cost)
            if i > 1 and j > 1 and a[i - 1] == b[j - 2] and a[i - 2] == b[j - 1]:
                d[i][j] = min(d[i][j], d[i - 2][j - 2] + 1)
    return d[-1][-1]


def _control_matches(control: str | None, sale_number: str) -> bool:
    """O número manuscrito da boleta é o final do `nrovenda`.

    O modelo às vezes come ou acrescenta um dígito ("0552" para "055"), então
    vale o número inteiro, sem zeros à esquerda, ou os três primeiros dígitos.
    Menos de dois dígitos não serve: casaria com qualquer venda.
    """
    digits = re.sub(r"\D", "", control or "")
    if not digits:
        return False
    candidates = {digits, digits.lstrip("0"), digits[:3], digits[:3].lstrip("0")}
    return any(len(c) >= 2 and sale_number.endswith(c) for c in candidates)


# --- peças ----------------------------------------------------------------------


def _boleta_pieces(boleta: Boleta) -> list[Piece]:
    pieces = [Piece(i.codigo, abs(i.value_cents), False) for i in boleta.items]
    pieces += [Piece(i.codigo, abs(i.value_cents), True) for i in boleta.returns]
    return pieces


def _sale_pieces(items: list[CrmItem]) -> tuple[Piece, ...]:
    pieces = []
    for item in items:
        paid = None if item.net_cents is None else abs(item.net_cents)
        piece = Piece(item.codigo, abs(item.gross_cents), item.is_return, item.product, paid)
        pieces.extend([piece] * max(1, item.quantity))
    return tuple(pieces)


@dataclass(slots=True)
class _PieceMatch:
    pairs: list[tuple[Piece, Piece, int]]
    only_boleta: list[Piece]
    only_crm: list[Piece]

    def score(self) -> int:
        total = 0
        for _, _, distance in self.pairs:
            if distance == 0:
                total += _SCORE_EXACT
            elif distance <= _SIMILAR_CODE_DISTANCE:
                total += _SCORE_SIMILAR
            else:
                total += _SCORE_SAME_VALUE
        return total


def _match_pieces(boleta_side: list[Piece], crm_side: list[Piece]) -> _PieceMatch:
    """Casa peças de mesmo valor, do código mais parecido para o menos.

    Venda e devolução podem casar entre si: o modelo às vezes transcreve a peça
    devolvida como vendida (a boleta 28857 trazia como venda a peça que o CRM
    lançou como devolução). Mesmo valor e mesmo lado ganham preferência.
    """
    candidates = []
    for bi, bp in enumerate(boleta_side):
        for ci, cp in enumerate(crm_side):
            if bp.value_cents != cp.value_cents:
                continue
            distance = code_distance(bp.codigo, cp.codigo)
            side_penalty = 0 if bp.is_return == cp.is_return else 1
            candidates.append((distance, side_penalty, bi, ci))
    candidates.sort()
    used_b, used_c = set(), set()
    pairs = []
    for distance, _, bi, ci in candidates:
        if bi in used_b or ci in used_c:
            continue
        used_b.add(bi)
        used_c.add(ci)
        pairs.append((boleta_side[bi], crm_side[ci], distance))
    return _PieceMatch(
        pairs=pairs,
        only_boleta=[p for i, p in enumerate(boleta_side) if i not in used_b],
        only_crm=[p for i, p in enumerate(crm_side) if i not in used_c],
    )


def _pair_score(boleta: Boleta, sale: Sale) -> int:
    match = _match_pieces(_boleta_pieces(boleta), list(sale.pieces))
    score = match.score()
    # Mesmo código com valor diferente — preço lido errado — ainda aponta para a
    # venda, com peso menor.
    for lida, crm_piece in _pair_by_code(match.only_boleta, match.only_crm)[0]:
        distance = code_distance(lida.codigo, crm_piece.codigo) if lida.codigo else 99
        if distance == 0:
            score += _SCORE_CODE_ONLY_EXACT
        elif distance <= _SIMILAR_CODE_DISTANCE:
            score += _SCORE_CODE_ONLY_SIMILAR
    if _control_matches(boleta.numero_controle, sale.number):
        score += _SCORE_CONTROL
    if boleta.total_cents is not None and abs(boleta.total_cents - sale.net_cents) <= 1:
        score += _SCORE_TOTAL
    return score


def _signed(piece: Piece) -> int:
    return -piece.value_cents if piece.is_return else piece.value_cents


def _pieces_text(pieces) -> str:
    return ", ".join(
        f"{p.codigo or 's/ código'} {'-' if p.is_return else ''}R$ {p.value_cents / 100:.2f}".replace(".", ",")
        for p in pieces
    )


# --- dia ------------------------------------------------------------------------


def _group_sales(day: date, items: list[CrmItem]) -> list[Sale]:
    by_number: dict[str, list[CrmItem]] = {}
    for item in items:
        by_number.setdefault(item.sale_number, []).append(item)
    return [
        Sale(day, number, lines[0].seller, _sale_pieces(lines))
        for number, lines in sorted(by_number.items())
    ]


def _match_day(day: date, boletas: list[Boleta], sales: list[Sale]):
    """Casa boletas e vendas do dia; devolve (pares, boletas sobrando, vendas sobrando)."""
    scored = []
    for bi, boleta in enumerate(boletas):
        for si, sale in enumerate(sales):
            score = _pair_score(boleta, sale)
            if score >= _MIN_PAIR_SCORE:
                scored.append((score, bi, si))
    scored.sort(key=lambda t: (-t[0], t[1], t[2]))
    used_b, used_s = set(), set()
    pairs = []
    for _, bi, si in scored:
        if bi in used_b or si in used_s:
            continue
        used_b.add(bi)
        used_s.add(si)
        pairs.append((boletas[bi], sales[si]))
    return (
        pairs,
        [b for i, b in enumerate(boletas) if i not in used_b],
        [s for i, s in enumerate(sales) if i not in used_s],
    )


def _closes(boleta: Boleta, sale: Sale) -> bool:
    """O TOTAL escrito na boleta fecha com o que o cliente pagou no CRM.

    O CRM informa o valor pago por peça, já com desconto (coluna `valor`): a
    venda 17415 soma R$ 207,60, o TOTAL da boleta 29203. Sem essa coluna, o
    SUB TOTAL contra o bruto cobre a venda com desconto e sem devolução.
    """
    if boleta.total_cents is not None and abs(boleta.total_cents - sale.net_cents) <= 1:
        return True
    if boleta.sub_total_cents is None or any(p.is_return for p in sale.pieces):
        return False
    return abs(boleta.sub_total_cents - sale.gross_cents) <= 1


def _self_consistent(boleta: Boleta) -> bool:
    """A boleta fecha consigo mesma: peças − trocas − desconto dá o TOTAL lido.

    Quando não fecha — ou o TOTAL não foi lido —, alguma coisa da boleta foi
    mal lida, e a divergência que ela gerar pode ser só da leitura.
    """
    if boleta.total_cents is None:
        return False
    items = sum(abs(i.value_cents) for i in boleta.items)
    returns = sum(abs(i.value_cents) for i in boleta.returns)
    discount = boleta.discount_cents or 0
    candidates = {items - returns - discount, items - discount, items - returns}
    if boleta.sub_total_cents is not None:
        candidates.add(boleta.sub_total_cents - discount)
        candidates.add(boleta.sub_total_cents - returns - discount)
    return any(abs(value - boleta.total_cents) <= 1 for value in candidates)


def _pair_by_code(only_boleta: list[Piece], only_crm: list[Piece]):
    """Sobras dos dois lados que são a mesma peça com valor diferente.

    Código igual ou parecido é a mesma peça. Sobrando uma peça de cada lado, as
    duas também são: peça sem código na boleta (o "presente" de R$ 19,90
    escrito à mão) contra a do CRM, ou código e valor lidos errados juntos.
    """
    candidates = []
    for bi, bp in enumerate(only_boleta):
        for ci, cp in enumerate(only_crm):
            if not bp.codigo or not cp.codigo:
                continue
            distance = code_distance(bp.codigo, cp.codigo)
            if distance <= _SIMILAR_CODE_DISTANCE:
                side_penalty = 0 if bp.is_return == cp.is_return else 1
                candidates.append((distance, side_penalty, bi, ci))
    candidates.sort()
    used_b, used_c, pairs = set(), set(), []
    for _, _, bi, ci in candidates:
        if bi in used_b or ci in used_c:
            continue
        used_b.add(bi)
        used_c.add(ci)
        pairs.append((only_boleta[bi], only_crm[ci]))
    rest_b = [p for i, p in enumerate(only_boleta) if i not in used_b]
    rest_c = [p for i, p in enumerate(only_crm) if i not in used_c]
    if len(rest_b) == 1 and len(rest_c) == 1 and rest_b[0].is_return == rest_c[0].is_return:
        pairs.append((rest_b[0], rest_c[0]))
        rest_b, rest_c = [], []
    return pairs, rest_b, rest_c


def _money(piece_or_cents) -> str:
    cents = _signed(piece_or_cents) if isinstance(piece_or_cents, Piece) else piece_or_cents
    return format_brl_currency(cents)


def _reconcile_pair(day: date, boleta: Boleta, sale: Sale) -> list[Discrepancy]:
    """Confere as peças de uma boleta dentro da venda casada."""
    match = _match_pieces(_boleta_pieces(boleta), list(sale.pieces))
    found: list[Discrepancy] = []
    common = dict(
        date=day, seller=sale.seller, boleta_numero=boleta.numero,
        boleta_id=boleta.image_id, sale_number=sale.number,
    )
    for lida, crm_piece, distance in match.pairs:
        if distance > 0:
            found.append(Discrepancy(
                kind=KIND_CODE_MISREAD, value_cents=crm_piece.value_cents,
                codigo=lida.codigo, is_return=crm_piece.is_return, product=crm_piece.product,
                detail=f"lido {lida.codigo or '(vazio)'}, no CRM {crm_piece.codigo}", **common,
            ))

    value_pairs, only_boleta, only_crm = _pair_by_code(match.only_boleta, match.only_crm)
    if not (value_pairs or only_boleta or only_crm):
        return found

    # O TOTAL escrito é o árbitro. Fechando com o que o cliente pagou, o que
    # sobrou nas peças é leitura: a boleta 28858 fecha em R$ 5,00 com a troca
    # de R$ 79,90 que o modelo não transcreveu.
    closes = _closes(boleta, sale)
    fecha = "; o total escrito na boleta fecha com a venda"

    for lida, crm_piece in value_pairs:
        if lida.is_return and crm_piece.is_return:
            # Troca: o CRM devolve a peça pelo valor pago na compra original
            # (R$ 96,77); a boleta anota o preço da etiqueta (R$ 99,90).
            found.append(Discrepancy(
                kind=KIND_READING_ONLY, value_cents=_signed(crm_piece), codigo=crm_piece.codigo,
                is_return=True, product=crm_piece.product,
                impact_cents=_signed(lida) - _signed(crm_piece),
                detail=(f"troca {crm_piece.codigo}: na boleta {_money(lida.value_cents)} "
                        f"(etiqueta), no CRM {_money(crm_piece.value_cents)} (valor pago na compra)"),
                **common,
            ))
            continue
        # Peça que a boleta pôs em "itens" e o CRM lançou como devolução: o
        # valor é comparado do mesmo lado.
        lida_cents = -lida.value_cents if crm_piece.is_return else lida.value_cents
        lado = " (na boleta como venda, no CRM como devolução)" if lida.is_return != crm_piece.is_return else ""
        codigo = "" if lida.codigo == crm_piece.codigo else f", código lido {lida.codigo or '(sem código)'}"
        found.append(Discrepancy(
            kind=KIND_READING_ONLY if closes else KIND_PIECE_VALUE,
            value_cents=_signed(crm_piece), codigo=crm_piece.codigo,
            is_return=crm_piece.is_return, product=crm_piece.product,
            impact_cents=lida_cents - _signed(crm_piece),
            detail=(f"na boleta {_money(lida.value_cents)}, no CRM {_money(crm_piece.value_cents)}"
                    f"{codigo}{lado}{fecha if closes else ''}"),
            **common,
        ))
    for piece in only_boleta:
        found.append(Discrepancy(
            kind=KIND_READING_ONLY if closes else KIND_PIECE_ONLY_BOLETA,
            value_cents=_signed(piece), codigo=piece.codigo,
            is_return=piece.is_return, impact_cents=_signed(piece),
            detail=(f"peça lida {piece.codigo or '(sem código)'} {_money(piece)} sem par na venda"
                    f"{fecha if closes else ''}"),
            **common,
        ))
    for piece in only_crm:
        o_que = "devolução do CRM não transcrita" if piece.is_return else "peça do CRM que não está na boleta"
        found.append(Discrepancy(
            kind=KIND_READING_ONLY if closes else KIND_PIECE_ONLY_CRM,
            value_cents=_signed(piece), codigo=piece.codigo,
            is_return=piece.is_return, product=piece.product, impact_cents=-_signed(piece),
            detail=f"{o_que} ({piece.codigo} {_money(piece)}){fecha if closes else ''}",
            **common,
        ))
    return found


@dataclass(slots=True)
class _Day:
    boletas: list[Boleta]
    matchable: list[Boleta]
    sales: list[Sale]
    pairs: list[tuple[Boleta, Sale]]
    loose_boletas: list[Boleta]
    loose_sales: list[Sale]
    other_day_pairs: list[tuple[date, Boleta, Sale]] = field(default_factory=list)
    total_pairs: list[tuple[Boleta, Sale]] = field(default_factory=list)


def _match_by_total(state: _Day) -> None:
    """Boleta e venda que sobraram no mesmo dia com o mesmo TOTAL — e só elas.

    O TOTAL manuscrito é o valor pago; peças e número de controle lidos errados
    juntos deixam a boleta sem outra evidência (a 28974 de 05/09 fecha em
    R$ 84,90 com a venda 17180, a única sobra do dia com esse valor).
    """
    for boleta in list(state.loose_boletas):
        total = boleta.total_cents
        if total is None:
            continue
        sales = [s for s in state.loose_sales if abs(s.net_cents - total) <= 1]
        twins = [b for b in state.loose_boletas
                 if b.total_cents is not None and abs(b.total_cents - total) <= 1]
        if len(sales) == 1 and len(twins) == 1:
            state.loose_boletas.remove(boleta)
            state.loose_sales.remove(sales[0])
            state.total_pairs.append((boleta, sales[0]))


def _match_other_days(days: dict[date, _Day]) -> None:
    """Casa a boleta que sobrou num dia com a venda que sobrou em dia próximo."""
    boletas = [(day, b) for day, state in days.items() for b in state.loose_boletas]
    sales = [(day, s) for day, state in days.items() for s in state.loose_sales]
    scored = []
    for bi, (boleta_day, boleta) in enumerate(boletas):
        for si, (sale_day, sale) in enumerate(sales):
            gap = abs((sale_day - boleta_day).days)
            if gap == 0 or gap > _MAX_OTHER_DAY_DISTANCE:
                continue
            score = _pair_score(boleta, sale)
            if score >= _MIN_OTHER_DAY_SCORE:
                scored.append((-score, gap, bi, si))
    scored.sort()
    used_b, used_s = set(), set()
    for _, _, bi, si in scored:
        if bi in used_b or si in used_s:
            continue
        used_b.add(bi)
        used_s.add(si)
        boleta_day, boleta = boletas[bi]
        sale_day, sale = sales[si]
        days[boleta_day].loose_boletas.remove(boleta)
        days[sale_day].loose_sales.remove(sale)
        days[sale_day].other_day_pairs.append((boleta_day, boleta, sale))


def _status(has_boletas: bool, divergent: list[Discrepancy], reading_doubt) -> str:
    if not has_boletas:
        return STATUS_NO_BOLETA
    if not divergent:
        return STATUS_OK
    # Se toda divergência do dia pode ser da leitura — boleta com dúvida nas
    # próprias peças (Nº PEÇAS que não bate, peça sem valor) ou boleta fora do
    # cruzamento que pode ser a da venda órfã —, o dia é inconclusivo, não
    # divergente.
    if all(reading_doubt(d) for d in divergent):
        return STATUS_REVIEW
    return STATUS_DIVERGENT


def crosscheck(
    start_date: date,
    end_date: date,
    crm: CrmData,
    boletas: BoletaData,
) -> CrosscheckReport:
    """Cruza boletas e vendas do CRM, dia a dia."""
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

    days: dict[date, _Day] = {}
    for day in sorted(set(by_day) | set(crm_by_day)):
        day_boletas = by_day.get(day, [])
        # Boleta com data sob suspeita está no dia errado: casada aqui, geraria
        # divergência falsa neste dia e deixaria a venda verdadeira órfã no outro.
        matchable = [b for b in day_boletas if not b.date_suspect]
        sales = _group_sales(day, crm_by_day.get(day, []))
        pairs, loose_boletas, loose_sales = _match_day(day, matchable, sales)
        days[day] = _Day(day_boletas, matchable, sales, pairs, loose_boletas, loose_sales)
    for state in days.values():
        _match_by_total(state)
    _match_other_days(days)

    # Boleta com dúvida de leitura nas peças ou que não fecha consigo mesma.
    doubtful_ids = {
        b.image_id for b in boletas.boletas if b.items_uncertain or not _self_consistent(b)
    }
    rows: list[CrosscheckRow] = []
    discrepancies: list[Discrepancy] = []
    for day, state in days.items():
        excluded = len(state.boletas) - len(state.matchable)
        day_found: list[Discrepancy] = []
        for boleta, sale in state.pairs:
            day_found.extend(_reconcile_pair(day, boleta, sale))
        for boleta, sale in state.total_pairs:
            day_found.append(Discrepancy(
                date=day, kind=KIND_TOTAL_MATCH, value_cents=sale.net_cents,
                seller=sale.seller, boleta_numero=boleta.numero, boleta_id=boleta.image_id,
                sale_number=sale.number,
                detail=f"peças e controle lidos diferentes; o TOTAL da boleta é o da venda "
                       f"({format_brl_currency(sale.net_cents)}) — confira as etiquetas",
            ))
            day_found.extend(_reconcile_pair(day, boleta, sale))
        for boleta_day, boleta, sale in state.other_day_pairs:
            day_found.append(Discrepancy(
                date=day, kind=KIND_OTHER_DAY, value_cents=sale.net_cents,
                seller=sale.seller, boleta_numero=boleta.numero, boleta_id=boleta.image_id,
                sale_number=sale.number,
                detail=f"a boleta veio no lote de {boleta_day:%d/%m}; a venda é de {day:%d/%m}",
            ))
            day_found.extend(_reconcile_pair(day, boleta, sale))
        for sale in state.loose_sales:
            day_found.append(Discrepancy(
                date=day, kind=KIND_SALE_WITHOUT_BOLETA, value_cents=sale.net_cents,
                impact_cents=-sale.net_cents, seller=sale.seller, sale_number=sale.number,
                product="; ".join(sorted({p.product for p in sale.pieces if p.product})) or None,
                detail=_pieces_text(sale.pieces),
            ))
        for boleta in state.loose_boletas:
            pieces = _boleta_pieces(boleta)
            # O TOTAL é o que o cliente pagou; sem ele, a soma das peças lidas.
            value = boleta.total_cents if boleta.total_cents is not None else sum(_signed(p) for p in pieces)
            day_found.append(Discrepancy(
                date=day, kind=KIND_BOLETA_WITHOUT_SALE, value_cents=value, impact_cents=value,
                seller=boleta.seller, boleta_numero=boleta.numero, boleta_id=boleta.image_id,
                detail=_pieces_text(pieces) or "nenhuma peça lida",
            ))
        discrepancies.extend(day_found)

        divergent = [d for d in day_found if d.is_divergence]
        loose_doubt = excluded > 0 or any(b.image_id in doubtful_ids for b in state.loose_boletas)

        def reading_doubt(d: Discrepancy) -> bool:
            if d.boleta_id is not None:
                return d.boleta_id in doubtful_ids
            return loose_doubt

        rows.append(CrosscheckRow(
            date=day,
            boleta_count=len(state.boletas),
            excluded_boletas=excluded,
            sale_count=len(state.sales),
            matched_sales=len(state.pairs) + len(state.total_pairs) + len(state.other_day_pairs),
            sales_without_boleta=len(state.loose_sales),
            boletas_without_sale=len(state.loose_boletas),
            piece_divergences=sum(d.kind in PIECE_KINDS for d in day_found),
            reading_notes=sum(d.kind in NOTE_KINDS for d in day_found),
            review_boletas=sum(b.needs_review for b in state.boletas),
            boleta_value_cents=sum(
                sum(_signed(p) for p in _boleta_pieces(b)) for b in state.matchable
            ),
            crm_value_cents=sum(s.net_cents for s in state.sales),
            difference_cents=sum(d.impact_cents for d in divergent),
            status=_status(bool(state.boletas or state.other_day_pairs), divergent, reading_doubt),
        ))

    # Dia que só existe porque uma boleta caiu nele com data errada não é dia de
    # movimento; a boleta segue visível na fila de revisão.
    excluded_total = sum(row.excluded_boletas for row in rows)
    rows = [r for r in rows if r.boleta_count > r.excluded_boletas or r.sale_count]

    totals = {
        "boletas": sum(r.boleta_count for r in rows),
        "excluded_boletas": excluded_total,
        "sales": sum(r.sale_count for r in rows),
        "matched_sales": sum(r.matched_sales for r in rows),
        "sales_without_boleta": sum(r.sales_without_boleta for r in rows),
        "boletas_without_sale": sum(r.boletas_without_sale for r in rows),
        "piece_divergences": sum(r.piece_divergences for r in rows),
        "reading_notes": sum(r.reading_notes for r in rows),
        "boleta_value_cents": sum(r.boleta_value_cents for r in rows),
        "crm_value_cents": sum(r.crm_value_cents for r in rows),
        "difference_cents": sum(r.difference_cents for r in rows),
    }
    return CrosscheckReport(
        start_date=start_date,
        end_date=end_date,
        rows=rows,
        discrepancies=discrepancies,
        totals=totals,
    )

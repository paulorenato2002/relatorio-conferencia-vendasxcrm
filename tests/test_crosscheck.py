"""Cruzamento por venda. Os casos vêm do dia modelo de 01/09/2026."""

from datetime import date

import pytest

from src.boletas.schema import build_boleta
from src.crosscheck import (
    KIND_BOLETA_WITHOUT_SALE,
    KIND_CODE_MISREAD,
    KIND_OTHER_DAY,
    KIND_PIECE_ONLY_BOLETA,
    KIND_PIECE_ONLY_CRM,
    KIND_PIECE_VALUE,
    KIND_READING_ONLY,
    KIND_SALE_WITHOUT_BOLETA,
    STATUS_DIVERGENT,
    STATUS_NO_BOLETA,
    STATUS_OK,
    STATUS_REVIEW,
    _control_matches,
    code_distance,
    crosscheck,
)
from src.models import BoletaData, CrmData, CrmItem


DIA = date(2026, 9, 1)
PERIODO = (date(2026, 9, 1), date(2026, 9, 30))


def crm_item(codigo, bruto, *, venda="17056", vendedora="DANIELY", qtd=1, dia=DIA):
    return CrmItem(
        date=dia, seller=vendedora, sale_number=venda, codigo=codigo,
        gross_cents=bruto, quantity=qtd, product="BR 00002 ARGOLA TRIPLA LISA",
    )


def crm_data(*items):
    return CrmData(
        file_name="crm.xlsx", daily_by_seller={DIA: {"DANIELY": 1}},
        sellers=("DANIELY",), all_dates={DIA}, items=items,
    )


def boleta(itens, *, numero="28856", controle=None, total=None, trocas=(), posicao=1,
           num_pecas=None, data="01/09", sub_total=None, desconto=None):
    payload = {
        "numero": numero, "numero_controle": controle, "data": data,
        "vendedora": "Dani", "cliente": "Erica", "telefone": None,
        "itens": [{"codigo": c, "valor": v, "manuscrito": False} for c, v in itens],
        "trocas": [{"codigo": c, "valor": v, "manuscrito": True} for c, v in trocas],
        "sub_total": sub_total, "desconto": desconto, "total": total,
        "num_pecas": num_pecas if num_pecas is not None else len(itens),
        "pagamento": "credito", "parcelas": None, "bandeira": None,
        "brinde": False, "presente": False, "whats": False, "cashback": False,
        "aniver": False, "outros": False, "campos_ilegiveis": [],
    }
    return build_boleta(payload, source_file="carla.pdf", page=1, position=posicao,
                        start=PERIODO[0], end=PERIODO[1])


def cruzar(crm_itens, boletas):
    return crosscheck(*PERIODO, crm_data(*crm_itens),
                      BoletaData(file_names=("carla.pdf",), boletas=tuple(boletas)))


def unica_linha(relatorio):
    assert len(relatorio.rows) == 1
    return relatorio.rows[0]


def linha_do_dia(relatorio, dia):
    return next(r for r in relatorio.rows if r.date == dia)


# --- comparação de códigos e controle -------------------------------------------


@pytest.mark.parametrize("a,b,esperado", [
    ("2149308001", "2149308001", 0),
    ("1970974772", "1570974772", 1),   # 01/09, venda 17046
    ("2149308001", "2149338801", 2),   # 01/09, venda 17056 (o exemplo do relatório)
    ("2239377971", "2293377971", 1),   # dois vizinhos invertidos contam como uma edição
    ("2239377971", "2293977971", 2),   # 01/09, venda 17049: inversão mais um dígito trocado
    ("2707049662", "270704962", 1),    # dígito faltando
])
def test_distancia_entre_codigos(a, b, esperado):
    assert code_distance(a, b) == esperado


@pytest.mark.parametrize("controle,venda,casa", [
    ("056", "17056", True),
    ("046", "17046", True),
    ("0552", "17055", True),    # modelo acrescentou um dígito
    ("0578", "17057", True),
    ("04", "17047", False),     # dígito comido: não dá para afirmar
    ("5", "17055", False),      # um dígito só casaria com qualquer venda
    (None, "17056", False),
])
def test_numero_de_controle_e_o_final_do_nrovenda(controle, venda, casa):
    assert _control_matches(controle, venda) is casa


# --- boleta casa com a venda ------------------------------------------------------


def test_boleta_e_venda_iguais_fecham_o_dia():
    linha = unica_linha(cruzar(
        [crm_item("2149308001", 11990)],
        [boleta([("2149308001", "119.90")], total="119.90")],
    ))
    assert linha.matched_sales == 1
    assert linha.difference_cents == 0
    assert linha.status == STATUS_OK


def test_codigo_lido_com_dois_digitos_errados_nao_e_divergencia():
    # O caso apontado no relatório de setembro: boleta 28856 lida como
    # 2149338801, venda 17056 com 2149308001, R$ 119,90.
    relatorio = cruzar(
        [crm_item("2149308001", 11990)],
        [boleta([("2149338801", "119.90")], controle="056", total="119.90")],
    )
    linha = unica_linha(relatorio)
    assert linha.status == STATUS_OK
    assert linha.difference_cents == 0
    lido = relatorio.by_kind(KIND_CODE_MISREAD)
    assert len(lido) == 1 and "2149338801" in lido[0].detail and "2149308001" in lido[0].detail
    assert not relatorio.by_kind(KIND_PIECE_ONLY_BOLETA)


def test_codigo_com_inversao_e_troca_casa_dentro_da_venda():
    # Venda 17049: 2239377971 lido como 2293977971 — dois vizinhos invertidos e
    # mais um dígito trocado.
    relatorio = cruzar(
        [crm_item("2707378362", 3500, venda="17049"), crm_item("2258486891", 4500, venda="17049"),
         crm_item("2239377971", 4500, venda="17049")],
        [boleta([("2293977971", "45.00"), ("2258486891", "45.00"), ("2707378362", "35.00")],
                numero="28851", total="125.00")],
    )
    assert unica_linha(relatorio).status == STATUS_OK


def test_troca_lida_como_venda_casa_com_a_devolucao_do_crm():
    # Venda 17057: o modelo pôs em "itens" a peça que o CRM lançou como devolução.
    relatorio = cruzar(
        [crm_item("2359928971", 4500, venda="17057"), crm_item("2339524662", -3990, venda="17057")],
        [boleta([("2339524662", "39.90"), ("2359928971", "45.00")], numero="28857", total="5.10")],
    )
    assert unica_linha(relatorio).status == STATUS_OK


def test_troca_nao_transcrita_mas_descontada_no_total():
    # Venda 17058: o CRM tem a devolução de R$ 79,90; a boleta não a lista, mas o
    # TOTAL de R$ 5,00 já a desconta.
    relatorio = cruzar(
        [crm_item("2350154712", 4500, venda="17058"), crm_item("2219769662", 3990, venda="17058"),
         crm_item("5232889401", -7990, venda="17058")],
        [boleta([("2219769662", "39.90"), ("2350154712", "45.00")], numero="28858", total="5.00")],
    )
    assert unica_linha(relatorio).status == STATUS_OK
    # Continua visível, como observação de leitura.
    [nota] = relatorio.by_kind(KIND_READING_ONLY)
    assert "devolução do CRM não transcrita" in nota.detail


def test_devolucao_do_crm_sem_troca_e_sem_desconto_no_total_diverge():
    relatorio = cruzar(
        [crm_item("2350154712", 4500, venda="17058"), crm_item("5232889401", -7990, venda="17058")],
        [boleta([("2350154712", "45.00")], numero="28858", total="45.00")],
    )
    assert len(relatorio.by_kind(KIND_PIECE_ONLY_CRM)) == 1
    assert unica_linha(relatorio).status == STATUS_DIVERGENT


# --- escolher a venda certa ------------------------------------------------------------


def test_numero_de_controle_desempata_vendas_de_mesmo_valor():
    # 28856 (R$ 119,90, código mal lido) poderia casar com 17056 ou 17061, as
    # duas de R$ 119,90; o "056" manuscrito resolve.
    relatorio = cruzar(
        [crm_item("2149308001", 11990, venda="17056"), crm_item("4350069911", 11990, venda="17061")],
        [boleta([("2149338801", "119.90")], controle="056", total="119.90")],
    )
    sobra = relatorio.by_kind(KIND_SALE_WITHOUT_BOLETA)
    assert [d.sale_number for d in sobra] == ["17061"]


def test_codigo_exato_vence_valor_igual():
    relatorio = cruzar(
        [crm_item("1111111111", 5990, venda="17001"), crm_item("2222222222", 5990, venda="17002")],
        [boleta([("2222222222", "59.90")], numero="1"), boleta([("1111111111", "59.90")], numero="2", posicao=2)],
    )
    assert unica_linha(relatorio).status == STATUS_OK


def test_so_o_valor_igual_nao_basta_para_casar_boleta_de_uma_peca():
    # R$ 59,90 é preço de metade da loja: sem código parecido nem controle, não
    # há como afirmar que é a mesma venda.
    relatorio = cruzar(
        [crm_item("1111111111", 5990, venda="17001")],
        [boleta([("9999999999", "59.90")], numero="1")],
    )
    assert len(relatorio.by_kind(KIND_SALE_WITHOUT_BOLETA)) == 1
    assert len(relatorio.by_kind(KIND_BOLETA_WITHOUT_SALE)) == 1


# --- divergências de verdade -------------------------------------------------------------


def test_venda_sem_boleta():
    # A 17061 de 01/09, enquanto a boleta 28861 não era recortada.
    relatorio = cruzar(
        [crm_item("2149308001", 11990, venda="17056"), crm_item("4350069911", 11990, venda="17061")],
        [boleta([("2149308001", "119.90")], controle="056")],
    )
    linha = unica_linha(relatorio)
    assert linha.sales_without_boleta == 1
    assert linha.difference_cents == -11990
    assert linha.status == STATUS_DIVERGENT


def test_boleta_sem_venda():
    relatorio = cruzar(
        [crm_item("2149308001", 11990)],
        [boleta([("2149308001", "119.90")]), boleta([("5555555555", "89.90")], numero="2", posicao=2)],
    )
    linha = unica_linha(relatorio)
    assert linha.boletas_without_sale == 1
    assert linha.difference_cents == 8990


def test_peca_a_mais_na_boleta_dentro_da_venda_casada():
    relatorio = cruzar(
        [crm_item("2149308001", 11990)],
        [boleta([("2149308001", "119.90"), ("7777777777", "29.90")], controle="056")],
    )
    assert len(relatorio.by_kind(KIND_PIECE_ONLY_BOLETA)) == 1
    assert unica_linha(relatorio).difference_cents == 2990


def test_valor_lido_errado_com_total_que_fecha_e_so_leitura():
    # 10/09: 33 boletas casadas com 33 vendas e R$ -1,80 de "diferença" — preço
    # de etiqueta lido errado, com o TOTAL manuscrito certo.
    relatorio = cruzar(
        [crm_item("2239377971", 4500, venda="17060"), crm_item("2258486891", 3990, venda="17060")],
        [boleta([("2239377971", "45.00"), ("2258486891", "38.90")], numero="28860", total="84.90")],
    )
    linha = unica_linha(relatorio)
    assert linha.status == STATUS_OK
    assert linha.difference_cents == 0
    [nota] = relatorio.by_kind(KIND_READING_ONLY)
    assert nota.impact_cents == -100
    assert "R$ 38,90" in nota.detail and "R$ 39,90" in nota.detail


def test_valor_diferente_sem_total_que_feche_e_divergencia_com_valor():
    relatorio = cruzar(
        [crm_item("2239377971", 4500, venda="17060"), crm_item("2258486891", 3990, venda="17060")],
        [boleta([("2239377971", "45.00"), ("2258486891", "38.90")], numero="28860", total="83.90")],
    )
    linha = unica_linha(relatorio)
    assert linha.status == STATUS_DIVERGENT
    assert linha.difference_cents == -100
    assert linha.piece_divergences == 1
    [divergencia] = relatorio.by_kind(KIND_PIECE_VALUE)
    assert divergencia.codigo == "2258486891"


def test_desconto_fecha_pelo_sub_total():
    # Com desconto, o TOTAL fica abaixo do bruto do CRM; o SUB TOTAL é que fecha.
    relatorio = cruzar(
        [crm_item("2149308001", 11990)],
        [boleta([("2149308001", "119.00")], controle="056", sub_total="119.90",
                desconto="10%", total="107.91")],
    )
    assert unica_linha(relatorio).status == STATUS_OK
    assert len(relatorio.by_kind(KIND_READING_ONLY)) == 1


def test_total_com_desconto_fecha_com_o_valor_pago_no_crm():
    # Venda 17415: 20% de desconto; o CRM informa o valor pago por peça e a
    # soma, R$ 87,84, é o TOTAL da boleta. O R$ 39,90 lido era R$ 49,90.
    from dataclasses import replace

    itens = [replace(crm_item("3222234002", 5990, venda="17415"), net_cents=4792),
             replace(crm_item("2706948001", 4990, venda="17415"), net_cents=3992)]
    relatorio = cruzar(itens, [boleta([("3222234002", "59.90"), ("2706948001", "39.90")],
                                      numero="29203", sub_total="99.80", desconto="20%",
                                      total="87.84")])
    assert unica_linha(relatorio).status == STATUS_OK
    assert len(relatorio.by_kind(KIND_READING_ONLY)) == 1


def test_troca_valorizada_pelo_preco_pago_nao_e_divergencia():
    # Venda 17110: o CRM devolve a peça por R$ 96,77, o que foi pago na compra;
    # a boleta anota a etiqueta, R$ 99,90.
    relatorio = cruzar(
        [crm_item("5235339712", 9990, venda="17110"), crm_item("4343459282", -9677, venda="17110")],
        [boleta([("5235339712", "99.90")], trocas=[("4343459282", "99.90")], numero="28910")],
    )
    assert not [d for d in relatorio.discrepancies if d.is_divergence]
    [nota] = relatorio.by_kind(KIND_READING_ONLY)
    assert "valor pago na compra" in nota.detail


def test_boleta_no_lote_do_dia_seguinte_casa_com_a_venda_da_vespera():
    relatorio = cruzar(
        [crm_item("2149308001", 11990)],
        [boleta([("2149338801", "119.90")], controle="056", total="119.90", data="02/09")],
    )
    venda = linha_do_dia(relatorio, DIA)
    lote = linha_do_dia(relatorio, date(2026, 9, 2))
    assert (venda.matched_sales, venda.sales_without_boleta, venda.status) == (1, 0, STATUS_OK)
    assert (lote.boleta_count, lote.boletas_without_sale, lote.status) == (1, 0, STATUS_OK)
    [nota] = relatorio.by_kind(KIND_OTHER_DAY)
    assert nota.sale_number == "17056" and "02/09" in nota.detail


def test_sobras_do_dia_com_o_mesmo_total_sao_a_mesma_venda():
    # 05/09: a boleta 28974 (peças e controle mal lidos) fecha em R$ 84,90, o
    # valor da venda 17180 — a única sobra do dia com esse total.
    from src.crosscheck import KIND_TOTAL_MATCH

    relatorio = cruzar(
        [crm_item("2565389661", 3500, venda="17180"), crm_item("1419168371", 4990, venda="17180")],
        [boleta([("1910163671", "42.90"), ("2002368011", "42.00")], numero="28974", total="84.90")],
    )
    linha = unica_linha(relatorio)
    assert (linha.matched_sales, linha.status, linha.difference_cents) == (1, STATUS_OK, 0)
    assert len(relatorio.by_kind(KIND_TOTAL_MATCH)) == 1


def test_mesmo_total_em_duas_sobras_nao_casa_nenhuma():
    relatorio = cruzar(
        [crm_item("1111111111", 8490, venda="17180"), crm_item("2222222222", 8490, venda="17181")],
        [boleta([("9999999999", "42.90"), ("8888888888", "42.00")], numero="1", total="84.90")],
    )
    assert relatorio.totals["matched_sales"] == 0


def test_outro_dia_exige_mais_que_valor_igual():
    relatorio = cruzar(
        [crm_item("1111111111", 5990, venda="17001")],
        [boleta([("9999999999", "59.90")], numero="1", data="02/09")],
    )
    assert relatorio.by_kind(KIND_OTHER_DAY) == []
    assert relatorio.totals["sales_without_boleta"] == 1
    assert relatorio.totals["boletas_without_sale"] == 1


def test_quantidade_maior_que_um_vira_varias_pecas():
    relatorio = cruzar(
        [crm_item("2707569661", 5990, qtd=2)],
        [boleta([("2707569661", "59.90"), ("2707569661", "59.90")])],
    )
    assert unica_linha(relatorio).status == STATUS_OK


# --- status ----------------------------------------------------------------------


def test_dia_sem_boleta():
    relatorio = cruzar([crm_item("2149308001", 11990)], [])
    assert unica_linha(relatorio).status == STATUS_NO_BOLETA


def test_duvida_nas_pecas_deixa_a_divergencia_inconclusiva():
    # A boleta diz 2 peças e só uma foi lida: a peça que falta na venda pode ser
    # a que o modelo não leu.
    relatorio = cruzar(
        [crm_item("2149308001", 11990), crm_item("4350069911", 11990)],
        [boleta([("2149308001", "119.90")], controle="056", num_pecas=2)],
    )
    assert len(relatorio.by_kind(KIND_PIECE_ONLY_CRM)) == 1
    assert unica_linha(relatorio).status == STATUS_REVIEW


def test_boleta_duvidosa_que_sobrou_pode_ser_a_da_venda_orfa():
    relatorio = cruzar(
        [crm_item("1111111111", 5990, venda="17001")],
        [boleta([("9999999999", "59.90")], numero="1", num_pecas=2)],
    )
    assert unica_linha(relatorio).status == STATUS_REVIEW


def test_duvida_em_outra_boleta_nao_esconde_venda_sem_boleta():
    # A boleta duvidosa casou com a venda dela; a 17061 continua sem boleta.
    relatorio = cruzar(
        [crm_item("2149308001", 11990), crm_item("4350069911", 11990, venda="17061")],
        [boleta([("2149308001", "119.90")], controle="056", total="119.90", num_pecas=2)],
    )
    assert unica_linha(relatorio).status == STATUS_DIVERGENT


def test_boleta_com_data_suspeita_fica_fora():
    from dataclasses import replace

    suspeita = replace(boleta([("2149308001", "119.90")]), date_suspect=True)
    relatorio = cruzar([crm_item("2149308001", 11990)], [suspeita])
    assert relatorio.totals["excluded_boletas"] == 1
    assert relatorio.by_kind(KIND_BOLETA_WITHOUT_SALE) == []


def test_totais_do_periodo():
    relatorio = cruzar(
        [crm_item("2149308001", 11990), crm_item("4350069911", 11990, venda="17061")],
        [boleta([("2149308001", "119.90")], controle="056")],
    )
    t = relatorio.totals
    assert (t["sales"], t["matched_sales"], t["sales_without_boleta"]) == (2, 1, 1)
    assert t["crm_value_cents"] == 23980
    assert t["boleta_value_cents"] == 11990
    assert t["difference_cents"] == -11990


def test_periodo_invalido():
    with pytest.raises(ValueError):
        crosscheck(date(2026, 9, 30), date(2026, 9, 1), crm_data(),
                   BoletaData(file_names=(), boletas=()))

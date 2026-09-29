from datetime import date

import pytest

from src.boletas.schema import build_boleta
from src.crosscheck import (
    KIND_MISSING_IN_BOLETA,
    KIND_MISSING_IN_CRM,
    KIND_SUSPECT_READ,
    STATUS_DIVERGENT,
    STATUS_NO_BOLETA,
    STATUS_OK,
    STATUS_REVIEW,
    crosscheck,
)
from src.models import BoletaData, CrmData, CrmItem


DIA = date(2026, 9, 22)
PERIODO = (date(2026, 9, 1), date(2026, 9, 30))


def crm_item(codigo, bruto, *, venda="15143", vendedora="CARLA", qtd=1, dia=DIA):
    return CrmItem(
        date=dia, seller=vendedora, sale_number=venda, codigo=codigo,
        gross_cents=bruto, quantity=qtd, product="BR 00616 BASE ONDULADA",
    )


def crm_data(*items, dia=DIA, liquido=11980):
    return CrmData(
        file_name="crm.xlsx", daily_by_seller={dia: {"CARLA": liquido}},
        sellers=("CARLA",), all_dates={dia}, items=items,
    )


def boleta(*, posicao=1, **overrides):
    payload = {
        "numero": "3650", "numero_controle": None, "data": "22/09",
        "vendedora": "Carla", "cliente": "Cida", "telefone": None,
        "itens": [
            {"codigo": "2707569661", "valor": "59.90", "manuscrito": False},
            {"codigo": "3698637992", "valor": "59.90", "manuscrito": False},
        ],
        "trocas": [], "sub_total": None, "desconto": None, "total": "119.80",
        "num_pecas": 2, "pagamento": "credito", "parcelas": None,
        "bandeira": "Mastercard", "brinde": False, "presente": False,
        "whats": False, "cashback": False, "aniver": False, "outros": False,
        "campos_ilegiveis": [],
    }
    payload.update(overrides)
    return build_boleta(
        payload, source_file="carla.pdf", page=1, position=posicao,
        start=PERIODO[0], end=PERIODO[1],
    )


def lote(*boletas):
    return BoletaData(file_names=("carla.pdf",), boletas=boletas)


# --- casamento ---------------------------------------------------------------


def test_dia_em_que_boleta_e_crm_coincidem_fica_ok():
    relatorio = crosscheck(*PERIODO, crm_data(crm_item("2707569661", 5990),
                                              crm_item("3698637992", 5990)), lote(boleta()))
    linha = relatorio.rows[0]
    assert linha.status == STATUS_OK
    assert linha.matched_items == 2
    assert relatorio.discrepancies == []


def test_peca_na_boleta_e_fora_do_crm_e_venda_sem_registro():
    relatorio = crosscheck(
        *PERIODO,
        crm_data(crm_item("2707569661", 5990)),
        lote(boleta(itens=[
            {"codigo": "2707569661", "valor": "59.90", "manuscrito": False},
            {"codigo": "9999999999", "valor": "89.90", "manuscrito": False},
        ], total="149.80")),
    )
    faltando = relatorio.by_kind(KIND_MISSING_IN_CRM)
    assert len(faltando) == 1
    assert faltando[0].codigo == "9999999999"
    assert faltando[0].value_cents == 8990
    assert relatorio.rows[0].status == STATUS_DIVERGENT


def test_peca_no_crm_e_fora_da_boleta_e_boleta_faltando():
    relatorio = crosscheck(
        *PERIODO,
        crm_data(crm_item("2707569661", 5990), crm_item("3698637992", 5990),
                 crm_item("1965798361", 7990, venda="15200")),
        lote(boleta()),
    )
    sobra = relatorio.by_kind(KIND_MISSING_IN_BOLETA)
    assert len(sobra) == 1
    assert sobra[0].codigo == "1965798361"
    assert sobra[0].sale_number == "15200"


def test_quantidade_maior_que_um_vira_varias_pecas():
    # Duas peças iguais na mesma linha do CRM, dois adesivos na boleta.
    relatorio = crosscheck(
        *PERIODO,
        crm_data(crm_item("2707569661", 5990, qtd=2)),
        lote(boleta(itens=[
            {"codigo": "2707569661", "valor": "59.90", "manuscrito": False},
            {"codigo": "2707569661", "valor": "59.90", "manuscrito": False},
        ])),
    )
    assert relatorio.rows[0].matched_items == 2
    assert relatorio.discrepancies == []


# --- erro de leitura x venda faltando ---------------------------------------


def test_codigo_a_um_digito_do_crm_e_erro_de_leitura_nao_venda_faltando():
    # Caso medido de verdade: o modelo leu 3698637912 onde a etiqueta diz
    # ...992, porque o loop do 9 saiu quebrado na impressão. Sozinha, essa
    # peça pareceria venda fora do sistema.
    relatorio = crosscheck(
        *PERIODO,
        crm_data(crm_item("2707569661", 5990), crm_item("3698637992", 5990)),
        lote(boleta(itens=[
            {"codigo": "2707569661", "valor": "59.90", "manuscrito": False},
            {"codigo": "3698637912", "valor": "59.90", "manuscrito": False},
        ])),
    )
    assert relatorio.by_kind(KIND_MISSING_IN_CRM) == []
    assert relatorio.by_kind(KIND_MISSING_IN_BOLETA) == []
    suspeitas = relatorio.by_kind(KIND_SUSPECT_READ)
    assert len(suspeitas) == 1
    assert "3698637912" in suspeitas[0].note and "3698637992" in suspeitas[0].note
    # Resolvida como a mesma peça: não sobra divergência para o dia.
    assert relatorio.rows[0].status == STATUS_OK
    assert relatorio.rows[0].suspect_reads == 1


def test_leitura_suspeita_nao_mascara_divergencia_real_no_mesmo_dia():
    relatorio = crosscheck(
        *PERIODO,
        crm_data(crm_item("2707569661", 5990), crm_item("3698637992", 5990)),
        lote(boleta(
            itens=[
                {"codigo": "2707569661", "valor": "59.90", "manuscrito": False},
                {"codigo": "3698637912", "valor": "59.90", "manuscrito": False},
                {"codigo": "9999999999", "valor": "89.90", "manuscrito": False},
            ],
            total="209.70", num_pecas=3,
        )),
    )
    assert len(relatorio.by_kind(KIND_SUSPECT_READ)) == 1
    assert len(relatorio.by_kind(KIND_MISSING_IN_CRM)) == 1
    assert relatorio.rows[0].status == STATUS_DIVERGENT


def test_codigo_parecido_mas_de_valor_diferente_nao_e_tratado_como_erro():
    # Mesmo a um dígito, valores diferentes são peças diferentes.
    relatorio = crosscheck(
        *PERIODO,
        crm_data(crm_item("2707569661", 5990), crm_item("3698637992", 7990)),
        lote(boleta(itens=[
            {"codigo": "2707569661", "valor": "59.90", "manuscrito": False},
            {"codigo": "3698637912", "valor": "59.90", "manuscrito": False},
        ])),
    )
    assert relatorio.by_kind(KIND_SUSPECT_READ) == []
    assert len(relatorio.by_kind(KIND_MISSING_IN_CRM)) == 1
    assert len(relatorio.by_kind(KIND_MISSING_IN_BOLETA)) == 1


def test_codigo_com_dois_digitos_de_diferenca_nao_e_erro_de_leitura():
    relatorio = crosscheck(
        *PERIODO,
        crm_data(crm_item("2707569661", 5990), crm_item("3698637992", 5990)),
        lote(boleta(itens=[
            {"codigo": "2707569661", "valor": "59.90", "manuscrito": False},
            {"codigo": "3698637911", "valor": "59.90", "manuscrito": False},
        ])),
    )
    assert relatorio.by_kind(KIND_SUSPECT_READ) == []
    assert len(relatorio.by_kind(KIND_MISSING_IN_CRM)) == 1


# --- trocas ------------------------------------------------------------------


def test_troca_da_boleta_casa_com_a_linha_negativa_do_crm():
    # O CRM lança devolução com valor bruto negativo; a boleta marca a peça
    # com (E). Sem casar os dois, toda troca viraria divergência.
    relatorio = crosscheck(
        *PERIODO,
        crm_data(crm_item("2707569661", 5990), crm_item("3698637992", 5990),
                 crm_item("1663890584", -15990)),
        lote(boleta(
            trocas=[{"codigo": "1663890584", "valor": "159.90", "manuscrito": True}],
            sub_total="119.80", desconto="159.90", total="-40.10",
        )),
    )
    assert relatorio.rows[0].matched_items == 3
    assert relatorio.discrepancies == []


def test_devolucao_no_crm_nao_casa_com_venda_na_boleta():
    # Mesmo código e mesmo valor, mas um é venda e o outro devolução.
    relatorio = crosscheck(
        *PERIODO,
        crm_data(crm_item("2707569661", 5990), crm_item("3698637992", -5990)),
        lote(boleta()),
    )
    assert len(relatorio.by_kind(KIND_MISSING_IN_CRM)) == 1
    assert len(relatorio.by_kind(KIND_MISSING_IN_BOLETA)) == 1


# --- status ------------------------------------------------------------------


def test_dia_sem_boleta_nao_e_divergencia():
    # Operador pode não ter enviado as boletas do dia; isso não é achado.
    relatorio = crosscheck(*PERIODO, crm_data(crm_item("2707569661", 5990)),
                           BoletaData(file_names=(), boletas=()))
    assert relatorio.rows[0].status == STATUS_NO_BOLETA
    assert relatorio.days_divergent == 0


def test_duvida_nas_pecas_impede_veredito_de_divergencia():
    # Nº PEÇAS diferente da quantidade lida: pode faltar etiqueta lida, e a
    # divergência pode ser nossa e não da loja.
    relatorio = crosscheck(
        *PERIODO,
        crm_data(crm_item("2707569661", 5990)),
        lote(boleta(num_pecas=3)),
    )
    assert relatorio.rows[0].status == STATUS_REVIEW


def test_total_manuscrito_errado_nao_mascara_peca_divergente():
    # O cruzamento compara etiquetas impressas. Um total que não fecha é
    # problema da boleta, não das peças: a peça fora do CRM continua sendo
    # divergência. Na primeira versão isto virava REVISAR, e no lote de
    # setembro/2026 os 22 dias saíam REVISAR.
    relatorio = crosscheck(
        *PERIODO,
        crm_data(crm_item("2707569661", 5990)),
        lote(boleta(total="200.00")),
    )
    assert relatorio.rows[0].review_boletas == 1
    assert relatorio.rows[0].status == STATUS_DIVERGENT


def test_boleta_fora_do_periodo_e_ignorada():
    fora = boleta(data="15/08", posicao=2)
    relatorio = crosscheck(
        *PERIODO,
        crm_data(crm_item("2707569661", 5990), crm_item("3698637992", 5990)),
        lote(boleta(), fora),
    )
    assert [row.date for row in relatorio.rows] == [DIA]
    assert relatorio.rows[0].boleta_count == 1


def test_totais_do_periodo():
    relatorio = crosscheck(
        *PERIODO,
        crm_data(crm_item("2707569661", 5990), crm_item("3698637992", 5990)),
        lote(boleta()),
    )
    assert relatorio.totals["boletas"] == 1
    assert relatorio.totals["matched_items"] == 2
    assert relatorio.totals["boleta_gross_cents"] == 11980
    assert relatorio.totals["crm_gross_cents"] == 11980
    assert relatorio.rows[0].gross_difference_cents == 0


def test_periodo_invalido():
    with pytest.raises(ValueError):
        crosscheck(date(2026, 9, 30), date(2026, 9, 1), crm_data(), lote())


# --- boleta com data suspeita fica fora do cruzamento ------------------------


def _com_data_suspeita(**overrides):
    from dataclasses import replace
    return replace(boleta(**overrides), date_suspect=True)


def test_boleta_com_data_suspeita_nao_gera_venda_sem_registro():
    # Medido de verdade: o modelo leu 27/09 numa boleta de 22/09. Cruzada nesse
    # dia, ela acusaria duas "vendas sem registro" — mandando auditar a loja por
    # erro de leitura nosso.
    relatorio = crosscheck(
        *PERIODO,
        crm_data(crm_item("2707569661", 5990), crm_item("3698637992", 5990)),
        lote(_com_data_suspeita(data="27/09")),
    )
    assert relatorio.by_kind(KIND_MISSING_IN_CRM) == []
    assert relatorio.totals["excluded_boletas"] == 1


def test_dia_que_so_existia_pela_data_errada_some_do_relatorio():
    relatorio = crosscheck(
        *PERIODO,
        crm_data(crm_item("2707569661", 5990), crm_item("3698637992", 5990)),
        lote(boleta(), _com_data_suspeita(data="27/09", posicao=2)),
    )
    assert [row.date for row in relatorio.rows] == [DIA]


def test_bruto_da_boleta_excluida_nao_entra_no_total_do_dia():
    # Somar uma boleta que não foi cruzada inflaria o bruto e faria a diferença
    # contra o CRM parecer menor do que é.
    relatorio = crosscheck(
        *PERIODO,
        crm_data(crm_item("2707569661", 5990), crm_item("3698637992", 5990),
                 crm_item("1965798361", 7990)),
        lote(boleta(), _com_data_suspeita(posicao=2)),
    )
    linha = relatorio.rows[0]
    assert linha.boleta_count == 2
    assert linha.excluded_boletas == 1
    assert linha.boleta_gross_cents == 11980


def test_boleta_em_revisao_por_outro_motivo_continua_sendo_cruzada():
    # Só a data desloca o balde. Total divergente não impede o casamento das
    # peças; com todas casadas, o dia fecha — o total fica na fila de revisão
    # das boletas, antes do processamento.
    relatorio = crosscheck(
        *PERIODO,
        crm_data(crm_item("2707569661", 5990), crm_item("3698637992", 5990)),
        lote(boleta(total="200.00")),
    )
    assert relatorio.rows[0].matched_items == 2
    assert relatorio.rows[0].excluded_boletas == 0
    assert relatorio.rows[0].status == STATUS_OK


def test_codigo_com_um_digito_a_menos_e_erro_de_leitura():
    # Lote de setembro/2026: 58 códigos lidos com 9 dígitos, nenhum no CRM,
    # onde todos têm 10. O modelo come um dígito da etiqueta.
    relatorio = crosscheck(
        *PERIODO,
        crm_data(crm_item("2707569661", 5990), crm_item("3698637992", 5990)),
        lote(boleta(itens=[
            {"codigo": "2707569661", "valor": "59.90", "manuscrito": False},
            {"codigo": "369863792", "valor": "59.90", "manuscrito": False},
        ])),
    )
    assert relatorio.by_kind(KIND_MISSING_IN_CRM) == []
    assert len(relatorio.by_kind(KIND_SUSPECT_READ)) == 1


def test_codigo_com_dois_digitos_a_menos_nao_e_tratado_como_erro_de_leitura():
    relatorio = crosscheck(
        *PERIODO,
        crm_data(crm_item("2707569661", 5990), crm_item("3698637992", 5990)),
        lote(boleta(itens=[
            {"codigo": "2707569661", "valor": "59.90", "manuscrito": False},
            {"codigo": "36986379", "valor": "59.90", "manuscrito": False},
        ])),
    )
    assert relatorio.by_kind(KIND_SUSPECT_READ) == []

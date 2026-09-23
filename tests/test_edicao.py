"""Correção manual das boletas: ida e volta entre tabela e transcrição.

O que importa aqui não é só o dado voltar igual, é a correção ser reconferida.
Um total digitado à mão tem que passar pela mesma checagem aritmética que o
total lido pelo modelo; uma data corrigida tem que voltar a ser comparada com
as boletas vizinhas. Caso contrário a tela vira um jeito de silenciar alerta.
"""

from datetime import date

import pytest

from src.boletas import build_boletas
from src.boletas.client import RawBoleta
from src.boletas.edicao import frames_from_raw, raw_from_frames, signature


PERIODO = (date(2026, 9, 1), date(2026, 9, 30))


def payload(**overrides) -> dict:
    base = {
        "numero": "3650", "numero_controle": "3850", "data": "22/09",
        "vendedora": "Carla", "cliente": "Cida", "telefone": "991745112",
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
    base.update(overrides)
    return base


def crua(posicao=1, **overrides) -> RawBoleta:
    return RawBoleta("carla.pdf", 1, posicao, payload(**overrides))


def editar(raw, boleta_index=0, **campos):
    """Aplica mudanças na tabela, como faria a tela."""
    cabecalhos = frames_from_raw(raw)
    for coluna, valor in campos.items():
        cabecalhos.loc[boleta_index, coluna] = valor
    return raw_from_frames(raw, cabecalhos)


# --- ida e volta -------------------------------------------------------------


def test_sem_edicao_a_transcricao_volta_igual():
    raw = [crua(), crua(2, numero="3652")]
    devolvido = raw_from_frames(raw, frames_from_raw(raw))
    assert [r.payload["numero"] for r in devolvido] == ["3650", "3652"]
    assert [r.payload["itens"] for r in devolvido] == [r.payload["itens"] for r in raw]
    assert signature(devolvido) == signature(raw)


def test_campos_que_a_tela_nao_mostra_sao_preservados():
    # `numero_controle`, `telefone` e as marcações não vão para a tabela; não
    # podem sumir por não estarem lá.
    raw = [crua(cashback=True)]
    devolvido = raw_from_frames(raw, frames_from_raw(raw))[0].payload
    assert devolvido["numero_controle"] == "3850"
    assert devolvido["telefone"] == "991745112"
    assert devolvido["cashback"] is True


def test_tabela_tem_uma_linha_por_boleta():
    raw = [crua(), crua(2)]
    assert len(frames_from_raw(raw)) == 2


def test_a_tabela_nao_expoe_codigo_de_barras():
    # Código e valor de etiqueta são impressos; ninguém reteclaria dez dígitos,
    # e o erro de um dígito o cruzamento com o CRM já identifica sozinho.
    colunas = set(frames_from_raw([crua()]).columns)
    assert "Código" not in colunas and "Valor" not in colunas


def test_pecas_e_trocas_atravessam_intactas():
    raw = [crua(trocas=[{"codigo": "1663890584", "valor": "159.90", "manuscrito": True}])]
    devolvido = raw_from_frames(raw, frames_from_raw(raw))[0].payload
    assert devolvido["itens"] == raw[0].payload["itens"]
    assert devolvido["trocas"] == raw[0].payload["trocas"]


def test_corrigir_o_cabecalho_nao_mexe_nas_pecas():
    raw = [crua()]
    devolvido = editar(raw, Data="23/09", Total="99.90")[0].payload
    assert devolvido["itens"] == raw[0].payload["itens"]


# --- a correção é reconferida ------------------------------------------------


def test_corrigir_o_total_faz_o_alerta_aritmetico_sumir():
    # Caso medido: o modelo leu 79,70 onde estava 79,90.
    raw = [crua(itens=[{"codigo": "1965798361", "valor": "79.90", "manuscrito": False}],
                total="79.70", num_pecas=1)]
    antes = build_boletas(raw, *PERIODO).boletas[0]
    assert any("soma dos itens" in c for c in antes.checks)

    depois = build_boletas(editar(raw, Total="79.90"), *PERIODO).boletas[0]
    assert depois.checks == ()
    assert depois.total_cents == 7990


def test_corrigir_o_total_para_outro_valor_errado_mantem_o_alerta():
    # A tela não é um jeito de calar o alerta: valor errado continua errado.
    raw = [crua(itens=[{"codigo": "1965798361", "valor": "79.90", "manuscrito": False}],
                total="79.70", num_pecas=1)]
    depois = build_boletas(editar(raw, Total="50.00"), *PERIODO).boletas[0]
    assert any("soma dos itens" in c for c in depois.checks)


def test_corrigir_a_data_devolve_a_boleta_ao_dia_certo():
    # Caso medido: o modelo leu 27/09 numa boleta de 22/09.
    raw = [crua(1, data="27/09"), crua(2), crua(3), crua(4), crua(5)]
    antes = build_boletas(raw, *PERIODO).boletas
    fora = [b for b in antes if b.date == date(2026, 9, 27)][0]
    assert fora.date_suspect is True

    corrigido = build_boletas(editar(raw, Data="22/09"), *PERIODO).boletas
    assert all(b.date == date(2026, 9, 22) for b in corrigido)
    assert all(b.date_suspect is False for b in corrigido)
    assert all(b.checks == () for b in corrigido)


def test_corrigir_a_data_para_outro_dia_solitario_mantem_a_suspeita():
    raw = [crua(1, data="27/09"), crua(2), crua(3), crua(4), crua(5)]
    corrigido = build_boletas(editar(raw, Data="15/09"), *PERIODO).boletas
    suspeitas = [b for b in corrigido if b.date_suspect]
    assert len(suspeitas) == 1
    assert suspeitas[0].date == date(2026, 9, 15)


def test_corrigir_numero_de_pecas_refaz_a_contagem():
    raw = [crua(num_pecas=5)]
    assert any("Nº PEÇAS" in c for c in build_boletas(raw, *PERIODO).boletas[0].checks)
    assert build_boletas(editar(raw, **{"Peças": 2}), *PERIODO).boletas[0].checks == ()


def test_apagar_o_total_reabre_o_alerta_de_total_nao_lido():
    raw = [crua()]
    depois = build_boletas(editar(raw, Total=""), *PERIODO).boletas[0]
    assert any("TOTAL não foi lido" in c for c in depois.checks)


# --- assinatura da aprovação -------------------------------------------------


def test_assinatura_muda_quando_qualquer_campo_muda():
    raw = [crua()]
    assert signature(raw) == signature(raw_from_frames(raw, frames_from_raw(raw)))
    assert signature(editar(raw, Total="99.90")) != signature(raw)
    assert signature(editar(raw, Cliente="Maria")) != signature(raw)


def test_assinatura_nao_depende_da_ordem_das_chaves():
    a = [RawBoleta("c.pdf", 1, 1, {"total": "10.00", "numero": "1"})]
    b = [RawBoleta("c.pdf", 1, 1, {"numero": "1", "total": "10.00"})]
    assert signature(a) == signature(b)


@pytest.mark.parametrize("campo,valor", [("Data", "23/09"), ("Vendedora", "Ester"),
                                         ("Pagamento", "pix"), ("Sub total", "120.00")])
def test_aprovacao_cai_depois_de_qualquer_correcao(campo, valor):
    raw = [crua()]
    aprovado = signature(raw)
    assert signature(editar(raw, **{campo: valor})) != aprovado

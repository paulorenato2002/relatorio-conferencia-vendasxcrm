from datetime import date
from pathlib import Path

import pytest

from src.boletas.client import RawBoleta, _extract, build_boletas
from src.boletas.render import BoletaRenderError, render_boletas
from src.boletas.schema import (
    BoletaSchemaError,
    barcode_to_crm_code,
    build_boleta,
    normalize_barcode,
    normalize_person,
    resolve_boleta_date,
)


PERIODO = (date(2026, 9, 1), date(2026, 9, 30))

BOLETAS_DIR = Path(__file__).resolve().parents[2] / "boletas"


def _scan(filename: str) -> Path:
    path = BOLETAS_DIR / filename
    if not path.exists():
        pytest.skip(f"Amostra local não versionada: {filename}")
    return path


def _boleta(**overrides) -> dict:
    """Boleta 3652 (Rebeca): três etiquetas, crédito 2x, só o TOTAL preenchido."""
    payload = {
        "numero": "3652",
        "numero_controle": "3853",
        "data": "22/09",
        "vendedora": "Carla",
        "cliente": "Rebeca",
        "telefone": "99284-0460",
        "itens": [
            {"codigo": "2520441552", "valor": "49.90", "manuscrito": False},
            {"codigo": "2344734722", "valor": "49.90", "manuscrito": False},
            {"codigo": "2256349712", "valor": "29.90", "manuscrito": False},
        ],
        "trocas": [],
        "sub_total": None,
        "desconto": None,
        "total": "129.70",
        "num_pecas": 3,
        "pagamento": "credito",
        "parcelas": 2,
        "bandeira": "Mastercard",
        "brinde": False,
        "presente": False,
        "whats": False,
        "cashback": False,
        "aniver": False,
        "outros": False,
        "campos_ilegiveis": [],
    }
    payload.update(overrides)
    return payload


def _build(payload: dict):
    return build_boleta(
        payload, source_file="carla.pdf", page=1, position=2,
        start=PERIODO[0], end=PERIODO[1],
    )


# --- normalização -----------------------------------------------------------


def test_codigo_da_etiqueta_vira_o_codigo_do_crm():
    # A etiqueta imprime 10 dígitos; o CRM guarda 13 com zeros à esquerda.
    assert normalize_barcode("2700149661") == "2700149661"
    assert barcode_to_crm_code("2700149661") == "0002700149661"
    assert normalize_barcode("0002219376911") == "2219376911"
    assert barcode_to_crm_code(normalize_barcode("0002219376911")) == "0002219376911"


def test_vendedora_normaliza_para_o_formato_do_crm():
    assert normalize_person("Carla") == "CARLA"
    assert normalize_person(" Mônica ") == "MONICA"
    assert normalize_person("") is None


# --- data sem ano -----------------------------------------------------------


def test_data_sem_ano_usa_o_periodo_da_tela():
    assert resolve_boleta_date("22/09", *PERIODO) == date(2026, 9, 22)
    assert resolve_boleta_date("22/9", *PERIODO) == date(2026, 9, 22)


def test_data_sem_ano_em_periodo_que_cruza_o_ano():
    inicio, fim = date(2026, 12, 20), date(2027, 1, 10)
    assert resolve_boleta_date("05/01", inicio, fim) == date(2027, 1, 5)
    assert resolve_boleta_date("28/12", inicio, fim) == date(2026, 12, 28)


def test_data_com_ano_explicito_e_respeitada():
    assert resolve_boleta_date("22/09/2025", *PERIODO) == date(2025, 9, 22)
    assert resolve_boleta_date("2026-09-22", *PERIODO) == date(2026, 9, 22)


def test_data_ilegivel_falha():
    with pytest.raises(BoletaSchemaError):
        resolve_boleta_date("trinta e dois", *PERIODO)


# --- auto-conferência -------------------------------------------------------


def test_boleta_que_fecha_sozinha_nao_vai_para_revisao():
    boleta = _build(_boleta())
    assert boleta.checks == ()
    assert boleta.needs_review is False
    assert boleta.total_cents == 12_970
    assert boleta.items_total_cents == 12_970
    assert boleta.seller == "CARLA"
    assert boleta.date == date(2026, 9, 22)
    assert boleta.payment_method == "credito"
    assert boleta.installments == 2


def test_soma_dos_itens_diferente_do_total_vai_para_revisao():
    boleta = _build(_boleta(total="139.70"))
    assert boleta.needs_review is True
    assert any("soma dos itens" in check for check in boleta.checks)


def test_numero_de_pecas_diferente_da_contagem_vai_para_revisao():
    boleta = _build(_boleta(num_pecas=4))
    assert any("Nº PEÇAS informa 4" in check for check in boleta.checks)


def test_troca_fecha_quando_o_desconto_bate_com_as_pecas_devolvidas():
    # Boleta 3654 (Daniela): devolveu 359,80 e levou 389,60, pagou 29,80.
    boleta = _build(
        _boleta(
            numero="3654",
            cliente="Daniela",
            itens=[
                {"codigo": "1611457021", "valor": "159.90", "manuscrito": False},
                {"codigo": "2707788461", "valor": "39.90", "manuscrito": False},
                {"codigo": "1962598001", "valor": "69.90", "manuscrito": False},
                {"codigo": "1916517331", "valor": "119.90", "manuscrito": False},
            ],
            trocas=[
                {"codigo": "1663890584", "valor": "159.90", "manuscrito": True},
                {"codigo": "1530200731", "valor": "199.90", "manuscrito": True},
            ],
            sub_total="389.60",
            desconto="359.80",
            total="29.80",
            num_pecas=4,
            parcelas=None,
        )
    )
    assert boleta.checks == ()
    assert boleta.sub_total_cents == 38_960
    assert boleta.total_cents == 2_980
    assert len(boleta.returns) == 2


def test_troca_que_nao_bate_com_o_desconto_vai_para_revisao():
    boleta = _build(
        _boleta(
            trocas=[{"codigo": "1663890584", "valor": "100.00", "manuscrito": True}],
            sub_total="129.70",
            desconto="50.00",
            total="79.70",
        )
    )
    assert any("soma das trocas" in check for check in boleta.checks)


def test_desconto_percentual_fecha():
    # Boleta 3651 (Patrícia): 179,70 - 26,97 = 152,73.
    boleta = _build(
        _boleta(
            itens=[
                {"codigo": "5810167881", "valor": "79.90", "manuscrito": False},
                {"codigo": "2707068361", "valor": "59.90", "manuscrito": False},
                {"codigo": "2710588462", "valor": "39.90", "manuscrito": False},
            ],
            sub_total="179.70",
            desconto="26.97",
            total="152.73",
            num_pecas=3,
            cashback=True,
        )
    )
    assert boleta.checks == ()
    assert "cashback" in boleta.flags


def test_campo_ilegivel_marca_a_boleta_para_revisao():
    boleta = _build(_boleta(cliente=None, campos_ilegiveis=["cliente"]))
    assert boleta.checks == ()
    assert boleta.unreadable_fields == ("cliente",)
    assert boleta.needs_review is True


def test_codigo_fora_do_padrao_de_10_digitos_e_sinalizado():
    boleta = _build(
        _boleta(itens=[{"codigo": "252044", "valor": "129.70", "manuscrito": False}], num_pecas=1)
    )
    assert any("dígitos" in check for check in boleta.checks)


def test_boleta_sem_total_vai_para_revisao():
    boleta = _build(_boleta(total=None))
    assert any("TOTAL não foi lido" in check for check in boleta.checks)


# --- agregação --------------------------------------------------------------


def test_agregacao_por_dia_e_vendedora():
    data = build_boletas(
        [
            RawBoleta("carla.pdf", 1, 1, _boleta(total="119.80", num_pecas=3)),
            RawBoleta("carla.pdf", 1, 2, _boleta()),
            RawBoleta("ester.pdf", 1, 1, _boleta(vendedora="Ester", total="25.00", num_pecas=3)),
        ],
        *PERIODO,
    )
    assert data.sellers == ("CARLA", "ESTER")
    assert data.all_dates == {date(2026, 9, 22)}
    assert data.daily_by_seller()[date(2026, 9, 22)] == {"CARLA": 24_950, "ESTER": 2_500}
    assert data.total_on(date(2026, 9, 22)) == 27_450


def test_boleta_invalida_vira_aviso_e_nao_derruba_o_lote():
    data = build_boletas(
        [
            RawBoleta("carla.pdf", 1, 1, _boleta()),
            RawBoleta("carla.pdf", 1, 2, _boleta(data="dia trinta")),
        ],
        *PERIODO,
    )
    assert len(data.boletas) == 1
    assert len(data.warnings) == 1
    assert "carla.pdf#p1b2" in data.warnings[0]


# --- resposta do n8n --------------------------------------------------------


def test_extract_aceita_os_formatos_de_resposta_do_n8n():
    assert _extract({"boleta": {"total": "10.00"}}) == {"total": "10.00"}
    assert _extract([{"boleta": {"total": "10.00"}}]) == {"total": "10.00"}
    assert _extract({"total": "10.00"}) == {"total": "10.00"}


def test_extract_propaga_o_erro_do_workflow():
    with pytest.raises(BoletaSchemaError, match="modelo não devolveu"):
        _extract({"error": "Falha na leitura: o modelo não devolveu conteúdo"})


# --- recorte das amostras reais ---------------------------------------------


def test_recorta_tres_boletas_da_primeira_pagina_da_carla():
    imagens = render_boletas(_scan("boleta carla (65).pdf"))
    assert len(imagens) == 5  # 3 na página 1, 2 na página 2
    pagina1 = [img for img in imagens if img.page == 1]
    assert len(pagina1) == 3
    assert [img.position for img in pagina1] == [1, 2, 3]


def test_recorta_duas_boletas_da_ester():
    imagens = render_boletas(_scan("boleta ester (51).pdf"))
    assert len(imagens) == 2
    assert all(img.png[:8] == b"\x89PNG\r\n\x1a\n" for img in imagens)


def test_recorte_cabe_na_resolucao_que_o_modelo_aceita_sem_reduzir():
    # O serviço reduz a imagem até o menor lado ficar em 768px. Recorte com
    # menor lado abaixo disso chega ao modelo sem perder resolução.
    imagens = render_boletas(_scan("boleta carla (65).pdf"))
    assert all(min(img.width, img.height) <= 768 for img in imagens)


def test_arquivo_vazio_falha_com_mensagem_clara():
    class _Vazio(bytes):
        name = "vazio.pdf"

    with pytest.raises(BoletaRenderError, match="arquivo vazio"):
        render_boletas(_Vazio())


# --- consenso de data entre boletas do mesmo lote ---------------------------


def _com_data(dia: str, arquivo: str = "carla.pdf", pos: int = 1):
    return RawBoleta(arquivo, 1, pos, _boleta(data=dia))


def test_data_solitaria_no_lote_vai_para_revisao():
    # Caso real medido: 4 boletas do arquivo liam 22/09 e uma leu 27/09.
    # Nenhuma checagem interna pega isso — a data não cruza com nada no papel.
    data = build_boletas(
        [
            _com_data("22/09", pos=1),
            _com_data("22/09", pos=2),
            _com_data("22/09", pos=3),
            _com_data("22/09", pos=4),
            _com_data("27/09", pos=5),
        ],
        *PERIODO,
    )
    destoante = [b for b in data.boletas if b.date == date(2026, 9, 27)]
    assert len(destoante) == 1
    assert destoante[0].needs_review is True
    assert any("aparece só nesta boleta" in c for c in destoante[0].checks)
    assert all(b.checks == () for b in data.boletas if b.date == date(2026, 9, 22))


def test_lote_que_cobre_varios_dias_nao_dispara_alarme_falso():
    # Um scan legítimo de vários dias não pode virar ruído: a regra só acusa
    # data que aparece uma única vez contra um dia dominante.
    data = build_boletas(
        [_com_data("22/09", pos=1), _com_data("22/09", pos=2),
         _com_data("23/09", pos=3), _com_data("23/09", pos=4)],
        *PERIODO,
    )
    assert all(b.checks == () for b in data.boletas)


def test_lote_pequeno_nao_tem_consenso_suficiente():
    data = build_boletas(
        [_com_data("22/09", pos=1), _com_data("22/09", pos=2), _com_data("27/09", pos=3)],
        *PERIODO,
    )
    assert all(b.checks == () for b in data.boletas)


def test_consenso_e_calculado_por_arquivo_e_nao_entre_arquivos():
    data = build_boletas(
        [
            _com_data("22/09", "carla.pdf", 1), _com_data("22/09", "carla.pdf", 2),
            _com_data("22/09", "carla.pdf", 3), _com_data("22/09", "carla.pdf", 4),
            _com_data("27/09", "ester.pdf", 1),
        ],
        *PERIODO,
    )
    ester = [b for b in data.boletas if b.source_file == "ester.pdf"]
    assert ester[0].checks == ()

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


def test_codigo_fora_do_padrao_nao_manda_a_boleta_para_revisao():
    # Não há o que corrigir na tela — código de barras não é editável — e o
    # cruzamento reconhece o código a um dígito de distância do CRM. No lote de
    # setembro/2026 este alerta punha 64 boletas em revisão sem ação possível.
    boleta = _build(
        _boleta(itens=[{"codigo": "252044", "valor": "129.70", "manuscrito": False}], num_pecas=1)
    )
    assert not any("dígitos" in check for check in boleta.checks)
    assert boleta.items[0].codigo == "252044"


def test_texto_null_do_modelo_e_tratado_como_vazio():
    # O modelo às vezes devolve o texto "null" no lugar do nulo do JSON; 3
    # boletas do lote de setembro/2026 eram descartadas por isso.
    boleta = _build(_boleta(sub_total="null", desconto="null", num_pecas="null"))
    assert boleta.sub_total_cents is None
    assert boleta.discount_cents is None
    assert boleta.piece_count is None


@pytest.mark.parametrize("ajuste,incerto", [
    ({}, False),
    ({"total": "200.00"}, False),
    ({"cliente": None, "campos_ilegiveis": ["cliente"]}, False),
    ({"num_pecas": 5}, True),
    ({"itens": []}, True),
    ({"itens": [{"codigo": "2520441552", "valor": None, "manuscrito": False}]}, True),
])
def test_duvida_nas_pecas_so_quando_afeta_as_pecas(ajuste, incerto):
    assert _build(_boleta(**ajuste)).items_uncertain is incerto


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


def test_data_ininteligivel_nao_derruba_a_boleta():
    # Antes a boleta inteira sumia do relatório. No lote de setembro/2026 foram
    # 208 de 783 — por um formato de data que o parser não conhecia.
    data = build_boletas(
        [
            RawBoleta("carla.pdf", 1, 1, _boleta()),
            RawBoleta("carla.pdf", 1, 2, _boleta(data="dia trinta")),
        ],
        *PERIODO,
    )
    assert len(data.boletas) == 2
    sem_data = data.boletas[1]
    assert sem_data.date is None
    assert any("não pôde ser interpretada" in c for c in sem_data.checks)


def test_payload_estruturalmente_invalido_vira_aviso_e_nao_derruba_o_lote():
    data = build_boletas(
        [
            RawBoleta("carla.pdf", 1, 1, _boleta()),
            RawBoleta("carla.pdf", 1, 2, _boleta(itens="não é lista")),
        ],
        *PERIODO,
    )
    assert len(data.boletas) == 1
    assert len(data.warnings) == 1
    assert "carla.pdf#p1b2" in data.warnings[0]


# --- datas como as vendedoras escrevem -----------------------------------------


@pytest.mark.parametrize("escrito", [
    "05/SET", "05/set", "05 SET", "5 SET", "05 SET.", "05/SET.", "05 SET. 2026",
    "05 SET 2026", "05 SET, 2026", "05/SET.2026", "5 de setembro", "05/09", "5/9",
])
def test_data_com_mes_por_extenso(escrito):
    assert resolve_boleta_date(escrito, *PERIODO) == date(2026, 9, 5)


def test_dia_sozinho_usa_o_mes_do_arquivo():
    inicio, fim = date(2026, 8, 20), date(2026, 9, 27)
    assert resolve_boleta_date("30", inicio, fim, month_hint=8) == date(2026, 8, 30)


def test_dia_sozinho_sem_mes_conhecido_e_ininteligivel():
    with pytest.raises(BoletaSchemaError):
        resolve_boleta_date("30", date(2026, 8, 20), date(2026, 9, 27))


# --- data pelo nome do arquivo -------------------------------------------------


def _com_arquivo(nome, **overrides):
    return build_boleta(
        _boleta(**overrides), source_file=nome, page=1, position=1,
        start=PERIODO[0], end=PERIODO[1],
    )


@pytest.mark.parametrize("nome,esperado", [
    ("Boletas Leide 05.09.pdf", date(2026, 9, 5)),
    ("boletas Daniely 01.09.pdf", date(2026, 9, 1)),
    ("Boletas Jaiza 21.09 (1).pdf", date(2026, 9, 21)),
    ("boleta carla (65).pdf", None),
    ("Boletas 31.09.pdf", None),
])
def test_data_no_nome_do_arquivo(nome, esperado):
    from src.boletas.schema import date_from_filename

    assert date_from_filename(nome, *PERIODO) == esperado


def test_data_em_branco_e_coberta_pelo_nome_do_arquivo():
    boleta = _com_arquivo("Boletas Leide 05.09.pdf", data=None, campos_ilegiveis=["data"])
    assert boleta.date == date(2026, 9, 5)
    assert boleta.date_source == "arquivo"
    assert "data" not in boleta.unreadable_fields
    assert boleta.checks == ()


def test_data_divergente_do_arquivo_usa_a_do_arquivo_e_avisa():
    # Caso típico medido: "1 SET" num arquivo de 21/09 — o modelo comeu o "2".
    boleta = _com_arquivo("Boletas Daniely 21.09.pdf", data="1 SET")
    assert boleta.date == date(2026, 9, 21)
    assert any("a boleta diz 01/09, o arquivo é de 21/09" in c for c in boleta.checks)
    assert boleta.date_suspect is False, "o dia do arquivo é confiável; não sai do cruzamento"


def test_data_digitada_na_tela_vale_mais_que_o_arquivo():
    boleta = _com_arquivo("Boletas Daniely 18.09.pdf", data="12/09", data_confirmada=True)
    assert boleta.date == date(2026, 9, 12)
    assert boleta.date_source == "confirmada"
    assert boleta.checks == ()


def test_consenso_entre_vizinhas_nao_se_aplica_a_arquivo_datado():
    data = build_boletas(
        [RawBoleta("Boletas Leide 05.09.pdf", 1, i, _boleta()) for i in range(1, 5)]
        + [RawBoleta("Boletas Leide 05.09.pdf", 1, 5, _boleta(data="12/09", data_confirmada=True))],
        *PERIODO,
    )
    assert not any(b.date_suspect for b in data.boletas)


# --- desconto em porcentagem --------------------------------------------------


def test_desconto_em_porcentagem_sobre_o_sub_total():
    # 48 boletas do lote de setembro/2026 traziam o desconto assim. Tratado como
    # dinheiro, "10%" virava R$ 10,00 sem aviso.
    boleta = _build(_boleta(
        itens=[{"codigo": "2707569661", "valor": "124.80", "manuscrito": False}],
        num_pecas=1, sub_total="124.80", desconto="10%", total="112.32",
    ))
    assert boleta.discount_cents == 1248
    assert boleta.checks == ()


def test_desconto_em_porcentagem_sem_sub_total_usa_a_soma_dos_itens():
    boleta = _build(_boleta(
        itens=[{"codigo": "2707569661", "valor": "204.80", "manuscrito": False}],
        num_pecas=1, sub_total=None, desconto="15%", total="174.08",
    ))
    assert boleta.discount_cents == 3072
    assert boleta.checks == ()


def test_desconto_em_porcentagem_tolera_um_centavo_de_arredondamento():
    boleta = _build(_boleta(
        itens=[{"codigo": "2707569661", "valor": "299.70", "manuscrito": False}],
        num_pecas=1, sub_total="299.70", desconto="10%", total="269.74",
    ))
    assert boleta.checks == ()


def test_desconto_em_porcentagem_com_total_errado_vai_para_revisao():
    boleta = _build(_boleta(
        itens=[{"codigo": "2707569661", "valor": "124.80", "manuscrito": False}],
        num_pecas=1, sub_total="124.80", desconto="10%", total="100.00",
    ))
    assert any("difere do TOTAL" in c for c in boleta.checks)


def test_desconto_em_dinheiro_nao_ganha_tolerancia():
    boleta = _build(_boleta(
        itens=[{"codigo": "2707569661", "valor": "124.80", "manuscrito": False}],
        num_pecas=1, sub_total="124.80", desconto="12.48", total="112.33",
    ))
    assert any("difere do TOTAL" in c for c in boleta.checks)


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
    assert all(img.data[:8] == b"\x89PNG\r\n\x1a\n" for img in imagens), "esperava PNG"
    assert all(img.mime == "image/png" for img in imagens)


def test_recorte_e_sem_perda():
    # JPEG foi medido e piorou a leitura da vendedora cursiva. O PNG entrega ao
    # modelo exatamente os pixels do recorte, qualquer que seja a compressão.
    from io import BytesIO

    import numpy as np
    from PIL import Image

    from src.boletas.render import MAX_SHORT_SIDE, _fit_short_side, _iter_pages, find_boleta_columns

    caminho = _scan("boleta ester (51).pdf")
    pagina = next(_iter_pages(caminho.read_bytes(), caminho.name, 200))
    esquerda, direita = find_boleta_columns(pagina)[0]
    original = _fit_short_side(
        pagina.crop((max(0, esquerda - 8), 0, min(pagina.width, direita + 8), pagina.height)),
        MAX_SHORT_SIDE,
    )
    enviado = render_boletas(caminho)[0]
    decodificado = Image.open(BytesIO(enviado.data)).convert("RGB")
    assert np.array_equal(np.asarray(decodificado), np.asarray(original))


def test_iter_boletas_devolve_o_mesmo_que_o_recorte_em_lista():
    from src.boletas.render import iter_boletas

    caminho = _scan("boleta carla (65).pdf")
    dados = caminho.read_bytes()
    uma_a_uma = list(iter_boletas(dados, caminho.name))
    em_lista = render_boletas(caminho)
    assert [(i.page, i.position, i.data) for i in uma_a_uma] == [
        (i.page, i.position, i.data) for i in em_lista
    ]


def test_mesmo_arquivo_gera_as_mesmas_imagens():
    # O cache de leituras depende disto: se o recorte não fosse determinístico,
    # nada impediria uma boleta de ser paga de novo.
    caminho = _scan("boleta ester (51).pdf")
    primeira = [i.data for i in render_boletas(caminho)]
    segunda = [i.data for i in render_boletas(caminho)]
    assert primeira == segunda


def test_recorte_cabe_na_resolucao_que_o_modelo_aceita_sem_reduzir():
    # O serviço reduz a imagem até o menor lado ficar em 768px. Recorte com
    # menor lado abaixo disso chega ao modelo sem perder resolução.
    imagens = render_boletas(_scan("boleta carla (65).pdf"))
    assert all(min(img.width, img.height) <= 768 for img in imagens)


def _pagina_sintetica(*, fundo: int, linha: int):
    """Duas boletas lado a lado: tabela de linhas finas e um pouco de manuscrito."""
    import numpy as np
    from PIL import Image

    pagina = np.full((1000, 1200), fundo, dtype=np.uint8)
    for esquerda in (100, 700):
        for y in range(150, 850, 25):
            pagina[y : y + 2, esquerda : esquerda + 400] = linha
        pagina[160:170, esquerda + 20 : esquerda + 80] = 60
    return Image.fromarray(pagina).convert("RGB")


def test_boleta_de_linhas_claras_nao_e_tomada_por_vao():
    # Daniely 01.09, página 4: a tabela da boleta 28861 é clara demais para
    # contar como tinta, e a boleta inteira sumia do recorte.
    from src.boletas.render import find_boleta_columns

    colunas = find_boleta_columns(_pagina_sintetica(fundo=255, linha=200))
    assert len(colunas) == 2
    assert colunas[0][0] <= 100 and colunas[0][1] >= 500
    assert colunas[1][0] <= 700 and colunas[1][1] >= 1100


def test_fundo_cinza_uniforme_continua_sendo_vao():
    # Scan escuro (Daniely 19.09, página 6): fundo cinza sem linha nenhuma não
    # pode virar tabela e colar as boletas numa só.
    from src.boletas.render import find_boleta_columns

    assert len(find_boleta_columns(_pagina_sintetica(fundo=185, linha=140))) == 2


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

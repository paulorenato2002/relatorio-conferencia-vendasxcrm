"""Roda a tela de verdade, com arquivo no uploader.

O bug que motivou este arquivo só aparecia depois do upload: até então o
`session_state` não tinha o valor do widget, e qualquer teste que apenas
importasse ou abrisse a página passava. Estes testes preenchem os uploaders e
reexecutam o script, que é o caminho onde o widget escreve no `session_state`.

Nenhum teste aqui chama o n8n: a leitura só acontece no clique do botão, que
ninguém aperta. O upload sozinho já exercita o trecho quebrado.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from streamlit.testing.v1 import AppTest

from src.boletas.edicao import files_signature


# Caminho relativo em `from_file` resolve contra o arquivo que chama, não
# contra a raiz do projeto.
APP = str(Path(__file__).resolve().parents[1] / "app.py")


PDF_MINIMO = (
    b"%PDF-1.4\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
    b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
    b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 200 200]>>endobj\n"
    b"trailer<</Root 1 0 R>>\n%%EOF\n"
)


@pytest.fixture
def app() -> AppTest:
    instancia = AppTest.from_file(APP, default_timeout=60)
    instancia.run()
    return instancia


def _uploader(app: AppTest, chave: str):
    for widget in app.get("file_uploader"):
        if widget.key == chave:
            return widget
    raise AssertionError(f"uploader {chave!r} não encontrado na tela")


def test_a_tela_abre_sem_erro(app):
    assert not app.exception, app.exception


def test_upload_de_boleta_nao_quebra_a_tela(app):
    # Regressão: `key="boletas"` no uploader colidia com a chave usada para
    # guardar a leitura, e o widget sobrescrevia o dicionário com a lista de
    # arquivos. Quebrava com "'list' object has no attribute 'get'".
    _uploader(app, "boletas").upload("boletas.pdf", PDF_MINIMO, "application/pdf")
    app.run()
    assert not app.exception, app.exception


def test_upload_de_boleta_revela_o_botao_de_leitura(app):
    _uploader(app, "boletas").upload("boletas.pdf", PDF_MINIMO, "application/pdf")
    app.run()
    rotulos = [botao.label for botao in app.get("button")]
    assert "Ler boletas no n8n" in rotulos


def test_sem_boleta_a_tela_orienta_em_vez_de_mostrar_botao(app):
    rotulos = [botao.label for botao in app.get("button")]
    assert "Ler boletas no n8n" not in rotulos
    textos = " ".join(str(info.value) for info in app.get("info"))
    assert "boletas escaneadas" in textos.lower()


def test_varios_uploads_de_boleta_seguem_funcionando(app):
    uploader = _uploader(app, "boletas")
    uploader.upload("um.pdf", PDF_MINIMO, "application/pdf")
    uploader.upload("dois.pdf", PDF_MINIMO, "application/pdf")
    app.run()
    assert not app.exception, app.exception


def test_upload_nos_quatro_campos_nao_quebra(app):
    # Os outros uploaders não tinham o problema, mas nada garante que uma chave
    # nova não volte a colidir; o custo de cobrir os quatro é baixo.
    _uploader(app, "boletas").upload("b.pdf", PDF_MINIMO, "application/pdf")
    _uploader(app, "cash").upload("caixa.pdf", PDF_MINIMO, "application/pdf")
    _uploader(app, "crm").upload("crm.xlsx", b"PK\x03\x04nao-e-um-xlsx-valido", "application/vnd.ms-excel")
    _uploader(app, "rede").upload("rede.xlsx", b"PK\x03\x04nao-e-um-xlsx-valido", "application/vnd.ms-excel")
    app.run()
    assert not app.exception, app.exception


def test_validar_com_planilha_invalida_mostra_erro_em_vez_de_estourar(app):
    _uploader(app, "crm").upload("crm.xlsx", b"conteudo invalido", "application/vnd.ms-excel")
    _uploader(app, "rede").upload("rede.xlsx", b"conteudo invalido", "application/vnd.ms-excel")
    app.run()
    botao = next(b for b in app.get("button") if b.label == "Validar arquivos")
    botao.click()
    app.run()
    assert not app.exception, app.exception
    assert app.get("error"), "a tela deveria explicar o arquivo inválido"


# --- aprovação das boletas antes de processar --------------------------------


def _leitura_simulada(app: AppTest, total="119.80"):
    """Injeta uma leitura já concluída, sem chamar o n8n."""
    from src.boletas.client import RawBoleta

    payload = {
        "numero": "3650", "numero_controle": "3850", "data": "22/09",
        "vendedora": "Carla", "cliente": "Cida", "telefone": None,
        "itens": [
            {"codigo": "2707569661", "valor": "59.90", "manuscrito": False},
            {"codigo": "3698637992", "valor": "59.90", "manuscrito": False},
        ],
        "trocas": [], "sub_total": None, "desconto": None, "total": total,
        "num_pecas": 2, "pagamento": "credito", "parcelas": None,
        "bandeira": "Mastercard", "brinde": False, "presente": False,
        "whats": False, "cashback": False, "aniver": False, "outros": False,
        "campos_ilegiveis": [],
    }
    class _Enviado:
        name = "boletas.pdf"

        @staticmethod
        def getvalue():
            return PDF_MINIMO

    _uploader(app, "boletas").upload("boletas.pdf", PDF_MINIMO, "application/pdf")
    app.run()
    # A tela só considera a leitura válida se ela corresponder aos arquivos que
    # estão no uploader agora; por isso a impressão digital tem que bater.
    app.session_state["boletas_leitura"] = {
        "fingerprint": files_signature([_Enviado()]),
        "raw": [RawBoleta("boletas.pdf", 1, 1, payload)],
        "warnings": [],
        "file_names": ("boletas.pdf",),
    }
    return app


def _botao(app: AppTest, rotulo: str):
    return next((b for b in app.get("button") if b.label.startswith(rotulo)), None)


def test_leitura_pendente_de_aprovacao_bloqueia_o_processamento(app):
    _leitura_simulada(app)
    app.run()
    assert not app.exception, app.exception
    processar = _botao(app, "Processar conferência")
    assert processar is not None and processar.disabled


def test_a_tela_diz_por_que_o_processamento_esta_travado(app):
    _leitura_simulada(app)
    app.run()
    avisos = " ".join(str(i.value) for i in app.get("info"))
    assert "Aprove as boletas" in avisos


def test_botao_de_aprovar_aparece_com_boletas_lidas(app):
    _leitura_simulada(app)
    app.run()
    assert _botao(app, "Aprovar boletas") is not None


def test_boleta_que_nao_fecha_muda_o_rotulo_do_botao(app):
    # Deixa explícito que se está aprovando algo ainda pendente.
    _leitura_simulada(app, total="200.00")
    app.run()
    assert _botao(app, "Aprovar mesmo com") is not None


def test_tabela_de_correcao_aparece_com_boletas_lidas(app):
    # O `data_editor` sobe como elemento "dataframe"; o que identifica os
    # editores é a chave que eles registram no estado ao renderizar.
    _leitura_simulada(app)
    app.run()
    assert "editor_cabecalhos" in app.session_state, "faltou a tabela de correção"


def test_sem_boleta_o_processamento_nao_depende_de_aprovacao(app):
    # Quem não usa boletas não pode ficar travado por causa delas.
    app.run()
    assert not app.exception, app.exception
    avisos = " ".join(str(i.value) for i in app.get("info"))
    assert "Aprove as boletas" not in avisos

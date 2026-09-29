"""Leitura das boletas pela tela, em segundo plano, contra um n8n falso.

Cobre o caminho que travava com 27 dias de boletas: o clique dispara a leitura
numa thread, a tela só acompanha, e o resultado é recolhido numa execução
seguinte do script.

Nenhum teste aqui pode chamar o n8n de verdade: o `.env` do projeto aponta
para a instância real. A fixture sobrescreve a URL pelo ambiente e confere que
ela aponta para o servidor local antes de qualquer clique.
"""

from __future__ import annotations

from pathlib import Path
import time

import pytest

from streamlit.testing.v1 import AppTest

import src.boletas.job as job_module
from src.boletas.cache import LeituraCache
from src.boletas.client import N8nConfig
from tests.test_job import FakeN8n, pdf


APP = str(Path(__file__).resolve().parents[1] / "app.py")


@pytest.fixture
def n8n(monkeypatch, tmp_path):
    fake = FakeN8n()
    monkeypatch.setenv("N8N_BOLETAS_WEBHOOK_URL", fake.url)
    monkeypatch.setenv("N8N_BOLETAS_TOKEN", "teste")
    # Trava de segurança: se a URL do ambiente não prevalecer sobre o .env,
    # o teste mandaria boletas para o n8n real e gastaria dinheiro.
    assert N8nConfig.from_env().webhook_url.startswith("http://127.0.0.1"), (
        "a configuração do teste não está apontando para o n8n falso"
    )
    monkeypatch.setattr(job_module, "_DEFAULT_CACHE", LeituraCache(tmp_path / "leituras"))
    job_module._REGISTRY.clear()
    yield fake
    for job in list(job_module._REGISTRY.values()):
        job.cancel()
        job.wait(timeout=10)
    job_module._REGISTRY.clear()
    fake.close()


@pytest.fixture
def app() -> AppTest:
    instancia = AppTest.from_file(APP, default_timeout=60)
    instancia.run()
    return instancia


def _uploader(app, chave):
    return next(w for w in app.get("file_uploader") if w.key == chave)


def _botao(app, inicio):
    return next((b for b in app.get("button") if b.label.startswith(inicio)), None)


def _ler_e_esperar(app):
    _botao(app, "Ler boletas").click()
    app.run()
    assert not app.exception, app.exception
    # Leitura que sai toda do cache termina antes de a tela reexecutar e já é
    # recolhida no mesmo clique; só a que ainda está rodando fica registrada.
    for job in list(job_module._REGISTRY.values()):
        assert job.config.webhook_url.startswith("http://127.0.0.1")
        assert job.wait(timeout=60), "a leitura não terminou"
    app.run()
    assert not app.exception, app.exception
    assert "boletas_leitura" in app.session_state, "o resultado não chegou na tela"


def test_leitura_roda_em_segundo_plano_e_o_resultado_chega_na_tela(n8n, app):
    _uploader(app, "boletas").upload("Boletas Leide 02.09.pdf", pdf([3, 2], 1), "application/pdf")
    app.run()
    _ler_e_esperar(app)

    leitura = app.session_state["boletas_leitura"]
    assert len(leitura["raw"]) == 5
    assert leitura["failed"] == 0
    assert "editor_cabecalhos" in app.session_state
    assert dict((m.label, m.value) for m in app.get("metric"))["Boletas lidas"] == "5"
    assert not job_module._REGISTRY, "a leitura concluída deveria ter sido recolhida"


def test_o_clique_nao_espera_a_leitura_terminar(n8n, app):
    # Antes, o clique só voltava depois de todas as boletas — 25 minutos num mês.
    n8n.delay = 0.5
    _uploader(app, "boletas").upload("b.pdf", pdf([3, 3], 2), "application/pdf")
    app.run()
    inicio = time.monotonic()
    _botao(app, "Ler boletas").click()
    app.run()
    assert time.monotonic() - inicio < 5, "a tela ficou presa esperando a leitura"
    job = next(iter(job_module._REGISTRY.values()))
    assert not job.snapshot().finished
    assert _botao(app, "Interromper leitura") is not None


def test_arquivo_repetido_vira_aviso_e_nao_entra_duas_vezes(n8n, app):
    conteudo = pdf([2], 3)
    uploader = _uploader(app, "boletas")
    uploader.upload("Boletas Jaiza 21.09.pdf", conteudo, "application/pdf")
    uploader.upload("Boletas Jaiza 21.09 (1).pdf", conteudo, "application/pdf")
    app.run()
    _ler_e_esperar(app)

    assert len(app.session_state["boletas_leitura"]["raw"]) == 2
    avisos = " ".join(str(w.value) for w in app.get("warning"))
    assert "idênticos" in avisos and "21.09 (1)" in avisos


def test_ler_de_novo_os_mesmos_arquivos_nao_chama_o_n8n(n8n, app):
    _uploader(app, "boletas").upload("c.pdf", pdf([3], 4), "application/pdf")
    app.run()
    _ler_e_esperar(app)
    chamadas = len(n8n.requests)

    # "Reler tudo" desmarcado e arquivo já lido: o botão fica travado; numa
    # sessão nova com os mesmos arquivos, tudo sai do cache.
    outra = AppTest.from_file(APP, default_timeout=60)
    outra.run()
    _uploader(outra, "boletas").upload("c.pdf", pdf([3], 4), "application/pdf")
    outra.run()
    _ler_e_esperar(outra)
    assert len(n8n.requests) == chamadas
    assert outra.session_state["boletas_leitura"]["stats"]["from_cache"] == 3


def test_sem_url_configurada_mostra_erro_e_nao_inicia(monkeypatch, tmp_path, app):
    monkeypatch.setenv("N8N_BOLETAS_WEBHOOK_URL", "")
    monkeypatch.setattr(job_module, "_DEFAULT_CACHE", LeituraCache(tmp_path / "leituras"))
    job_module._REGISTRY.clear()
    _uploader(app, "boletas").upload("d.pdf", pdf([1], 5), "application/pdf")
    app.run()
    _botao(app, "Ler boletas").click()
    app.run()
    assert not app.exception, app.exception
    assert not job_module._REGISTRY
    assert any("N8N_BOLETAS_WEBHOOK_URL" in str(e.value) for e in app.get("error"))

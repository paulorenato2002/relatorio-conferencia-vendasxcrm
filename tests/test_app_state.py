"""Garante que nenhuma chave de `session_state` colide com chave de widget.

O Streamlit grava o valor de cada widget no `session_state` sob a chave passada
em `key=`. Usar o mesmo nome para guardar estado próprio faz o widget
sobrescrever esse estado assim que o usuário interage com ele — e o erro só
aparece em runtime, depois do upload, que é justamente o caminho que um teste
de importação não percorre.

Aconteceu com `key="boletas"` do uploader contra o cache da leitura das
boletas: o dicionário virava a lista de arquivos enviados e a tela quebrava com
`'list' object has no attribute 'get'`.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest


APP = Path(__file__).resolve().parents[1] / "app.py"


def _tree() -> ast.Module:
    return ast.parse(APP.read_text(encoding="utf-8"), filename="app.py")


def widget_keys(tree: ast.Module) -> set[str]:
    """Valores de `key=` passados a chamadas `st.<algo>(...)`."""
    keys: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name)):
            continue
        if func.value.id != "st":
            continue
        for keyword in node.keywords:
            if keyword.arg == "key" and isinstance(keyword.value, ast.Constant):
                if isinstance(keyword.value.value, str):
                    keys.add(keyword.value.value)
    return keys


def session_state_keys(tree: ast.Module) -> set[str]:
    """Chaves usadas em `st.session_state[...]`, `.get(...)` e `.pop(...)`."""
    keys: set[str] = set()

    def is_session_state(node: ast.AST) -> bool:
        return (
            isinstance(node, ast.Attribute)
            and node.attr == "session_state"
            and isinstance(node.value, ast.Name)
            and node.value.id == "st"
        )

    for node in ast.walk(tree):
        if isinstance(node, ast.Subscript) and is_session_state(node.value):
            index = node.slice
            if isinstance(index, ast.Constant) and isinstance(index.value, str):
                keys.add(index.value)
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in {"get", "pop", "setdefault"}
            and is_session_state(node.func.value)
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
        ):
            keys.add(node.args[0].value)
    return keys


def test_o_teste_enxerga_as_chaves():
    # Sem isso, uma mudança de estilo em app.py poderia esvaziar os dois
    # conjuntos e o teste passaria sem conferir nada.
    tree = _tree()
    assert widget_keys(tree), "nenhuma chave de widget encontrada em app.py"
    assert session_state_keys(tree), "nenhuma chave de session_state encontrada"


def test_nenhuma_chave_de_widget_e_usada_como_estado_proprio():
    tree = _tree()
    colisoes = widget_keys(tree) & session_state_keys(tree)
    assert not colisoes, (
        "estas chaves são de widget e de session_state ao mesmo tempo: "
        f"{sorted(colisoes)}. O widget sobrescreve o estado guardado; "
        "renomeie a chave do session_state."
    )


@pytest.mark.parametrize("chave", ["boletas", "crm", "rede", "cash"])
def test_uploaders_nao_guardam_estado_proprio(chave):
    tree = _tree()
    assert chave in widget_keys(tree)
    assert chave not in session_state_keys(tree)

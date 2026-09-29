"""Correção manual das boletas lidas, na tela.

Apontar que uma boleta não fecha sem deixar corrigi-la ali não resolve nada: o
operador tem o papel na mão e sabe o que está escrito.

Só entram aqui os campos manuscritos, que são os que uma pessoa confere de
relance contra o papel: data, vendedora, cliente, número, peças, os totais,
forma de pagamento e bandeira. Código de barras e valor de etiqueta ficam de
fora de propósito — são impressos, ninguém vai reteclar dez dígitos, e o erro
de um dígito o cruzamento com o CRM já identifica sozinho.

A edição acontece sobre o payload cru, não sobre a `Boleta` já montada. Assim a
correção atravessa exatamente a mesma conversão e as mesmas checagens
aritméticas que a leitura do n8n atravessou: um total corrigido à mão é
reconferido contra a soma dos itens como qualquer outro, e uma data corrigida
volta a ser comparada com a das boletas vizinhas.
"""

from __future__ import annotations

import hashlib
import json
from typing import Iterable, Sequence

import pandas as pd

from src.boletas.client import RawBoleta


PAGAMENTOS = ["", "pix", "dinheiro", "credito", "debito"]

# Coluna da tabela -> campo do payload. Só campos manuscritos.
CAMPOS = {
    "Nº": "numero",
    "Data": "data",
    "Vendedora": "vendedora",
    "Cliente": "cliente",
    "Peças": "num_pecas",
    "Sub total": "sub_total",
    "Desconto": "desconto",
    "Total": "total",
    "Pagamento": "pagamento",
    "Bandeira": "bandeira",
}
COLUNAS = ("Boleta", *CAMPOS)


def raw_id(item: RawBoleta) -> str:
    return f"{item.source_file}#p{item.page}b{item.position}"


def _text(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, float) and pd.isna(value):
        return None
    text = str(value).strip()
    return text or None


def frames_from_raw(raw: Iterable[RawBoleta]) -> pd.DataFrame:
    """Transcrições cruas -> tabela editável, uma linha por boleta."""
    linhas = []
    for item in raw:
        payload = item.payload
        linha = {"Boleta": raw_id(item)}
        for coluna, campo in CAMPOS.items():
            valor = payload.get(campo)
            linha[coluna] = valor if campo == "num_pecas" else str(valor or "")
        linhas.append(linha)
    return pd.DataFrame(linhas, columns=list(COLUNAS))


def raw_from_frames(raw: Sequence[RawBoleta], headers: pd.DataFrame) -> list[RawBoleta]:
    """Devolve as transcrições com as correções da tela aplicadas.

    O que a tabela não mostra — códigos das peças, telefone, número de
    controle, marcações — passa intacto.
    """
    por_boleta = {str(row["Boleta"]): row.to_dict() for _, row in headers.iterrows()}

    corrigidas: list[RawBoleta] = []
    for item in raw:
        payload = dict(item.payload)
        editado = por_boleta.get(raw_id(item))
        if editado:
            for coluna, campo in CAMPOS.items():
                valor = editado.get(coluna)
                if campo == "num_pecas":
                    payload[campo] = (
                        None if valor is None or pd.isna(valor) else int(valor)
                    )
                else:
                    payload[campo] = _text(valor)
            # Data digitada na tela é do operador, que tem o papel na mão: vale
            # mais que a data do nome do arquivo.
            if payload["data"] is not None and payload["data"] != _text(item.payload.get("data")):
                payload["data_confirmada"] = True
        corrigidas.append(
            RawBoleta(
                source_file=item.source_file,
                page=item.page,
                position=item.position,
                payload=payload,
            )
        )
    return corrigidas


def files_signature(uploads) -> str | None:
    """Impressão digital dos arquivos de boleta enviados.

    Presa aos arquivos, e não ao período ou à empresa: a leitura no n8n é a
    única etapa paga do fluxo, e mudar um filtro da tela não pode custar uma
    releitura das mesmas imagens.
    """
    if not uploads:
        return None
    digest = hashlib.sha256()
    for upload in uploads:
        digest.update(str(upload.name).encode("utf-8", errors="replace"))
        digest.update(upload.getvalue())
    return digest.hexdigest()


def signature(raw: Iterable[RawBoleta]) -> str:
    """Muda sempre que qualquer campo de qualquer boleta muda.

    É o que amarra a aprovação ao conteúdo aprovado: mexeu depois de aprovar,
    a aprovação cai e precisa ser refeita.
    """
    digest = hashlib.sha256()
    for item in raw:
        digest.update(raw_id(item).encode())
        digest.update(json.dumps(item.payload, sort_keys=True, default=str).encode())
    return digest.hexdigest()

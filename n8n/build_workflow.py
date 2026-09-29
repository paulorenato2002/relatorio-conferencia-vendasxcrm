"""Gera `workflow.json` e `montar-requisicao.js` a partir das partes legíveis.

Cadeia: Webhook -> Montar requisicao (Code) -> OpenAI Visao (HTTP) ->
Montar resposta (Code) -> Responder.

O Code fica logo depois do Webhook para garantir acesso ao binário, e carrega
prompt e schema como constantes: ambos são editáveis pela UI do n8n.

Edite o prompt (`SYSTEM`) ou o schema (`SCHEMA`) aqui e rode:

    python n8n/build_workflow.py

Editar o `workflow.json` na mão funciona, mas a próxima geração desfaz. Para
ajustar a leitura sem re-importar o workflow no n8n — o que obrigaria a
reconfigurar as credenciais —, gere os arquivos e cole o conteúdo de
`montar-requisicao.js` no node `Montar requisicao` pela interface do n8n.
"""

import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
OUT = HERE / "workflow.json"
JS_OUT = HERE / "montar-requisicao.js"
JS_RESPOSTA_OUT = HERE / "montar-resposta.js"

SYSTEM = """Você transcreve boletas manuscritas de pedido de compra de uma loja de bijuterias (rede Morana). Você devolve apenas o que está escrito. Você nunca inventa, nunca completa e nunca corrige um valor.

LAYOUT DA BOLETA (formulário pré-impresso, preenchido à caneta):
- Topo, fora da moldura: dois números manuscritos. O da ESQUERDA é o telefone do cliente (9 dígitos, às vezes com DDD). O da DIREITA é o número de controle/venda do sistema (4 a 6 dígitos). Muitas boletas têm só um dos dois, ou nenhum.
- Dentro da moldura: "Data:" (escrita como 22/09 ou 22/9, normalmente SEM o ano), "Nº" (impresso, não manuscrito), "Vendedora:" e "Cliente:" (manuscritos, primeiro nome).
- Corpo: linhas de itens. Cada item aparece de uma destas duas formas:
  (a) ETIQUETA ADESIVA colada na linha, com "R$ 59.90" e, logo abaixo, um código de barras com um número de 10 dígitos impresso (ex.: 2707569661). Este é o caso comum.
  (b) ITEM MANUSCRITO: o código de 10 dígitos escrito à caneta na linha, e o valor escrito na coluna da direita.
- Rodapé: "SUB TOTAL", "DESCONTO", "TOTAL:" (manuscritos, muitas vezes só o TOTAL é preenchido).
- Caixas de marcação: BRINDE, PRESENTE, WHATS à esquerda; FORMAS DE PAGAMENTO (PIX, DINHEIRO, CRÉDITO, DEBITO) à direita; CASHBACK, ANIVER, OUTROS na faixa inferior. Marcadas com X ou rabisco.
- "Nº PEÇAS": quantidade de peças, dentro de um quadro (ex.: 02, 03).
- "BANDEIRA": bandeira do cartão escrita à mão (Mastercard, Visa Electron, Elo crédito, Pix...).
- Ao lado da caixa de CRÉDITO pode haver o número de parcelas escrito, como "2X".

TROCA/DEVOLUÇÃO: algumas boletas trazem, nas primeiras linhas, um marcador "(E)" circulado seguido de itens manuscritos. São peças DEVOLVIDAS pela cliente, cujo valor é lançado na linha DESCONTO. Esses itens vão em `trocas`, NUNCA em `itens`.

REGRAS DE TRANSCRIÇÃO:
1. Copie os dígitos exatamente como aparecem. Não normalize, não arredonde, não "conserte" um valor que pareça errado.
2. Valores em reais no formato "159.90" (ponto decimal, sem separador de milhar, sem "R$").
3. `data`: copie como está escrito, ex.: "22/09". Não invente o ano.
4. Campo em branco no papel -> null. Não confunda com ilegível.
5. Campo preenchido mas que você NÃO consegue ler com segurança -> null, E acrescente o nome do campo em `campos_ilegiveis`. Chutar é pior que admitir. Exemplo: um nome de cliente em cursiva fechada vira `cliente: null` + `campos_ilegiveis: ["cliente"]`.
6. Dígito rasurado ou sobrescrito: transcreva sua melhor leitura E inclua o campo em `campos_ilegiveis`.
7. `pagamento`: exatamente um de "pix", "dinheiro", "credito", "debito", ou null se nenhuma caixa estiver marcada.
8. `num_pecas`: o número dentro do quadro, não a contagem que você fez dos itens. Se estiver em branco, null.
9. NÃO calcule nada. Se o SUB TOTAL está em branco no papel, devolva null — mesmo que você consiga somar os itens. A conferência aritmética é feita depois, fora daqui, e depende de receber o que está escrito.
10. A imagem contém UMA boleta. Se houver pedaço de outra boleta na borda, ignore."""

USER = "Transcreva esta boleta seguindo as regras. Devolva apenas o JSON."

ITEM_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["codigo", "valor", "manuscrito"],
    "properties": {
        "codigo": {
            "type": ["string", "null"],
            "description": "Código de 10 dígitos da etiqueta, apenas dígitos.",
        },
        "valor": {"type": ["string", "null"], "description": "Valor da peça, ex.: '59.90'."},
        "manuscrito": {
            "type": "boolean",
            "description": "true quando o item foi escrito à caneta em vez de etiqueta.",
        },
    },
}

SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "numero", "numero_controle", "data", "vendedora", "cliente", "telefone",
        "itens", "trocas", "sub_total", "desconto", "total", "num_pecas",
        "pagamento", "parcelas", "bandeira", "brinde", "presente", "whats",
        "cashback", "aniver", "outros", "campos_ilegiveis",
    ],
    "properties": {
        "numero": {"type": ["string", "null"], "description": "Nº impresso da boleta."},
        "numero_controle": {"type": ["string", "null"], "description": "Número manuscrito no canto superior direito."},
        "data": {"type": ["string", "null"], "description": "Como escrito, ex.: '22/09'."},
        "vendedora": {"type": ["string", "null"]},
        "cliente": {"type": ["string", "null"]},
        "telefone": {"type": ["string", "null"], "description": "Número manuscrito no canto superior esquerdo."},
        "itens": {"type": "array", "items": ITEM_SCHEMA},
        "trocas": {"type": "array", "items": ITEM_SCHEMA, "description": "Peças devolvidas, marcadas com (E)."},
        "sub_total": {"type": ["string", "null"]},
        "desconto": {"type": ["string", "null"]},
        "total": {"type": ["string", "null"]},
        "num_pecas": {"type": ["integer", "null"]},
        "pagamento": {"type": ["string", "null"], "enum": ["pix", "dinheiro", "credito", "debito", None]},
        "parcelas": {"type": ["integer", "null"]},
        "bandeira": {"type": ["string", "null"]},
        "brinde": {"type": "boolean"},
        "presente": {"type": "boolean"},
        "whats": {"type": "boolean"},
        "cashback": {"type": "boolean"},
        "aniver": {"type": "boolean"},
        "outros": {"type": "boolean"},
        "campos_ilegiveis": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Nomes dos campos preenchidos que não puderam ser lidos com segurança.",
        },
    },
}

BUILD_CODE = """// Monta a requisição do OpenAI para UMA boleta.
// Para ajustar a leitura, edite SYSTEM (as regras) ou MODELO. O schema abaixo
// é o contrato com o app Python: mudou aqui, muda em src/boletas/schema.py.

const MODELO = 'gpt-4o';

const SYSTEM = %(system)s;

const USER = %(user)s;

const SCHEMA = %(schema)s;

// O n8n nomeia a propriedade binária conforme o campo do multipart, mas o
// nome varia entre versões (boleta, file0, data...). Descobrir em vez de
// assumir evita um 500 sem explicação.
const entrada = $input.first();
const binarios = Object.keys(entrada.binary || {});
if (binarios.length === 0) {
  throw new Error(
    'A requisição chegou sem imagem. Campos JSON recebidos: ' +
    JSON.stringify(Object.keys(entrada.json || {}))
  );
}
const propriedade = binarios.includes('boleta') ? 'boleta' : binarios[0];
// O tipo vem do próprio upload (o app manda PNG), para que outro formato
// funcione sem mexer aqui.
const mime = entrada.binary[propriedade]?.mimeType || 'image/png';

// Dois caminhos para o base64. O helper é o correto quando o n8n guarda o
// binário em disco (o Cloud faz isso acima de certo tamanho) e nem sempre está
// exposto no sandbox do Code node; o campo .data serve quando está em memória.
let base64;
if (typeof this?.helpers?.getBinaryDataBuffer === 'function') {
  const buffer = await this.helpers.getBinaryDataBuffer(0, propriedade);
  base64 = buffer.toString('base64');
} else if (entrada.binary[propriedade] && entrada.binary[propriedade].data) {
  base64 = entrada.binary[propriedade].data;
} else {
  throw new Error(
    'Não consegui ler a imagem da propriedade `' + propriedade +
    '`. Propriedades binárias presentes: ' + JSON.stringify(binarios)
  );
}
if (!base64) {
  throw new Error('A imagem chegou vazia na propriedade `' + propriedade + '`.');
}
const meta = (entrada.json && entrada.json.body) || entrada.json || {};

return [{
  json: {
    source_file: meta.source_file || null,
    page: meta.page ? Number(meta.page) : null,
    position: meta.position ? Number(meta.position) : null,
    image_id: meta.image_id || null,
    payload: {
      model: MODELO,
      temperature: 0,
      max_tokens: 2000,
      response_format: {
        type: 'json_schema',
        json_schema: { name: 'boleta', strict: true, schema: SCHEMA },
      },
      messages: [
        { role: 'system', content: SYSTEM },
        {
          role: 'user',
          content: [
            { type: 'text', text: USER },
            {
              type: 'image_url',
              image_url: { url: `data:${mime};base64,${base64}`, detail: 'high' },
            },
          ],
        },
      ],
    },
  },
}];
""" % {
    "system": json.dumps(SYSTEM, ensure_ascii=False),
    "user": json.dumps(USER, ensure_ascii=False),
    "schema": json.dumps(SCHEMA, ensure_ascii=False, indent=2),
}

RESPONSE_CODE = r"""// Desempacota a resposta do OpenAI no contrato acordado com o app Python.
// Falha vira { error }: o cliente registra um aviso naquela boleta e segue com
// o resto do lote, em vez de derrubar a leitura inteira.
const origem = $('Montar requisicao').first().json;
const identidade = {
  source_file: origem.source_file,
  page: origem.page,
  position: origem.position,
  image_id: origem.image_id,
};

const resposta = $input.first().json;
const conteudo = resposta?.choices?.[0]?.message?.content;

if (!conteudo) {
  const erro = resposta?.error || {};
  const motivo = erro.message || erro.description || 'o modelo não devolveu conteúdo';
  // Status HTTP da OpenAI, quando houver: é o que diz ao app se a falha é
  // passageira (429, 5xx — vale tentar de novo) ou definitiva.
  const status = Number(
    erro.httpCode || erro.status || erro.cause?.status ||
    (String(motivo).match(/\b(4\d\d|5\d\d)\b/) || [])[1]
  ) || null;
  return [{ json: { ...identidade, upstream_status: status, error: `Falha na leitura: ${motivo}` } }];
}

let boleta;
try {
  boleta = JSON.parse(conteudo);
} catch (e) {
  return [{ json: { ...identidade, error: `Resposta do modelo não é JSON: ${e.message}` } }];
}

return [{ json: { ...identidade, boleta } }];
"""

workflow = {
    "name": "Boletas - leitura OCR",
    "nodes": [
        {
            "parameters": {
                "httpMethod": "POST",
                "path": "boletas",
                "authentication": "headerAuth",
                "responseMode": "responseNode",
                "options": {"binaryPropertyName": "boleta"},
            },
            "id": "webhook-boletas",
            "name": "Webhook",
            "type": "n8n-nodes-base.webhook",
            "typeVersion": 2,
            "position": [-320, 0],
            "webhookId": "boletas-ocr",
            "notes": "Recebe UMA boleta ja recortada pelo app. Imagem no campo binario `boleta`.",
            "notesInFlow": True,
        },
        {
            "parameters": {"jsCode": BUILD_CODE},
            "id": "montar-requisicao",
            "name": "Montar requisicao",
            "type": "n8n-nodes-base.code",
            "typeVersion": 2,
            "position": [-80, 0],
            "notes": "Prompt, modelo e JSON Schema ficam aqui.",
            "notesInFlow": True,
        },
        {
            "parameters": {
                "method": "POST",
                "url": "https://api.openai.com/v1/chat/completions",
                "authentication": "predefinedCredentialType",
                "nodeCredentialType": "openAiApi",
                "sendBody": True,
                "specifyBody": "json",
                "jsonBody": "={{ JSON.stringify($json.payload) }}",
                "options": {"timeout": 120000},
            },
            "id": "openai-visao",
            "name": "OpenAI Visao",
            "type": "n8n-nodes-base.httpRequest",
            "typeVersion": 4.2,
            "position": [160, 0],
            "notes": "Structured Outputs garante a forma do JSON. A chave fica na credencial OpenAI.",
            "notesInFlow": True,
            "retryOnFail": True,
            "maxTries": 5,
            "waitBetweenTries": 5000,
            "onError": "continueRegularOutput",
        },
        {
            "parameters": {"jsCode": RESPONSE_CODE},
            "id": "montar-resposta",
            "name": "Montar resposta",
            "type": "n8n-nodes-base.code",
            "typeVersion": 2,
            "position": [400, 0],
        },
        {
            "parameters": {"respondWith": "allIncomingItems", "options": {}},
            "id": "responder",
            "name": "Responder",
            "type": "n8n-nodes-base.respondToWebhook",
            "typeVersion": 1.1,
            "position": [640, 0],
        },
    ],
    "connections": {
        "Webhook": {"main": [[{"node": "Montar requisicao", "type": "main", "index": 0}]]},
        "Montar requisicao": {"main": [[{"node": "OpenAI Visao", "type": "main", "index": 0}]]},
        "OpenAI Visao": {"main": [[{"node": "Montar resposta", "type": "main", "index": 0}]]},
        "Montar resposta": {"main": [[{"node": "Responder", "type": "main", "index": 0}]]},
    },
    "settings": {
        "executionOrder": "v1",
        # Cada execução carrega a imagem e o payload em base64 (~1 MB). Um mês
        # de uma loja são ~800 execuções; guardar as bem-sucedidas enche o
        # armazenamento do n8n Cloud sem servir para nada. Erros continuam salvos.
        "saveDataSuccessExecution": "none",
        "saveDataErrorExecution": "all",
        "saveManualExecutions": False,
        "saveExecutionProgress": False,
    },
    "pinData": {},
}

OUT.write_text(json.dumps(workflow, ensure_ascii=False, indent=2), encoding="utf-8")
JS_OUT.write_text(BUILD_CODE, encoding="utf-8")
JS_RESPOSTA_OUT.write_text(RESPONSE_CODE, encoding="utf-8")
print(f"gerado: {OUT.name} ({OUT.stat().st_size} bytes)")
print(f"gerado: {JS_OUT.name} ({len(BUILD_CODE.splitlines())} linhas, para colar no n8n)")
print("nodes:", " -> ".join(n["name"] for n in workflow["nodes"]))

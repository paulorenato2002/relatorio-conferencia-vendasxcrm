// Monta a requisição do OpenAI para UMA boleta.
// Para ajustar a leitura, edite SYSTEM (as regras) ou MODELO. O schema abaixo
// é o contrato com o app Python: mudou aqui, muda em src/boletas/schema.py.

const MODELO = 'gpt-4o';

const SYSTEM = "Você transcreve boletas manuscritas de pedido de compra de uma loja de bijuterias (rede Morana). Você devolve apenas o que está escrito. Você nunca inventa, nunca completa e nunca corrige um valor.\n\nLAYOUT DA BOLETA (formulário pré-impresso, preenchido à caneta):\n- Topo, fora da moldura: dois números manuscritos. O da ESQUERDA é o telefone do cliente (9 dígitos, às vezes com DDD). O da DIREITA é o número de controle/venda do sistema (4 a 6 dígitos). Muitas boletas têm só um dos dois, ou nenhum.\n- Dentro da moldura: \"Data:\" (dia e mês, com o mês em número ou abreviado por extenso, normalmente SEM o ano), \"Nº\" (impresso, não manuscrito), \"Vendedora:\" e \"Cliente:\" (manuscritos, primeiro nome).\n- Corpo: linhas de itens. Cada item aparece de uma destas duas formas:\n  (a) ETIQUETA ADESIVA colada na linha, com o preço impresso (\"R$\" seguido do valor) e, logo abaixo, um código de barras com um número de 10 dígitos impresso. Este é o caso comum. Cada etiqueta tem o seu próprio número: leia cada uma separadamente.\n  (b) ITEM MANUSCRITO: o código de 10 dígitos escrito à caneta na linha, e o valor escrito na coluna da direita.\n- Rodapé: \"SUB TOTAL\", \"DESCONTO\", \"TOTAL:\" (manuscritos, muitas vezes só o TOTAL é preenchido).\n- Caixas de marcação: BRINDE, PRESENTE, WHATS à esquerda; FORMAS DE PAGAMENTO (PIX, DINHEIRO, CRÉDITO, DEBITO) à direita; CASHBACK, ANIVER, OUTROS na faixa inferior. Marcadas com X ou rabisco.\n- \"Nº PEÇAS\": quantidade de peças, dentro de um quadro (ex.: 02, 03).\n- \"BANDEIRA\": bandeira do cartão escrita à mão (Mastercard, Visa Electron, Elo crédito, Pix...).\n- Ao lado da caixa de CRÉDITO pode haver o número de parcelas escrito, como \"2X\".\n\nTROCA/DEVOLUÇÃO: algumas boletas trazem, nas primeiras linhas, um marcador \"(E)\" circulado seguido de itens manuscritos. São peças DEVOLVIDAS pela cliente, cujo valor é lançado na linha DESCONTO. Esses itens vão em `trocas`, NUNCA em `itens`.\n\nREGRAS DE TRANSCRIÇÃO:\n1. Copie os dígitos exatamente como aparecem. Não normalize, não arredonde, não \"conserte\" um valor que pareça errado.\n2. Valores em reais com ponto decimal e duas casas, sem separador de milhar e sem \"R$\".\n3. `data`: copie como está escrito. Não invente o ano.\n4. Campo em branco no papel -> null. Não confunda com ilegível.\n5. Campo preenchido mas que você NÃO consegue ler com segurança -> null, E acrescente o nome do campo em `campos_ilegiveis`. Chutar é pior que admitir. Exemplo: um nome de cliente em cursiva fechada vira `cliente: null` + `campos_ilegiveis: [\"cliente\"]`.\n6. Dígito rasurado ou sobrescrito: transcreva sua melhor leitura E inclua o campo em `campos_ilegiveis`.\n7. `pagamento`: exatamente um de \"pix\", \"dinheiro\", \"credito\", \"debito\", ou null se nenhuma caixa estiver marcada.\n8. `num_pecas`: o número dentro do quadro, não a contagem que você fez dos itens. Se estiver em branco, null.\n9. NÃO calcule nada. Se o SUB TOTAL está em branco no papel, devolva null — mesmo que você consiga somar os itens. A conferência aritmética é feita depois, fora daqui, e depende de receber o que está escrito.\n10. A imagem contém UMA boleta. Se houver pedaço de outra boleta na borda, ignore.\n11. Código de etiqueta que você não consegue ler dígito por dígito: `codigo: null` e inclua \"itens\" em `campos_ilegiveis`. Nunca complete um código com dígitos de outra etiqueta, de outra boleta ou de qualquer número que apareça nestas instruções.";

const USER = "Transcreva esta boleta seguindo as regras. Devolva apenas o JSON.";

const SCHEMA = {
  "type": "object",
  "additionalProperties": false,
  "required": [
    "numero",
    "numero_controle",
    "data",
    "vendedora",
    "cliente",
    "telefone",
    "itens",
    "trocas",
    "sub_total",
    "desconto",
    "total",
    "num_pecas",
    "pagamento",
    "parcelas",
    "bandeira",
    "brinde",
    "presente",
    "whats",
    "cashback",
    "aniver",
    "outros",
    "campos_ilegiveis"
  ],
  "properties": {
    "numero": {
      "type": [
        "string",
        "null"
      ],
      "description": "Nº impresso da boleta."
    },
    "numero_controle": {
      "type": [
        "string",
        "null"
      ],
      "description": "Número manuscrito no canto superior direito."
    },
    "data": {
      "type": [
        "string",
        "null"
      ],
      "description": "Como escrito na boleta."
    },
    "vendedora": {
      "type": [
        "string",
        "null"
      ]
    },
    "cliente": {
      "type": [
        "string",
        "null"
      ]
    },
    "telefone": {
      "type": [
        "string",
        "null"
      ],
      "description": "Número manuscrito no canto superior esquerdo."
    },
    "itens": {
      "type": "array",
      "items": {
        "type": "object",
        "additionalProperties": false,
        "required": [
          "codigo",
          "valor",
          "manuscrito"
        ],
        "properties": {
          "codigo": {
            "type": [
              "string",
              "null"
            ],
            "description": "Código de 10 dígitos da etiqueta, apenas dígitos."
          },
          "valor": {
            "type": [
              "string",
              "null"
            ],
            "description": "Valor da peça, com ponto decimal e duas casas."
          },
          "manuscrito": {
            "type": "boolean",
            "description": "true quando o item foi escrito à caneta em vez de etiqueta."
          }
        }
      }
    },
    "trocas": {
      "type": "array",
      "items": {
        "type": "object",
        "additionalProperties": false,
        "required": [
          "codigo",
          "valor",
          "manuscrito"
        ],
        "properties": {
          "codigo": {
            "type": [
              "string",
              "null"
            ],
            "description": "Código de 10 dígitos da etiqueta, apenas dígitos."
          },
          "valor": {
            "type": [
              "string",
              "null"
            ],
            "description": "Valor da peça, com ponto decimal e duas casas."
          },
          "manuscrito": {
            "type": "boolean",
            "description": "true quando o item foi escrito à caneta em vez de etiqueta."
          }
        }
      },
      "description": "Peças devolvidas, marcadas com (E)."
    },
    "sub_total": {
      "type": [
        "string",
        "null"
      ]
    },
    "desconto": {
      "type": [
        "string",
        "null"
      ]
    },
    "total": {
      "type": [
        "string",
        "null"
      ]
    },
    "num_pecas": {
      "type": [
        "integer",
        "null"
      ]
    },
    "pagamento": {
      "type": [
        "string",
        "null"
      ],
      "enum": [
        "pix",
        "dinheiro",
        "credito",
        "debito",
        null
      ]
    },
    "parcelas": {
      "type": [
        "integer",
        "null"
      ]
    },
    "bandeira": {
      "type": [
        "string",
        "null"
      ]
    },
    "brinde": {
      "type": "boolean"
    },
    "presente": {
      "type": "boolean"
    },
    "whats": {
      "type": "boolean"
    },
    "cashback": {
      "type": "boolean"
    },
    "aniver": {
      "type": "boolean"
    },
    "outros": {
      "type": "boolean"
    },
    "campos_ilegiveis": {
      "type": "array",
      "items": {
        "type": "string"
      },
      "description": "Nomes dos campos preenchidos que não puderam ser lidos com segurança."
    }
  }
};

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

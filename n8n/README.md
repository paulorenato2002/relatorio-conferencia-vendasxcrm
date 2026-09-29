# Leitura das boletas no n8n

O app recorta cada boleta do scan e manda **uma boleta por requisição** para
este workflow, que chama o modelo de visão e devolve a transcrição em JSON.

```
app.py  ──POST multipart──►  Webhook
                             Montar requisicao   (prompt + JSON Schema)
                             OpenAI Visao        (credencial OpenAI)
                             Montar resposta
        ◄──── JSON ────────  Responder
```

## Por que uma boleta por requisição

O payload fica pequeno, cada boleta tem retentativa própria, o progresso
aparece na tela conforme cada uma volta, e uma imagem ruim não derruba o lote.

O custo disso é uma execução do n8n por boleta. Um mês de uma loja são ~800
boletas, então ~800 execuções — duas lojas, ~1.600. Confira a cota mensal do
seu plano. O app guarda cada leitura (`.cache/leituras/`), então repetir o mesmo
mês não gasta execução de novo.

## Ajustes para volume

Três ajustes no workflow, feitos depois de medir um mês inteiro. Nenhum é
obrigatório para o app funcionar — ele já trata rate limit pelo texto do erro —,
mas os três reduzem custo e falha num lote grande.

1. **Não guardar execuções bem-sucedidas.** `Workflow` → `Settings` →
   *Save successful production executions*: **Do not save**. Cada execução
   carrega a imagem e o payload em base64 (~1 MB); guardar 800 por mês enche o
   armazenamento do n8n Cloud sem servir para nada. As com erro continuam salvas.

2. **Mais retentativas dentro da mesma execução.** Node `OpenAI Visao` → aba
   `Settings` → *Retry On Fail*: **Max Tries 5**, **Wait Between Tries 5000**.
   Retentar aqui não gasta execução nova do plano; retentar do lado do app gasta.

3. **Status de origem no erro.** Cole o conteúdo de `montar-resposta.js` no node
   `Montar resposta`. Ele passa a devolver o status HTTP da OpenAI
   (`upstream_status`), e o app decide com certeza se vale tentar de novo em vez
   de depender do texto da mensagem.

Opcional: `montar-requisicao.js` no node `Montar requisicao` passa a respeitar o
tipo da imagem enviada em vez de assumir PNG. O app manda PNG, então hoje não
muda nada.

Concorrência medida na conta da Rezende: 8 em paralelo, 95 boletas em 58 s, sem
rate limit. Configurado no `.env` do app (`N8N_BOLETAS_CONCURRENCY`).

## Instalação

1. **Importar o workflow.** No n8n Cloud: `Workflows` → `...` → `Import from
   File` → selecione `workflow.json`.

2. **Credencial da OpenAI.** Abra o node `OpenAI Visao` → campo `Credential to
   connect with` → `Create new credential` → cole a chave da OpenAI. O node usa
   o tipo `OpenAi API` mesmo sendo um HTTP Request: a chave nunca aparece no
   workflow nem no app.

3. **Credencial do webhook.** Abra o node `Webhook` → `Authentication` já está
   em `Header Auth` → `Create new credential`:
   - `Name`: `X-Boletas-Token`
   - `Value`: gere um token qualquer, ex.: `openssl rand -hex 24`

4. **Ativar.** Botão `Active` no canto superior direito. Sem isso a URL de
   produção responde 404.

5. **Copiar a URL.** No node `Webhook`, aba `Production URL`. Deve terminar em
   `/webhook/boletas`. A URL de *Test* não serve para o uso normal: ela só
   responde enquanto você está com o `Listen for test event` ligado.

6. **Configurar o app.** Na raiz do projeto, copie `.env.example` para `.env` e
   preencha `N8N_BOLETAS_WEBHOOK_URL` e `N8N_BOLETAS_TOKEN` (o mesmo valor do
   passo 3).

## Teste rápido

Com uma imagem de boleta recortada à mão:

```bash
curl -X POST "$N8N_BOLETAS_WEBHOOK_URL" \
  -H "X-Boletas-Token: $N8N_BOLETAS_TOKEN" \
  -F "boleta=@uma-boleta.png" \
  -F "source_file=teste.pdf" -F "page=1" -F "position=1" -F "image_id=teste"
```

Resposta esperada: um objeto com `boleta` dentro, ou `error` com o motivo.

## Contrato com o app

O JSON Schema no node `Montar requisicao` é o contrato. O app espera estes
campos e valida o resultado em `src/boletas/schema.py`.

| Campo | Observação |
|---|---|
| `numero` | Nº impresso na boleta |
| `numero_controle` | número manuscrito no canto superior direito |
| `data` | como escrito, ex.: `"22/09"` — **sem ano** |
| `vendedora`, `cliente`, `telefone` | manuscritos |
| `itens[]` | `{codigo, valor, manuscrito}` — código de 10 dígitos da etiqueta |
| `trocas[]` | peças devolvidas, marcadas com `(E)` na boleta |
| `sub_total`, `desconto`, `total` | como escritos; `null` quando em branco |
| `num_pecas` | o número dentro do quadro |
| `pagamento` | `pix` / `dinheiro` / `credito` / `debito` / `null` |
| `parcelas`, `bandeira` | do cartão |
| `brinde`, `presente`, `whats`, `cashback`, `aniver`, `outros` | caixas marcadas |
| `campos_ilegiveis[]` | campos preenchidos no papel que não deram para ler |

**Mudou o schema aqui, mude em `src/boletas/schema.py`.** Os dois lados
precisam concordar.

### O modelo não calcula nada

O prompt proíbe o modelo de somar, arredondar ou "consertar" um valor que
pareça errado. Se o `SUB TOTAL` está em branco no papel, ele devolve `null`
mesmo conseguindo somar os itens.

Isso é deliberado. A conferência acontece depois, no Python: soma dos itens
contra `SUB TOTAL`, `SUB TOTAL - DESCONTO` contra `TOTAL`, `Nº PEÇAS` contra a
quantidade de itens lidos, e soma das trocas contra `DESCONTO`. Se o modelo
preenchesse as lacunas, essas checagens passariam sempre e não valeriam nada —
uma leitura errada entraria no relatório sem ninguém perceber.

Boleta que não fecha sozinha aparece na tela na fila de revisão.

## Ajustes

- **Trocar de modelo**: constante `MODELO` no node `Montar requisicao`.
- **Melhorar a leitura**: constante `SYSTEM`, no mesmo node. As regras de
  layout vieram da leitura das boletas reais da loja; se o bloco de boletas
  mudar de formato, é esse texto que precisa acompanhar.
- **`detail: 'high'`** é obrigatório para manuscrito. Em `low` o modelo recebe
  uma miniatura e a leitura de números à caneta degrada muito.

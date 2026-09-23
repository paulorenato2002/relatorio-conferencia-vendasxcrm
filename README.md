# Conferência de vendas - Rezende e L2H

Aplicação local em Python/Streamlit para conferir, por dia, as vendas do CRM Morana, os valores de PIX e dinheiro dos fechamentos de caixa e as vendas de cartão aprovadas na Rede.

Todo o processamento ocorre em memória durante a sessão do Streamlit. A aplicação não usa banco de dados nem autenticação, e não grava os uploads.

**Exceção: as boletas escaneadas.** Elas são manuscritas, então a leitura é
feita por um modelo de visão, através de um workflow no n8n. Quando essa etapa
é acionada, as imagens das boletas saem da máquina: vão para o n8n Cloud e de
lá para a OpenAI. Nenhum outro arquivo do fluxo sai daqui, e a etapa só roda
quando o operador clica em `Ler boletas no n8n` — enviar boletas na tela não
dispara nada sozinho. Detalhes em [`n8n/README.md`](n8n/README.md).

Planilhas e PDFs operacionais não são versionados. Cada usuário fornece seus
arquivos diretamente na interface durante a sessão.

## Instalação

Requer Python 3.11 ou superior.

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
```

## Execução

Use preferencialmente o lançador `executar_app.bat` (duplo clique) ou execute o
Python da `.venv` explicitamente. Isso evita conflitos quando há mais de uma
versão do Python instalada no Windows.

```powershell
.\.venv\Scripts\python.exe -m streamlit run app.py
```

Na tela:

1. Selecione `Rezende` ou `L2H` e informe o período exato.
2. Envie o XLSX do CRM, o XLSX da Rede e os PDFs diários de fechamento recebidos.
3. Clique em `Validar arquivos` e corrija qualquer erro bloqueante.
4. Opcional: envie as boletas escaneadas e clique em `Ler boletas no n8n`.
5. Clique em `Processar conferência`.
6. Revise/edite as observações e baixe o PDF final.

## Boletas escaneadas

As boletas são o registro manuscrito do balcão. Configuração do n8n em
[`n8n/README.md`](n8n/README.md); a URL e o token do webhook vão no `.env`
(modelo em `.env.example`).

O caminho é: recortar uma boleta por imagem (`src/boletas/render.py`), mandar
para o n8n (`src/boletas/client.py`), conferir a boleta contra ela mesma e
converter para os tipos do projeto (`src/boletas/schema.py`).

**Recorte.** Os scans trazem de duas a três boletas lado a lado. O recorte usa
projeção de coluna — boletas são separadas por faixas verticais sem tinta — e
não envolve aprendizado de máquina. O menor lado do recorte é limitado a 768px,
que é onde o serviço de visão corta de qualquer forma: enviar mais é banda
gasta em pixels descartados.

**Auto-conferência.** O modelo é proibido de calcular: campo em branco no papel
volta como `null`, mesmo quando daria para somar. Isso é o que permite conferir
a leitura depois, no Python:

```text
soma dos itens          = SUB TOTAL
SUB TOTAL - DESCONTO    = TOTAL
quantidade de itens     = Nº PEÇAS
soma das trocas         = DESCONTO
```

Boleta que não fecha, ou que tem campo preenchido e ilegível, aparece na fila
de revisão da tela em vez de entrar calada no relatório.

**Código de barras.** A etiqueta imprime 10 dígitos; o campo `codigo` do CRM
guarda os mesmos dígitos com zeros à esquerda até 13. `barcode_to_crm_code()`
faz a conversão, que é o que liga a peça da boleta à linha do CRM.

**Custo.** A leitura é a única etapa paga e roda só no clique. O resultado fica
em cache pelos arquivos enviados: trocar empresa ou período reaproveita a
transcrição, sem reenviar as imagens.

## Regra de cálculo

Todos os valores são convertidos para centavos antes dos cálculos.

```text
Total CRM - PIX do caixa - dinheiro do caixa = total esperado em cartão
Total esperado em cartão - crédito/débito aprovado na Rede = diferença
```

Diferença igual a zero centavos gera `OK`; qualquer outro valor, inclusive um centavo, gera `DIVERGÊNCIA`.

O CRM usa exclusivamente `valor_base_calculo_comissao` e não filtra o campo `tipo`. A Rede usa exclusivamente `valor da venda original`, status `aprovada` e modalidades `crédito`/`débito`. O PIX da Rede é apenas diagnóstico auxiliar; PIX e dinheiro sempre vêm dos PDFs.

## Identificadores empresariais

Aliases, razões sociais, CNPJs e filiais ficam centralizados em `src/config.py`.

- Rezende: filial CRM `00353`; CNPJ Rede `18.547.721/0001-81`.
- L2H: razão social `L2H BIJUTERIAS E ACESSORIOS FEMININOS LTDA`.
- Fechamento Rezende: filial `MORANA ASA NORTE BSB`.
- Fechamento L2H: filial `MORANA JARDIM BOTANICO SHOPPING`.

As amostras não informam a filial ou o CNPJ da L2H. Quando esses identificadores forem conhecidos, basta acrescentá-los no mesmo arquivo de configuração. Arquivos identificados explicitamente como pertencentes à outra empresa são sempre bloqueados.

## Validações importantes

- Cabeçalhos de CRM e Rede são localizados mesmo fora da primeira linha e normalizados quanto a acentos, espaços e maiúsculas.
- Datas brasileiras são interpretadas com dia antes do mês; datas ISO, células Excel e seriais Excel também são aceitos.
- PDF sem texto extraível, sem formato reconhecível ou sem total gera erro.
  Quando as linhas `PIX` ou `DINHEIRO` não aparecem, a forma de pagamento é
  considerada sem movimento (`R$ 0,00`) e a validação exibe um aviso. Se a linha
  existir, mas o saldo estiver ilegível, o processamento é bloqueado.
- Fechamento ausente não é tratado como zero: o dia fica `PENDENTE`, os campos de
  PIX/dinheiro/cálculo permanecem não informados e o PDF solicita o reenvio do fechamento.
- PDF duplicado com os mesmos valores é ignorado com aviso; valores conflitantes bloqueiam.
- Datas fora do período e presenças/ausências entre fontes são mostradas na validação.

## Testes

```powershell
pytest -q
```

Os testes cobrem os parsers com os arquivos fornecidos, datas e valores, empresas incompatíveis, fechamento ausente/duplicado, divergência de um centavo, totalização e geração do PDF A4 retrato.

As validações específicas das amostras locais são executadas quando os arquivos
estão disponíveis. No GitHub Actions, elas são ignoradas de forma explícita para
que nenhum dado operacional precise ser publicado; os demais testes continuam
sendo executados automaticamente em cada push e pull request.

# Conferência de vendas - Rezende e L2H

Aplicação local em Python/Streamlit para conferir, por dia, as vendas do CRM Morana, os valores de PIX e dinheiro dos fechamentos de caixa e as vendas de cartão aprovadas na Rede.

O processamento ocorre em memória durante a sessão do Streamlit. A aplicação não usa banco de dados nem autenticação, e não grava os arquivos enviados. A única coisa gravada em disco é a transcrição das boletas lidas, em `.cache/leituras/` (fora do git) — ver abaixo.

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
5. Confira o que foi lido, corrija na tabela o que estiver errado e clique em
   `Aprovar boletas`. Sem essa aprovação a conferência não processa.
6. Clique em `Processar conferência`.
7. Revise/edite as observações e baixe o PDF final.

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

**Correção e aprovação.** Apontar o problema sem deixar corrigir não resolve: o
operador tem o papel na mão. A tela traz uma tabela editável com os campos
manuscritos — data, vendedora, cliente, número, peças, totais, pagamento e
bandeira. Código de barras e valor de etiqueta ficam de fora: são impressos, e
o erro de um dígito o cruzamento com o CRM identifica sozinho.

A edição é aplicada sobre a transcrição crua e passa de novo por `build_boletas`,
ou seja, pelas mesmas checagens da leitura automática. Corrigir um total para
outro valor errado não silencia o alerta; corrigir a data faz a boleta voltar a
ser comparada com as vizinhas. A conferência só processa depois de `Aprovar
boletas`, e a aprovação cai sozinha se qualquer campo mudar depois.

**Data fora do consenso.** A data é o único campo importante sem conferência
possível dentro da própria boleta. Quando uma data aparece uma única vez no
arquivo e outras três ou mais concordam em outro dia, a boleta é marcada e fica
**fora do cruzamento** até ser corrigida — cruzá-la no dia errado produziria
acusações de venda não registrada que são erro de leitura, não da loja.

**Código de barras.** A etiqueta imprime 10 dígitos; o campo `codigo` do CRM
guarda os mesmos dígitos com zeros à esquerda até 13. `barcode_to_crm_code()`
faz a conversão, que é o que liga a peça da boleta à linha do CRM.

### Lote de um mês

Medido com as boletas da Rezende de 01 a 27/09/2026: 63 PDFs, 332 páginas,
**799 boletas**. A primeira versão tentava ler tudo dentro do clique do botão e
travava. Três coisas mudaram.

**Leitura em segundo plano** (`src/boletas/job.py`). O Streamlit reexecuta o
script a cada clique na tela, interrompendo a execução em curso; uma leitura de
25 minutos dentro do clique era morta pelo primeiro clique em qualquer outro
lugar e levava junto o que já tinha sido lido. Agora ela roda numa thread
própria e a tela só acompanha, com progresso e tempo restante. Pode mexer no
resto da tela, fechar a aba e voltar — reenviar os mesmos arquivos reencontra a
leitura.

**Leituras guardadas** (`src/boletas/cache.py`). Cada boleta lida é gravada em
`.cache/leituras/` assim que volta, pelo conteúdo do arquivo (não pelo nome).
Interrompeu, ler de novo continua de onde parou; o mesmo arquivo nunca é pago
duas vezes. Arquivos idênticos no mesmo envio — o `21.09` e o `21.09 (1)` que o
navegador gera — são lidos uma vez só e avisados, senão as peças entrariam em
dobro no cruzamento. A transcrição inclui nome e telefone de cliente; a tela
tem um botão para apagar tudo, e `Reler tudo` força nova leitura depois de
mudar prompt ou modelo no n8n.

**Recorte e envio encadeados.** O recorte gera uma boleta por vez e o envio
começa na primeira; a memória fica no que está em trânsito. Recortar os 27 dias
caiu de 308 s para 128 s (a imagem da página ia para PNG e voltava sem
necessidade), e a primeira boleta sai em 0,4 s em vez de depois de tudo
recortado. O formato continua PNG: JPEG foi medido e errou a vendedora cursiva
3x mais.

| 8 em paralelo | resultado |
|---|---|
| 95 boletas de um dia cheio | 58 s, nenhum rate limit |
| vazão | 1,6 boleta/s |
| um mês de uma loja | ~8 min |

Rate limit da OpenAI é esperado num lote desse tamanho e é tentado de novo com
espera crescente, em vez de virar boleta perdida. Configuração em `.env`
(`N8N_BOLETAS_CONCURRENCY`) e ajustes do lado do n8n em
[`n8n/README.md`](n8n/README.md).

### O que um mês inteiro revelou

As boletas de referência (duas vendedoras, 7 boletas) não mostravam o jeito
como as outras preenchem. Com as 783 do mês:

- **Data com mês por extenso** — `05/SET`, `19 SET. 2026`, `9 SET`. O parser só
  entendia `05/09` e **descartava a boleta inteira**: 208 sumiam do relatório.
  Hoje entende os dois, e data que não dá para interpretar deixa a boleta sem
  data em vez de derrubá-la.
- **Data pelo nome do arquivo.** A loja salva `Boletas Leide 05.09.pdf`; o nome
  bateu com a data escrita em 648 boletas e divergiu em 33, quase todas o
  modelo comendo um dígito (`1 SET` num arquivo de 21/09). Vale a data do
  arquivo, a boleta que diverge vai para revisão mostrando as duas, e o que for
  digitado na tela vale mais que ambas. Cobre também as 99 boletas sem data
  escrita.
- **Desconto em porcentagem** — `10%`, `15%`. Era lido como R$ 10,00, sem
  aviso. Hoje incide sobre o SUB TOTAL (ou a soma das peças), com um centavo de
  tolerância para o arredondamento do caixa.
- **Código com um dígito a menos.** 58 códigos lidos com 9 dígitos, nenhum no
  CRM, onde todos têm 10. O cruzamento passou a reconhecer dígito faltando, além
  de trocado, como erro de leitura.
- **Veredito por dia.** Qualquer boleta em revisão travava o dia em REVISAR, e
  os 22 dias saíam assim. Hoje só trava o que pode esconder peça (Nº PEÇAS que
  não bate, peça sem valor); total manuscrito que não fecha não mexe no
  casamento das etiquetas impressas.

Resultado no mesmo lote, sem nenhuma leitura nova: 783 de 783 boletas no
relatório (eram 570), 1.159 peças casadas com o CRM (eram 690), 415 "só no CRM"
(eram 993).

**O que ainda é ruído.** Das peças "só na boleta", 219 têm uma de mesmo valor no
mesmo dia entre as "só no CRM", 89 delas a dois dígitos de distância: é o modelo
errando o número impresso da etiqueta. A tolerância não foi estendida a dois
dígitos porque variantes do mesmo produto podem ter códigos próximos e o mesmo
preço, e casar errado esconderia divergência real. O caminho medido é ler as
barras: um decodificador de código de barras (formato ITF) acertou 15 de 15
etiquetas de referência e todas as 711 que conseguiu ler no mês existem no CRM,
mas só acha 45% das etiquetas a 200 DPI.

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

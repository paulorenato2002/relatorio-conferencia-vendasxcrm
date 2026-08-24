# Conferência de vendas - Rezende e L2H

Aplicação local em Python/Streamlit para conferir, por dia, as vendas do CRM Morana, os valores de PIX e dinheiro dos fechamentos de caixa e as vendas de cartão aprovadas na Rede.

Todo o processamento ocorre em memória durante a sessão do Streamlit. A aplicação não usa banco de dados, autenticação, API externa ou inteligência artificial, e não grava os uploads.

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
4. Clique em `Processar conferência`.
5. Revise/edite as observações e baixe o PDF final.

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

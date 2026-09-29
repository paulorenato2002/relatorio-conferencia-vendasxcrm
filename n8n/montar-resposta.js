// Desempacota a resposta do OpenAI no contrato acordado com o app Python.
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

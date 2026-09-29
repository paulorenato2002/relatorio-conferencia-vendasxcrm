from __future__ import annotations

from datetime import date, datetime
import hashlib

import pandas as pd
import streamlit as st

from src.boletas import BoletaClientError, N8nConfig, build_boletas
from src.boletas.job import (
    STATUS_CANCELLED,
    STATUS_PREPARING,
    LeituraJob,
    UploadedBatchFile,
    default_cache,
    running_jobs,
)
from src.boletas.edicao import (
    PAGAMENTOS,
    files_signature,
    frames_from_raw,
    raw_from_frames,
    signature,
)
from src.crosscheck import NOTE_KINDS, crosscheck
from src.diagnostics import suggest_observations
from src.formatters import format_brl_cents, format_brl_currency, format_date_br
from src.parsers import (
    CashPdfParseError,
    CrmParseError,
    RedeParseError,
    parse_cash_pdfs,
    parse_crm,
    parse_rede,
)
from src.pdf_report import generate_pdf_report
from src.reconciliation import reconcile
from src.validation import validate_inputs


st.set_page_config(
    page_title="Conferência de Vendas",
    page_icon="✓",
    layout="wide",
    initial_sidebar_state="collapsed",
)

st.markdown(
    """
    <style>
    .block-container {max-width: 1220px; padding-top: 2rem; padding-bottom: 3rem;}
    h1, h2, h3 {color: #243447;}
    [data-testid="stMetric"] {background: #f6f8fa; border: 1px solid #e2e8f0; padding: 14px; border-radius: 8px;}
    .validation-title {font-size: 0.9rem; color: #5c6b7a; text-transform: uppercase; letter-spacing: .05em;}
    </style>
    """,
    unsafe_allow_html=True,
)


def _fingerprint(company, start_date, end_date, crm_file, rede_file, cash_files) -> str | None:
    if not crm_file or not rede_file:
        return None
    digest = hashlib.sha256()
    digest.update(f"{company}|{start_date.isoformat()}|{end_date.isoformat()}".encode())
    for upload in [crm_file, rede_file, *(cash_files or [])]:
        digest.update(upload.name.encode("utf-8", errors="replace"))
        digest.update(upload.getvalue())
    return digest.hexdigest()


def _streamlit_secrets() -> dict[str, str]:
    """`st.secrets` estoura quando não existe secrets.toml. Ausência não é erro."""
    try:
        return {key: str(value) for key, value in st.secrets.items()}
    except Exception:
        return {}


_MARKDOWN_ESPECIAIS = ("\\", "$", "*", "_", "`")


def _md(texto: object) -> str:
    """Texto dinâmico seguro para `st.markdown`.

    O markdown do Streamlit lê o trecho entre dois `$` como fórmula: um aviso
    como "SUB TOTAL (R$ 129,80) difere do TOTAL (R$ 116,80)" saía com os `R$`
    sumidos e o miolo em fonte de fórmula.
    """
    resultado = str(texto)
    for caractere in _MARKDOWN_ESPECIAIS:
        resultado = resultado.replace(caractere, "\\" + caractere)
    return resultado


def _duracao(segundos: float | None) -> str:
    if segundos is None:
        return "calculando"
    minutos, segundos = divmod(int(segundos), 60)
    if minutos >= 60:
        horas, minutos = divmod(minutos, 60)
        return f"{horas}h{minutos:02d}"
    return f"{minutos} min {segundos:02d} s" if minutos else f"{segundos} s"


@st.fragment(run_every=2)
def _acompanhar_leitura(fingerprint: str) -> None:
    """Progresso da leitura, atualizado a cada 2 s sem reexecutar a página."""
    job = running_jobs().get(fingerprint)
    if job is None:
        return
    snap = job.snapshot()
    if snap.finished:
        st.rerun()
        return

    if snap.status == STATUS_PREPARING:
        st.progress(0.0, text=f"Preparando {snap.total_files} arquivo(s)...")
    else:
        texto = f"{snap.done} de ~{snap.estimated_total} boletas lidas"
        if job.cancelled:
            texto = "Interrompendo — esperando as boletas que já estavam no n8n..."
        st.progress(snap.progress, text=texto)
    colunas = st.columns(4)
    colunas[0].metric("Páginas recortadas", f"{snap.pages_done}/{snap.total_pages}")
    colunas[1].metric("Já estavam guardadas", snap.from_cache)
    colunas[2].metric("Falharam", snap.failed)
    colunas[3].metric("Tempo restante", _duracao(snap.eta))
    st.caption(
        "A leitura roda em segundo plano: pode mexer no resto da tela. Cada boleta "
        "é guardada assim que volta; se algo interromper, ler de novo continua "
        "de onde parou."
    )
    if not job.cancelled and st.button("Interromper leitura", key="interromper_leitura"):
        job.cancel()


def _boletas_dataframe(data) -> pd.DataFrame:
    rows = []
    for boleta in data.boletas:
        pagamento = boleta.payment_method or "—"
        if boleta.payment_method == "credito" and boleta.installments:
            pagamento = f"crédito {boleta.installments}x"
        rows.append(
            {
                "Arquivo": f"{boleta.source_file} (p{boleta.page}/{boleta.position})",
                "Nº": boleta.numero or "—",
                "Data": format_date_br(boleta.date) if boleta.date else "Não lida",
                "Vendedora": boleta.seller or "—",
                "Cliente": boleta.client or "—",
                "Peças": boleta.piece_count if boleta.piece_count is not None else "—",
                "Itens lidos": len(boleta.items),
                "Total": format_brl_cents(boleta.total_cents) if boleta.total_cents is not None else "Não lido",
                "Pagamento": pagamento,
                "Bandeira": boleta.card_brand or "—",
                "Conferência": "Revisar" if boleta.needs_review else "OK",
            }
        )
    return pd.DataFrame(rows)


def _crosscheck_dataframe(report) -> pd.DataFrame:
    frame = pd.DataFrame(
        [
            {
                "Data": format_date_br(row.date),
                "Boletas": row.boleta_count,
                "Fora do cruzamento": row.excluded_boletas,
                "Vendas CRM": row.sale_count,
                "Casadas": row.matched_sales,
                "Venda sem boleta": row.sales_without_boleta,
                "Boleta sem venda": row.boletas_without_sale,
                "Peças que não fecham": row.piece_divergences,
                "Valor CRM": format_brl_cents(row.crm_value_cents),
                "Diferença": format_brl_cents(row.difference_cents),
                "Status": row.status,
            }
            for row in report.rows
        ]
    )
    if not report.totals.get("excluded_boletas"):
        frame = frame.drop(columns=["Fora do cruzamento"])
    return frame


def _discrepancy_dataframe(items) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "Data": format_date_br(item.date),
                "Tipo": item.label,
                "Venda CRM": item.sale_number or "—",
                "Boleta": item.boleta_numero or (item.boleta_id or "—"),
                "Vendedora": item.seller or "—",
                "Código": item.codigo or "—",
                "Diferença": format_brl_cents(item.impact_cents) if item.impact_cents else "—",
                "Detalhe": item.detail,
            }
            for item in items
        ]
    )


def _daily_dataframe(report) -> pd.DataFrame:
    records = []
    for row in report.rows:
        record = {"Data": format_date_br(row.date)}
        record.update(
            {seller: format_brl_cents(row.sellers_cents.get(seller, 0)) for seller in report.sellers}
        )
        record.update(
            {
                "Total CRM": format_brl_cents(row.total_crm_cents),
                "PIX do caixa": "Não informado" if row.pix_cash_cents is None else format_brl_cents(row.pix_cash_cents),
                "Dinheiro do caixa": "Não informado" if row.cash_cents is None else format_brl_cents(row.cash_cents),
                "Total Sistema C/D": "Não calculado" if row.expected_card_cents is None else format_brl_cents(row.expected_card_cents),
                "Total Rede": format_brl_cents(row.rede_cents),
                "Diferença": "Não calculada" if row.difference_cents is None else format_brl_cents(row.difference_cents),
                "Status": row.status,
            }
        )
        records.append(record)
    return pd.DataFrame(records)


def _observation_dataframe(report, suggestions) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "Data": format_date_br(row.date),
                "Status": row.status,
                "Observação": suggestions.get(row.date, ""),
            }
            for row in report.rows
        ]
    )


st.title("Conferência de vendas")
st.caption(
    "CRM Morana × fechamentos diários de caixa × Rede. "
    "Os arquivos são processados somente na memória desta sessão."
)

with st.container(border=True):
    col_company, col_start, col_end = st.columns([1.15, 1, 1])
    with col_company:
        company = st.selectbox("Empresa", ["Rezende", "L2H"])
    with col_start:
        start_date = st.date_input("Data inicial", value=date.today(), format="DD/MM/YYYY")
    with col_end:
        end_date = st.date_input("Data final", value=date.today(), format="DD/MM/YYYY")

    col_crm, col_rede, col_cash, col_boletas = st.columns(4)
    with col_crm:
        crm_file = st.file_uploader("CRM Morana (.xlsx)", type=["xlsx"], key="crm")
    with col_rede:
        rede_file = st.file_uploader("Relatório Rede (.xlsx)", type=["xlsx"], key="rede")
    with col_cash:
        cash_files = st.file_uploader(
            "Fechamentos de caixa (.pdf)", type=["pdf"], accept_multiple_files=True, key="cash"
        )
    with col_boletas:
        boleta_files = st.file_uploader(
            "Boletas escaneadas",
            type=["pdf", "png", "jpg", "jpeg", "webp", "tif", "tiff"],
            accept_multiple_files=True,
            key="boletas",
            help="Scans com várias boletas por página são recortados automaticamente.",
        )

    validate_clicked = st.button("Validar arquivos", type="primary", width="stretch")

current_fingerprint = _fingerprint(company, start_date, end_date, crm_file, rede_file, cash_files)

if validate_clicked:
    st.session_state.pop("processed", None)
    st.session_state.pop("observations", None)
    if not crm_file or not rede_file:
        st.session_state["validation"] = {
            "fingerprint": current_fingerprint,
            "error": "Envie os arquivos do CRM e da Rede. PDFs de caixa ausentes serão marcados como pendentes.",
        }
    elif start_date > end_date:
        st.session_state["validation"] = {
            "fingerprint": current_fingerprint,
            "error": "A data inicial deve ser menor ou igual à data final.",
        }
    else:
        try:
            with st.spinner("Lendo e validando os arquivos..."):
                crm_data = parse_crm(crm_file)
                rede_data = parse_rede(rede_file)
                cash_data = parse_cash_pdfs(cash_files or [])
                validation = validate_inputs(
                    company, start_date, end_date, crm_data, rede_data, cash_data
                )
            st.session_state["validation"] = {
                "fingerprint": current_fingerprint,
                "crm": crm_data,
                "rede": rede_data,
                "cash": cash_data,
                "result": validation,
            }
        except (CrmParseError, RedeParseError, CashPdfParseError, ValueError) as exc:
            st.session_state["validation"] = {
                "fingerprint": current_fingerprint,
                "error": str(exc),
            }

validation_state = st.session_state.get("validation")
validation_is_current = bool(
    validation_state
    and current_fingerprint
    and validation_state.get("fingerprint") == current_fingerprint
)

st.subheader("Validação")
if not validation_is_current:
    st.info("Selecione o período, envie os três tipos de arquivo e clique em “Validar arquivos”.")
elif validation_state.get("error"):
    st.error(validation_state["error"])
else:
    validation = validation_state["result"]
    if validation.errors:
        for message in validation.errors:
            st.error(message)
    else:
        st.success("Arquivos validados. A conferência pode ser processada.")
    for message in validation.warnings:
        st.warning(message)

    crm_data = validation_state["crm"]
    rede_data = validation_state["rede"]
    cash_data = validation_state["cash"]
    summary_cols = st.columns(3)
    summary_cols[0].metric("CRM", f"{crm_data.record_count} registros")
    summary_cols[1].metric("Rede", f"{rede_data.record_count} transações")
    summary_cols[2].metric("Fechamentos únicos", len(cash_data.closings))
    with st.expander("Detalhes da validação"):
        st.write(
            {
                "Datas no CRM": [format_date_br(day) for day in sorted(validation.source_dates.get("CRM", set()))],
                "Datas na Rede": [format_date_br(day) for day in sorted(validation.source_dates.get("Rede", set()))],
                "Datas nos fechamentos": [format_date_br(day) for day in sorted(validation.source_dates.get("Caixa", set()))],
                "Filiais CRM": sorted(crm_data.branch_codes),
                "CNPJs Rede": sorted(rede_data.cnpjs),
            }
        )

st.subheader("Boletas")
boletas_data = None
boletas_fingerprint = files_signature(boleta_files)
# Chave diferente do `key="boletas"` do uploader de propósito: o Streamlit
# grava o valor de cada widget no session_state sob a chave do widget, e
# reaproveitar o nome faz a lista de arquivos sobrescrever este cache.
boletas_state = st.session_state.get("boletas_leitura")
boletas_are_current = bool(
    boletas_state and boletas_fingerprint and boletas_state.get("fingerprint") == boletas_fingerprint
)

job = running_jobs().get(boletas_fingerprint) if boletas_fingerprint else None

# Leitura terminou numa execução anterior do script: passa o resultado para a
# sessão, onde o resto da tela (correção, aprovação, cruzamento) o encontra.
if job is not None and job.snapshot().finished and job.snapshot().status != STATUS_CANCELLED:
    snap = job.snapshot()
    st.session_state["boletas_leitura"] = {
        "fingerprint": boletas_fingerprint,
        "raw": job.results(),
        "warnings": snap.warnings,
        "failed": snap.failed,
        "duplicates": snap.duplicates,
        "file_names": job.file_names(),
        "stats": {"total": snap.done, "from_cache": snap.from_cache, "elapsed": snap.elapsed},
    }
    running_jobs().pop(boletas_fingerprint, None)
    job = None
    boletas_state = st.session_state["boletas_leitura"]
    boletas_are_current = True

if not boleta_files:
    st.info(
        "Envie as boletas escaneadas para incluí-las na conferência. "
        "A leitura é feita pelo n8n e roda sob demanda."
    )
elif job is not None and not job.snapshot().finished:
    _acompanhar_leitura(boletas_fingerprint)
else:
    if job is not None and job.snapshot().status == STATUS_CANCELLED:
        snap = job.snapshot()
        st.warning(
            f"Leitura interrompida com {snap.done} boleta(s) lidas. O que já foi lido "
            "está guardado: ler de novo continua de onde parou."
        )
        running_jobs().pop(boletas_fingerprint, None)

    falhas = (boletas_state or {}).get("failed", 0) if boletas_are_current else 0
    if boletas_are_current and falhas:
        rotulo = f"Tentar de novo as {falhas} boleta(s) que falharam"
    else:
        rotulo = "Ler boletas no n8n"
    col_ler, col_reler = st.columns([4, 1])
    with col_reler:
        reler = st.checkbox(
            "Reler tudo",
            help="Ignora as leituras já guardadas e manda todas as boletas de novo. "
            "Use depois de mudar o prompt ou o modelo no n8n.",
        )
    with col_ler:
        read_clicked = st.button(
            rotulo,
            width="stretch",
            disabled=boletas_are_current and not falhas and not reler,
            help="Já lido. Marque “Reler tudo” para ler de novo." if boletas_are_current else None,
        )
    if read_clicked:
        try:
            config = N8nConfig.from_env(_streamlit_secrets())
        except BoletaClientError as exc:
            st.error(str(exc))
        else:
            arquivos = [UploadedBatchFile(item.name, item.getvalue()) for item in boleta_files]
            running_jobs()[boletas_fingerprint] = LeituraJob(
                arquivos, config, default_cache(), ignore_cache=reler
            ).start()
            st.session_state.pop("boletas_leitura", None)
            st.rerun()

    guardados = default_cache().size()
    if guardados:
        with st.expander(f"Leituras guardadas neste computador ({guardados} arquivo(s))"):
            st.caption(
                "Cada boleta lida fica salva em `.cache/leituras/`, para que uma "
                "leitura interrompida continue de onde parou e o mesmo arquivo não "
                "seja pago duas vezes. Contém o que foi transcrito, inclusive nome e "
                "telefone de cliente."
            )
            if st.button("Apagar leituras guardadas"):
                default_cache().clear()
                st.rerun()

boletas_aprovadas = False
if boletas_are_current:
    raw_original = boletas_state["raw"]

    # Os editores partem sempre da leitura original. O que o usuário corrigiu
    # vive no estado do próprio `data_editor`; realimentar o editor com o
    # resultado já corrigido faria as linhas adicionadas serem reaplicadas a
    # cada execução e duplicarem.
    cabecalhos_lidos = frames_from_raw(raw_original)

    st.caption(
        "Confira o que foi lido e corrija direto na tabela. A correção passa "
        "pelas mesmas checagens da leitura automática."
    )
    cabecalhos_editados = st.data_editor(
        cabecalhos_lidos,
        hide_index=True,
        width="stretch",
        disabled=["Boleta"],
        column_config={
            "Boleta": st.column_config.TextColumn("Arquivo", width="medium"),
            "Data": st.column_config.TextColumn(
                "Data", help="Como está escrito na boleta, ex.: 22/09", width="small"
            ),
            "Peças": st.column_config.NumberColumn("Peças", min_value=0, step=1, width="small"),
            "Pagamento": st.column_config.SelectboxColumn("Pagamento", options=PAGAMENTOS),
        },
        key="editor_cabecalhos",
    )

    raw_corrigido = raw_from_frames(raw_original, cabecalhos_editados)
    st.session_state["boletas_corrigidas"] = raw_corrigido

    boletas_data = build_boletas(
        raw_corrigido,
        start_date,
        end_date,
        file_names=boletas_state["file_names"],
        warnings=boletas_state["warnings"],
    )
    review = boletas_data.review_queue
    boleta_cols = st.columns(3)
    boleta_cols[0].metric("Boletas lidas", len(boletas_data.boletas))
    boleta_cols[1].metric("Conferem", len(boletas_data.boletas) - len(review))
    boleta_cols[2].metric("A conferir", len(review))

    duplicados = boletas_state.get("duplicates") or []
    if duplicados:
        st.warning(
            f"{len(duplicados)} arquivo(s) idênticos a outros foram ignorados, para "
            "que as mesmas boletas não entrassem duas vezes no cruzamento: "
            + "; ".join(f"“{copia}” = “{original}”" for copia, original in duplicados)
        )

    stats = boletas_state.get("stats") or {}
    if stats.get("from_cache"):
        st.caption(
            f"{stats['from_cache']} de {stats['total']} boletas vieram de leituras já "
            f"guardadas, sem nova chamada ao n8n. Tempo da leitura: {_duracao(stats.get('elapsed'))}."
        )

    # Num lote de um mês, avisos individuais empilhados escondem a tela; ficam
    # agrupados quando passam de poucos.
    if len(boletas_data.warnings) > 3:
        with st.expander(f"{len(boletas_data.warnings)} aviso(s) da leitura", expanded=False):
            for message in boletas_data.warnings:
                st.markdown(f"- {_md(message)}")
    else:
        for message in boletas_data.warnings:
            st.warning(message)

    fora_do_periodo = sorted(
        day for day in boletas_data.all_dates if not (start_date <= day <= end_date)
    )
    if fora_do_periodo:
        st.warning(
            "Boletas com data fora do período selecionado: "
            + ", ".join(format_date_br(day) for day in fora_do_periodo)
        )

    if review:
        with st.expander(f"O que não fechou ({len(review)})", expanded=True):
            st.caption(
                "Corrija na tabela acima. O alerta some assim que a conta fechar."
            )
            for boleta in review:
                rotulo = f"{boleta.source_file} · p{boleta.page}/{boleta.position}"
                if boleta.numero:
                    rotulo += f" · Nº {boleta.numero}"
                st.markdown(f"**{_md(rotulo)}**")
                for check in boleta.checks:
                    st.markdown(f"- {_md(check)}")
                for field in boleta.unreadable_fields:
                    st.markdown(f"- campo `{field}` preenchido no papel, mas ilegível")

    assinatura = signature(raw_corrigido)
    boletas_aprovadas = st.session_state.get("boletas_aprovacao") == assinatura

    if boletas_aprovadas:
        st.success(
            f"{len(boletas_data.boletas)} boleta(s) aprovadas. "
            "A conferência já pode ser processada."
        )
    else:
        rotulo = "Aprovar boletas e liberar a conferência"
        if review:
            rotulo = f"Aprovar mesmo com {len(review)} boleta(s) a conferir"
            st.caption(
                "Aprovar sem corrigir é possível: as boletas seguem marcadas, e o dia "
                "de uma boleta com dúvida nas próprias peças sai como REVISAR em vez "
                "de DIVERGÊNCIA."
            )
        if st.button(rotulo, type="primary", width="stretch"):
            st.session_state["boletas_aprovacao"] = assinatura
            st.rerun()

process_enabled = bool(
    validation_is_current
    and not validation_state.get("error")
    and validation_state["result"].is_valid
    # Boleta lida e não aprovada trava o processamento: o relatório sairia com
    # um cruzamento que o operador ainda não conferiu.
    and (not boletas_are_current or boletas_aprovadas)
)
# A conferência processada precisa cair quando as boletas mudam: senão uma
# correção feita depois de processar deixaria na tela um relatório montado
# sobre os dados antigos, com aparência de atual.
processed_fingerprint = current_fingerprint and "|".join(
    [current_fingerprint, signature(st.session_state.get("boletas_corrigidas") or ())]
)

if boletas_are_current and not boletas_aprovadas:
    st.info("Aprove as boletas acima para liberar o processamento da conferência.")

process_clicked = st.button(
    "Processar conferência",
    type="primary",
    width="stretch",
    disabled=not process_enabled,
)

if process_clicked:
    report = reconcile(
        company,
        start_date,
        end_date,
        validation_state["crm"],
        validation_state["rede"],
        validation_state["cash"],
        validation_state["result"],
    )
    suggestions = suggest_observations(
        report, validation_state["crm"], validation_state["rede"]
    )
    st.session_state["processed"] = {
        "fingerprint": processed_fingerprint,
        "report": report,
        "crosscheck": (
            crosscheck(start_date, end_date, validation_state["crm"], boletas_data)
            if boletas_data is not None
            else None
        ),
    }
    st.session_state["observations"] = _observation_dataframe(report, suggestions)

processed = st.session_state.get("processed")
if processed and processed.get("fingerprint") == processed_fingerprint:
    report = processed["report"]
    st.divider()
    st.subheader("Resumo do período")
    metric_values = [
        ("Total CRM", report.totals["total_crm_cents"]),
        ("PIX do caixa (recebidos)", report.totals["pix_cash_cents"]),
        ("Dinheiro do caixa (recebidos)", report.totals["cash_cents"]),
        ("Esperado em cartão (dias completos)", report.totals["expected_card_cents"]),
        ("Aprovado na Rede", report.totals["rede_cents"]),
        ("Diferença (dias completos)", report.totals["difference_cents"]),
    ]
    for offset in (0, 3):
        columns = st.columns(3)
        for column, (label, value) in zip(columns, metric_values[offset : offset + 3]):
            column.metric(label, format_brl_currency(value))
    count_cols = st.columns(3)
    count_cols[0].metric("Dias OK", report.ok_days)
    count_cols[1].metric("Dias com divergência", report.divergent_days)
    count_cols[2].metric("Dias pendentes", report.pending_days)

    if report.overall_status == "PENDENTE":
        st.warning("Status geral do período: PENDENTE - há fechamento(s) de caixa não enviado(s).")
    elif report.overall_status == "OK":
        st.success("Status geral do período: OK")
    else:
        st.error("Status geral do período: DIVERGÊNCIA")

    st.subheader("Conferência diária")
    daily_df = _daily_dataframe(report)
    st.dataframe(daily_df, hide_index=True, width="stretch")

    cross = processed.get("crosscheck")
    if cross is not None and cross.rows:
        st.divider()
        st.subheader("Cruzamento boletas × CRM")
        t = cross.totals
        resumo = f"{t['boletas']} boleta(s) e {t['sales']} venda(s) do CRM"
        if t.get("excluded_boletas"):
            resumo += f"; {t['excluded_boletas']} boleta(s) fora do cruzamento (data sob suspeita)"
        st.caption(
            f"{resumo}. Cada boleta é casada com a sua venda pelo número de controle, "
            "pelas peças e pelo total; dentro da venda, as peças são conferidas uma a "
            "uma. Diferença = boletas − CRM: negativa quando o CRM tem venda que não "
            "aparece nas boletas."
        )
        cross_cols = st.columns(5)
        cross_cols[0].metric("Vendas casadas", f"{t['matched_sales']} de {t['sales']}")
        cross_cols[1].metric("Vendas sem boleta", t["sales_without_boleta"])
        cross_cols[2].metric("Boletas sem venda", t["boletas_without_sale"])
        cross_cols[3].metric("Peças que não fecham", t["piece_divergences"])
        cross_cols[4].metric("Diferença", format_brl_currency(t["difference_cents"]))

        if cross.days_divergent:
            st.error(
                f"{cross.days_divergent} dia(s) com divergência entre boletas e CRM — "
                "detalhe abaixo, com o valor de cada uma."
            )
        if cross.days_review:
            st.warning(
                f"{cross.days_review} dia(s) com divergência que pode ser da leitura: a "
                "boleta envolvida tem peça ilegível ou ficou fora do cruzamento. "
                "Confira a boleta antes de concluir."
            )
        if cross.days_ok and not (cross.days_divergent or cross.days_review):
            st.success("Todas as boletas enviadas fecham com as vendas do CRM.")
        if cross.days_without_boleta:
            st.info(
                f"{cross.days_without_boleta} dia(s) do período sem boleta enviada — "
                "esses dias não foram cruzados."
            )
        if t.get("excluded_boletas"):
            st.warning(
                f"{t['excluded_boletas']} boleta(s) ficaram fora do cruzamento por "
                "terem data sob suspeita. Enquanto a data não for corrigida, a venda "
                "delas aparece abaixo como venda sem boleta."
            )

        st.dataframe(_crosscheck_dataframe(cross), hide_index=True, width="stretch")

        divergencias = [item for item in cross.discrepancies if item.is_divergence]
        if divergencias:
            st.markdown("**Divergências**")
            st.caption(
                "Confira na boleta física. Venda sem boleta: boleta não enviada, "
                "ilegível ou venda sem papel. Boleta sem venda: venda que não entrou "
                "no sistema. Peça que não fecha: dentro da venda casada, com o TOTAL "
                "da boleta diferente do CRM."
            )
            st.dataframe(
                _discrepancy_dataframe(divergencias), hide_index=True, width="stretch"
            )

        notas = [item for item in cross.discrepancies if item.kind in NOTE_KINDS]
        if notas:
            with st.expander(
                f"Observações de leitura ({len(notas)}) — não contam como divergência"
            ):
                st.caption(
                    "Diferenças explicadas pela leitura: código de barras lido com "
                    "dígito trocado, preço lido errado ou troca não transcrita numa "
                    "boleta cujo TOTAL fecha com a venda, e boleta que veio no lote de "
                    "outro dia."
                )
                st.dataframe(
                    _discrepancy_dataframe(notas), hide_index=True, width="stretch"
                )
    elif cross is not None:
        st.info(
            "Nenhuma boleta do período foi cruzada. Verifique as datas lidas nas boletas."
        )

    st.subheader("Observações")
    st.caption("As sugestões abaixo usam apenas evidências dos arquivos e podem ser editadas.")
    edited_observations = st.data_editor(
        st.session_state["observations"],
        hide_index=True,
        width="stretch",
        disabled=["Data", "Status"],
        column_config={"Observação": st.column_config.TextColumn(width="large")},
        key="observations_editor",
    )
    st.session_state["observations"] = edited_observations
    observation_map = {}
    for index, row in enumerate(report.rows):
        value = edited_observations.iloc[index]["Observação"]
        observation_map[row.date] = "" if pd.isna(value) else str(value)
    try:
        pdf_bytes = generate_pdf_report(report, observation_map, crosscheck=cross)
        file_name = (
            f"conferencia_{company.lower()}_"
            f"{start_date.strftime('%Y%m%d')}_{end_date.strftime('%Y%m%d')}.pdf"
        )
        st.download_button(
            "Baixar PDF final",
            data=pdf_bytes,
            file_name=file_name,
            mime="application/pdf",
            type="primary",
            width="stretch",
        )
    except Exception as exc:
        st.error(f"Não foi possível gerar o PDF final: {exc}")

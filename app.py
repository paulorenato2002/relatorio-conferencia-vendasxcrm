from __future__ import annotations

from datetime import date, datetime
import hashlib

import pandas as pd
import streamlit as st

from src.boletas import (
    BoletaClientError,
    BoletaParseError,
    N8nConfig,
    build_boletas,
    fetch_boletas,
    render_many,
)
from src.boletas.edicao import (
    PAGAMENTOS,
    files_signature,
    frames_from_raw,
    raw_from_frames,
    signature,
)
from src.crosscheck import (
    KIND_MISSING_IN_BOLETA,
    KIND_MISSING_IN_CRM,
    KIND_SUSPECT_READ,
    crosscheck,
)
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
    return pd.DataFrame(
        [
            {
                "Data": format_date_br(row.date),
                "Boletas": row.boleta_count,
                "Fora do cruzamento": row.excluded_boletas,
                "Peças casadas": row.matched_items,
                "Só na boleta": row.only_boleta,
                "Só no CRM": row.only_crm,
                "Leitura suspeita": row.suspect_reads,
                "Bruto boletas": format_brl_cents(row.boleta_gross_cents),
                "Bruto CRM": format_brl_cents(row.crm_gross_cents),
                "Diferença": format_brl_cents(row.gross_difference_cents),
                "Status": row.status,
            }
            for row in report.rows
        ]
    )


def _discrepancy_dataframe(items) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "Data": format_date_br(item.date),
                "Código": item.codigo,
                "Valor": format_brl_cents(item.value_cents),
                "Tipo": "Devolução" if item.is_return else "Venda",
                "Vendedora": item.seller or "—",
                "Boleta": item.boleta_numero or (item.boleta_id or "—"),
                "Venda CRM": item.sale_number or "—",
                "Produto": item.product or "—",
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

    validate_clicked = st.button("Validar arquivos", type="primary", use_container_width=True)

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

if not boleta_files:
    st.info(
        "Envie as boletas escaneadas para incluí-las na conferência. "
        "A leitura é feita pelo n8n e roda sob demanda."
    )
else:
    read_clicked = st.button(
        "Ler boletas no n8n",
        use_container_width=True,
        disabled=boletas_are_current,
        help="Já lido: reenvie apenas se trocar os arquivos." if boletas_are_current else None,
    )
    if read_clicked:
        progress = st.progress(0.0, text="Recortando as boletas...")
        try:
            images = render_many(list(boleta_files))
            progress.progress(0.0, text=f"0 de {len(images)} boletas lidas...")

            def _on_progress(done: int, total: int) -> None:
                progress.progress(done / total, text=f"{done} de {total} boletas lidas...")

            raw, warnings = fetch_boletas(
                images, config=N8nConfig.from_env(_streamlit_secrets()), on_progress=_on_progress
            )
            st.session_state["boletas_leitura"] = {
                "fingerprint": boletas_fingerprint,
                "raw": raw,
                "warnings": warnings,
                "file_names": tuple(dict.fromkeys(image.source_file for image in images)),
            }
            boletas_state = st.session_state["boletas_leitura"]
            boletas_are_current = True
        except (BoletaParseError, BoletaClientError, ValueError) as exc:
            st.session_state.pop("boletas_leitura", None)
            st.error(str(exc))
        finally:
            progress.empty()

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
        use_container_width=True,
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
                st.markdown(f"**{rotulo}**")
                for check in boleta.checks:
                    st.markdown(f"- {check}")
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
                "Aprovar sem corrigir é possível: as boletas seguem marcadas e o "
                "cruzamento não conclui divergência em cima delas."
            )
        if st.button(rotulo, type="primary", use_container_width=True):
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
    use_container_width=True,
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
    st.dataframe(daily_df, hide_index=True, use_container_width=True)

    cross = processed.get("crosscheck")
    if cross is not None and cross.rows:
        st.divider()
        st.subheader("Cruzamento boletas × CRM")
        cruzadas = cross.totals["boletas"]
        fora = cross.totals.get("excluded_boletas", 0)
        resumo = f"{cruzadas} boleta(s) cruzadas"
        if fora:
            resumo += f", {fora} fora do cruzamento (data sob suspeita)"
        st.caption(
            f"{resumo}. Compara peça a peça pelo código de barras da etiqueta, "
            "que é o mesmo código do CRM sem os zeros à esquerda."
        )
        cross_cols = st.columns(4)
        cross_cols[0].metric("Peças casadas", cross.totals["matched_items"])
        cross_cols[1].metric("Na boleta, fora do CRM", cross.totals["only_boleta"])
        cross_cols[2].metric("No CRM, fora da boleta", cross.totals["only_crm"])
        cross_cols[3].metric("Leitura suspeita", cross.totals["suspect_reads"])

        if cross.days_divergent:
            st.error(
                f"{cross.days_divergent} dia(s) com peça sem correspondência entre "
                "boleta e CRM."
            )
        elif cross.days_review:
            st.warning(
                f"{cross.days_review} dia(s) dependem de revisão manual antes de "
                "concluir: há boleta com leitura duvidosa."
            )
        elif cross.days_ok:
            st.success("Todas as peças das boletas enviadas batem com o CRM.")
        if cross.days_without_boleta:
            st.info(
                f"{cross.days_without_boleta} dia(s) do período sem boleta enviada — "
                "esses dias não foram cruzados."
            )
        if cross.totals.get("excluded_boletas"):
            st.warning(
                f"{cross.totals['excluded_boletas']} boleta(s) ficaram fora do "
                "cruzamento por terem data sob suspeita. Enquanto a data não for "
                "corrigida, as peças dessas boletas aparecem abaixo como “no CRM, "
                "fora da boleta” — não são vendas sem registro."
            )

        st.dataframe(
            _crosscheck_dataframe(cross), hide_index=True, use_container_width=True
        )

        sem_crm = cross.by_kind(KIND_MISSING_IN_CRM)
        if sem_crm:
            st.markdown("**Peças na boleta e fora do CRM**")
            st.caption(
                "Venda registrada no papel sem linha correspondente no sistema. "
                "Confirme a peça na boleta física antes de tratar como não registrada."
            )
            st.dataframe(
                _discrepancy_dataframe(sem_crm), hide_index=True, use_container_width=True
            )

        sem_boleta = cross.by_kind(KIND_MISSING_IN_BOLETA)
        if sem_boleta:
            st.markdown("**Peças no CRM e fora das boletas**")
            st.caption("Venda no sistema sem boleta correspondente entre as enviadas.")
            st.dataframe(
                _discrepancy_dataframe(sem_boleta),
                hide_index=True,
                use_container_width=True,
            )

        suspeitas = cross.by_kind(KIND_SUSPECT_READ)
        if suspeitas:
            st.markdown("**Prováveis erros de leitura**")
            st.caption(
                "O código da boleta não existe no CRM, mas existe um a um dígito de "
                "distância, no mesmo dia e com o mesmo valor. É mais provável que o "
                "modelo tenha lido um dígito errado do que ser venda fora do sistema."
            )
            for item in suspeitas:
                st.markdown(
                    f"- {format_date_br(item.date)} · "
                    f"{format_brl_currency(item.value_cents)} · {item.note}"
                    + (f" · {item.product}" if item.product else "")
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
        use_container_width=True,
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
            use_container_width=True,
        )
    except Exception as exc:
        st.error(f"Não foi possível gerar o PDF final: {exc}")

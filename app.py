"""AskMXP 2.0 — Mixpanel 事件查詢 + 批次監控 + AI 備忘錄 + PPT 週報 + Chat 分享。"""
from __future__ import annotations

import io
import os
import tempfile
from datetime import datetime

from dotenv import load_dotenv
load_dotenv()

import pandas as pd
import plotly.graph_objects as go
import requests
import streamlit as st

from mixpanel_client import (
    fetch_event_data,
    fetch_retention,
    compute_period_delta,
    compute_stickiness,
)
from ai_insight import (
    generate_memo,
    load_history,
    add_history_entry,
    delete_history_entry,
)
from notifier import send_analysis_share
from ppt_exporter import build_weekly_report


st.set_page_config(page_title="AskMXP", page_icon="📊", layout="wide")
st.title("📊 AskMXP")
st.caption("Mixpanel 事件查詢 · 批次監控 · AI 營銷備忘錄 · PPT 週報 · Google Chat 分享")


def get_secret(key: str, default: str = "") -> str:
    """Resolve a secret from (in order): Streamlit Cloud Secrets → env var → default.
    Safe against missing secrets.toml in local dev."""
    try:
        if key in st.secrets:
            return str(st.secrets[key])
    except Exception:
        pass
    return os.environ.get(key, default)


# ── Sidebar ──
with st.sidebar:
    st.header("🔑 認證設定")
    mp_username = st.text_input("Mixpanel Username", value=get_secret("MIXPANEL_USERNAME"))
    mp_secret = st.text_input("Mixpanel Secret", type="password",
                              value=get_secret("MIXPANEL_SECRET"))
    mp_project_id = st.text_input("Mixpanel Project ID",
                                  value=get_secret("MIXPANEL_PROJECT_ID"))

    st.divider()
    st.subheader("🤖 Claude LLM")
    anthropic_key = st.text_input("Anthropic API Key", type="password",
                                  value=get_secret("ANTHROPIC_API_KEY"),
                                  help="設定後 AI 備忘錄會使用 Claude 深度分析")

    st.divider()
    st.subheader("💬 Google Chat")
    chat_webhook = st.text_input("Webhook URL", type="password",
                                 value=get_secret("GOOGLE_CHAT_WEBHOOK"))

    with st.expander("❓ 如何獲取 Mixpanel 金鑰？"):
        st.markdown(
            """
1. 登入 Mixpanel → **Organization Settings → Service Accounts**
2. **+ Add Service Account** → 取得 Username / Secret
3. 進入專案 → **Project Settings** → 取得 Project ID
            """
        )


# ── Session state ──
if "batch_results" not in st.session_state:
    st.session_state.batch_results = []  # list of dicts: {chinese_name, event_id, df, memo, error}
if "selected_idx" not in st.session_state:
    st.session_state.selected_idx = 0


# ── 步驟一：上傳事件對照表 ──
st.subheader("步驟一：上傳事件對照表")
uploaded = st.file_uploader(
    "請上傳 Excel (.xlsx) 或 CSV（批次監控用的 config_events.xlsx 也可直接拖進來）",
    type=["xlsx", "xls", "csv"],
)

if uploaded is None:
    st.info("請先上傳事件對照表。")
    st.stop()

if uploaded.name.endswith(".csv"):
    df_mapping = pd.read_csv(uploaded)
else:
    df_mapping = pd.read_excel(uploaded)

st.success(f"成功讀取 {len(df_mapping)} 筆事件")
with st.expander("預覽事件表"):
    st.dataframe(df_mapping, use_container_width=True)

columns = df_mapping.columns.tolist()

st.subheader("步驟二：選擇欄位對應")
c1, c2 = st.columns(2)
with c1:
    cn_col = st.selectbox("「頁面中文名稱」對應欄位", options=columns, index=0)
with c2:
    en_col = st.selectbox("「事件英文 ID」對應欄位", options=columns,
                          index=min(1, len(columns) - 1))


# ── 步驟三：查詢參數 ──
st.subheader("步驟三：查詢參數")
p1, p2, p3 = st.columns(3)
with p1:
    metric_label = st.selectbox(
        "指標",
        ["總次數 (Total events)", "獨立用戶 (Unique users)"],
        help="『總次數』＝事件被觸發幾次；『獨立用戶』＝有多少不重複的人，對應 Mixpanel 的 DAU/WAU/MAU",
    )
    metric = "unique" if "獨立" in metric_label else "total"
with p2:
    unit_label = st.selectbox("時間粒度", ["日", "週", "月"])
    unit = {"日": "day", "週": "week", "月": "month"}[unit_label]
with p3:
    days = st.selectbox("查詢區間", [7, 14, 30, 60, 90], index=2,
                        format_func=lambda d: f"過去 {d} 天")

# ── 步驟四：執行模式 ──
st.subheader("步驟四：執行模式")
mode = st.radio(
    "選擇查詢方式",
    ["🔍 單一事件查詢", "🚀 批次執行（所有事件）", "🔄 留存分析（Retention）"],
    horizontal=True,
)


def _auth_ok() -> bool:
    if not (mp_username and mp_secret and mp_project_id):
        st.error("請先在左側欄填寫 Mixpanel 認證資訊。")
        return False
    return True


def _metric_label(metric: str) -> str:
    return "獨立用戶數" if metric == "unique" else "事件次數"


def _unit_label(unit: str) -> str:
    return {"day": "每日", "week": "每週", "month": "每月"}.get(unit, "每日")


def _plot(df: pd.DataFrame, chinese_name: str,
          metric: str = "total", unit: str = "day", days: int = 30) -> go.Figure:
    y_label = _metric_label(metric)
    unit_label = _unit_label(unit)

    fig = go.Figure()
    fig.add_trace(go.Scatter(
        x=df["日期"], y=df["事件次數"],
        mode="lines+markers",
        line=dict(color="#E62117", width=2),
        marker=dict(size=5),
        name=chinese_name,
        hovertemplate="%{x|%Y-%m-%d}<br>" + y_label + "：%{y:,}<extra></extra>",
    ))

    # Highlight anomaly days: drop > 20% vs 7-day rolling mean
    if len(df) >= 8:
        s = df["事件次數"].astype(float)
        rolling = s.rolling(window=7, min_periods=3).mean().shift(1)
        drop_mask = (s - rolling) / rolling < -0.2
        anomalies = df[drop_mask.fillna(False)]
        if not anomalies.empty:
            fig.add_trace(go.Scatter(
                x=anomalies["日期"], y=anomalies["事件次數"],
                mode="markers",
                marker=dict(size=12, color="rgba(230,33,23,0.25)",
                            line=dict(color="#E62117", width=2)),
                name="⚠️ 明顯下跌（>20%）",
                hovertemplate="%{x|%Y-%m-%d}<br>下跌日<extra></extra>",
            ))

    fig.update_layout(
        title=f"「{chinese_name}」{unit_label}{y_label}（過去 {days} 天）",
        xaxis_title="日期",
        yaxis_title=y_label,
        template="plotly_white",
        hovermode="x unified",
    )
    return fig


def _render_delta_cards(df: pd.DataFrame, metric: str):
    """Show WoW and MoM delta cards above the chart."""
    y_label = _metric_label(metric)

    wow = compute_period_delta(df, period_days=7)
    mom = compute_period_delta(df, period_days=30)

    c1, c2, c3 = st.columns(3)
    total = int(df["事件次數"].sum()) if not df.empty else 0
    c1.metric(f"期間{y_label}總計", f"{total:,}")

    if wow["has_data"]:
        c2.metric(
            f"本週 vs 上週",
            f"{wow['current']:,}",
            delta=f"{wow['pct']*100:+.1f}%",
        )
    else:
        c2.metric("本週 vs 上週", "—", help="資料不足 14 天，無法比較")

    if mom["has_data"]:
        c3.metric(
            f"本月 vs 上月",
            f"{mom['current']:,}",
            delta=f"{mom['pct']*100:+.1f}%",
        )
    else:
        c3.metric("本月 vs 上月", "—", help="資料不足 60 天，無法比較")


# ── 單一事件模式 ──
if mode == "🔍 單一事件查詢":
    options = [
        f"{row[cn_col]}（{row[en_col]}）"
        for _, row in df_mapping.iterrows()
    ]
    selected = st.selectbox("請選擇事件", options=options)

    if st.button("🔍 查詢事件資料", type="primary"):
        if not _auth_ok():
            st.stop()

        idx = options.index(selected)
        event_id = str(df_mapping.iloc[idx][en_col])
        chinese_name = str(df_mapping.iloc[idx][cn_col])

        with st.spinner("正在向 Mixpanel 查詢資料…"):
            try:
                df_result = fetch_event_data(
                    mp_username, mp_secret, mp_project_id, event_id,
                    days=days, metric=metric, unit=unit,
                )
            except Exception as e:
                st.error(f"查詢失敗：{e}")
                st.stop()

        with st.spinner("AI 正在產出營銷備忘錄…"):
            memo = generate_memo(df_result, event_id, chinese_name,
                                 api_key=anthropic_key or None)

        st.session_state.batch_results = [{
            "chinese_name": chinese_name,
            "event_id": event_id,
            "df": df_result,
            "memo": memo,
            "error": None,
            "metric": metric,
            "unit": unit,
            "days": days,
        }]
        st.session_state.selected_idx = 0

# ── 批次模式 ──
elif mode == "🚀 批次執行（所有事件）":
    st.info(f"將循環處理 **{len(df_mapping)}** 個事件。建議事件超過 20 筆時分批操作。")
    if st.button("🚀 開始批次執行", type="primary"):
        if not _auth_ok():
            st.stop()

        total_events = len(df_mapping)
        progress = st.progress(0.0, text="準備中…")
        status = st.empty()

        results = []
        for i, row in df_mapping.iterrows():
            chinese_name = str(row[cn_col])
            event_id = str(row[en_col])
            status.write(f"[{i+1}/{total_events}] 抓取 **{chinese_name}**（{event_id}）…")

            try:
                df_r = fetch_event_data(
                    mp_username, mp_secret, mp_project_id, event_id,
                    days=days, metric=metric, unit=unit,
                )
                memo = generate_memo(df_r, event_id, chinese_name,
                                     api_key=anthropic_key or None)
                results.append({
                    "chinese_name": chinese_name,
                    "event_id": event_id,
                    "df": df_r,
                    "memo": memo,
                    "error": None,
                    "metric": metric,
                    "unit": unit,
                    "days": days,
                })
            except Exception as e:
                results.append({
                    "chinese_name": chinese_name,
                    "event_id": event_id,
                    "df": pd.DataFrame(columns=["日期", "事件次數"]),
                    "memo": f"查詢失敗：{e}",
                    "error": str(e),
                    "metric": metric,
                    "unit": unit,
                    "days": days,
                })

            progress.progress((i + 1) / total_events, text=f"{i+1}/{total_events} 完成")

        status.empty()
        progress.empty()
        st.session_state.batch_results = results
        st.session_state.selected_idx = 0
        ok = sum(1 for r in results if r["error"] is None)
        st.success(f"批次完成：{ok}/{total_events} 成功")

# ── 留存分析模式 ──
else:
    st.caption("留存 = 有多少比例的新用戶，在之後的日子還會回來使用。")

    event_options = [
        f"{row[cn_col]}（{row[en_col]}）"
        for _, row in df_mapping.iterrows()
    ]

    r1, r2 = st.columns(2)
    with r1:
        born_sel = st.selectbox(
            "初次事件（Born Event）",
            options=event_options,
            help="定義用戶『加入 cohort』的事件。例：首次註冊、首次開啟 App",
        )
    with r2:
        return_opts = ["（任何事件）"] + event_options
        return_sel = st.selectbox(
            "回訪事件（Return Event）",
            options=return_opts,
            help="計算回訪時看的事件。選『任何事件』＝只要用戶回來做任何事都算留下",
        )

    r3, r4 = st.columns(2)
    with r3:
        ret_type_label = st.selectbox(
            "留存類型",
            ["Birth（首次 cohort，經典留存）", "Compounded（累積留存，看回訪）"],
        )
        retention_type = "birth" if "Birth" in ret_type_label else "compounded"
    with r4:
        interval_count = st.slider("顯示幾個週期", min_value=7, max_value=30, value=14)

    if st.button("🔄 計算留存", type="primary"):
        if not _auth_ok():
            st.stop()

        born_idx = event_options.index(born_sel)
        born_event = str(df_mapping.iloc[born_idx][en_col])

        return_event = None
        if return_sel != "（任何事件）":
            ret_idx = return_opts.index(return_sel) - 1
            return_event = str(df_mapping.iloc[ret_idx][en_col])

        with st.spinner("正在向 Mixpanel Retention API 查詢…"):
            try:
                df_ret = fetch_retention(
                    mp_username, mp_secret, mp_project_id,
                    born_event=born_event,
                    return_event=return_event,
                    days=days, retention_type=retention_type,
                    unit=unit, interval_count=interval_count,
                )
            except Exception as e:
                st.error(f"留存查詢失敗：{e}")
                st.stop()

        st.session_state["retention_df"] = df_ret
        st.session_state["retention_meta"] = {
            "born_event": born_event,
            "return_event": return_event or "（任何事件）",
            "unit": unit,
            "retention_type": retention_type,
        }

    # Render retention result
    ret_df = st.session_state.get("retention_df")
    ret_meta = st.session_state.get("retention_meta")
    if ret_df is not None and ret_meta is not None:
        st.divider()
        st.subheader("🔄 留存矩陣")
        st.caption(
            f"初次事件：**{ret_meta['born_event']}** ｜ "
            f"回訪事件：**{ret_meta['return_event']}** ｜ "
            f"類型：**{ret_meta['retention_type']}** ｜ "
            f"粒度：**{_unit_label(ret_meta['unit'])}**"
        )

        if ret_df.empty:
            st.warning("查無資料。可能區間內沒有新 cohort。")
        else:
            interval_cols = [c for c in ret_df.columns if c.startswith("D")]
            if interval_cols:
                # Heatmap
                import plotly.express as px
                heat = ret_df.set_index("cohort")[interval_cols]
                fig_heat = px.imshow(
                    heat.values,
                    x=interval_cols,
                    y=heat.index.astype(str),
                    color_continuous_scale=[[0, "#FFFFFF"], [1, "#E62117"]],
                    aspect="auto",
                    text_auto=".1f",
                    labels=dict(x="週期", y="Cohort 日期", color="留存 %"),
                )
                fig_heat.update_layout(
                    title="Cohort 留存熱度圖",
                    template="plotly_white",
                )
                st.plotly_chart(fig_heat, use_container_width=True)

                # Summary — average per-interval retention
                avg_row = {c: round(float(ret_df[c].mean()), 1) for c in interval_cols}
                st.markdown("**各週期平均留存率：**")
                st.dataframe(
                    pd.DataFrame([avg_row]),
                    use_container_width=True, hide_index=True,
                )

            st.markdown("**完整 cohort 表：**")
            st.dataframe(ret_df, use_container_width=True, hide_index=True)

            csv_bytes = ret_df.to_csv(index=False).encode("utf-8-sig")
            st.download_button(
                "⬇️ 下載留存 CSV",
                data=csv_bytes,
                file_name=f"retention_{ret_meta['born_event']}.csv",
                mime="text/csv",
            )

    st.stop()  # 留存模式到此為止，底下的事件結果區不適用


# ── 結果區 ──
results = st.session_state.batch_results
if results:
    st.divider()
    st.subheader("📈 分析結果")

    # ── 批次模式：事件排行總覽 ──
    if len(results) > 1:
        valid_results = [r for r in results if r["error"] is None and not r["df"].empty]
        if valid_results:
            rank_rows = []
            for r in valid_results:
                wow = compute_period_delta(r["df"], period_days=7)
                total = int(r["df"]["事件次數"].sum())
                rank_rows.append({
                    "事件": f"{r['chinese_name']}（{r['event_id']}）",
                    f"期間{_metric_label(r.get('metric', 'total'))}總計": total,
                    "WoW 變化 %": round(wow["pct"] * 100, 1) if wow["has_data"] else None,
                })
            rank_df = pd.DataFrame(rank_rows)

            tab_top, tab_grow, tab_drop = st.tabs(["🏆 Top 10", "📈 成長最快", "📉 衰退最多"])
            total_col = [c for c in rank_df.columns if c.startswith("期間")][0]
            with tab_top:
                st.dataframe(
                    rank_df.sort_values(total_col, ascending=False).head(10),
                    use_container_width=True, hide_index=True,
                )
            with tab_grow:
                grow_df = rank_df.dropna(subset=["WoW 變化 %"]).sort_values(
                    "WoW 變化 %", ascending=False,
                ).head(10)
                st.dataframe(grow_df, use_container_width=True, hide_index=True)
            with tab_drop:
                drop_df = rank_df.dropna(subset=["WoW 變化 %"]).sort_values(
                    "WoW 變化 %", ascending=True,
                ).head(10)
                st.dataframe(drop_df, use_container_width=True, hide_index=True)

            st.divider()

        labels = [
            f"{'✅' if r['error'] is None else '⚠️'} {r['chinese_name']}（{r['event_id']}）"
            for r in results
        ]
        st.session_state.selected_idx = st.selectbox(
            "選擇事件檢視",
            options=list(range(len(results))),
            format_func=lambda i: labels[i],
            index=st.session_state.selected_idx,
        )

    current = results[st.session_state.selected_idx]
    df_cur = current["df"]
    name_cur = current["chinese_name"]
    id_cur = current["event_id"]
    metric_cur = current.get("metric", "total")
    unit_cur = current.get("unit", "day")
    days_cur = current.get("days", 30)

    if current["error"]:
        st.error(f"此事件查詢失敗：{current['error']}")
    elif df_cur.empty:
        st.warning(f"該事件在過去 {days_cur} 天內無任何資料。")
    else:
        # WoW / MoM 卡片
        _render_delta_cards(df_cur, metric_cur)
        # 折線圖（含異常標註）
        st.plotly_chart(
            _plot(df_cur, name_cur, metric=metric_cur, unit=unit_cur, days=days_cur),
            use_container_width=True,
        )

        # 黏著度 Stickiness — 只在獨立用戶模式下顯示
        if metric_cur == "unique":
            with st.expander("📊 黏著度分析 (DAU/MAU Stickiness)", expanded=False):
                st.caption(
                    "Stickiness = 平均 DAU ÷ MAU。一般產品 10–20% 算可接受，"
                    "社群/通訊類產品可達 50%+。"
                )
                if st.button("計算黏著度", key=f"stick_btn_{id_cur}"):
                    with st.spinner("計算中…"):
                        stick = compute_stickiness(
                            mp_username, mp_secret, mp_project_id,
                            event_name=id_cur, days=days_cur,
                        )
                    if stick["has_data"]:
                        sc1, sc2, sc3 = st.columns(3)
                        sc1.metric("平均 DAU", f"{stick['dau_avg']:,.0f}")
                        sc2.metric("MAU", f"{stick['mau']:,}")
                        sc3.metric("Stickiness", f"{stick['stickiness']*100:.1f}%")
                    else:
                        st.warning("資料不足以計算。")

    # AI 備忘錄
    st.markdown("### 🤖 AI 營銷備忘錄")
    st.markdown(current["memo"])

    # ── 歷史活動標註 ──
    with st.expander("🏷️ 歷史活動標註（讓 AI 下次分析時參考）", expanded=False):
        st.caption("把營銷活動、改版、推播等事件標註起來，AI 會在產生備忘錄時比對這些日期。")
        a1, a2 = st.columns([1, 2])
        with a1:
            anno_date = st.date_input("活動日期", value=datetime.now().date(), key="anno_date")
            anno_tag = st.text_input("類型標籤", placeholder="例：EDM / 推播 / 改版",
                                     key="anno_tag")
        with a2:
            anno_label = st.text_input("活動名稱",
                                       placeholder="例：母親節限時 8 折",
                                       key="anno_label")
            anno_note = st.text_area("備註", placeholder="活動細節、投放渠道、預期影響…",
                                     key="anno_note", height=80)

        scope = st.radio("套用範圍", ["僅本事件", "全部事件（*）"], horizontal=True, key="anno_scope")

        if st.button("➕ 新增標註"):
            if not anno_label.strip():
                st.warning("請至少填寫活動名稱。")
            else:
                add_history_entry(
                    entry_date=anno_date.strftime("%Y-%m-%d"),
                    event_id="*" if scope == "全部事件（*）" else id_cur,
                    label=anno_label.strip(),
                    note=anno_note.strip(),
                    tag=anno_tag.strip(),
                )
                st.success(f"已新增標註：{anno_label}")
                st.rerun()

        history = load_history()
        if history:
            st.markdown("**目前的歷史標註：**")
            hist_df = pd.DataFrame(history)
            st.dataframe(hist_df, use_container_width=True, hide_index=False)

            del_idx = st.number_input("刪除第幾筆（index）", min_value=0,
                                      max_value=max(0, len(history) - 1), value=0, step=1)
            if st.button("🗑️ 刪除標註"):
                delete_history_entry(int(del_idx))
                st.success("已刪除")
                st.rerun()
        else:
            st.caption("目前尚無標註。")

    # ── 匯出與分享 ──
    st.divider()
    st.subheader("📤 匯出與分享")

    col_a, col_b, col_c = st.columns(3)

    # CSV
    with col_a:
        if not df_cur.empty:
            csv_bytes = df_cur.to_csv(index=False).encode("utf-8-sig")
            st.download_button(
                "⬇️ 下載本事件 CSV",
                data=csv_bytes,
                file_name=f"{id_cur}_{days_cur}days_{metric_cur}.csv",
                mime="text/csv",
            )

    # PPT 週報
    with col_b:
        if st.button("📊 產出 PPT 週報"):
            valid = [r for r in results if r["error"] is None and not r["df"].empty]
            if not valid:
                st.warning("沒有可輸出的事件資料。")
            else:
                with st.spinner("正在產生簡報…"):
                    try:
                        tmp_path = os.path.join(
                            tempfile.gettempdir(),
                            f"AskMXP_週報_{datetime.now().strftime('%Y%m%d_%H%M%S')}.pptx",
                        )
                        build_weekly_report(valid, tmp_path)
                        with open(tmp_path, "rb") as f:
                            ppt_bytes = f.read()
                        st.session_state["ppt_bytes"] = ppt_bytes
                        st.session_state["ppt_name"] = os.path.basename(tmp_path)
                        st.success(f"已產出 {len(valid)} 個事件的簡報")
                    except Exception as e:
                        st.error(f"PPT 產出失敗：{e}")

        if "ppt_bytes" in st.session_state:
            st.download_button(
                "⬇️ 下載 PPT",
                data=st.session_state["ppt_bytes"],
                file_name=st.session_state["ppt_name"],
                mime="application/vnd.openxmlformats-officedocument.presentationml.presentation",
            )

    # Google Chat 分享
    with col_c:
        if st.button("💬 分享到 Google Chat"):
            if not chat_webhook:
                st.error("請先在左側欄填寫 Google Chat Webhook URL。")
            else:
                try:
                    send_analysis_share(
                        chat_webhook,
                        event_name=f"{name_cur}（{id_cur}）",
                        summary=current["memo"],
                    )
                    st.success("已發送到 Google Chat")
                except requests.exceptions.HTTPError as e:
                    st.error(f"發送失敗：{e.response.status_code} — {e.response.text[:200]}")
                except Exception as e:
                    st.error(f"發送失敗：{e}")

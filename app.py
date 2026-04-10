import json

import streamlit as st
import pandas as pd
import plotly.graph_objects as go
import requests
from datetime import datetime, timedelta

st.set_page_config(page_title="AskMXP", page_icon="📊", layout="wide")
st.title("📊 AskMXP — Mixpanel 事件查詢工具")

# ── Sidebar: Mixpanel 認證資訊 ──
with st.sidebar:
    st.header("Mixpanel 認證設定")
    mp_username = st.text_input("服務帳號 Username", type="default")
    mp_secret = st.text_input("服務帳號 Secret", type="password")
    mp_project_id = st.text_input("專案 ID")

    with st.expander("❓ 如何獲取 Mixpanel 金鑰?"):
        st.markdown(
            """
**服務帳號 Username & Secret：**
1. 登入 [Mixpanel](https://mixpanel.com/)。
2. 點擊左下角齒輪圖示，進入 **Organization Settings**。
3. 在左側選單中選擇 **Service Accounts**。
4. 點擊 **+ Add Service Account** 建立新帳號。
5. 建立完成後即可取得 **Username** 與 **Secret**。

> ⚠️ Secret 只會顯示一次，請立即複製並妥善保管。

**專案 ID：**
1. 進入你要查詢的專案。
2. 點擊左下角齒輪圖示，進入 **Project Settings**。
3. 頁面上方即可看到 **Project ID**（一串數字）。
            """
        )


def _auth_headers(project_id: str) -> dict:
    """共用 HTTP 標頭，含 project_id。"""
    return {
        "Accept": "application/json",
        "X-Mixpanel-Project-Id": project_id,
    }


def _try_export_api(username: str, secret: str, project_id: str,
                    event_name: str, from_date, to_date) -> pd.DataFrame | None:
    """方法一：Raw Export API (data.mixpanel.com) — 所有方案皆可使用。
    回傳 JSONL 格式，每行一筆事件，需自行彙總為每日計數。"""
    url = "https://data.mixpanel.com/api/2.0/export"
    params = {
        "project_id": project_id,
        "from_date": from_date.isoformat(),
        "to_date": to_date.isoformat(),
        "event": json.dumps([event_name]),
    }
    resp = requests.get(
        url, params=params,
        auth=(username, secret),
        headers=_auth_headers(project_id),
        timeout=60,
        stream=True,
    )
    resp.raise_for_status()

    counts: dict[str, int] = {}
    for line in resp.iter_lines():
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        props = obj.get("properties", {})
        # Mixpanel export 回傳 UNIX timestamp (秒)
        ts = props.get("time")
        if ts is None:
            continue
        day = datetime.utcfromtimestamp(ts).strftime("%Y-%m-%d")
        counts[day] = counts.get(day, 0) + 1

    if not counts:
        return pd.DataFrame(columns=["日期", "事件次數"])

    rows = [{"日期": k, "事件次數": v} for k, v in sorted(counts.items())]
    df = pd.DataFrame(rows)
    df["日期"] = pd.to_datetime(df["日期"])
    return df


def _try_segmentation_api(username: str, secret: str, project_id: str,
                          event_name: str, from_date, to_date) -> pd.DataFrame | None:
    """方法二：Segmentation API (mixpanel.com) — 部分方案可用。"""
    url = "https://mixpanel.com/api/2.0/segmentation"
    params = {
        "project_id": project_id,
        "event": event_name,
        "from_date": from_date.isoformat(),
        "to_date": to_date.isoformat(),
        "type": "general",
        "unit": "day",
    }
    resp = requests.get(
        url, params=params,
        auth=(username, secret),
        headers=_auth_headers(project_id),
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()

    values = data.get("data", {}).get("values", {})
    if not values:
        return pd.DataFrame(columns=["日期", "事件次數"])

    series = list(values.values())[0]
    rows = [{"日期": k, "事件次數": v} for k, v in sorted(series.items())]
    df = pd.DataFrame(rows)
    df["日期"] = pd.to_datetime(df["日期"])
    return df


def fetch_event_data(username: str, secret: str, project_id: str, event_name: str):
    """依序嘗試 Export API → Segmentation API，取得過去 30 天每日事件總數。"""
    today = datetime.utcnow().date()
    from_date = today - timedelta(days=30)

    errors: list[str] = []

    # 優先使用 Raw Export API（所有方案皆可用）
    try:
        df = _try_export_api(username, secret, project_id, event_name, from_date, today)
        if df is not None:
            return df
    except requests.exceptions.HTTPError as e:
        errors.append(f"Export API {e.response.status_code}: {e.response.text[:200]}")
    except Exception as e:
        errors.append(f"Export API 錯誤: {e}")

    # 備用：Segmentation API
    try:
        df = _try_segmentation_api(username, secret, project_id, event_name, from_date, today)
        if df is not None:
            return df
    except requests.exceptions.HTTPError as e:
        errors.append(f"Segmentation API {e.response.status_code}: {e.response.text[:200]}")
    except Exception as e:
        errors.append(f"Segmentation API 錯誤: {e}")

    # 兩者皆失敗
    raise RuntimeError("所有 API 端點皆無法取得資料。\n" + "\n".join(errors))


def generate_insight(df: pd.DataFrame, event_name: str, chinese_name: str) -> str:
    """根據資料產生簡要的繁體中文摘要。"""
    if df.empty:
        return "查無資料，無法產生摘要。"

    total = int(df["事件次數"].sum())
    avg = df["事件次數"].mean()
    max_row = df.loc[df["事件次數"].idxmax()]
    min_row = df.loc[df["事件次數"].idxmin()]
    max_date = max_row["日期"].strftime("%Y-%m-%d")
    min_date = min_row["日期"].strftime("%Y-%m-%d")

    first_half = df.iloc[: len(df) // 2]["事件次數"].mean()
    second_half = df.iloc[len(df) // 2 :]["事件次數"].mean()
    if second_half > first_half * 1.1:
        trend = "呈上升趨勢 📈"
    elif second_half < first_half * 0.9:
        trend = "呈下降趨勢 📉"
    else:
        trend = "大致持平 ➡️"

    return (
        f"📌 事件「{chinese_name}」（{event_name}）過去 30 天摘要：\n\n"
        f"• 總觸發次數：{total:,}\n"
        f"• 日均觸發次數：{avg:,.1f}\n"
        f"• 最高日：{max_date}（{int(max_row['事件次數']):,} 次）\n"
        f"• 最低日：{min_date}（{int(min_row['事件次數']):,} 次）\n"
        f"• 整體趨勢：{trend}\n"
    )


# ── 主區域：上傳檔案 ──
st.subheader("步驟一：上傳事件對照表")
uploaded = st.file_uploader(
    "請上傳 Excel (.xlsx) 或 CSV 檔案",
    type=["xlsx", "xls", "csv"],
)

if uploaded is not None:
    # 讀取檔案
    if uploaded.name.endswith(".csv"):
        df_mapping = pd.read_csv(uploaded)
    else:
        df_mapping = pd.read_excel(uploaded)

    st.success(f"成功讀取 {len(df_mapping)} 筆資料")
    st.dataframe(df_mapping.head(), use_container_width=True)

    columns = df_mapping.columns.tolist()

    st.subheader("步驟二：選擇欄位對應")
    col1, col2 = st.columns(2)
    with col1:
        cn_col = st.selectbox("「頁面中文名稱」對應欄位", options=columns, index=0)
    with col2:
        en_col = st.selectbox("「事件英文 ID」對應欄位", options=columns, index=min(1, len(columns) - 1))

    # 建立選項清單
    options = []
    for _, row in df_mapping.iterrows():
        cn = str(row[cn_col])
        en = str(row[en_col])
        options.append(f"{cn}（{en}）")

    st.subheader("步驟三：選擇要查詢的事件")
    selected = st.selectbox("請選擇事件", options=options)

    if st.button("🔍 查詢事件資料"):
        if not mp_username or not mp_secret or not mp_project_id:
            st.error("請先在左側欄填寫 Mixpanel 認證資訊。")
        else:
            idx = options.index(selected)
            event_id = str(df_mapping.iloc[idx][en_col])
            chinese_name = str(df_mapping.iloc[idx][cn_col])

            with st.spinner("正在向 Mixpanel 查詢資料，請稍候⋯"):
                try:
                    df_result = fetch_event_data(mp_username, mp_secret, mp_project_id, event_id)
                except requests.exceptions.HTTPError as e:
                    st.error(f"Mixpanel API 回傳錯誤：{e.response.status_code} — {e.response.text}")
                    st.stop()
                except requests.exceptions.ConnectionError:
                    st.error("無法連線至 Mixpanel，請檢查網路連線。")
                    st.stop()
                except Exception as e:
                    st.error(f"查詢時發生錯誤：{e}")
                    st.stop()

            if df_result.empty:
                st.warning("該事件在過去 30 天內無任何資料。")
            else:
                st.success(f"成功取得 {len(df_result)} 天的資料！")

                # ── 繪製折線圖 ──
                fig = go.Figure()
                fig.add_trace(
                    go.Scatter(
                        x=df_result["日期"],
                        y=df_result["事件次數"],
                        mode="lines+markers",
                        line=dict(color="#E62117", width=2),
                        marker=dict(size=5),
                        name=chinese_name,
                    )
                )
                fig.update_layout(
                    title=f"「{chinese_name}」每日觸發次數（過去 30 天）",
                    xaxis_title="日期",
                    yaxis_title="事件次數",
                    template="plotly_white",
                )
                st.plotly_chart(fig, use_container_width=True)

                # ── AI 數據摘要 ──
                st.subheader("🤖 數據摘要")
                insight = generate_insight(df_result, event_id, chinese_name)
                st.text_area("AI 分析結果", value=insight, height=220, disabled=True)

"""LLM-powered marketing memo using Anthropic Claude.

The generator takes the event's 30-day dataframe plus any matching history
annotations from history_events.json, and asks Claude to write a short
marketing memo that explicitly ties the numbers to known activities.
"""
from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Optional

import pandas as pd

HISTORY_PATH = Path(__file__).parent / "history_events.json"
MODEL = "claude-sonnet-4-6"


def load_history() -> list[dict]:
    if not HISTORY_PATH.exists():
        return []
    try:
        return json.loads(HISTORY_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []


def save_history(entries: list[dict]) -> None:
    HISTORY_PATH.write_text(
        json.dumps(entries, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def add_history_entry(entry_date: str, event_id: str, label: str,
                      note: str = "", tag: str = "") -> list[dict]:
    entries = load_history()
    entries.append({
        "date": entry_date,
        "event_id": event_id,
        "label": label,
        "note": note,
        "tag": tag,
        "created_at": datetime.now().isoformat(timespec="seconds"),
    })
    save_history(entries)
    return entries


def delete_history_entry(index: int) -> list[dict]:
    entries = load_history()
    if 0 <= index < len(entries):
        entries.pop(index)
        save_history(entries)
    return entries


def _match_history(df: pd.DataFrame, event_id: str) -> list[dict]:
    """Pick history entries that fall inside the dataframe's date window
    and that either match this event_id or are tagged as global."""
    if df.empty:
        return []
    start = df["日期"].min().date()
    end = df["日期"].max().date()
    matched = []
    for h in load_history():
        try:
            d = datetime.strptime(h["date"], "%Y-%m-%d").date()
        except (ValueError, KeyError):
            continue
        if not (start <= d <= end):
            continue
        if h.get("event_id") and h["event_id"] != event_id and h["event_id"] != "*":
            continue
        matched.append(h)
    return matched


def _rule_based_summary(df: pd.DataFrame, event_name: str, chinese_name: str) -> str:
    if df.empty:
        return "查無資料，無法產生摘要。"
    total = int(df["事件次數"].sum())
    avg = df["事件次數"].mean()
    max_row = df.loc[df["事件次數"].idxmax()]
    min_row = df.loc[df["事件次數"].idxmin()]
    first_half = df.iloc[: len(df) // 2]["事件次數"].mean()
    second_half = df.iloc[len(df) // 2:]["事件次數"].mean()
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
        f"• 最高日：{max_row['日期'].strftime('%Y-%m-%d')}（{int(max_row['事件次數']):,} 次）\n"
        f"• 最低日：{min_row['日期'].strftime('%Y-%m-%d')}（{int(min_row['事件次數']):,} 次）\n"
        f"• 整體趨勢：{trend}\n"
    )


def generate_memo(df: pd.DataFrame, event_id: str, chinese_name: str,
                  api_key: Optional[str] = None) -> str:
    """Generate a marketing memo. Uses Claude if ANTHROPIC_API_KEY is set,
    otherwise falls back to the rule-based summary."""
    if df.empty:
        return "查無資料，無法產生營銷備忘錄。"

    api_key = api_key or os.environ.get("ANTHROPIC_API_KEY")
    rule_summary = _rule_based_summary(df, event_id, chinese_name)
    history_matches = _match_history(df, event_id)

    if not api_key:
        base = rule_summary
        if history_matches:
            base += "\n📎 對應歷史活動標註：\n"
            for h in history_matches:
                base += f"  • {h['date']} — {h.get('label', '')}"
                if h.get("note"):
                    base += f"（{h['note']}）"
                base += "\n"
        base += "\n💡 （未設定 ANTHROPIC_API_KEY，使用規則式摘要。設定後可啟用 Claude 深度分析。）"
        return base

    try:
        from anthropic import Anthropic
    except ImportError:
        return rule_summary + "\n\n⚠️ 未安裝 anthropic 套件，無法啟用 LLM 分析。"

    daily_lines = "\n".join(
        f"  {row['日期'].strftime('%Y-%m-%d')}: {int(row['事件次數'])}"
        for _, row in df.iterrows()
    )
    if history_matches:
        history_block = "\n".join(
            f"  - {h['date']} [{h.get('tag', '')}] {h.get('label', '')}"
            f"{'：' + h['note'] if h.get('note') else ''}"
            for h in history_matches
        )
    else:
        history_block = "  （無已標註的歷史活動）"

    prompt = f"""你是一位資深數據分析師兼營銷顧問。請根據以下 Mixpanel 事件資料與已知的歷史營銷活動標註，產出一份繁體中文的「營銷備忘錄」。

【事件資訊】
- 中文名稱：{chinese_name}
- 事件 ID：{event_id}

【過去 30 天每日觸發次數】
{daily_lines}

【期間內的歷史營銷活動標註】
{history_block}

請輸出一份結構化的備忘錄，包含以下四個區段（每段 2-4 句話，務必具體、避免泛泛而談）：

1. **數據摘要**：總量、日均、最高/最低日、整體趨勢。
2. **異常與洞察**：指出異常高峰或低谷的日期，若與歷史活動標註相符，請明確說明因果關係。
3. **營銷建議**：根據資料模式與歷史活動成效，給出 2-3 條可執行的下一步建議。
4. **後續追蹤**：建議接下來要關注哪些指標或驗證哪些假設。

請直接輸出備忘錄內容，不要額外開場白。"""

    try:
        client = Anthropic(api_key=api_key)
        resp = client.messages.create(
            model=MODEL,
            max_tokens=1500,
            messages=[{"role": "user", "content": prompt}],
        )
        text_parts = [b.text for b in resp.content if hasattr(b, "text")]
        return "\n".join(text_parts).strip() or rule_summary
    except Exception as e:
        return rule_summary + f"\n\n⚠️ LLM 呼叫失敗，已回退規則式摘要：{e}"

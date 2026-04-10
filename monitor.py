"""Background monitor: runs daily via GitHub Actions.

Reads config_events.xlsx from repo, fetches every event's last 30 days from
Mixpanel, compares today's count to the 7-day rolling average, and pushes a
Google Chat alert when the drop exceeds the threshold.

Environment variables (set as GitHub repo secrets for Actions):
  MIXPANEL_USERNAME
  MIXPANEL_SECRET
  MIXPANEL_PROJECT_ID
  GOOGLE_CHAT_WEBHOOK
  EVENTS_FILE       (optional, default: config_events.xlsx)
  DROP_THRESHOLD    (optional, default: 0.20)
  CN_COL            (optional, default: 頁面中文名稱)
  EN_COL            (optional, default: 事件英文ID)
"""
from __future__ import annotations

import os
import sys
from datetime import datetime

from dotenv import load_dotenv
load_dotenv()

import pandas as pd

from mixpanel_client import fetch_event_data, compute_drop_vs_baseline
from notifier import send_chat_alert, send_chat_message


def load_events(path: str, cn_col: str, en_col: str) -> list[dict]:
    if path.endswith(".csv"):
        df = pd.read_csv(path)
    else:
        df = pd.read_excel(path)

    if cn_col not in df.columns or en_col not in df.columns:
        raise ValueError(
            f"Excel 欄位不符：需要 '{cn_col}' 與 '{en_col}'，"
            f"實際欄位：{df.columns.tolist()}"
        )

    events = []
    for _, row in df.iterrows():
        events.append({
            "chinese_name": str(row[cn_col]).strip(),
            "event_id": str(row[en_col]).strip(),
        })
    return events


def run_monitor() -> int:
    username = os.environ.get("MIXPANEL_USERNAME")
    secret = os.environ.get("MIXPANEL_SECRET")
    project_id = os.environ.get("MIXPANEL_PROJECT_ID")
    webhook = os.environ.get("GOOGLE_CHAT_WEBHOOK")

    missing = [k for k, v in {
        "MIXPANEL_USERNAME": username,
        "MIXPANEL_SECRET": secret,
        "MIXPANEL_PROJECT_ID": project_id,
        "GOOGLE_CHAT_WEBHOOK": webhook,
    }.items() if not v]
    if missing:
        print(f"[ERROR] 缺少環境變數: {', '.join(missing)}", file=sys.stderr)
        return 1

    events_file = os.environ.get("EVENTS_FILE", "config_events.xlsx")
    threshold = float(os.environ.get("DROP_THRESHOLD", "0.20"))
    cn_col = os.environ.get("CN_COL", "頁面中文名稱")
    en_col = os.environ.get("EN_COL", "事件英文ID")

    if not os.path.exists(events_file):
        print(f"[ERROR] 找不到事件對照表：{events_file}", file=sys.stderr)
        return 1

    events = load_events(events_file, cn_col, en_col)
    print(f"[INFO] 載入 {len(events)} 筆事件，開始監控…")

    alerts: list[dict] = []
    failures: list[str] = []

    for i, ev in enumerate(events, 1):
        name = ev["chinese_name"]
        event_id = ev["event_id"]
        print(f"[{i}/{len(events)}] 抓取 {name} ({event_id})…")

        try:
            df = fetch_event_data(username, secret, project_id, event_id, days=30)
        except Exception as e:
            failures.append(f"{name} ({event_id}): {e}")
            continue

        metrics = compute_drop_vs_baseline(df, baseline_days=7)
        if not metrics["has_data"]:
            continue

        drop = metrics["drop_ratio"]
        if drop <= -threshold:
            alerts.append({
                "name": name,
                "event_id": event_id,
                "today": metrics["today_count"],
                "baseline": metrics["baseline_avg"],
                "drop_ratio": drop,
            })
            print(f"  ⚠️ 跌幅 {drop*100:.1f}%（今日 {metrics['today_count']} vs 7日均 {metrics['baseline_avg']:.1f}）")

    today_str = datetime.now().strftime("%Y-%m-%d")

    if alerts:
        for a in alerts:
            send_chat_alert(
                webhook,
                title=f"事件異常跌幅警報：{a['name']}",
                subtitle=f"AskMXP 每日監控 · {today_str}",
                rows=[
                    ("事件 ID", a["event_id"]),
                    ("今日觸發次數", f"{a['today']:,}"),
                    ("過去 7 日均值", f"{a['baseline']:.1f}"),
                    ("跌幅", f"{a['drop_ratio']*100:.1f}%"),
                    ("警示門檻", f"-{threshold*100:.0f}%"),
                ],
                severity="CRITICAL",
            )
        print(f"[INFO] 已發送 {len(alerts)} 則警報")
    else:
        print("[INFO] 所有事件皆在正常範圍，未觸發警報")

    if failures:
        fail_text = "\n".join(f"• {x}" for x in failures)
        send_chat_message(webhook, f"⚠️ AskMXP 監控部分失敗 ({today_str}):\n{fail_text}")
        print(f"[WARN] {len(failures)} 個事件抓取失敗")

    return 0


if __name__ == "__main__":
    sys.exit(run_monitor())

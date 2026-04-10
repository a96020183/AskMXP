# AskMXP — Mixpanel 事件查詢工具

透過上傳事件對照表，快速查詢 Mixpanel 事件過去 30 天的每日數據並產生視覺化圖表與摘要。

## 安裝方式

```bash
# 1. 建立虛擬環境（建議）
python -m venv venv
# Windows
venv\Scripts\activate
# macOS / Linux
source venv/bin/activate

# 2. 安裝相依套件
pip install -r requirements.txt
```

## 啟動應用程式

```bash
streamlit run app.py
```

瀏覽器會自動開啟 `http://localhost:8501`。

## 使用流程

1. **填寫認證資訊**：在左側欄輸入 Mixpanel 服務帳號的 Username、Secret 及專案 ID。
2. **上傳對照表**：上傳包含「頁面中文名稱」與「事件英文 ID」的 Excel 或 CSV 檔案。
3. **選擇欄位**：透過下拉選單指定哪一欄是中文名稱、哪一欄是英文事件 ID。
4. **查詢事件**：選擇要查詢的事件後按下「查詢事件資料」按鈕。
5. **檢視結果**：系統會顯示折線圖及 AI 數據摘要。

## 如何取得 Mixpanel 服務帳號

1. 登入 [Mixpanel](https://mixpanel.com/)。
2. 點擊右上角齒輪圖示，進入 **Organization Settings**。
3. 選擇左側選單中的 **Service Accounts**。
4. 點擊 **+ Add Service Account**，建立新的服務帳號。
5. 建立完成後即可取得 **Username** 與 **Secret**。
6. **專案 ID** 可在 **Project Settings** 頁面中找到。

> ⚠️ 請妥善保管服務帳號的 Secret，切勿將其提交至版本控制系統。

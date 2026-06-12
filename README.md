# CodexUsage

Windows 系統匣小工具，即時監控 Codex CLI（ChatGPT Plus / Pro 訂閱）的剩餘用量。

不用再切回終端機打 `/status`——session 與每週額度的使用率直接顯示在螢幕角落，快超標時主動跳通知提醒。

是 [ClaudeUsage](https://github.com/pigbaby1231/windows-claude-usage) 的 Codex 版本。

## 功能

- **系統匣圖示**：直接畫出 5 小時 session 使用率數字，顏色隨用量變化（綠 <60%、黃 60–85%、紅 ≥85%），滑鼠懸停顯示完整資訊與重置時間
- **懸浮顯示窗**：永遠置頂的小長條，直接顯示 `S 33%  W 11%`，不用懸停就能看到；可拖曳到任意位置（位置會記住）、右鍵隱藏、從系統匣選單重新開啟
- **用量提醒**：session 或週用量達 **85%** 與 **95%** 時各跳一次 Windows 通知（每個重置週期重新計算）
- **每 3 分鐘自動更新**，右鍵選單可手動立即更新
- **單一實例保護**：重複執行不會開出第二份

## 運作原理

工具讀取本機 Codex CLI 登入後留下的 OAuth token（`~/.codex/auth.json`），向 ChatGPT 後端的用量端點發出查詢，取得與 Codex CLI 內建 `/status` 指令相同的官方數據：

- `primary_window`：主要窗口的使用率與重置時間（Plus / Pro 為 5 小時 session；free 方案為 30 天）
- `secondary_window`：次要窗口（Plus / Pro 為每週額度；free 方案沒有此窗口）

顯示標籤會依 API 回傳的窗口長度自動標成 `5h`、`7d` 或 `30d`，free 與付費方案都適用。

**對 `~/.codex` 完全唯讀**：只讀取 `auth.json`，不寫入任何檔案。Codex 的 access token 約一小時過期，遇到 401/403 時工具會用 refresh token 向 `auth.openai.com` 換一顆新 token，但**只放在記憶體**，不會寫回 `auth.json`（避免和 Codex CLI 自己的刷新流程打架）。token 不會送往 OpenAI 以外的任何地方。

## 需求

- Windows 10 / 11
- 已安裝 [Codex CLI](https://developers.openai.com/codex/cli) 並以 **ChatGPT 訂閱帳號**登入過（`codex login`）
- 從原始碼執行需 Python 3.10+；用打包好的 exe 則不需要

## 安裝與使用

### 方法一：從原始碼執行

```powershell
git clone https://github.com/pigbaby1231/windows-codex-usage.git
cd windows-codex-usage
pip install -r requirements.txt
pythonw codex_usage_tray.pyw
```

### 方法二：自行打包成單一 exe

```powershell
pip install -r requirements.txt pyinstaller
pyinstaller --onefile --noconsole --name CodexUsage codex_usage_tray.pyw
# 成品在 dist\CodexUsage.exe，複製到任何電腦皆可直接執行（該電腦需登入過 Codex CLI）
```

> 自行打包的 exe 沒有數位簽章，第一次執行時 Windows SmartScreen 可能警告，
> 點「其他資訊 → 仍要執行」即可。

### 開機自動啟動

`Win + R` 輸入 `shell:startup`，把 `CodexUsage.exe`（或 `codex_usage_tray.pyw`）的捷徑放進去。

## 設定檔

懸浮窗的位置與開關狀態存在：

```
%APPDATA%\CodexUsage\config.json
```

刪除此檔即可重置為預設（懸浮窗開啟、貼齊右下角）。

## 已知限制

- 用量端點屬於 OpenAI **未公開文件的內部 API**（Codex CLI 的 `/status` 也是用它），格式若改版工具會顯示灰色「?」，需配合更新
- 僅支援 Windows（系統匣與通知皆使用 Windows 專屬機制）
- 顯示的是 ChatGPT 訂閱方案的額度使用率；以 API key（pay-as-you-go）登入的帳號不適用
- 若同時有自訂 `CODEX_HOME` 環境變數，工具會跟著讀該位置的 `auth.json`

## 免責聲明

本工具為社群作品，與 OpenAI 無關。依賴未公開的內部端點，隨時可能因官方改版而失效。

# Highlight Builder 與 highlight_editor 整合

更新日期：2026-10-11

本文記錄目前本機網頁與剪輯後台的整合方式。Highlight Builder 負責提供操作介面及流程編排；`highlight_editor` 負責驗證剪輯專案、逐影格裁切片段並輸出影片。兩者是同一個 repository 中可分開呼叫的元件。

## 目前功能

| 功能 | 實作位置 |
|---|---|
| 選擇影片與比賽、篩選事件、產生工作預覽 | `web/`、`highlight_builder/job.py` |
| 執行事件資料與短片工作、顯示進度 | `highlight_builder/runner.py`、`highlight_builder/server.py` |
| 播放、裁切、排序片段 | `web/app.js` |
| 取得片段影格資訊、驗證專案、背景輸出影片 | `highlight_builder/editor.py`、`highlight_editor/` |
| 輸出完成後播放影片 | `highlight_builder/server.py` 的 `/media/` 路由 |

網頁剪輯狀態存在頁面中；輸出時會組成 `highlight_editor` 的 Project JSON，並以實際片段 fps 換算影格範圍。輸出檔限制在 `out/` 底下。

## 架構與資料流

```text
瀏覽器（web/）
  │ HTTP：docs/api/highlight-builder.openapi.yaml
  ▼
本機伺服器（highlight_builder/server.py）
  ├── 工作流程 CLI（highlight_builder/runner.py）
  │     └── MLB ground truth、make_clip、mask_flags
  └── 剪輯橋接（highlight_builder/editor.py）
        └── highlight_editor CLI：list、validate、render
```

瀏覽器透過本機 HTTP API 呼叫伺服器；伺服器以子程序執行工具，不直接由瀏覽器啟動 Python。工作流程與剪輯輸出使用各自的端點及 CLI。伺服器回傳執行結果，並以 Server-Sent Events 傳送長工作業進度。

## API 與剪輯契約

- `docs/api/highlight-builder.openapi.yaml` 定義網頁使用的 HTTP API，包括工作預覽／執行，以及片段清單、剪輯驗證、輸出與進度端點。
- `docs/api/highlight-editor.openapi.yaml` 定義剪輯資料契約；它描述資料格式，不是 HTTP API。
- `highlight_editor` 的 `list` 回傳片段的 fps、影格數與媒體資訊；`validate` 檢查 Project；`render` 輸出影片與各片段在成品中的位置。
- 輸出路徑預設位於 `out/reels/`。輸出存在時須明確允許覆寫。
- 剪輯後台以單一 JSON 回報結果，進度以 JSON Lines 輸出；錯誤代碼與 HTTP 狀態由橋接層轉換。

更多 Project 欄位、影格區間、錯誤格式與 CLI 範例，見 [highlight_editor/README.md](../highlight_editor/README.md)。

## 執行與測試

在 repository 根目錄執行：

```bash
python3 -m highlight_builder.server
python3 -m unittest discover -s tests -t .
python3 -m unittest discover -s highlight_editor/tests -t .
```

網頁預設位於 `http://localhost:8787`，工具頁路徑為 `/app`。伺服器只使用 Python 標準函式庫；工具執行環境需有 `requests`、`opencv-python`、`numpy`，剪輯輸出另需 `ffmpeg` 與 `ffprobe`。缺少 ffmpeg 或影片仍是 Git LFS 佔位檔時，Builder 會進入示範模式。

## 已知限制

- 整場影片不在 repository 中；目前實際操作需先取得 LFS 影片，並可使用已提供的半局影片。
- 比賽編號需手動輸入；每次工作執行會重新取得 MLB 資料並計算事件時間，目前不能直接沿用既有 ground truth CSV。
- 頁面上的裁切與排序狀態尚未另存成可重新載入的剪輯專案。

## 相關實作

- `highlight_builder/server.py`：HTTP API、工作管理、SSE 與媒體播放。
- `highlight_builder/runner.py`：工作流程子程序、進度與示範模式。
- `highlight_builder/job.py`：工作請求驗證及工具參數轉換。
- `highlight_builder/editor.py`：呼叫剪輯後台並轉換結果。
- `highlight_editor/`：Project 驗證與影片輸出引擎。
- `web/app.js`：前端工作流程、片段編輯與匯出。

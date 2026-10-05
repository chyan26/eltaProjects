# MLB Highlight Ground Truth

這個專案整合 MLB 的 `/feed/live` 與 `/content` API，將棒球轉播影片的逐球事件換算成影片時間，並標記 MLB 官方精華，產生 CSV ground truth。

`/feed/live` 提供逐球時間；`/content` 提供官方精華標題、長度與播放 URL。程式預設輸出所有事件，官方精華只作為標籤，不會過濾掉普通事件。這是 ground truth 產生器，不是影片分類模型。

## 專案結構

```text
sport_hightlight/
├── csv_data/
│   ├── bottom2_ground_truth.csv
│   └── full_game_ground_truth.csv
├── dataset/
│   ├── bottom2/
│   └── full_game/
│       ├── positive/
│       ├── hard_negative/
│       └── manifest.csv
├── make_clip.py
└── mlb_highlight_groundtruth.py
video/
├── eltaMax10_Reds_Brewers_0913.mp4              # 整場，連續錄影
├── eltaMax10_Reds_Brewers_0913_bottom2nd.mp4
└── eltaMax10_Reds_Brewers_0913_top6th.mp4
```

影片使用 Git LFS 管理。實際下載影片前，請先安裝並執行：

```bash
git lfs install
git lfs pull
```

## 安裝依賴

建議使用虛擬環境：

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install requests opencv-python numpy
```

音訊校準另外需要系統已安裝 `ffmpeg`：

```bash
ffmpeg -version
```

## 產生單一半局 ground truth

### bottom2

`--auto-anchor` 會自動尋找影片中首次出現的球場畫面與 live scorebug，避免手動輸入影片起始秒數。

```bash
python sport_hightlight/mlb_highlight_groundtruth.py \
  --game-pk 823734 \
  --video video/eltaMax10_Reds_Brewers_0913_bottom2nd.mp4 \
  --clip-inning 2 \
  --clip-half bottom \
  --auto-anchor \
  --audio-calibrate \
  --out sport_hightlight/csv_data/bottom2_ground_truth.csv
```

目前已驗證的結果：

```text
自動 anchor：約 60.0 秒
全壘打：約 374.9 秒（約 6:15）
音訊校準：約 374.3 秒
```

### top6

```bash
python sport_hightlight/mlb_highlight_groundtruth.py \
  --game-pk 823734 \
  --video video/eltaMax10_Reds_Brewers_0913_top6th.mp4 \
  --clip-inning 6 \
  --clip-half top \
  --auto-anchor \
  --audio-calibrate \
  --out sport_hightlight/csv_data/top6_ground_truth.csv
```

目前已驗證的結果：

```text
自動 anchor：約 34.5 秒
top6 全壘打：約 369.5 秒
```

`--audio-calibrate` 會在事件附近找音訊能量上升沿，並新增：

- `audio_calibrated_video_seconds`：音訊校準後的影片秒數
- `audio_offset_seconds`：相對於 `predicted_video_seconds` 的校正量

原始 API 預測值會保留，方便比較音訊校準前後的差異。搜尋範圍可用
`--audio-search-before` 和 `--audio-search-after` 調整。

## 事件模式

預設輸出指定半局的所有事件。若只需要得分或關鍵字事件，可加入 `--key-events-only`：

```bash
python sport_hightlight/mlb_highlight_groundtruth.py \
  --game-pk 823734 \
  --video video/eltaMax10_Reds_Brewers_0913_bottom2nd.mp4 \
  --clip-inning 2 \
  --clip-half bottom \
  --auto-anchor \
  --key-events-only \
  --out sport_hightlight/csv_data/bottom2_key_events.csv
```

事件配對主要依官方精華的 `id`（多半是 play 描述，或「投手-in-play-打者」）、打者與投手姓名、關鍵字中的球員，以及標題內有出現時的局數/半局；同分無法判定時不標記，同一個 play 有多支精華（如 ABS 挑戰）只保留一支。官方精華 URL 是 MLB 提供的剪輯影片，不是本地完整轉播影片中的時間位置。

## CSV 欄位

- `game_pk`、`video`：比賽與來源影片
- `inning`：局數
- `half`：`top` 或 `bottom`
- `event_type`：事件類型，例如 `Home Run`
- `description`：MLB API 的事件描述
- `is_scoring_play`：是否為得分事件
- `predicted_video_seconds`：事件在影片中的預測秒數
- `audio_calibrated_video_seconds`：音訊校準後的時間
- `is_official_highlight`：是否配對到 MLB 官方精華
- `official_highlight_title`、`official_highlight_duration`、`official_highlight_url`：官方精華資訊

## 產生短片資料集

使用 [make_clip.py](sport_hightlight/make_clip.py) 讀取 ground truth，預設擷取事件前 8 秒、後 12 秒，並建立 `positive`、`hard_negative` 與 `manifest.csv`：

```bash
python sport_hightlight/make_clip.py \
  --ground-truth sport_hightlight/csv_data/bottom2_ground_truth.csv \
  --video video/eltaMax10_Reds_Brewers_0913_bottom2nd.mp4 \
  --out-dir sport_hightlight/dataset/bottom2
```

需要系統安裝 `ffmpeg` 與 `ffprobe`。`positive` 是 MLB 官方精華事件，其餘事件放在 `hard_negative`；兩者與 `manifest.csv` 都位於輸出目錄。短片不進版控（見 `.gitignore`），只保留 `manifest.csv`。

整場資料集（指令見「完整轉播模式」）：72 個 20 秒短片，約 1.3 GB，片段之間沒有重疊；依目前配對規則，`positive` 為 16 個、`hard_negative` 為 56 個。`manifest.csv` 除了標籤與時間，還記錄 `game_pk` 與 `source_video`，合併多場比賽時可追溯來源。已存在的短片預設不重新編碼，需要重切請加 `--overwrite`。

## 手動指定 anchor

如果影片的轉播版型不同，或自動偵測找不到正確畫面，可以改用手動 anchor：

```bash
python sport_hightlight/mlb_highlight_groundtruth.py \
  --game-pk 823734 \
  --video video/example.mp4 \
  --clip-inning 2 \
  --clip-half bottom \
  --anchor-video-seconds 60.1 \
  --out ground_truth.csv
```

`--anchor-video-seconds` 是指定半局第一個打席開始在影片中的秒數，不一定是影片的第 0 秒。

## 完整轉播模式

整場錄影是連續錄影時，影片時間與 API 時間的偏移量在全場近乎固定，所以用一個 `--game-offset-seconds`（第一個打席在影片中的秒數）換算全部事件。完整流程（在專案根目錄執行）：

```bash
cd /Users/chyan/Projects/eltaProjects
conda activate base

# 1. 產生整場 ground truth
python sport_hightlight/mlb_highlight_groundtruth.py \
  --game-pk 823734 \
  --video video/eltaMax10_Reds_Brewers_0913.mp4 \
  --game-offset-seconds 150.5 \
  --out sport_hightlight/csv_data/full_game_ground_truth.csv

# 2. 刪除舊的短片資料集（避免舊分類殘留）
rm -rf sport_hightlight/dataset/full_game

# 3. 依 ground truth 從整場影片切出短片（約 3.5 分鐘）
python sport_hightlight/make_clip.py \
  --ground-truth sport_hightlight/csv_data/full_game_ground_truth.csv \
  --video video/eltaMax10_Reds_Brewers_0913.mp4 \
  --out-dir sport_hightlight/dataset/full_game

# 4. 檢查結果（預期 72 筆、16 個 positive、56 個 hard_negative）
python - <<'EOF'
import csv
rows = list(csv.DictReader(open("sport_hightlight/csv_data/full_game_ground_truth.csv")))
print(len(rows), sum(r["is_official_highlight"] == "True" for r in rows))
EOF
ls sport_hightlight/dataset/full_game/positive | wc -l
ls sport_hightlight/dataset/full_game/hard_negative | wc -l
```

第 2 步的 `rm -rf` 會永久刪除 `dataset/full_game/`，執行前請確認路徑。官方精華標記改變時，`positive` 與 `hard_negative` 的分類會跟著改變，所以重跑前需要先刪除舊資料夾；只加 `--overwrite` 的話，舊分類資料夾中的短片不會被移動。

這支影片的值是用已知片段的音訊互相關求得：bottom2 為 151.0 秒、top6 為 150.5 秒，相隔約 3,350 秒仍只差 0.5 秒。輸出的全壘打時間（bottom2：2029.4、top6：5401.5）與獨立量測一致。

求得偏移量的方式：有已知片段時，將片段音訊與整場音訊互相關，得到片段在整場的起點，再加上片段的 anchor 減去該半局的 API 相對時間。

> 測試過「逐半局用 scorebug 邊緣搜尋 anchor」，在這支影片上偏差約 ±15 秒且假邊緣多（轉場、重播），不夠精確，因此沒有採用。直接用整場音訊與事件時間求偏移也不可靠（事件附近的起音離散度太大）。若你的錄影有剪接或廣告，偏移量會隨半局變化，這個模式就不適用。

## 目前限制

- `--auto-anchor` 的視覺啟發式是依目前轉播畫面的球場與右下角 live scorebug 設計；不同電視台或畫面配置可能需要調整。
- 程式使用 API 產生標籤，不會直接從影片畫面辨識事件。
- `--auto-anchor` 找不到可靠訊號時會停止，請改用手動 anchor。
- 音訊校準找的是音量上升沿，可能對應播報、觀眾聲或撞擊聲，不保證就是球棒擊球瞬間；應搭配人工抽查與驗證集評估。
- `game-pk` 必須對應到影片中的同一場比賽。

## 下一步

可以使用 `sport_hightlight/csv_data/bottom2_ground_truth.csv` 作為開發樣本，調整影片特徵與偵測規則，再用 `sport_hightlight/csv_data/top6_ground_truth.csv` 作為未參與調參的驗證樣本，計算精華事件偵測的 precision、recall 與時間誤差。

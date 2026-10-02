# MLB Highlight Ground Truth

這個專案整合 MLB 的 `/feed/live` 與 `/content` API，將棒球轉播影片的逐球事件換算成影片時間，並標記 MLB 官方精華，產生 CSV ground truth。

`/feed/live` 提供逐球時間；`/content` 提供官方精華標題、長度與播放 URL。程式預設輸出所有事件，官方精華只作為標籤，不會過濾掉普通事件。這是 ground truth 產生器，不是影片分類模型。

## 專案結構

```text
sport_hightlight/
└── mlb_highlight_groundtruth.py
video/
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
  --out bottom2_ground_truth.csv
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
  --out top6_ground_truth.csv
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
  --out bottom2_key_events.csv
```

事件配對會參考局數、上下半局、事件類型與球員/事件描述。官方精華 URL 是 MLB 提供的剪輯影片，不是本地完整轉播影片中的時間位置。

## CSV 欄位

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
  --ground-truth csv_data/bottom2_ground_truth.csv \
  --video video/eltaMax10_Reds_Brewers_0913_bottom2nd.mp4 \
  --out-dir dataset/bottom2
```

需要系統安裝 `ffmpeg` 與 `ffprobe`。`positive` 是 MLB 官方精華事件，其餘事件放在 `hard_negative`。

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

若輸入的是涵蓋整場比賽的完整轉播，可以使用比分板 ROI 偵測半局切換：

```bash
python sport_hightlight/mlb_highlight_groundtruth.py \
  --game-pk 823734 \
  --video full_broadcast.mp4 \
  --roi X Y WIDTH HEIGHT \
  --sample-fps 1 \
  --out ground_truth.csv
```

ROI 格式為：`x y width height`。需要依實際影片畫面指定比分板所在區域。

## 目前限制

- `--auto-anchor` 的視覺啟發式是依目前轉播畫面的球場與右下角 live scorebug 設計；不同電視台或畫面配置可能需要調整。
- 程式使用 API 產生標籤，不會直接從影片畫面辨識事件。
- `--auto-anchor` 找不到可靠訊號時會停止，請改用手動 anchor。
- 音訊校準找的是音量上升沿，可能對應播報、觀眾聲或撞擊聲，不保證就是球棒擊球瞬間；應搭配人工抽查與驗證集評估。
- `game-pk` 必須對應到影片中的同一場比賽。

## 下一步

可以使用 `bottom2_ground_truth.csv` 作為開發樣本，調整影片特徵與偵測規則，再用 `top6_ground_truth.csv` 作為未參與調參的驗證樣本，計算精華事件偵測的 precision、recall 與時間誤差。

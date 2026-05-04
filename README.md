# ボートレース予想 MCPサーバー

claude.aiモバイルアプリから「住之江10R分析して」と打つだけで、
必要な全データを自動取得して構造化された分析結果を返すシステム。

## 提供ツール（7つ）

| ツール名 | 説明 | データソース |
|---|---|---|
| `get_race_card` | 出走表（選手成績・モーター・ボート情報） | BoatraceOpenAPI |
| `get_pre_race_info` | 直前情報（展示タイム・展示ST・気象） | BoatraceOpenAPI |
| `get_odds` | 3連単オッズ（人気順） | boatrace.jp スクレイピング |
| `get_racer_course_stats` | 選手コース別成績 | boatrace.jp スクレイピング |
| `get_recent_10_races` | 枠番別過去10走 | boatrace.jp スクレイピング |
| `get_learning_rules` | 蓄積学習ルール（R1〜R14＋会場別・条件別） | ローカルYAMLファイル |
| `calc_trigami_threshold` | トリガミ回避ライン計算 | 計算式 |

## ルール管理（YAMLファイル）

```
rules/
├── general.yaml          ← R1〜R14（全会場共通ルール）
├── conditions.yaml       ← 天候・風・波高・季節別ルール
├── venues/               ← 全24場の会場別ルール
│   ├── 04_heiwajima.yaml ← 平和島（記入済み）
│   ├── 12_suminoe.yaml   ← 住之江（記入済み）
│   └── ...               ← 他22場（テンプレートあり）
└── racers/               ← 選手別蓄積ナレッジ（レースごとに追加）
```

新しいルールを追加するときは、該当のYAMLファイルを編集するだけ。
コードの変更は不要。

## セットアップ

### 1. Python仮想環境を作る

```bash
cd ~/boatrace-mcp
python3 -m venv .venv
source .venv/bin/activate
```

### 2. 必要なライブラリをインストール

```bash
pip install -r requirements.txt
```

### 3. ローカルで動作確認

```bash
python server.py
```

### 4. 動作テスト（別ターミナルで）

```bash
# MCPのテストツールで確認
mcp dev server.py
```

## Renderへのデプロイ

1. GitHubにリポジトリを作成してPush
2. Render.com でWebServiceを新規作成
3. 以下を設定：
   - **Build Command**: `pip install -r requirements.txt`
   - **Start Command**: `python server.py`
   - **Environment Variable**: `PORT` は自動設定
4. DeployするとURLが発行される（例: `https://boatrace-mcp.onrender.com`）

## claude.aiへの接続設定

Renderのデプロイ後、claude.aiのMCP設定で以下を入力：

```
URL: https://your-app-name.onrender.com/mcp
```

## 会場ID対応表

| ID | 会場 | ID | 会場 | ID | 会場 |
|---|---|---|---|---|---|
| 1 | 桐生 | 9 | 津 | 17 | 宮島 |
| 2 | 戸田 | 10 | 三国 | 18 | 徳山 |
| 3 | 江戸川 | 11 | びわこ | 19 | 下関 |
| 4 | 平和島 | 12 | 住之江 | 20 | 若松 |
| 5 | 多摩川 | 13 | 尼崎 | 21 | 芦屋 |
| 6 | 浜名湖 | 14 | 鳴門 | 22 | 福岡 |
| 7 | 蒲郡 | 15 | 丸亀 | 23 | 唐津 |
| 8 | 常滑 | 16 | 児島 | 24 | 大村 |

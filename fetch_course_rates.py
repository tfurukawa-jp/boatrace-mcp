#!/usr/bin/env python3
"""
boatrace.jp「ボートレース場データ」から会場別・コース別の入着率を取得し、
data/course_rates.yaml に保存する。手動実行用。

    python fetch_course_rates.py            # 全24場を取得
    python fetch_course_rates.py 11 12      # 会場を指定して取得（動作確認用）

取得元: https://www.boatrace.jp/owpc/pc/data/stadium?jcd={01〜24}

このページには集計期間の異なる複数の表が載っている。
  - コース別入着率＆決まり手  … 直近3ヶ月
  - 枠番別コース取得率        … 直近3ヶ月
  - 季節別のコース別入着率    … 春季/夏季/秋季/冬季（それぞれ別の3ヶ月）
表ごとに「（集計期間：YYYY/MM/DD～YYYY/MM/DD）」が併記されているため、
表と期間を DOM 上の位置関係で対応づけて記録する。
見出し・期間の並び順を決め打ちにすると、ページ構成が変わったときに
別の期間の数値を正しい期間として保存してしまうため、必ずアンカーで取る。
"""
import json
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
import yaml
from bs4 import BeautifulSoup

JST = timezone(timedelta(hours=9))
BASE_URL = "https://www.boatrace.jp/owpc/pc/data/stadium?jcd={jcd:02d}"
OUT_PATH = Path(__file__).parent / "data" / "course_rates.yaml"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "ja,en-US;q=0.9,en;q=0.8",
}

VENUE_NAMES = {
    1: "桐生", 2: "戸田", 3: "江戸川", 4: "平和島", 5: "多摩川", 6: "浜名湖",
    7: "蒲郡", 8: "常滑", 9: "津", 10: "三国", 11: "びわこ", 12: "住之江",
    13: "尼崎", 14: "鳴門", 15: "丸亀", 16: "児島", 17: "宮島", 18: "徳山",
    19: "下関", 20: "若松", 21: "芦屋", 22: "福岡", 23: "唐津", 24: "大村",
}

_RE_PERIOD = re.compile(r"集計期間：\s*([\d/]+)\s*[～〜]\s*([\d/]+)")
SEASONS = ("春季", "夏季", "秋季", "冬季")


def _label_of(table) -> str:
    """表の直前にある見出し（title7_mainLabel / title9_mainLabel）を返す"""
    el = table.find_previous(class_=re.compile(r"title\d_mainLabel"))
    return el.get_text(strip=True) if el else ""


def _period_of(table) -> str:
    """表の直後にある「（集計期間：…）」を返す"""
    node = table.find_next(string=_RE_PERIOD)
    if node is None:
        return ""
    m = _RE_PERIOD.search(str(node))
    return f"{m.group(1)}〜{m.group(2)}" if m else ""


def _rate_rows(table, n_cols: int = 6) -> dict:
    """先頭セルがコース番号（1〜6）の行から、続く n_cols 個の数値を取り出す"""
    out = {}
    for tr in table.select("tr"):
        cells = [c.get_text(strip=True) for c in tr.select("td")]
        if len(cells) < n_cols + 1:
            continue
        head = cells[0]
        if not head.isdigit() or not (1 <= int(head) <= 6):
            continue
        try:
            out[int(head)] = [float(x) for x in cells[1:n_cols + 1]]
        except ValueError:
            continue
    return out


def _expected_place(rates: dict) -> dict:
    """入着率（%）から期待着順 Σ(着順 × 率/100) を計算する"""
    exp = {}
    for course, row in rates.items():
        total = sum(row)
        if total <= 0:
            continue
        # 率の合計が100.0ちょうどにならないことがあるため正規化してから加重平均を取る
        exp[course] = round(sum((i + 1) * p for i, p in enumerate(row)) / total, 3)
    return exp


def parse_stadium_page(html: str) -> dict:
    soup = BeautifulSoup(html, "html.parser")
    tables = soup.select("div.table1 table")
    if not tables:
        raise ValueError("表が1つも見つかりません")

    recent = None
    frame_to_course = None
    seasonal = {}

    for tbl in tables:
        label = _label_of(tbl)
        period = _period_of(tbl)

        if "コース別入着率" in label and "決まり手" in label:
            rates = _rate_rows(tbl, 6)
            if len(rates) == 6:
                recent = {"period": period, "course_rates": rates,
                          "expected_place": _expected_place(rates)}
        elif "枠番別コース取得率" in label:
            rows = _rate_rows(tbl, 6)
            if len(rows) == 6:
                frame_to_course = {"period": period, "rates": rows}
        elif label in SEASONS:
            rates = _rate_rows(tbl, 6)
            if len(rates) == 6:
                seasonal[label] = {"period": period, "course_rates": rates,
                                   "expected_place": _expected_place(rates)}

    if recent is None:
        raise ValueError("「コース別入着率＆決まり手」の表を読み取れません")
    missing = [s for s in SEASONS if s not in seasonal]
    if missing:
        raise ValueError(f"季節別データが欠けています: {missing}")
    if frame_to_course is None:
        raise ValueError("「枠番別コース取得率」の表を読み取れません")

    return {"recent_3months": recent, "seasonal": seasonal,
            "frame_to_course": frame_to_course}


def fetch(jcd: int) -> dict:
    resp = requests.get(BASE_URL.format(jcd=jcd), headers=HEADERS, timeout=20)
    resp.raise_for_status()
    data = parse_stadium_page(resp.text)
    data["name"] = VENUE_NAMES[jcd]
    return data


def main(targets):
    venues, errors = {}, []
    t0 = time.time()
    for jcd in targets:
        try:
            venues[jcd] = fetch(jcd)
            p = venues[jcd]["recent_3months"]["period"]
            print(f"  {jcd:02d} {VENUE_NAMES[jcd]:<5} OK   期間={p}", flush=True)
        except Exception as e:
            errors.append((jcd, str(e)))
            print(f"  {jcd:02d} {VENUE_NAMES[jcd]:<5} NG   {e}", flush=True)

    if errors:
        # 一部だけ更新すると、会場ごとに取得日が食い違うファイルができる。
        # どの数値がいつのものか分からなくなるため、全場そろわない限り保存しない。
        print(f"\n{len(errors)}場で失敗したため保存しません。", file=sys.stderr)
        return 1

    doc = {
        "fetched_on": datetime.now(JST).strftime("%Y-%m-%d"),
        "source": "https://www.boatrace.jp/owpc/pc/data/stadium?jcd={01〜24}",
        "note": (
            "このファイルは fetch_course_rates.py が生成する。手で編集しない。"
            "expected_place は Σ(着順 × 入着率) を率の合計で正規化した加重平均。"
        ),
        "venues": {jcd: venues[jcd] for jcd in sorted(venues)},
    }
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        yaml.safe_dump(doc, f, allow_unicode=True, sort_keys=False, width=200)

    print(f"\n所要 {time.time() - t0:.1f}秒 / {len(targets)}場")
    print(f"保存: {OUT_PATH}")
    return 0


if __name__ == "__main__":
    args = [int(a) for a in sys.argv[1:]] or list(range(1, 25))
    sys.exit(main(args))

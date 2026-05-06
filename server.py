#!/usr/bin/env python3
"""
ボートレース予想MCPサーバー
claude.aiモバイルアプリから呼び出し、レース分析に必要なデータを自動取得する
"""

import os
import re
import math
import json
import time
import secrets
import yaml
import requests
from pathlib import Path
from datetime import date as date_module
from typing import Optional
from bs4 import BeautifulSoup
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

# ── FastMCPインスタンス ──────────────────────────────
# Renderでは外部ドメインからリクエストが来るためDNSリバインディング保護を無効化
mcp = FastMCP(
    "boatrace-mcp",
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
)

# ── 定数 ──────────────────────────────────────────
RULES_DIR = Path(__file__).parent / "rules"

VENUE_NAMES = {
    1: "桐生", 2: "戸田", 3: "江戸川", 4: "平和島", 5: "多摩川",
    6: "浜名湖", 7: "蒲郡", 8: "常滑", 9: "津", 10: "三国",
    11: "びわこ", 12: "住之江", 13: "尼崎", 14: "鳴門", 15: "丸亀",
    16: "児島", 17: "宮島", 18: "徳山", 19: "下関", 20: "若松",
    21: "芦屋", 22: "福岡", 23: "唐津", 24: "大村",
}

CLASS_MAP = {1: "A1", 2: "A2", 3: "B1", 4: "B2"}

WEATHER_MAP = {1: "晴れ", 2: "曇り", 3: "小雨", 4: "雨", 5: "雪"}

WIND_DIR_MAP = {
    1: "北(N)", 2: "北北東(NNE)", 3: "北東(NE)", 4: "東北東(ENE)",
    5: "東(E)", 6: "東南東(ESE)", 7: "南東(SE)", 8: "南南東(SSE)",
    9: "南(S)", 10: "南南西(SSW)", 11: "南西(SW)", 12: "西南西(WSW)",
    13: "西(W)", 14: "西北西(WNW)", 15: "北西(NW)", 16: "北北西(NNW)",
}

# boatrace.jpへのアクセス用ヘッダー（ブロック回避）
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "ja,en-US;q=0.9,en;q=0.8",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}


# ── ヘルパー関数 ─────────────────────────────────────

def _today() -> str:
    return date_module.today().strftime("%Y%m%d")


def _resolve_date(d: str) -> str:
    return _today() if d == "today" else d


def _venue_name(venue_id: int) -> str:
    return VENUE_NAMES.get(venue_id, f"会場{venue_id}")


def _find_race(programs: list, venue: int, race_no: int) -> Optional[dict]:
    """programs/previews APIのフラットなリストから会場・レースを絞り込む"""
    for item in programs:
        if item.get("race_stadium_number") == venue and item.get("race_number") == race_no:
            return item
    return None


# ── Tool 1: 出走表 ────────────────────────────────────

@mcp.tool()
def get_race_card(venue: int, race_no: int, date: str = "today") -> str:
    """
    出走表を取得する。
    venue: 会場ID（1〜24、例: 12=住之江、4=平和島）
    race_no: レース番号（1〜12）
    date: 日付（"today" または "YYYYMMDD" 形式、例: "20260504"）
    """
    target_date = _resolve_date(date)

    url = (
        "https://boatraceopenapi.github.io/programs/v2/today.json"
        if date == "today"
        else f"https://boatraceopenapi.github.io/programs/v2/{target_date}.json"
    )

    try:
        resp = requests.get(url, headers=HEADERS, timeout=15)
        resp.raise_for_status()
        data = resp.json()
    except requests.RequestException as e:
        return f"【エラー】BoatraceOpenAPIへの接続に失敗しました。\n詳細: {e}"
    except Exception as e:
        return f"【エラー】JSONの解析に失敗しました。\n詳細: {e}"

    race = _find_race(data.get("programs", []), venue, race_no)
    if race is None:
        return (
            f"【データなし】{_venue_name(venue)} {race_no}R の出走表が見つかりません。\n"
            f"日付: {target_date} / 本日の開催会場: "
            + ", ".join(str(p.get("race_stadium_number")) for p in
                        {p["race_stadium_number"]: p for p in data.get("programs", [])}.values())
        )

    lines = [
        "=" * 50,
        f"  {_venue_name(venue)}競艇  {race_no}R  出走表  ({target_date})",
        f"  {race.get('race_title', '')}  {race.get('race_subtitle', '')}",
        f"  距離: {race.get('race_distance', '-')}m",
        "=" * 50,
        "",
    ]

    boats = race.get("boats", [])
    # boats はリスト形式
    for boat in boats:
        frame   = boat.get("racer_boat_number", "?")
        name    = boat.get("racer_name", "不明")
        reg_no  = boat.get("racer_number", "?")
        cls     = CLASS_MAP.get(boat.get("racer_class_number", 0), "?")
        age     = boat.get("racer_age", "-")
        weight  = boat.get("racer_weight", "-")
        f_cnt   = boat.get("racer_flying_count", 0)
        l_cnt   = boat.get("racer_late_count", 0)

        nat_1 = boat.get("racer_national_top_1_percent", "-")
        nat_2 = boat.get("racer_national_top_2_percent", "-")
        nat_3 = boat.get("racer_national_top_3_percent", "-")
        loc_1 = boat.get("racer_local_top_1_percent", "-")
        loc_2 = boat.get("racer_local_top_2_percent", "-")
        loc_3 = boat.get("racer_local_top_3_percent", "-")
        avg_st = boat.get("racer_average_start_timing", "-")

        motor_no = boat.get("racer_assigned_motor_number", "?")
        motor_2  = boat.get("racer_assigned_motor_top_2_percent", "-")
        motor_3  = boat.get("racer_assigned_motor_top_3_percent", "-")
        boat_no  = boat.get("racer_assigned_boat_number", "?")
        boat_2   = boat.get("racer_assigned_boat_top_2_percent", "-")
        boat_3   = boat.get("racer_assigned_boat_top_3_percent", "-")

        fl_str = f"  F{f_cnt}L{l_cnt}" if f_cnt or l_cnt else ""

        lines += [
            f"【{frame}号艇】{name}（登録{reg_no}）{cls}  {age}歳  {weight}kg{fl_str}",
            f"  全国: 勝率{nat_1} / 2連{nat_2} / 3連{nat_3}",
            f"  当地: 勝率{loc_1} / 2連{loc_2} / 3連{loc_3}",
            f"  平均ST: {avg_st}",
            f"  モーター#{motor_no}: 2連{motor_2}% / 3連{motor_3}%",
            f"  ボート  #{boat_no}: 2連{boat_2}% / 3連{boat_3}%",
            "",
        ]

    return "\n".join(lines)


# ── Tool 2: 直前情報（boatrace.jp スクレイピング）────────

@mcp.tool()
def get_pre_race_info(venue: int, race_no: int, date: str = "today") -> str:
    """
    直前情報（展示タイム・展示ST・気象・チルト・体重）を取得する。
    venue: 会場ID（1〜24）
    race_no: レース番号（1〜12）
    date: 日付（"today" または "YYYYMMDD"）
    ※ 直前情報はレース開始の約40分前から取得可能です。
    """
    target_date = _resolve_date(date)
    url = (
        f"https://www.boatrace.jp/owpc/pc/race/beforeinfo"
        f"?rno={race_no}&jcd={venue:02d}&hd={target_date}"
    )

    try:
        resp = requests.get(url, headers=HEADERS, timeout=15)
        resp.raise_for_status()
    except Exception as e:
        return f"【エラー】boatrace.jpへの接続に失敗しました。\n詳細: {e}"

    soup = BeautifulSoup(resp.text, "html.parser")
    tables = soup.find_all("table")

    if len(tables) < 2:
        return (
            f"【データなし】{_venue_name(venue)} {race_no}R の直前情報が見つかりません。\n"
            "直前情報はレース開始約40分前から公開されます。"
        )

    # ── 気象情報（weather1 div）──
    weather_div = soup.find("div", class_=lambda c: c and "weather1" in (c.split() if c else []))
    weather_raw = weather_div.get_text(" ", strip=True) if weather_div else ""
    temp_m   = re.search(r'気温\s*([\d.]+)', weather_raw)
    wtemp_m  = re.search(r'水温\s*([\d.]+)', weather_raw)
    wind_m   = re.search(r'風速\s*([\d.]+)', weather_raw)
    wave_m   = re.search(r'波高\s*([\d.]+)', weather_raw)
    weather_label = re.search(r'(晴|曇|雨|雪|霧)', weather_raw)

    # ── スタート展示ST（Table2: is-typeN → 艇番マッピング）──
    st_by_boat: dict[str, str] = {}
    if len(tables) >= 3:
        for row in tables[2].find_all("tr"):
            div = row.find("div", class_="table1_boatImage1")
            if not div:
                continue
            num_span  = div.find("span", class_=lambda c: c and "Number" in c)
            time_span = div.find("span", class_=lambda c: c and "Time" in c)
            if num_span and time_span:
                # is-typeN の N が艇番
                type_cls = next(
                    (c for c in num_span.get("class", []) if c.startswith("is-type")), None
                )
                if type_cls:
                    boat_num = type_cls.replace("is-type", "")
                    st_by_boat[boat_num] = time_span.get_text(strip=True)

    # ── 選手別データ（Table1: 艇ごとに4行ずつ）──
    rows = tables[1].find_all("tr")
    boats_data: dict[str, dict] = {}
    i = 2  # 先頭2行はヘッダー
    while i < len(rows):
        cells = [td.get_text(" ", strip=True) for td in rows[i].find_all(["th", "td"])]
        if cells and re.match(r"^[1-6]$", cells[0]):
            frame    = cells[0]
            name     = cells[2].replace("　", " ").strip() if len(cells) > 2 else "-"
            weight   = cells[3] if len(cells) > 3 else "-"
            exhibit_t = cells[4] if len(cells) > 4 else "-"
            tilt     = cells[5] if len(cells) > 5 else "-"
            # 3行目サブ行から体重調整を取得
            w_adj = "0.0"
            if i + 2 < len(rows):
                sub = [td.get_text(" ", strip=True) for td in rows[i + 2].find_all(["th", "td"])]
                if sub and sub[0] not in ["進入", "着順"]:
                    w_adj = sub[0]
            boats_data[frame] = {
                "name": name,
                "weight": weight,
                "exhibit_t": exhibit_t,
                "tilt": tilt,
                "w_adj": w_adj,
                "st": st_by_boat.get(frame, "-"),
            }
            i += 4
        else:
            i += 1

    if not boats_data:
        return (
            f"【データなし】{_venue_name(venue)} {race_no}R の直前情報が見つかりません。\n"
            "直前情報はレース開始約40分前から公開されます。"
        )

    lines = [
        "=" * 50,
        f"  {_venue_name(venue)}競艇  {race_no}R  直前情報  ({target_date})",
        "=" * 50,
        "",
        "【気象情報】",
        f"  天候: {weather_label.group(1) if weather_label else '-'}",
        f"  風速: {wind_m.group(1) + ' m/s' if wind_m else '-'}",
        f"  波高: {wave_m.group(1) + ' cm' if wave_m else '-'}",
        f"  気温: {temp_m.group(1) + ' ℃' if temp_m else '-'}",
        f"  水温: {wtemp_m.group(1) + ' ℃' if wtemp_m else '-'}",
        "",
        "【選手別直前情報】",
        f"  {'枠':>3}  {'選手名':<10}  {'展示T':>6}  {'展示ST':>7}  {'チルト':>6}  {'体重':>7}  {'調整':>5}",
        "  " + "-" * 58,
    ]

    for frame in sorted(boats_data.keys(), key=int):
        b = boats_data[frame]
        lines.append(
            f"  {frame}号艇  {b['name']:<10}  {b['exhibit_t']:>6}  {b['st']:>7}"
            f"  {b['tilt']:>6}  {b['weight']:>7}  {b['w_adj']:>5}"
        )

    lines.append("")
    return "\n".join(lines)


# ── Tool 3: 3連単オッズ（スクレイピング）──────────────

@mcp.tool()
def get_odds(venue: int, race_no: int, date: str = "today") -> str:
    """
    3連単オッズを取得する（boatrace.jp公式サイトからスクレイピング）。
    venue: 会場ID（1〜24）
    race_no: レース番号（1〜12）
    date: 日付（"today" または "YYYYMMDD"）
    ※ オッズはレース締め切り直前まで変動します。
    """
    target_date = _resolve_date(date)
    url = (
        f"https://www.boatrace.jp/owpc/pc/race/odds3t"
        f"?rno={race_no}&jcd={venue:02d}&hd={target_date}"
    )

    try:
        resp = requests.get(url, headers=HEADERS, timeout=15)
        resp.raise_for_status()
    except Exception as e:
        return f"【エラー】boatrace.jpへの接続に失敗しました。\n詳細: {e}"

    soup = BeautifulSoup(resp.text, "html.parser")

    lines = [
        "=" * 50,
        f"  {_venue_name(venue)}競艇  {race_no}R  3連単オッズ  ({target_date})",
        "=" * 50,
        "",
    ]

    odds_entries = []  # (オッズ値float, "1-2-3"形式の文字列)

    # boatrace.jpの3連単オッズテーブル構造（確認済み）:
    # - テーブルは2つ（1つ目=メタ情報, 2つ目=オッズ本体）
    # - ヘッダー行: 1着ボートの枠番と選手名が交互に並ぶ th タグ
    # - データ行: 6グループ×3セル(2着, 3着, oddsPoint) で1行あたり18セル
    # - 20行 × 6グループ = 120通りの3連単

    tables = soup.find_all("table")
    if len(tables) >= 2:
        table2 = tables[1]
        rows = table2.find_all("tr")

        if rows:
            # ヘッダーから1着枠番リストを取得（偶数インデックスのthが枠番 = 1,2,3,4,5,6）
            header_ths = rows[0].find_all("th")
            first_boats = [header_ths[i].get_text(strip=True)
                           for i in range(0, len(header_ths), 2)
                           if re.match(r'^[1-6]$', header_ths[i].get_text(strip=True))]

            # 構造: 5ブロック × 4行（1行=18セル + 3行=12セル）= 20データ行 × 6列 = 120通り
            # 18セル行: 6グループ×3セル（2着[rowspan=4], 3着, odds）
            # 12セル行: 6グループ×2セル（3着, odds） ← 2着はrowspanで前行から引き継ぎ
            current_second = [""] * len(first_boats)  # 各列の現在の2着番号

            for row in rows[1:]:
                tds = row.find_all("td")
                n_groups = len(first_boats)

                if len(tds) == n_groups * 3:  # 18セル行: 2着+3着+odds
                    for g, first in enumerate(first_boats):
                        idx = g * 3
                        second   = tds[idx].get_text(strip=True)
                        third    = tds[idx + 1].get_text(strip=True)
                        odds_val = tds[idx + 2].get_text(strip=True)
                        current_second[g] = second
                        if (re.match(r'^[1-6]$', second)
                                and re.match(r'^[1-6]$', third)
                                and re.match(r'^\d+(\.\d+)?$', odds_val)):
                            odds_entries.append((float(odds_val), f"{first}-{second}-{third}"))

                elif len(tds) == n_groups * 2:  # 12セル行: 3着+odds（2着はrowspan継続）
                    for g, first in enumerate(first_boats):
                        idx = g * 2
                        second   = current_second[g]
                        third    = tds[idx].get_text(strip=True)
                        odds_val = tds[idx + 1].get_text(strip=True)
                        if (re.match(r'^[1-6]$', second)
                                and re.match(r'^[1-6]$', third)
                                and re.match(r'^\d+(\.\d+)?$', odds_val)):
                            odds_entries.append((float(odds_val), f"{first}-{second}-{third}"))

    if odds_entries:
        odds_entries.sort(key=lambda x: x[0])
        lines.append("【人気順（低オッズ順）上位30件】")
        lines.append(f"  {'順位':>4}  {'組み合わせ':>12}  {'オッズ':>8}")
        lines.append("  " + "-" * 30)
        for i, (val, combo) in enumerate(odds_entries[:30], 1):
            lines.append(f"  {i:>4}  {combo:>12}  {val:>7.1f}倍")
        lines.append(f"\n  （全{len(odds_entries)}通り取得）")
    else:
        lines += [
            "オッズデータを取得できませんでした。",
            "考えられる原因:",
            "  - レース前でオッズが未確定",
            "  - レース終了後",
            "  - boatrace.jpのHTML構造変更",
            f"\n参照URL: {url}",
        ]

    return "\n".join(lines)


# ── Tool 4: 選手コース別成績（スクレイピング）───────────

@mcp.tool()
def get_racer_course_stats(racer_id: str) -> str:
    """
    選手のコース別成績（各コースの1着率・2着率・3着率・進入率・平均ST）を取得する。
    racer_id: 選手登録番号（4〜5桁の数字、例: "3997"）
    """
    # コース別成績は profile ではなく course エンドポイントにある
    url = f"https://www.boatrace.jp/owpc/pc/data/racersearch/course?toban={racer_id}"

    try:
        resp = requests.get(url, headers=HEADERS, timeout=15)
        resp.raise_for_status()
    except Exception as e:
        return f"【エラー】boatrace.jpへの接続に失敗しました。\n詳細: {e}"

    soup = BeautifulSoup(resp.text, "html.parser")

    # ページタイトルでエラー判定
    title = soup.find("title")
    if title and "システムエラー" in title.get_text():
        return f"【エラー】登録番号 {racer_id} の選手が見つかりません。\n参照URL: {url}"

    lines = [
        "=" * 50,
        f"  選手コース別成績  登録番号: {racer_id}",
        "=" * 50,
        "",
    ]

    # 選手名を取得
    h3 = soup.find("h3", class_=re.compile(r"title|name")) or soup.find("h3")
    if h3:
        name_text = h3.get_text(strip=True)
        if name_text and name_text != "コース別成績":
            lines.append(f"選手名: {name_text}")
            lines.append("")

    tables = soup.find_all("table")

    def extract_per_course(table):
        """コース別の値（1コース〜6コース）をtableから取得"""
        result = {}
        for row in table.find_all("tr"):
            th = row.find("th")
            td = row.find("td")
            if th and td:
                th_text = th.get_text(strip=True)
                if re.match(r'^[1-6]$', th_text):
                    result[th_text] = td.get_text(strip=True)
        return result

    def extract_course_rates(table):
        """3連対率テーブル：CSSのwidth値から1着率・2着率・3着率を取得"""
        result = {}
        for row in table.find_all("tr"):
            th = row.find("th")
            td = row.find("td")
            if th and td and re.match(r'^[1-6]$', th.get_text(strip=True)):
                course = th.get_text(strip=True)
                rates = {}
                # 合計3連対率はラベルspanのテキスト
                label = td.find("span", class_="table1_progress2Label")
                rates["3連"] = label.get_text(strip=True) if label else "-"
                # 1〜3着率はis-progress spanの親のstyle width値
                for n in (1, 2, 3):
                    inner = td.find("span", class_=f"is-progress{n}")
                    if inner and inner.parent:
                        m = re.search(r'width:\s*([\d.]+)%', inner.parent.get("style", ""))
                        rates[f"{n}着"] = m.group(1) + "%" if m else "-"
                result[course] = rates
        return result

    # テーブル割り当て（確認済み構造：Table1=進入率, Table2=3連対率, Table3=平均ST）
    approach_data = extract_per_course(tables[0]) if len(tables) > 0 else {}
    rates_data    = extract_course_rates(tables[1]) if len(tables) > 1 else {}
    avg_st_data   = extract_per_course(tables[2]) if len(tables) > 2 else {}

    lines.append("【コース別成績（今期）】")
    lines.append(f"  {'C':>2}  {'進入率':>7}  {'1着%':>7}  {'2着%':>7}  {'3着%':>7}  {'3連%':>7}  {'均ST':>6}")
    lines.append("  " + "-" * 56)

    for c in "123456":
        approach = approach_data.get(c, "-")
        r = rates_data.get(c, {})
        r1 = r.get("1着", "-")
        r2 = r.get("2着", "-")
        r3 = r.get("3着", "-")
        r3c = r.get("3連", "-")
        st = avg_st_data.get(c, "-")
        lines.append(
            f"  {c}コース  {approach:>7}  {r1:>7}  {r2:>7}  {r3:>7}  {r3c:>7}  {st:>6}"
        )

    lines.append("")
    return "\n".join(lines)


# ── Tool 5: 枠番別過去成績（スクレイピング）─────────────

@mcp.tool()
def get_recent_10_races(venue: int, race_no: int, date: str = "today") -> str:
    """
    出走選手の枠番別・今節過去成績（着順・枠・ST・進入コース）を取得する。
    venue: 会場ID（1〜24）
    race_no: レース番号（1〜12）
    date: 日付（"today" または "YYYYMMDD"）
    """
    target_date = _resolve_date(date)
    url = (
        f"https://www.boatrace.jp/owpc/pc/race/racelist"
        f"?rno={race_no}&jcd={venue:02d}&hd={target_date}"
    )

    try:
        resp = requests.get(url, headers=HEADERS, timeout=15)
        resp.raise_for_status()
    except Exception as e:
        return f"【エラー】boatrace.jpへの接続に失敗しました。\n詳細: {e}"

    soup = BeautifulSoup(resp.text, "html.parser")

    lines = [
        "=" * 50,
        f"  {_venue_name(venue)}競艇  {race_no}R  今節過去成績",
        "=" * 50,
        "",
        "  着順の色 = is-boatColor{N}: N=着順 / セル内テキスト=レース番号",
        "",
    ]

    tables = soup.find_all("table")
    if len(tables) < 2:
        lines.append("データが見つかりませんでした。")
        return "\n".join(lines)

    table2 = tables[1]
    all_rows = table2.find_all("tr")

    # 主行（is-fs14クラスを持つtd = 今日の枠番を表すtd）のインデックスを収集
    main_row_indices = [
        i for i, row in enumerate(all_rows)
        if row.find("td", class_=re.compile(r'is-boatColor\d.*is-fs14|is-fs14.*is-boatColor\d'))
    ]

    for idx in main_row_indices:
        main_row = all_rows[idx]
        tds = main_row.find_all("td")

        # 今日の枠番
        frame_td = main_row.find("td", class_=re.compile(r'is-boatColor\d.*is-fs14|is-fs14.*is-boatColor\d'))
        today_frame = frame_td.get_text(strip=True) if frame_td else "?"

        # 選手名（プロフィールリンクのうちテキストが入っているものを探す）
        name = "-"
        for td in tds:
            for a in td.find_all("a"):
                if "racersearch/profile" in a.get("href", ""):
                    text = a.get_text(strip=True).replace("　", " ").strip()
                    if text:  # 写真リンク（テキスト空）はスキップ
                        name = text
                        break
            if name != "-":
                break

        # 過去レース結果セルを抽出
        # CLASS is-boatColor{N}（is-fs14なし）のtd → N=着順、テキスト=レース番号
        result_cells = [
            td for td in tds
            if re.search(r'is-boatColor\d', " ".join(td.get("class", [])))
            and "is-fs14" not in " ".join(td.get("class", []))
        ]

        # サブ行から枠番・ST・進入コースを取得（最大4行後まで）
        sub_frame_row = all_rows[idx + 1] if idx + 1 < len(all_rows) else None
        sub_st_row    = all_rows[idx + 2] if idx + 2 < len(all_rows) else None
        sub_course_row = all_rows[idx + 3] if idx + 3 < len(all_rows) else None

        def get_sub_values(row):
            if row is None:
                return []
            return [td.get_text(strip=True) for td in row.find_all("td") if td.get_text(strip=True)]

        sub_frames  = get_sub_values(sub_frame_row)
        sub_sts     = get_sub_values(sub_st_row)
        sub_courses = get_sub_values(sub_course_row)

        # 結果を整形して表示
        results_str_parts = []
        for i, td in enumerate(result_cells):
            classes = " ".join(td.get("class", []))
            m = re.search(r'is-boatColor(\d)', classes)
            finish  = m.group(1) if m else "?"
            race_no_str = td.get_text(strip=True)
            frame   = sub_frames[i]  if i < len(sub_frames)  else "-"
            st      = sub_sts[i]     if i < len(sub_sts)     else "-"
            course  = sub_courses[i] if i < len(sub_courses) else "-"

            results_str_parts.append(
                f"R{race_no_str}:{finish}着(枠{frame}/C{course}/ST{st})"
            )

        # 次の予定レース
        next_race_td = next(
            (td for td in tds
             if not td.get("class") and re.match(r'^\d+R$', td.get_text(strip=True))),
            None
        )
        next_race = next_race_td.get_text(strip=True) if next_race_td else "-"

        result_line = "  ".join(results_str_parts) if results_str_parts else "（今節初走）"
        lines.append(f"  {today_frame}号艇 {name}:  {result_line}")
        lines.append(f"           次走: {next_race}")
        lines.append("")

    if len(lines) <= 6:
        lines.append("データが見つかりませんでした。")

    return "\n".join(lines)


# ── Tool 6: 学習ルール取得 ────────────────────────────

@mcp.tool()
def get_learning_rules(
    venue: Optional[int] = None,
    conditions: Optional[str] = None,
    racer_ids: Optional[str] = None,
) -> str:
    """
    蓄積した学習ルールをテキストで返す。
    venue: 会場ID（指定すると会場別ルールも追加、例: 12）
    conditions: 条件キーワード（カンマ区切り可、例: "向風,安定板"）
    racer_ids: 選手登録番号（カンマ区切り可、例: "3997,4444"）
    ※ 引数なしで呼ぶと全ジェネラルルール（R1〜R14）を返す。
    """
    result_sections = []

    # ── 出力フォーマットガイド（最優先・常に先頭に返す）──
    fmt_path = RULES_DIR / "output_format.yaml"
    if fmt_path.exists():
        with open(fmt_path, encoding="utf-8") as f:
            fdata = yaml.safe_load(f)
        fmt_block = [
            "【⚠️ 絶対遵守：予想レポート出力フォーマット】",
            "買い目を提示するとき・最終予想を出すときは、例外なく以下のMarkdown形式で出力せよ。",
            "このフォーマットを使わない出力は不完全とみなす。",
            "",
        ]
        for line in fdata.get("format", "").strip().splitlines():
            fmt_block.append(line)
        notes = fdata.get("notes", [])
        if notes:
            fmt_block.append("")
            fmt_block.append("【出力ルール】")
            for note in notes:
                fmt_block.append(f"  - {note}")
        result_sections.append("\n".join(fmt_block))

    # ── ジェネラルルール（常に返す）──
    general_path = RULES_DIR / "general.yaml"
    if general_path.exists():
        with open(general_path, encoding="utf-8") as f:
            general = yaml.safe_load(f)
        rules = general.get("rules", [])
        if rules:
            section = ["【ジェネラルルール（R1〜R15）】"]
            for r in rules:
                section.append(f"  {r['id']}: {r['text']}")
            result_sections.append("\n".join(section))

    # ── 会場別ルール ──
    if venue is not None:
        venue_files = sorted(RULES_DIR.glob(f"venues/{venue:02d}_*.yaml"))
        if venue_files:
            with open(venue_files[0], encoding="utf-8") as f:
                vdata = yaml.safe_load(f)

            vname = vdata.get("venue_name", str(venue))
            section = [f"\n【{vname}会場ルール】"]

            water = vdata.get("water_type", "")
            dist = vdata.get("course_length", "")
            stable = vdata.get("stable_board_used", False)
            night = "ナイター開催あり" if vdata.get("night_race") else "デイ開催"
            dist_str = f"{dist}m（標準。荒天・時間短縮時1200m。安定板は強風時に使用）"
            section.append(f"  基本情報: {water} / {dist_str} / {night}")

            for label, key in [("▼特性", "characteristics"), ("▼注意点", "cautions"), ("▼推奨戦略", "recommended_strategy")]:
                items = vdata.get(key, [])
                if items and items[0] != "（未記入）":
                    section.append(f"  {label}")
                    for c in items:
                        section.append(f"    - {c}")

            result_sections.append("\n".join(section))
        else:
            result_sections.append(f"\n【会場{venue}】ルールファイルが見つかりません。")

    # ── 条件別ルール ──
    if conditions:
        cond_path = RULES_DIR / "conditions.yaml"
        if cond_path.exists():
            with open(cond_path, encoding="utf-8") as f:
                cdata = yaml.safe_load(f)

            keywords = [kw.strip() for kw in conditions.split(",")]
            matched = []
            for category in cdata.values():
                if not isinstance(category, list):
                    continue
                for item in category:
                    cond_text = item.get("condition", "")
                    rule_text = item.get("rule", "")
                    if any(kw in cond_text or kw in rule_text for kw in keywords):
                        matched.append(f"  [{cond_text}] {rule_text}")

            if matched:
                section = ["\n【条件別ルール（マッチ分）】"]
                section.extend(matched)
                result_sections.append("\n".join(section))

    # ── 選手別ルール ──
    if racer_ids:
        ids = [rid.strip() for rid in racer_ids.split(",")]
        racer_lines = ["\n【選手別ルール】"]
        found_any = False
        for rid in ids:
            racer_files = sorted(RULES_DIR.glob(f"racers/{rid}_*.yaml"))
            if racer_files:
                with open(racer_files[0], encoding="utf-8") as f:
                    rdata = yaml.safe_load(f)
                racer_lines.append(
                    f"  {rdata.get('racer_name', rid)}（{rid}）:\n"
                    + "\n".join(f"    - {note}" for note in rdata.get("notes", []))
                )
                found_any = True
            else:
                racer_lines.append(f"  登録番号{rid}: 蓄積データなし")
        if found_any:
            result_sections.append("\n".join(racer_lines))

    if not result_sections:
        return "ルールデータが見つかりませんでした。rules/ディレクトリを確認してください。"

    header = ["=" * 50, "  ボートレース予想 学習ルール", "=" * 50]
    return "\n".join(header) + "\n\n" + "\n\n".join(result_sections)


# ── Tool 7: トリガミ回避ライン計算 ──────────────────────

@mcp.tool()
def calc_trigami_threshold(odds: float, total_budget: int) -> str:
    """
    トリガミ（賭け金合計より払戻が少ない状態）を回避するための最小賭金を計算する。
    odds: 対象組み合わせのオッズ（例: 15.6）
    total_budget: 1レースに使う総予算（円、例: 3000）
    ※ 賭け金は100円単位で計算します。
    """
    if odds <= 1.0:
        return "【エラー】オッズは1.0より大きい値を入力してください。"
    if total_budget <= 0:
        return "【エラー】総予算は1円以上の値を入力してください。"

    # トリガミ回避最小賭金: bet × odds > total_budget を満たす最小の100円単位
    raw_min = total_budget / odds
    min_bet_100 = math.ceil(raw_min / 100) * 100
    payout = min_bet_100 * odds
    # ちょうど予算と同額になる場合（利益ゼロ）は100円繰り上げて確実にプラスにする
    if payout <= total_budget:
        min_bet_100 += 100
        payout = min_bet_100 * odds
    profit = payout - total_budget
    budget_ratio = min_bet_100 / total_budget * 100

    lines = [
        "=" * 50,
        "  トリガミ回避ライン計算",
        "=" * 50,
        "",
        f"  オッズ:          {odds:.1f} 倍",
        f"  総予算:          {total_budget:,} 円",
        "",
        "  ▼ 計算結果",
        f"  トリガミ回避最小賭金: {min_bet_100:,} 円",
        f"  この賭金での払戻:     {payout:,.0f} 円",
        f"  差し引き純利益:       {profit:+,.0f} 円",
        f"  予算に対する割合:     {budget_ratio:.0f}%",
        "",
        f"  ★ 【必須】この組み合わせへの賭金は必ず {min_bet_100:,}円 以上にすること。",
        f"     {min_bet_100 - 100:,}円 以下で買うと当選してもトリガミ確定。",
        f"     均一分配や他の買い目と合計して予算内に収めるとき、",
        f"     この組み合わせだけは {min_bet_100:,}円 を死守すること（R7）。",
        "",
        "  ▼ R7判断ガイド（死守 or 切る。中間なし）",
    ]

    if budget_ratio > 60:
        lines.append(
            f"  ✕ 切り推奨: 予算の{budget_ratio:.0f}%を1点に集中しないと回避できないオッズ。"
        )
        lines.append("    このオッズで買い続けると長期期待値がマイナス。R7に従い「切る」が正解。")
    elif budget_ratio > 40:
        lines.append(
            f"  △ 要注意: 予算の{budget_ratio:.0f}%（{min_bet_100:,}円）が必要。"
        )
        lines.append("    他の買い目を絞ってこの金額を確保できるか確認してから買うこと。")
    else:
        lines.append(
            f"  ○ 許容範囲: 予算の{budget_ratio:.0f}%（{min_bet_100:,}円）で回避可能。"
        )
        lines.append("    ただし最終買い目でこの金額を下回らないよう必ず確認すること。")

    lines.append("")
    return "\n".join(lines)


# ── Tool 8〜12: 収支記録 ──────────────────────────────

@mcp.tool()
def record_bets(
    venue: int,
    race_no: int,
    date: str,
    bets: str,
    memo: str = "",
) -> str:
    """
    買い目を一括記録する。
    venue: 会場ID（1〜24）
    race_no: レース番号（1〜12）
    date: 日付（"today" または "YYYYMMDD"）
    bets: JSON文字列 例: '[{"combination":"1-2-3","amount":600,"odds":5.1}, ...]'
    memo: メモ（任意）
    """
    target_date = _resolve_date(date)
    race_id = f"{target_date}_{venue:02d}_{race_no:02d}"

    try:
        bets_list = json.loads(bets)
    except json.JSONDecodeError as e:
        return f"【エラー】betsのJSON形式が正しくありません。\n詳細: {e}"

    if not bets_list:
        return "【エラー】買い目が1件もありません。"

    try:
        ws = _get_sheet()
    except Exception as e:
        return f"【エラー】Google Sheetsへの接続に失敗しました。\n詳細: {e}"

    rows = [
        [
            race_id,
            target_date,
            _venue_name(venue),
            race_no,
            b.get("combination", ""),
            b.get("amount", 0),
            b.get("odds", 0),
            "",   # result（未記録）
            "",   # payout（未記録）
            "",   # profit（未記録）
            memo,
        ]
        for b in bets_list
    ]

    try:
        ws.append_rows(rows, value_input_option="RAW")
    except Exception as e:
        return f"【エラー】スプレッドシートへの書き込みに失敗しました。\n詳細: {e}"

    total_amount = sum(b.get("amount", 0) for b in bets_list)
    lines = [
        "=" * 50,
        f"  買い目記録完了：{_venue_name(venue)} {race_no}R",
        "=" * 50,
        f"  race_id: {race_id}",
        f"  記録点数: {len(rows)}点",
        "",
        f"  {'組み合わせ':<10}  {'賭金':>6}  {'オッズ':>6}",
        "  " + "-" * 30,
    ]
    for b in bets_list:
        lines.append(f"  {b.get('combination',''):<10}  {b.get('amount',0):>5,}円  {b.get('odds',0):>5.1f}倍")
    lines.append("")
    lines.append(f"  合計賭け金: {total_amount:,}円")
    lines.append("")
    return "\n".join(lines)


@mcp.tool()
def record_result(
    venue: int,
    race_no: int,
    date: str,
    result_combination: str,
) -> str:
    """
    レース結果を記録し、的中・払戻・収支を自動計算してシートを更新する。
    venue: 会場ID（1〜24）
    race_no: レース番号（1〜12）
    date: 日付（"today" または "YYYYMMDD"）
    result_combination: 実際の3連単着順（例: "1-2-4"）
    """
    target_date = _resolve_date(date)
    race_id = f"{target_date}_{venue:02d}_{race_no:02d}"

    try:
        ws = _get_sheet()
        all_rows = ws.get_all_values()
    except Exception as e:
        return f"【エラー】Google Sheetsへの接続に失敗しました。\n詳細: {e}"

    # ヘッダー: race_id(0) date(1) venue(2) race_no(3) combination(4)
    #           amount(5) odds(6) result(7) payout(8) profit(9) memo(10)
    hit = False
    total_invested = 0
    total_payout = 0
    matched = 0

    for i, row in enumerate(all_rows):
        if i == 0 or not row or row[0] != race_id:
            continue
        try:
            combination = row[4]
            amount      = int(float(row[5])) if row[5] else 0
            odds        = float(row[6]) if row[6] else 0.0
        except (ValueError, IndexError):
            continue

        total_invested += amount
        matched += 1

        if combination == result_combination:
            payout = round(amount * odds)
            profit = payout - amount
            hit = True
            total_payout += payout
        else:
            payout = 0
            profit = -amount

        row_num = i + 1  # スプレッドシートは1始まり
        try:
            ws.update(f"H{row_num}:J{row_num}", [[result_combination, payout, profit]])
        except Exception as e:
            return f"【エラー】{row_num}行目の更新に失敗しました。\n詳細: {e}"

    if matched == 0:
        return (
            f"【エラー】race_id「{race_id}」の記録が見つかりません。\n"
            "先に record_bets で買い目を記録してください。"
        )

    net = total_payout - total_invested
    roi = total_payout / total_invested * 100 if total_invested > 0 else 0

    lines = [
        "=" * 50,
        f"  結果記録完了：{_venue_name(venue)} {race_no}R",
        "=" * 50,
        f"  結果: {result_combination}",
        f"  {'【的中あり】' if hit else '【全外れ】'}",
        "",
        f"  総投資額: {total_invested:,}円",
        f"  総払戻額: {total_payout:,}円",
        f"  収支:     {net:+,}円",
        f"  回収率:   {roi:.1f}%",
        f"  更新行数: {matched}行",
        "",
    ]
    return "\n".join(lines)


@mcp.tool()
def get_pnl_summary(period_type: str = "all", period_value: str = "") -> str:
    """
    収支サマリを返す。
    period_type: "month"（月次）/ "year"（年次）/ "all"（全期間）
    period_value: "2026-05"（monthの場合）/ "2026"（yearの場合）/ 空文字（allの場合）
    """
    try:
        ws = _get_sheet()
        all_rows = ws.get_all_values()
    except Exception as e:
        return f"【エラー】Google Sheetsへの接続に失敗しました。\n詳細: {e}"

    # 結果が記録済みの行だけ対象（result列が空でない）
    def _match_period(date_str: str) -> bool:
        if period_type == "all":
            return True
        if period_type == "month":
            # date_str は "20260505" 形式 → "2026-05" と比較
            return f"{date_str[:4]}-{date_str[4:6]}" == period_value
        if period_type == "year":
            return date_str[:4] == period_value
        return True

    # race_id単位で集計
    races: dict[str, dict] = {}
    for i, row in enumerate(all_rows):
        if i == 0 or not row or len(row) < 10:
            continue
        race_id     = row[0]
        date_str    = row[1]
        venue_name  = row[2]
        result      = row[7]
        if not result:  # 未記録のレースは除外
            continue
        if not _match_period(date_str):
            continue

        try:
            amount = int(float(row[5])) if row[5] else 0
            payout = int(float(row[8])) if row[8] else 0
            profit = int(float(row[9])) if row[9] else 0
        except ValueError:
            continue

        if race_id not in races:
            races[race_id] = {"venue": venue_name, "invested": 0, "payout": 0, "hit": False}
        races[race_id]["invested"] += amount
        races[race_id]["payout"]   += payout
        if payout > 0:
            races[race_id]["hit"] = True

    if not races:
        return "【データなし】指定期間に記録済みのレースがありません。"

    total_races    = len(races)
    hit_races      = sum(1 for r in races.values() if r["hit"])
    total_invested = sum(r["invested"] for r in races.values())
    total_payout   = sum(r["payout"]   for r in races.values())
    net            = total_payout - total_invested
    roi            = total_payout / total_invested * 100 if total_invested > 0 else 0
    hit_rate       = hit_races / total_races * 100 if total_races > 0 else 0

    avg_invested   = total_invested / total_races if total_races > 0 else 0
    avg_net        = net / total_races if total_races > 0 else 0

    race_nets      = [r["payout"] - r["invested"] for r in races.values()]
    best_payout    = max((r["payout"] for r in races.values()), default=0)

    # 会場別集計
    venue_stats: dict[str, dict] = {}
    for r in races.values():
        v = r["venue"]
        if v not in venue_stats:
            venue_stats[v] = {"races": 0, "invested": 0, "payout": 0}
        venue_stats[v]["races"]    += 1
        venue_stats[v]["invested"] += r["invested"]
        venue_stats[v]["payout"]   += r["payout"]

    period_label = {"all": "全期間", "month": period_value, "year": period_value}.get(period_type, "全期間")

    lines = [
        "=" * 50,
        f"  収支サマリ（{period_label}）",
        "=" * 50,
        "",
        "【全体成績】",
        f"  投票レース数: {total_races}R",
        f"  的中レース数: {hit_races}R  （的中率 {hit_rate:.1f}%）",
        "",
        "【収支】",
        f"  総投資額:   {total_invested:,}円",
        f"  総払戻額:   {total_payout:,}円",
        f"  収支:       {net:+,}円",
        f"  回収率:     {roi:.1f}%",
        "",
        "【平均】",
        f"  1レース平均投資: {avg_invested:,.0f}円",
        f"  1レース平均収支: {avg_net:+,.0f}円",
        f"  最高払戻:        {best_payout:,}円",
        "",
        "【会場別】",
    ]

    for v, s in sorted(venue_stats.items(), key=lambda x: x[1]["payout"] - x[1]["invested"], reverse=True):
        v_net = s["payout"] - s["invested"]
        v_roi = s["payout"] / s["invested"] * 100 if s["invested"] > 0 else 0
        lines.append(f"  {v}: {s['races']}R  {v_net:+,}円  回収率{v_roi:.0f}%")

    lines.append("")
    return "\n".join(lines)


@mcp.tool()
def get_recent_bets(limit: int = 20) -> str:
    """
    直近N件の投票記録をシートから取得して表示する。
    limit: 取得件数（デフォルト20、最大100）
    """
    limit = max(1, min(limit, 100))

    try:
        ws = _get_sheet()
        all_rows = ws.get_all_values()
    except Exception as e:
        return f"【エラー】Google Sheetsへの接続に失敗しました。\n詳細: {e}"

    data_rows = [r for r in all_rows[1:] if len(r) >= 6 and r[0]]
    if not data_rows:
        return "【データなし】まだ投票記録がありません。"

    recent = data_rows[-limit:][::-1]  # 新しい順

    lines = [
        "=" * 54,
        f"  直近{min(limit, len(recent))}件の投票記録",
        "=" * 54,
        f"  {'日付':<10}  {'会場':<6}  {'R':<3}  {'組合せ':<8}  {'賭金':>6}  {'オッズ':>6}  {'結果':<4}  {'収支':>8}",
        "  " + "-" * 52,
    ]

    for row in recent:
        d        = row[1] if len(row) > 1 else ""
        venue    = row[2] if len(row) > 2 else ""
        rno      = row[3] if len(row) > 3 else ""
        combo    = row[4] if len(row) > 4 else ""
        amount   = row[5] if len(row) > 5 else ""
        odds_str = row[6] if len(row) > 6 else ""
        result   = row[7] if len(row) > 7 else "-"
        profit_s = row[9] if len(row) > 9 else ""

        try:
            profit = int(profit_s) if profit_s not in ("", "-") else None
        except ValueError:
            profit = None

        profit_disp = f"{profit:+,}円" if profit is not None else "-"
        lines.append(
            f"  {d:<10}  {venue:<6}  {rno:<3}  {combo:<8}  {amount:>5}円  {odds_str:>5}倍  {result:<4}  {profit_disp:>8}"
        )

    lines.append("")
    return "\n".join(lines)


@mcp.tool()
def get_losing_races(period: str = "month") -> str:
    """
    負けたレース（収支マイナス）を一覧表示する。振り返り・反省分析用。
    period: "month"（今月）/ "year"（今年）/ "all"（全期間）
    """
    today = _resolve_date("today")
    period_label_map = {"all": "全期間", "month": f"{today[:4]}-{today[4:6]}", "year": today[:4]}
    period_label = period_label_map.get(period, "全期間")

    def _match(date_str: str) -> bool:
        if period == "all":
            return True
        if period == "month":
            return f"{date_str[:4]}-{date_str[4:6]}" == period_label
        if period == "year":
            return date_str[:4] == period_label
        return True

    try:
        ws = _get_sheet()
        all_rows = ws.get_all_values()
    except Exception as e:
        return f"【エラー】Google Sheetsへの接続に失敗しました。\n詳細: {e}"

    data_rows = [r for r in all_rows[1:] if len(r) >= 6 and r[0]]

    races: dict[str, dict] = {}
    for row in data_rows:
        race_id = row[0]
        if not race_id:
            continue

        d_str  = row[1] if len(row) > 1 else ""
        venue  = row[2] if len(row) > 2 else ""
        rno    = row[3] if len(row) > 3 else ""
        result = row[7] if len(row) > 7 else ""

        if not _match(d_str):
            continue

        try:
            amount = int(float(row[5])) if len(row) > 5 and row[5] not in ("", "-") else 0
            payout = int(float(row[8])) if len(row) > 8 and row[8] not in ("", "-") else 0
        except ValueError:
            amount, payout = 0, 0

        if race_id not in races:
            races[race_id] = {
                "date": d_str, "venue": venue, "race_no": rno,
                "invested": 0, "payout": 0, "result": "", "settled": False,
            }
        races[race_id]["invested"] += amount
        races[race_id]["payout"]   += payout
        if result:
            races[race_id]["result"]  = result
            races[race_id]["settled"] = True

    losing = [
        (rid, r) for rid, r in races.items()
        if r["settled"] and (r["payout"] - r["invested"]) < 0
    ]
    losing.sort(key=lambda x: x[1]["date"], reverse=True)

    if not losing:
        return f"【{period_label}】負けたレースはありません。"

    total_loss = sum(r["payout"] - r["invested"] for _, r in losing)

    lines = [
        "=" * 54,
        f"  負けレース一覧（{period_label}）  計{len(losing)}R",
        "=" * 54,
        f"  {'日付':<10}  {'会場':<6}  {'R':<3}  {'着順':<8}  {'投資':>7}  {'収支':>8}",
        "  " + "-" * 52,
    ]

    for _, r in losing:
        net = r["payout"] - r["invested"]
        lines.append(
            f"  {r['date']:<10}  {r['venue']:<6}  {r['race_no']:<3}  {r['result']:<8}  {r['invested']:>6,}円  {net:+,}円"
        )

    lines += [
        "",
        f"  合計損失: {total_loss:+,}円",
        "",
    ]
    return "\n".join(lines)


# ── Google Sheets 接続ヘルパー ────────────────────────

SPREADSHEET_ID = os.getenv("SPREADSHEET_ID", "1fTicLnOCDAYU0d9z6UN2futydjjRT7bIa2VI2_69VKs")
_SHEETS_SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]


def _get_sheet():
    """bets ワークシートを返す。Render環境は環境変数JSON、ローカルはファイルパスで認証。"""
    try:
        import gspread
        from google.oauth2.service_account import Credentials
    except ImportError:
        raise RuntimeError("gspread / google-auth が未インストールです。pip install gspread google-auth を実行してください。")

    key_json = os.getenv("GOOGLE_SHEETS_KEY_JSON")
    if key_json:
        creds = Credentials.from_service_account_info(json.loads(key_json), scopes=_SHEETS_SCOPES)
    else:
        key_path = os.getenv(
            "GOOGLE_SHEETS_KEY_PATH",
            str(Path.home() / "boatrace-mcp/google-sheets-key.json"),
        )
        creds = Credentials.from_service_account_file(key_path, scopes=_SHEETS_SCOPES)

    gc = gspread.authorize(creds)
    return gc.open_by_key(SPREADSHEET_ID).worksheet("bets")


# ── レポートストレージ ────────────────────────────────

_REPORT_TTL = 86400  # 24時間
_reports: dict[str, tuple[dict, float]] = {}  # id -> (data, expiry)


def _cleanup_reports() -> None:
    now = time.time()
    for rid in [k for k, (_, exp) in _reports.items() if now > exp]:
        del _reports[rid]


def _html_report(d: dict) -> str:
    venue      = d.get("venue", "")
    race_no    = d.get("race_no", "")
    date_str   = d.get("date", "")
    distance   = d.get("distance", "")
    stable     = "あり" if d.get("stable_board") else "なし"
    wind       = d.get("wind", "-")
    wave       = d.get("wave", "-")
    tod        = d.get("time_of_day", "")
    formation  = d.get("formation", "")
    conclusion = d.get("conclusion", "")
    budget     = d.get("total_budget", 0)
    bets       = d.get("bets", [])
    racers     = d.get("racers", [])
    rules      = d.get("rules_applied", [])
    memo       = d.get("memo", "")
    pnl        = d.get("pnl_summary", "")

    total_amount = sum(b.get("amount", 0) for b in bets)

    bet_rows = ""
    for i, b in enumerate(bets, 1):
        odds   = float(b.get("odds", 0))
        amount = int(b.get("amount", 0))
        net    = int(odds * amount) - amount
        cls    = "gp" if net >= 0 else "rp"
        sign   = "+" if net >= 0 else ""
        bet_rows += (
            f"<tr><td>{i}</td>"
            f"<td class='cb'>{b.get('combination','')}</td>"
            f"<td>{odds:.1f}倍</td>"
            f"<td>{amount:,}円</td>"
            f"<td class='{cls}'>{sign}{net:,}円</td></tr>"
        )

    label_color = {
        "★本命軸":   "#ffd700",
        "★2着候補":  "#00b4d8",
        "★3着付け":  "#7b61ff",
        "切り":      "#555",
    }
    racer_cards = ""
    for r in racers:
        lbl   = r.get("label", "")
        color = label_color.get(lbl, "#888")
        racer_cards += (
            f"<div class='rc' style='border-left:3px solid {color}'>"
            f"<div class='rh'>"
            f"<span class='bn' style='background:{color}22;color:{color}'>{r.get('boat_no','')}号艇</span>"
            f"<span class='rn'>{r.get('name','')}</span>"
            f"<span class='rl' style='color:{color}'>{lbl}</span>"
            f"</div>"
            f"<p class='rr'>{r.get('reason','')}</p>"
            f"</div>"
        )

    rules_html = "".join(f"<li>✅ {x}</li>" for x in rules)
    pnl_html   = (
        f"<section class='card'><h2>📊 直近収支</h2><p class='pt'>{pnl}</p></section>"
        if pnl else ""
    )
    formation_html = (
        f"<p class='fi'>編成: {formation}</p>" if formation else ""
    )

    return f"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>{venue} {race_no}R 分析レポート</title>
<style>
:root{{--bg:#f7f7f5;--white:#ffffff;--tx:#1a1a1a;--mt:#888;
  --br:#e8e8e4;--ac:#1a1a1a;--gn:#2a7a4a;--rd:#c0392b;
  --lc:#4a90d9}}
*{{box-sizing:border-box;margin:0;padding:0}}
body{{background:var(--bg);color:var(--tx);
  font-family:-apple-system,'Helvetica Neue',Helvetica,sans-serif;
  font-size:16px;line-height:1.75;padding-bottom:56px}}
header{{background:var(--white);padding:32px 20px 24px;
  border-bottom:1px solid var(--br);text-align:center}}
header h1{{font-size:20px;font-weight:700;letter-spacing:.06em;color:var(--tx)}}
header .sub{{font-size:13px;color:var(--mt);margin-top:6px;letter-spacing:.03em}}
.card{{margin:12px 16px;background:var(--white);border-radius:8px;
  padding:20px 18px;border:1px solid var(--br)}}
.card h2{{font-size:11px;font-weight:700;color:var(--mt);letter-spacing:.12em;
  text-transform:uppercase;margin-bottom:14px;padding-bottom:10px;
  border-bottom:1px solid var(--br)}}
.ig{{display:grid;grid-template-columns:1fr 1fr;gap:1px;
  background:var(--br);border:1px solid var(--br);border-radius:6px;overflow:hidden}}
.ii{{background:var(--white);padding:12px 14px}}
.il{{font-size:11px;color:var(--mt);margin-bottom:2px;letter-spacing:.04em}}
.iv{{font-size:15px;font-weight:600}}
.fi{{margin-top:12px;font-size:13px;color:var(--mt)}}
.ct{{font-size:16px;font-weight:700;line-height:1.6;
  border-left:3px solid var(--tx);padding:10px 14px;
  background:var(--bg);border-radius:0 6px 6px 0}}
.tw{{overflow-x:auto;-webkit-overflow-scrolling:touch;margin:0 -2px}}
table{{width:100%;border-collapse:collapse;min-width:300px}}
th{{font-size:11px;color:var(--mt);font-weight:600;letter-spacing:.06em;
  padding:8px 10px;text-align:center;border-bottom:2px solid var(--br)}}
td{{padding:11px 10px;text-align:center;border-bottom:1px solid var(--br);font-size:14px}}
tr:last-child td{{border-bottom:none}}
td.cb{{font-size:15px;font-weight:700;font-family:monospace;letter-spacing:.1em}}
.gp{{color:var(--gn);font-weight:700}}
.rp{{color:var(--rd);font-weight:700}}
.bs{{margin-top:14px;padding:10px 14px;background:var(--bg);
  border-radius:6px;font-size:13px;color:var(--mt);display:flex;
  justify-content:space-between;align-items:center}}
.bs strong{{color:var(--tx);font-size:15px}}
.rc{{padding:14px 0;border-bottom:1px solid var(--br)}}
.rc:last-child{{border-bottom:none;padding-bottom:0}}
.rh{{display:flex;align-items:baseline;gap:10px;margin-bottom:6px;flex-wrap:wrap}}
.bn{{font-size:12px;font-weight:700;color:var(--mt);
  border:1px solid var(--br);padding:2px 8px;border-radius:3px;white-space:nowrap}}
.rn{{font-size:15px;font-weight:700;flex:1}}
.rl{{font-size:12px;font-weight:600;color:var(--lc);white-space:nowrap}}
.rl.cut{{color:var(--mt)}}
.rr{{font-size:13px;color:var(--mt);line-height:1.65;padding-left:2px}}
ul.rl2{{list-style:none;padding:0}}
ul.rl2 li{{font-size:13px;padding:7px 0;border-bottom:1px solid var(--br);
  color:var(--tx);line-height:1.6}}
ul.rl2 li:last-child{{border-bottom:none}}
.memo{{font-size:14px;border-left:2px solid var(--br);
  padding:10px 14px;color:#444;line-height:1.75}}
.pt{{font-size:15px}}
footer{{text-align:center;font-size:11px;color:var(--mt);
  margin-top:28px;padding:0 20px;letter-spacing:.04em}}
@media(min-width:600px){{
  .card{{margin:12px auto;max-width:580px}}
  header{{padding:40px 20px 28px}}
}}
</style>
</head>
<body>
<header>
  <h1>🚤 {venue} {race_no}R 分析レポート</h1>
  <div class="sub">{date_str}　{tod}</div>
</header>

<section class="card">
  <h2>Race Info</h2>
  <div class="ig">
    <div class="ii"><div class="il">距離</div><div class="iv">{distance}m</div></div>
    <div class="ii"><div class="il">安定板</div><div class="iv">{stable}</div></div>
    <div class="ii"><div class="il">風</div><div class="iv">{wind}</div></div>
    <div class="ii"><div class="il">波</div><div class="iv">{wave}</div></div>
  </div>
  {formation_html}
</section>

<section class="card">
  <h2>Conclusion</h2>
  <div class="ct">{conclusion}</div>
</section>

<section class="card">
  <h2>Bets &nbsp;／&nbsp; 予算 {budget:,}円</h2>
  <div class="tw">
    <table>
      <thead><tr><th>#</th><th>買い目</th><th>オッズ</th><th>賭金</th><th>想定収支</th></tr></thead>
      <tbody>{bet_rows}</tbody>
    </table>
  </div>
  <div class="bs"><span>合計投資</span><strong>{total_amount:,}円</strong></div>
</section>

<section class="card">
  <h2>Each Boat</h2>
  {racer_cards}
</section>

<section class="card">
  <h2>Rules Applied</h2>
  <ul class="rl2">{rules_html}</ul>
</section>

<section class="card">
  <h2>Memo</h2>
  <div class="memo">{memo}</div>
</section>

{pnl_html}

<footer>
  <p>boatrace-mcp &nbsp;|&nbsp; Generated by Claude</p>
  <p style="margin-top:4px">このレポートは一定時間後に自動削除されます</p>
</footer>
</body>
</html>"""


@mcp.tool()
def generate_visual_report(prediction_json: str) -> str:
    """
    予想分析データからHTMLレポートを生成し、閲覧URLを返す。
    prediction_json: 以下キーを持つJSON文字列
      venue(str), race_no(int/str), date(str), distance(int), stable_board(bool),
      wind(str), wave(str), time_of_day(str), formation(str), conclusion(str),
      total_budget(int),
      bets: [{combination, odds, amount}],
      racers: [{boat_no, name, label, reason}],
        labelは「★本命軸」「★2着候補」「★3着付け」「切り」のいずれか
      rules_applied: [str],
      memo(str),
      pnl_summary(str, optional)
    """
    try:
        data = json.loads(prediction_json)
    except json.JSONDecodeError as e:
        return f"【エラー】JSONのパースに失敗しました: {e}"

    venue   = data.get("venue", "")
    race_no = data.get("race_no", "")
    html    = _html_report(data)

    base_url = os.getenv("RENDER_EXTERNAL_URL", "").rstrip("/")

    if base_url:
        # Renderのみ：メモリに保存してURLを返す
        _cleanup_reports()
        report_id = secrets.token_urlsafe(16)
        _reports[report_id] = (data, time.time() + _REPORT_TTL)
        url = f"{base_url}/reports/{report_id}"
        return (
            f"✅ レポートを生成しました\n\n"
            f"🚤 {venue} {race_no}R 分析レポート\n"
            f"🔗 {url}\n\n"
            f"⏱ このURLは24時間後に失効します"
        )
    else:
        # ローカルモード：HTMLファイルに保存してブラウザで開く
        import tempfile, subprocess
        tmp = tempfile.NamedTemporaryFile(
            suffix=".html",
            prefix=f"boatrace_{venue}{race_no}R_",
            delete=False,
            mode="w",
            encoding="utf-8",
        )
        tmp.write(html)
        tmp.close()
        subprocess.Popen(["open", tmp.name])
        return (
            f"✅ レポートをブラウザで開きました\n\n"
            f"🚤 {venue} {race_no}R 分析レポート\n"
            f"📄 {tmp.name}\n\n"
            f"💡 スマホで見たい場合はclaude.ai（Web版）をご利用ください"
        )


# ── レポートHTTPエンドポイント（FastMCP公式のcustom_routeを使用）──

@mcp.custom_route("/reports/{report_id}", methods=["GET"])
async def serve_report(request):
    from starlette.responses import HTMLResponse
    report_id = request.path_params["report_id"]
    _cleanup_reports()
    entry = _reports.get(report_id)
    if not entry:
        return HTMLResponse(
            "<html><body style='background:#0d0d1a;color:#e8e8f0;"
            "font-family:sans-serif;display:flex;align-items:center;"
            "justify-content:center;height:100vh;margin:0'>"
            "<div style='text-align:center'><p style='font-size:48px'>🚤</p>"
            "<p style='font-size:20px;margin-top:16px'>レポートが見つかりません</p>"
            "<p style='color:#8888aa;margin-top:8px'>期限切れまたは無効なURLです</p>"
            "</div></body></html>",
            status_code=404,
        )
    data, _ = entry
    return HTMLResponse(_html_report(data))


# ── エントリーポイント ────────────────────────────────

if __name__ == "__main__":
    port_env = os.getenv("PORT")
    if port_env:  # Render上ではPORTが自動設定される → HTTPモード
        import uvicorn
        uvicorn.run(mcp.streamable_http_app(), host="0.0.0.0", port=int(port_env))
    else:  # ローカル（Claude Desktop）→ stdioモード
        mcp.run()

#!/usr/bin/env python3
"""
ボートレース予想MCPサーバー
claude.aiモバイルアプリから呼び出し、レース分析に必要なデータを自動取得する
"""

import os
import re
import math
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


# ── Tool 2: 直前情報 ──────────────────────────────────

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
        "https://boatraceopenapi.github.io/previews/v2/today.json"
        if date == "today"
        else f"https://boatraceopenapi.github.io/previews/v2/{target_date}.json"
    )

    try:
        resp = requests.get(url, headers=HEADERS, timeout=15)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        return f"【エラー】データ取得に失敗しました。\n詳細: {e}"

    race = _find_race(data.get("previews", []), venue, race_no)
    if race is None:
        return (
            f"【データなし】{_venue_name(venue)} {race_no}R の直前情報が見つかりません。\n"
            "直前情報はレース開始約40分前から公開されます。"
        )

    # 気象情報
    weather_num = race.get("race_weather_number", 0)
    wind_dir_num = race.get("race_wind_direction_number", 0)

    lines = [
        "=" * 50,
        f"  {_venue_name(venue)}競艇  {race_no}R  直前情報  ({target_date})",
        "=" * 50,
        "",
        "【気象情報】",
        f"  天候: {WEATHER_MAP.get(weather_num, f'番号{weather_num}')}",
        f"  風向: {WIND_DIR_MAP.get(wind_dir_num, f'番号{wind_dir_num}')}",
        f"  風速: {race.get('race_wind', '-')} m/s",
        f"  波高: {race.get('race_wave', '-')} cm",
        f"  気温: {race.get('race_temperature', '-')} ℃",
        f"  水温: {race.get('race_water_temperature', '-')} ℃",
        "",
        "【選手別直前情報】",
        f"  {'枠':>3}  {'展示タイム':>10}  {'展示ST':>8}  {'チルト':>6}  {'体重':>6}  {'体重調整':>6}",
        "  " + "-" * 48,
    ]

    # boats は dict {"1": {...}, "2": {...}, ...}
    boats = race.get("boats", {})
    for frame_str in sorted(boats.keys(), key=lambda x: int(x)):
        b = boats[frame_str]
        exhibit_t   = b.get("racer_exhibition_time", 0)
        exhibit_st  = b.get("racer_start_timing")
        tilt        = b.get("racer_tilt_adjustment", 0)
        weight      = b.get("racer_weight", "-")
        w_adj       = b.get("racer_weight_adjustment", 0)

        exhibit_t_str  = f"{exhibit_t:.2f}" if isinstance(exhibit_t, (int, float)) and exhibit_t > 0 else "未公開"
        exhibit_st_str = f"{exhibit_st:.2f}" if exhibit_st is not None else "未公開"
        tilt_str       = f"{tilt:+.1f}" if isinstance(tilt, (int, float)) else str(tilt)
        w_adj_str      = f"{w_adj:+.1f}" if w_adj else "  0.0"

        lines.append(
            f"  {frame_str:>3}号艇  {exhibit_t_str:>10}  {exhibit_st_str:>8}"
            f"  {tilt_str:>6}  {weight:>6}kg  {w_adj_str:>6}kg"
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

    # ── ジェネラルルール（常に返す）──
    general_path = RULES_DIR / "general.yaml"
    if general_path.exists():
        with open(general_path, encoding="utf-8") as f:
            general = yaml.safe_load(f)
        rules = general.get("rules", [])
        if rules:
            section = ["【ジェネラルルール（R1〜R14）】"]
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
            night = "ナイター開催あり" if vdata.get("night_race") else "デイ開催"
            section.append(f"  基本情報: {water} / {dist}m / {night}")

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

    # 払戻 = bet × odds > total_budget を満たす最小の100円単位賭金
    min_bet_100 = math.ceil(total_budget / odds / 100) * 100
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
        f"  差し引き損益:        {profit:+,.0f} 円",
        f"  予算に対する割合:     {budget_ratio:.0f}%",
        "",
        "  ▼ 判断ガイド（R7ルール適用）",
    ]

    if budget_ratio > 60:
        lines.append(
            f"  ⚠ 予算の{budget_ratio:.0f}%をこの1点に集中させる必要があります。"
        )
        lines.append("    オッズが低すぎます。R7に従い「切る」か「トリガミ承知で少額のみ」を検討してください。")
    elif budget_ratio > 40:
        lines.append(
            f"  △ 予算の{budget_ratio:.0f}%（{min_bet_100}円）が必要です。"
        )
        lines.append("    他の買い目とのバランスを確認してください。")
    else:
        lines.append(
            f"  ○ 予算の{budget_ratio:.0f}%（{min_bet_100}円）でトリガミを回避できます。"
        )
        lines.append("    合理的なライン内です。")

    lines.append("")
    return "\n".join(lines)


# ── エントリーポイント ────────────────────────────────

if __name__ == "__main__":
    port_env = os.getenv("PORT")
    if port_env:  # Render上ではPORTが自動設定される → HTTPモード
        import uvicorn
        app = mcp.streamable_http_app()
        uvicorn.run(app, host="0.0.0.0", port=int(port_env))
    else:  # ローカル（Claude Desktop）→ stdioモード
        mcp.run()

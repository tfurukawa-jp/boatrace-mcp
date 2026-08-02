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
from datetime import datetime, timezone, timedelta
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

# レースの日付は常に日本時間で決まる。実行環境のローカル時刻（Renderでは UTC）を
# 使うと、日本時間 0:00〜9:00 の間だけ前日の日付になってしまうため JST を明示する。
JST = timezone(timedelta(hours=9))


def _today() -> str:
    return datetime.now(JST).strftime("%Y%m%d")


def _resolve_date(d: str) -> str:
    """"today" をここで YYYYMMDD に正規化する。以降は日付指定と同じ経路を通す。"""
    return _today() if d == "today" else d


def _venue_name(venue_id: int) -> str:
    return VENUE_NAMES.get(venue_id, f"会場{venue_id}")


def _find_race(programs: list, venue: int, race_no: int) -> dict:
    """
    programs/previews APIのフラットなリストから会場・レースを絞り込む。
    配列の並び順には一切依存せず、場コードとレース番号の一致でのみ選ぶ。
    該当が無ければ例外。None を返して呼び出し側の判断に委ねると、
    「見つからなかった」が黙って素通りする経路ができてしまうため。
    """
    for item in programs:
        if item.get("race_stadium_number") == venue and item.get("race_number") == race_no:
            return item

    available = sorted({
        p.get("race_stadium_number") for p in programs
        if p.get("race_stadium_number") is not None
    })
    raise ValueError(
        f"{_venue_name(venue)}({venue}) {race_no}R が配信データに含まれていません"
        f"（収録会場: {', '.join(str(v) for v in available) or 'なし'}）"
    )


# ── 出走表の取得経路 ───────────────────────────────────
# 主: boatrace.jp（公式・当日分が確実に存在する）
# 副: BoatraceOpenAPI（配信停止時に古いデータを黙って返すため主にはしない）

BOATRACE_JP_RACELIST_URL = "https://www.boatrace.jp/owpc/pc/race/racelist"

CLASS_NUM = {v: k for k, v in CLASS_MAP.items()}  # "A1" -> 1

_RE_PLACE_IMG = re.compile(r"text_place2_(\d+)\.png")
_RE_GRADE_CLS = re.compile(r"is-(SG|G1|G2|G3)")
_RE_BOAT_COLOR = re.compile(r"is-boatColor(\d)")
_RE_DISTANCE = re.compile(r"(\d+)\s*m")
_RE_AGE_WEIGHT = re.compile(r"(\d+)歳\s*/\s*([\d.]+)kg")
_RE_FL_ST = re.compile(r"F(\d+)\s*L(\d+)\s*([\d.]+)")
_RE_MMDD = re.compile(r"(\d{1,2})月(\d{1,2})日")


def _to_num(text: str, cast=float):
    """'51.75' → 51.75 / '-' や空文字 → None（数値化できないものは黙って0にしない）"""
    try:
        return cast(text)
    except (TypeError, ValueError):
        return None


def _parse_racelist_page(html: str) -> dict:
    """
    boatrace.jp 出走表ページを BoatraceOpenAPI v2 と同じキー構造の dict に正規化する。
    ページが自分自身について名乗っている値（会場・レース番号・日付）も併せて返し、
    呼び出し側が要求値と突合できるようにする。
    解析できない場合は ValueError を送出する（それらしい別データを返す経路を作らない）。
    """
    soup = BeautifulSoup(html, "html.parser")

    # ── ページ自身が名乗る会場（画像ファイル名に場コードが埋まっている）
    place_img = soup.select_one(".heading2_area img")
    if place_img is None:
        raise ValueError("会場情報が見つかりません（開催のない会場・日付の可能性）")
    m = _RE_PLACE_IMG.search(place_img.get("src", ""))
    if not m:
        raise ValueError("会場コードを特定できません")
    page_venue = int(m.group(1))
    page_venue_name = place_img.get("alt", "").strip()

    # ── ページ自身が名乗る日付（"8月2日 最終日"）
    day_tab = soup.select_one(".is-active2")
    page_date_label = day_tab.get_text(" ", strip=True) if day_tab else ""

    # ── ページ自身が名乗るレース番号
    # レース選択行のうち、色クラス（is-thColor2=終了済 / is-thColor3=未発走）が
    # 付いていない唯一の <th> が表示中のレース。
    # ※ is-activeColor1 は「次に締切が来るレース」であり表示中レースではないため使わない。
    page_race_no = None
    closed_at = None
    for table in soup.find_all("table"):
        head = table.find("tr")
        if head is None:
            continue
        ths = head.find_all("th")
        labels = [th.get_text(strip=True) for th in ths]
        if not labels or labels[0] != "レース":
            continue
        for i, th in enumerate(ths[1:], start=1):
            if th.get("class") is None:
                page_race_no = _to_num(labels[i].rstrip("Rr"), int)
                # 締切予定時刻の行は先頭セルが見出し（"締切予定時刻"）なので、
                # ヘッダ行の th インデックス i と td インデックスがそのまま対応する。
                time_row = table.find_all("tr")[1] if len(table.find_all("tr")) > 1 else None
                if time_row:
                    tds = time_row.find_all("td")
                    if i < len(tds) and tds[0].get_text(strip=True) == "締切予定時刻":
                        closed_at = tds[i].get_text(strip=True)
                break
        break

    # ── 大会名・グレード・副題・距離
    title_el = soup.select_one(".heading2_title")
    race_title = title_el.get_text(strip=True) if title_el else ""
    grade = "一般"
    if title_el is not None:
        gm = _RE_GRADE_CLS.search(" ".join(title_el.get("class") or []))
        if gm:
            grade = gm.group(1)

    detail_el = soup.select_one(".title16_titleDetail__add2020")
    detail = detail_el.get_text(" ", strip=True) if detail_el else ""
    dm = _RE_DISTANCE.search(detail)
    race_distance = int(dm.group(1)) if dm else None
    race_subtitle = _RE_DISTANCE.sub("", detail).replace("　", " ").strip()

    # ── 出走6艇
    tables = soup.find_all("table")
    if len(tables) < 2:
        raise ValueError("出走表テーブルが見つかりません")

    boats = []
    for tbody in tables[1].find_all("tbody"):
        head_row = tbody.find("tr")
        if head_row is None:
            continue
        tds = head_row.find_all("td")
        if len(tds) < 8:
            continue

        bm = _RE_BOAT_COLOR.search(" ".join(tds[0].get("class") or []))
        if not bm:
            continue
        frame = int(bm.group(1))

        # 選手情報セル: ['4980', '/', 'A1', '佐々木　完太', '山口/山口', '30歳/50.5kg']
        info = [t.strip() for t in tds[2].get_text("\n", strip=True).split("\n") if t.strip()]
        info = [t for t in info if t != "/"]
        if len(info) < 5:
            raise ValueError(f"{frame}号艇の選手情報を解析できません")

        reg_no = _to_num(info[0], int)
        cls_label = info[1]
        name = info[2].replace("　", " ")
        branch, _, birthplace = info[3].partition("/")
        am = _RE_AGE_WEIGHT.search(info[4])
        age = _to_num(am.group(1), int) if am else None
        weight = _to_num(am.group(2), float) if am else None

        fm = _RE_FL_ST.search(tds[3].get_text(" ", strip=True))
        flying = _to_num(fm.group(1), int) if fm else None
        late = _to_num(fm.group(2), int) if fm else None
        avg_st = _to_num(fm.group(3), float) if fm else None

        def cells(td):
            return td.get_text(" ", strip=True).split()

        national = cells(tds[4])
        local = cells(tds[5])
        motor = cells(tds[6])
        boat = cells(tds[7])
        for label, vals, need in (
            ("全国成績", national, 3), ("当地成績", local, 3),
            ("モーター", motor, 3), ("ボート", boat, 3),
        ):
            if len(vals) < need:
                raise ValueError(f"{frame}号艇の{label}を解析できません")

        boats.append({
            "racer_boat_number": frame,
            "racer_name": name,
            "racer_number": reg_no,
            "racer_class_number": CLASS_NUM.get(cls_label, 0),
            "racer_branch": branch.strip(),
            "racer_birthplace": birthplace.strip(),
            "racer_age": age,
            "racer_weight": weight,
            "racer_flying_count": flying,
            "racer_late_count": late,
            "racer_average_start_timing": avg_st,
            "racer_national_top_1_percent": _to_num(national[0]),
            "racer_national_top_2_percent": _to_num(national[1]),
            "racer_national_top_3_percent": _to_num(national[2]),
            "racer_local_top_1_percent": _to_num(local[0]),
            "racer_local_top_2_percent": _to_num(local[1]),
            "racer_local_top_3_percent": _to_num(local[2]),
            "racer_assigned_motor_number": _to_num(motor[0], int),
            "racer_assigned_motor_top_2_percent": _to_num(motor[1]),
            "racer_assigned_motor_top_3_percent": _to_num(motor[2]),
            "racer_assigned_boat_number": _to_num(boat[0], int),
            "racer_assigned_boat_top_2_percent": _to_num(boat[1]),
            "racer_assigned_boat_top_3_percent": _to_num(boat[2]),
        })

    if not boats:
        raise ValueError("出走選手を1名も取得できませんでした")

    return {
        "race_stadium_number": page_venue,
        "race_stadium_name": page_venue_name,
        "race_number": page_race_no,
        "race_date_label": page_date_label,
        "race_closed_at": closed_at,
        "race_title": race_title,
        "race_subtitle": race_subtitle,
        "race_grade_label": grade,
        "race_distance": race_distance,
        "boats": boats,
    }


def _verify_racecard(race: dict, venue: int, race_no: int, target_date: str, source: str) -> None:
    """
    応答検証層。取得データが「要求したレースそのもの」かを突合する。
    1つでも食い違えば例外。照合材料が無くて検証できない場合も例外にする。
    （配信停止中のAPIが古いデータを返し続けても、ここで必ず止まる）
    """
    problems = []

    got_venue = race.get("race_stadium_number")
    if got_venue != venue:
        problems.append(f"会場: 要求={venue}({_venue_name(venue)}) / 取得={got_venue}")

    got_no = race.get("race_number")
    if got_no != race_no:
        problems.append(f"レース番号: 要求={race_no}R / 取得={got_no}R")

    want_y, want_m, want_d = target_date[:4], int(target_date[4:6]), int(target_date[6:8])

    if race.get("race_date"):
        # BoatraceOpenAPI: "2026-08-01" 形式。年月日すべて突合する。
        want_iso = f"{want_y}-{want_m:02d}-{want_d:02d}"
        got_iso = str(race["race_date"])[:10]
        if got_iso != want_iso:
            problems.append(f"日付: 要求={want_iso} / 取得={got_iso}")
    elif race.get("race_date_label"):
        # boatrace.jp: "8月2日 最終日" 形式。ページに年が無いため月日で突合する。
        m = _RE_MMDD.search(race["race_date_label"])
        if not m:
            problems.append(f"日付: ページの日付表記を解釈できません（{race['race_date_label']}）")
        elif (int(m.group(1)), int(m.group(2))) != (want_m, want_d):
            problems.append(
                f"日付: 要求={want_m}月{want_d}日 / 取得={m.group(1)}月{m.group(2)}日"
            )
    else:
        # 照合材料が無い＝正しさを保証できない。通さない。
        problems.append("日付: 取得データに日付情報が無く、検証できません")

    if problems:
        raise ValueError(
            "取得したデータが要求と一致しません（" + source + "）: " + " / ".join(problems)
        )


def _fetch_racecard_from_boatrace_jp(venue: int, race_no: int, target_date: str) -> dict:
    """主経路。取得・解析・検証のいずれかに失敗したら例外を送出する。"""
    url = f"{BOATRACE_JP_RACELIST_URL}?rno={race_no}&jcd={venue:02d}&hd={target_date}"
    resp = requests.get(url, headers=HEADERS, timeout=15)
    resp.raise_for_status()
    race = _parse_racelist_page(resp.text)
    source = f"boatrace.jp racelist (rno={race_no}&jcd={venue:02d}&hd={target_date})"
    _verify_racecard(race, venue, race_no, target_date, source)
    race["_source"] = source
    return race


def _fetch_racecard_from_openapi(venue: int, race_no: int, target_date: str) -> dict:
    """
    副経路。主経路が失敗したときのみ使う。
    日付別配信のURLは v2/{YYYY}/{YYYYMMDD}.json（年フォルダが必要）。
    年フォルダを省いた v2/{YYYYMMDD}.json はどの日付でも必ず404になる。

    today.json は使わない。配信が停止すると停止時点の中身を返し続けるため、
    「当日を要求したのに前日が返る」という今回の不具合の発生源そのものだった。
    日付指定なら、その日のファイルが無ければ404になり、古いデータを掴む余地がない。
    """
    url = f"https://boatraceopenapi.github.io/programs/v2/{target_date[:4]}/{target_date}.json"
    resp = requests.get(url, headers=HEADERS, timeout=15)
    resp.raise_for_status()
    race = dict(_find_race(resp.json().get("programs", []), venue, race_no))
    source = f"BoatraceOpenAPI {url.split('/programs/')[1]}"
    _verify_racecard(race, venue, race_no, target_date, source)
    race["_source"] = source
    return race


# v3配信データの grade_number と grade_label を突合して確認した対応
# （2 は標本に出現しなかったため G1 と推定。表示ラベルのみに使用）
GRADE_NUM_MAP = {1: "SG", 2: "G1", 3: "G2", 4: "G3", 5: "一般"}


def _racecard_header(race: dict) -> list:
    """
    出走表ヘッダを組み立てる。
    表示する会場・レース番号・日付は、すべて「取得したデータが名乗っている値」から取る。
    引数から作らないのは、中身と表示がズレたときに表示が嘘をつかないようにするため。
    """
    stadium_no = race.get("race_stadium_number")
    venue_label = race.get("race_stadium_name") or _venue_name(stadium_no) if stadium_no else "?"
    if stadium_no:
        venue_label = f"{venue_label}(場{stadium_no})"

    no = race.get("race_number")
    race_label = f"{no}R" if no is not None else "?R"

    date_label = race.get("race_date") or race.get("race_date_label") or "日付不明"

    grade = race.get("race_grade_label") or GRADE_NUM_MAP.get(race.get("race_grade_number"), "")
    title = " ".join(x for x in [grade, race.get("race_title", "")] if x).strip()
    subtitle = race.get("race_subtitle", "") or ""
    distance = race.get("race_distance")

    lines = [
        "=" * 50,
        f"  {venue_label}  {race_label}  出走表  [{date_label}]",
        f"  {title}  {subtitle}".rstrip(),
        f"  距離: {distance if distance is not None else '-'}m",
    ]
    if race.get("race_closed_at"):
        lines.append(f"  締切予定: {race['race_closed_at']}")

    lines.append(f"  [出典: {race.get('_source', '不明')}]")
    if race.get("_fallback_reason"):
        lines.append(f"  ※ 副経路に降格しました（主経路 boatrace.jp の失敗: {race['_fallback_reason']}）")
    lines += ["=" * 50, ""]
    return lines


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

    # 主経路: boatrace.jp（公式）。失敗したときだけ OpenAPI に降りる。
    errors = []
    race = None
    try:
        race = _fetch_racecard_from_boatrace_jp(venue, race_no, target_date)
    except Exception as e:
        errors.append(f"boatrace.jp: {e}")
        try:
            race = _fetch_racecard_from_openapi(venue, race_no, target_date)
            race["_fallback_reason"] = str(e)
        except Exception as e2:
            errors.append(f"BoatraceOpenAPI: {e2}")

    if race is None:
        return (
            f"【データなし】{_venue_name(venue)} {race_no}R（{target_date}）の出走表を取得できませんでした。\n"
            "いずれの取得経路も失敗しています。\n  - " + "\n  - ".join(errors)
        )

    lines = _racecard_header(race)

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


# ── 穴予想エンジン: 展示スコア計算 ───────────────────────

@mcp.tool()
def calc_exhibition_score(racers_data: str) -> str:
    """
    各艇の展示スコア（0〜100点）を計算する。穴予想エンジンの「展示重視穴」ロジックの素材。

    racers_data: 6艇分の直前情報リストのJSON文字列。
      例: '[{"boat_no":1,"name":"...","exhibit_time":"6.78","weight":52,"tilt":0.0,"exhibit_st":"0.16"}, ...]'
      欠損値は null か "-" を入れてOK（その項目は0点扱い）。

    スコア配分（古川さん仕様）:
      加点
        展示タイム順位: 1位+30 / 2位+20 / 3位+10
        体重:           ≦51kg+15 / ≦52kg+5
        チルト:         ≦-0.5+10
        展示ST:         ≦.05+20 / ≦.10+10 / ≦.15+5
      減点
        展示ST  ≧.20: -15
        体重    ≧55kg: -10
        チルト  ≧0.0:  -5
      合計を 0〜100 にクリップ。

    戻り値: JSON文字列（structured data）
      {"scores": [{"boat_no", "name", "score", "exhibit_time_rank",
                   "breakdown": {"time_rank_pts","weight_pts","tilt_pts","st_pts","deductions"}}, ...]}
      scores はスコア降順でソート済み。
    """
    try:
        racers = json.loads(racers_data)
    except json.JSONDecodeError as e:
        return json.dumps({"error": f"racers_dataのJSON形式が不正: {e}"}, ensure_ascii=False)

    if not isinstance(racers, list) or not racers:
        return json.dumps({"error": "racers_dataは1艇以上のリストである必要があります"}, ensure_ascii=False)

    def _to_float(v):
        try:
            if v is None or v == "" or v == "-":
                return None
            return float(v)
        except (TypeError, ValueError):
            return None

    # 展示タイム順位（小さい順）— タイム不明艇は順位なし
    times_with_idx = []
    for i, r in enumerate(racers):
        t = _to_float(r.get("exhibit_time"))
        if t is not None and t > 0:
            times_with_idx.append((t, i))
    times_with_idx.sort(key=lambda x: x[0])
    rank_by_idx = {idx: rank + 1 for rank, (_, idx) in enumerate(times_with_idx)}

    scores = []
    for i, r in enumerate(racers):
        boat_no = r.get("boat_no")
        name    = r.get("name", "")
        weight  = _to_float(r.get("weight"))
        tilt    = _to_float(r.get("tilt"))
        st      = _to_float(r.get("exhibit_st"))
        rank    = rank_by_idx.get(i, 0)  # 0=未ランク

        # 加点
        time_rank_pts = {1: 30, 2: 20, 3: 10}.get(rank, 0)

        weight_pts = 0
        if weight is not None:
            if weight <= 51:
                weight_pts = 15
            elif weight <= 52:
                weight_pts = 5

        tilt_pts = 10 if (tilt is not None and tilt <= -0.5) else 0

        st_pts = 0
        if st is not None:
            if st <= 0.05:
                st_pts = 20
            elif st <= 0.10:
                st_pts = 10
            elif st <= 0.15:
                st_pts = 5

        # 減点
        deductions = 0
        if st is not None and st >= 0.20:
            deductions += -15
        if weight is not None and weight >= 55:
            deductions += -10
        if tilt is not None and tilt >= 0.0:
            deductions += -5

        total = time_rank_pts + weight_pts + tilt_pts + st_pts + deductions
        total = max(0, min(100, total))  # 0〜100にクリップ

        scores.append({
            "boat_no": boat_no,
            "name": name,
            "score": total,
            "exhibit_time_rank": rank if rank > 0 else None,
            "breakdown": {
                "time_rank_pts": time_rank_pts,
                "weight_pts": weight_pts,
                "tilt_pts": tilt_pts,
                "st_pts": st_pts,
                "deductions": deductions,
            },
        })

    scores.sort(key=lambda x: x["score"], reverse=True)
    return json.dumps({"scores": scores}, ensure_ascii=False, indent=2)


# ── 穴予想エンジン: 市場本命オッズ統計 ───────────────────

@mcp.tool()
def calc_market_favorite_oddsstats(odds_data: str) -> str:
    """
    3連単オッズ全件から市場本命統計を計算し、逆張り強度を判定する。

    odds_data: 3連単オッズリストのJSON文字列。
      例: '[{"combination":"1-2-3","odds":3.4}, {"combination":"1-3-2","odds":4.1}, ...]'
      get_odds の出力（人気上位30件）でも、120件全件でも動く。

    判定ロジック（古川さん仕様）:
      最低オッズ組み合わせ（=市場本命）の3連単オッズで:
        ≦8.0倍   : 市場が固い         → 逆張り強(strong)
        8.0〜12.0倍: 妥当な人気       → 逆張り中(medium)
        ≧12.0倍  : 実力均衡/不確定要素 → 逆張り発動せず(off)
      追加: 1-X-Y上位3パターン平均オッズが10倍以下 → 1コース過信フラグON

    戻り値: JSON文字列
      {"favorite": {"combination","odds"},
       "first_course_top3_avg": float,
       "second_course_top3_avg": float,
       "third_course_top3_avg": float,
       "verdict": "tight"|"moderate"|"balanced",
       "reverse_intensity": "strong"|"medium"|"off",
       "first_course_overconfidence": bool,
       "interpretation": "..."}
    """
    try:
        odds_list = json.loads(odds_data)
    except json.JSONDecodeError as e:
        return json.dumps({"error": f"odds_dataのJSON形式が不正: {e}"}, ensure_ascii=False)

    if not isinstance(odds_list, list) or not odds_list:
        return json.dumps({"error": "odds_dataは1件以上のリストである必要があります"}, ensure_ascii=False)

    # オッズ値で正規化（数値変換できないものは除外）
    parsed = []
    for entry in odds_list:
        combo = entry.get("combination", "")
        try:
            odds_val = float(entry.get("odds", 0))
        except (TypeError, ValueError):
            continue
        if odds_val <= 0 or not re.match(r"^[1-6]-[1-6]-[1-6]$", combo):
            continue
        parsed.append((odds_val, combo))

    if not parsed:
        return json.dumps({"error": "有効なオッズデータが1件もありません"}, ensure_ascii=False)

    parsed.sort(key=lambda x: x[0])
    fav_odds, fav_combo = parsed[0]

    # 1着頭別の上位3平均
    def _top3_avg(first_digit: str) -> Optional[float]:
        filtered = [o for o, c in parsed if c.startswith(f"{first_digit}-")]
        top3 = filtered[:3] if len(filtered) >= 3 else filtered
        return round(sum(top3) / len(top3), 2) if top3 else None

    first_avg  = _top3_avg("1")
    second_avg = _top3_avg("2")
    third_avg  = _top3_avg("3")

    # 逆張り強度判定（市場本命オッズベース）
    if fav_odds <= 8.0:
        verdict = "tight"
        reverse_intensity = "strong"
        verdict_jp = "市場が固い"
        intent_jp = "本命1着固定の信頼度を下げ、2-1/3-1の差し1着シナリオを本線格上げ"
    elif fav_odds <= 12.0:
        verdict = "moderate"
        reverse_intensity = "medium"
        verdict_jp = "妥当な人気"
        intent_jp = "本命系も維持しつつ、3着付けで穴艇を保険厚めに含める"
    else:
        verdict = "balanced"
        reverse_intensity = "off"
        verdict_jp = "実力均衡または不確定要素多い"
        intent_jp = "逆張りは発動せず、展示重視穴のみで全方位カバー"

    # 1コース過信フラグ
    first_overconf = first_avg is not None and first_avg <= 10.0

    interpretation = (
        f"市場本命 {fav_combo} のオッズ {fav_odds:.1f}倍 → {verdict_jp}。"
        f"逆張り強度: {reverse_intensity}。{intent_jp}。"
    )
    if first_overconf:
        interpretation += f" さらに1-X-Y上位3平均{first_avg:.1f}倍で1コース過信気味、本命切り検討。"

    return json.dumps({
        "favorite": {"combination": fav_combo, "odds": fav_odds},
        "first_course_top3_avg":  first_avg,
        "second_course_top3_avg": second_avg,
        "third_course_top3_avg":  third_avg,
        "verdict": verdict,
        "reverse_intensity": reverse_intensity,
        "first_course_overconfidence": first_overconf,
        "interpretation": interpretation,
    }, ensure_ascii=False, indent=2)


# ── 穴予想エンジン: 本体 ───────────────────────────────

def _parse_pct(val) -> float:
    """'55%' / '55.0' / 55 / 55.0 を float に変換。欠損は0.0。"""
    if val is None:
        return 0.0
    s = str(val).strip().replace("%", "")
    try:
        return float(s)
    except (TypeError, ValueError):
        return 0.0


def _build_aggressive_bets(
    intensity: str,
    fav_combo: str,
    fav_odds: float,
    dark_horses: list,
    third_pool: list,
    scores: list,
    odds_dict: dict,
    budget: int,
) -> list:
    """
    逆張り強度に応じて買い目を機械的に組み立てる。
    戦略は以下の3通り:
      strong : 本命100円 + 差し1着系70% + 展示穴3着付け30%
      medium : 本命系50% + 差し系30% + 展示穴20%
      off    : 本命100円 + 展示重視全方位
    """
    bets = []

    def _add(combo: str, amount: int, rationale: str):
        o = odds_dict.get(combo)
        if o is None or amount < 100:
            return
        bets.append({
            "combination": combo,
            "odds": o,
            "amount": amount,
            "rationale": rationale,
        })

    def _allocate(combos: list, total: int, rationale_fn):
        """combos = [combo文字列, ...] に total円を100円単位で均等配分"""
        if not combos or total < 100:
            return
        per = max(100, (total // len(combos)) // 100 * 100)
        for c in combos:
            _add(c, per, rationale_fn(c))

    # 高スコア艇（展示重視穴の素材）
    high_score_boats = [s["boat_no"] for s in scores if s["score"] >= 60]

    if intensity == "strong":
        # 1. 本命最小保証
        _add(fav_combo, 100, "本命最小保証（市場が固く本命1着固定の信頼度低）")
        budget_left = budget - 100

        # 2. 差し1着シナリオ: 2-1-X, 3-1-X（3着候補プールから）
        sashi_combos = []
        for first in ("2", "3"):
            for third in third_pool:
                if str(third) in (first, "1"):
                    continue
                c = f"{first}-1-{third}"
                if c in odds_dict and 5 <= odds_dict[c] <= 80:
                    sashi_combos.append(c)
        sashi_budget = int(budget_left * 0.7)
        _allocate(sashi_combos, sashi_budget,
                  lambda c: f"逆張り強：差し1着シナリオ（{c[0]}コース→1コース連→{c[-1]}号艇）")
        spent = sum(b["amount"] for b in bets if b["combination"] in sashi_combos)
        budget_left -= spent

        # 3. 展示穴3着付け: 1-2-{穴}, 1-3-{穴}, 2-1-{穴}
        ana_combos = []
        for h in dark_horses:
            for prefix in ("1-2-", "1-3-", "2-1-"):
                c = f"{prefix}{h}"
                if str(h) in prefix.split("-")[:2]:
                    continue
                if c in odds_dict and odds_dict[c] >= 10:
                    ana_combos.append(c)
        _allocate(list(dict.fromkeys(ana_combos)), budget_left,
                  lambda c: f"展示穴3着付け（{c[-1]}号艇=展示スコア60+の逆襲候補）")

    elif intensity == "medium":
        # 1. 本命系: 1-2-X, 1-3-X（3着候補プールから上位3）
        honmei_combos = []
        for second in ("2", "3"):
            for third in third_pool:
                if str(third) in (second, "1"):
                    continue
                c = f"1-{second}-{third}"
                if c in odds_dict:
                    honmei_combos.append((c, odds_dict[c]))
        honmei_combos.sort(key=lambda x: x[1])  # 低オッズ優先
        honmei_combos = [c for c, _ in honmei_combos[:4]]
        _allocate(honmei_combos, int(budget * 0.5),
                  lambda c: f"本命系：1コース1着、{c[2]}コース2着、{c[-1]}号艇3着")

        # 2. 差し系保険: 2-1-X, 3-1-X（オッズが付いているもの中心1〜2点）
        sashi_combos = []
        for first in ("2", "3"):
            for third in third_pool:
                if str(third) in (first, "1"):
                    continue
                c = f"{first}-1-{third}"
                if c in odds_dict and 5 <= odds_dict[c] <= 80:
                    sashi_combos.append((c, odds_dict[c]))
        sashi_combos.sort(key=lambda x: x[1])
        sashi_combos = [c for c, _ in sashi_combos[:3]]
        _allocate(sashi_combos, int(budget * 0.3),
                  lambda c: f"逆張り中：差し系保険（{c[0]}コース1着シナリオ）")

        # 3. 展示穴3着付け
        ana_combos = []
        for h in dark_horses:
            c = f"1-2-{h}"
            if h not in (1, 2) and c in odds_dict and odds_dict[c] >= 10:
                ana_combos.append(c)
            c = f"1-3-{h}"
            if h not in (1, 3) and c in odds_dict and odds_dict[c] >= 10:
                ana_combos.append(c)
        _allocate(list(dict.fromkeys(ana_combos)), int(budget * 0.2),
                  lambda c: f"展示穴3着付け（{c[-1]}号艇=逆襲候補）")

    else:  # off — 展示重視全方位（実力均衡。逆張り発動せず、展示穴のみで広く）
        # 1. 本命最小保証
        _add(fav_combo, 100, "本命最小保証（実力均衡なので逆張りオフ）")
        budget_left = budget - 100

        # 2. 1コース1着×3着候補プール広範囲（実力均衡なので1コースは保持しつつ2-3着を広げる）
        sweep = []
        for second in third_pool:
            if second == 1:
                continue
            for third in third_pool:
                if third in (1, second):
                    continue
                c = f"1-{second}-{third}"
                if c in odds_dict and 5 <= odds_dict[c] <= 200:
                    sweep.append((c, odds_dict[c]))

        # 3. 穴艇1着の妙味枠（高オッズ）
        for h in dark_horses:
            for third in third_pool:
                if third in (h, 1) or third == h:
                    continue
                c = f"{h}-1-{third}"
                if c in odds_dict and odds_dict[c] >= 20:
                    sweep.append((c, odds_dict[c]))

        sweep.sort(key=lambda x: x[1])  # 低オッズ優先で採用
        unique_combos = list(dict.fromkeys(c for c, _ in sweep))[:6]
        _allocate(unique_combos, budget_left,
                  lambda c: (
                      f"展示重視・穴艇1着妙味（{c[0]}-{c[2]}-{c[-1]}）"
                      if c[0] != "1"
                      else f"展示重視全方位（1コース1着、{c[2]}-{c[-1]}3着候補プール）"
                  ))

    # 同じ買い目が複数戦略から重複した場合は1点に合算（賭金合計＋根拠を「/」で結合）
    merged: dict[str, dict] = {}
    for b in bets:
        c = b["combination"]
        if c in merged:
            merged[c]["amount"] += b["amount"]
            if b["rationale"] not in merged[c]["rationale"]:
                merged[c]["rationale"] += " / " + b["rationale"]
        else:
            merged[c] = dict(b)
    return list(merged.values())


@mcp.tool()
def get_aggressive_prediction(
    racers_data: str,
    exhibition_data: str,
    odds_data: str,
    budget: int,
) -> str:
    """
    穴予想エンジン本体。市場本命オッズ＋展示スコア＋当地3連率から穴買い目を組み立てる。

    racers_data: 出走表JSON。
      例: '[{"boat_no":1,"name":"...","local_top_3":55.2}, ...]'
    exhibition_data: 直前情報JSON。calc_exhibition_score の入力と同形式。
      例: '[{"boat_no":1,"name":"...","exhibit_time":"6.78","weight":52,"tilt":0.0,"exhibit_st":"0.16"}, ...]'
    odds_data: 3連単オッズJSON。calc_market_favorite_oddsstats の入力と同形式。
      例: '[{"combination":"1-2-3","odds":3.4}, ...]'
    budget: 予算（円、整数）

    内部処理:
      1) calc_exhibition_score を呼んで6艇のスコア取得
      2) calc_market_favorite_oddsstats を呼んで市場判定（strong/medium/off）
      3) 当地3連率と1着頭オッズの乖離で逆張り強度を補正
      4) 展示穴艇判定（スコア60+ かつ 当地3連率上位3外）
      5) 戦略別に買い目構築

    戻り値: JSON文字列（recommended_bets / candidates / market_assessment 等を含む）
    """
    # ── 入力パース ──
    try:
        racers   = json.loads(racers_data)
        odds_list = json.loads(odds_data)
    except json.JSONDecodeError as e:
        return json.dumps({"error": f"入力JSONの形式が不正: {e}"}, ensure_ascii=False)

    if budget < 1000:
        return json.dumps({"error": "予算は1000円以上を指定してください"}, ensure_ascii=False)

    # ── 1. 展示スコア計算 ──
    scores_result = json.loads(calc_exhibition_score(exhibition_data))
    if "error" in scores_result:
        return json.dumps({"error": f"展示スコア計算エラー: {scores_result['error']}"}, ensure_ascii=False)
    scores = scores_result["scores"]

    # ── 2. 市場本命統計 ──
    market_result = json.loads(calc_market_favorite_oddsstats(odds_data))
    if "error" in market_result:
        return json.dumps({"error": f"オッズ統計エラー: {market_result['error']}"}, ensure_ascii=False)

    fav_combo = market_result["favorite"]["combination"]
    fav_odds  = market_result["favorite"]["odds"]
    fav_first = int(fav_combo.split("-")[0])
    intensity = market_result["reverse_intensity"]

    # ── 3. 当地3連率×1着頭オッズの乖離で補正 ──
    fav_first_local3 = next(
        (_parse_pct(r.get("local_top_3", 0)) for r in racers if r.get("boat_no") == fav_first),
        0.0,
    )
    adjustment_notes = []
    if fav_odds <= 5.0:
        if fav_first_local3 >= 80:
            intensity = "off"
            adjustment_notes.append(
                f"{fav_first}号艇の当地3連率{fav_first_local3:.0f}%超 + 1着頭オッズ{fav_odds:.1f}倍 → 市場判断妥当、逆張りオフに補正"
            )
        elif fav_first_local3 <= 60:
            if intensity == "off":
                intensity = "medium"
            else:
                intensity = "strong"
            adjustment_notes.append(
                f"{fav_first}号艇の当地3連率{fav_first_local3:.0f}%以下 + 1着頭オッズ{fav_odds:.1f}倍 → 市場過大評価、逆張り発動"
            )

    # ── 4. 展示穴艇判定 ──
    high_score_boats = [s["boat_no"] for s in scores if s["score"] >= 60]
    sorted_local3 = sorted(racers, key=lambda r: _parse_pct(r.get("local_top_3", 0)), reverse=True)
    top_local3_boats = [r["boat_no"] for r in sorted_local3[:3]]
    dark_horses = [b for b in high_score_boats if b not in top_local3_boats]
    third_pool  = sorted(set(high_score_boats + top_local3_boats))

    # ── 5. 買い目構築 ──
    odds_dict = {}
    for e in odds_list:
        try:
            odds_dict[e["combination"]] = float(e["odds"])
        except (KeyError, TypeError, ValueError):
            continue

    bets = _build_aggressive_bets(
        intensity, fav_combo, fav_odds, dark_horses, third_pool, scores, odds_dict, budget
    )

    # ── 6. 集計 ──
    total_amount = sum(b["amount"] for b in bets)
    profits = [int(b["amount"] * b["odds"]) - total_amount for b in bets]
    min_profit = min(profits) if profits else 0
    max_profit = max(profits) if profits else 0

    # ── 7. candidates 整形（各艇の役割） ──
    candidates = []
    score_by_boat = {s["boat_no"]: s["score"] for s in scores}
    for boat in (1, 2, 3, 4, 5, 6):
        score = score_by_boat.get(boat, 0)
        local3 = next((_parse_pct(r.get("local_top_3", 0)) for r in racers if r.get("boat_no") == boat), 0)
        if boat in dark_horses:
            role = "逆襲候補（展示重視穴）"
            rationale = f"展示スコア{score}点。当地3連率{local3:.0f}%（上位3外）。3着付けで穴妙味"
        elif boat == fav_first and intensity == "strong":
            role = "本命切り候補"
            rationale = f"市場本命だが逆張り強発動。1着固定の信頼度低、最小保証のみ"
        elif boat in (2, 3) and intensity in ("strong", "medium") and boat != fav_first:
            role = "差し1着候補"
            rationale = f"差し1着シナリオの主軸。{boat}-1-X系で攻める"
        elif boat in top_local3_boats:
            role = "当地3連率上位"
            rationale = f"当地3連率{local3:.0f}%。3着候補プール入り"
        else:
            role = "切り候補"
            rationale = f"展示スコア{score}点、当地3連率{local3:.0f}%"
        candidates.append({
            "boat_no": boat,
            "exhibition_score": score,
            "local_top_3": round(local3, 1),
            "role": role,
            "rationale": rationale,
        })

    intensity_jp = {"strong": "強", "medium": "中", "off": "発動せず"}[intensity]
    strategy_summary = (
        f"逆張り強度: {intensity_jp}（{intensity}）。{market_result['interpretation']}"
    )
    if adjustment_notes:
        strategy_summary += " 補正: " + " / ".join(adjustment_notes)

    return json.dumps({
        "engine": "aggressive",
        "market_assessment": {
            "favorite": market_result["favorite"],
            "verdict": market_result["verdict"],
            "reverse_intensity_initial": market_result["reverse_intensity"],
            "reverse_intensity_final": intensity,
            "first_course_overconfidence": market_result["first_course_overconfidence"],
            "first_course_top3_avg": market_result["first_course_top3_avg"],
        },
        "adjustment_notes": adjustment_notes,
        "exhibition_dark_horses": dark_horses,
        "third_targets_pool": third_pool,
        "candidates": candidates,
        "recommended_bets": bets,
        "total_amount": total_amount,
        "min_potential_profit": min_profit,
        "max_potential_profit": max_profit,
        "strategy_summary": strategy_summary,
    }, ensure_ascii=False, indent=2)


# ── 穴予想エンジン: 統合（堅実派×穴派） ────────────────

@mcp.tool()
def get_synthesis_prediction(
    safe_bets: str,
    aggressive_bets: str,
    budget: int,
) -> str:
    """
    堅実派買い目（Claudeが組み立てたもの）と穴派買い目（get_aggressive_prediction の出力）を
    統合し、3つの配分案＋対立ポイントを返す。

    safe_bets: 堅実派買い目のJSON文字列。形式は次のどちらか:
      A) リスト: '[{"combination":"1-2-3","odds":3.4,"amount":1500}, ...]'
      B) ラッパー: '{"recommended_bets":[...], "candidates":[...]}'
    aggressive_bets: get_aggressive_prediction の戻り値JSON（recommended_bets / candidates 含む）。
    budget: 予算（円、整数）

    統合ロジック:
      - 両者一致買い目 → 両者 amount の平均（厚め配分）
      - 堅実派のみ     → 堅実派 amount を半額
      - 穴派のみ       → 穴派 amount を半額
      - 合計が予算超過 → 比率で按分削減
      - 合計が予算未満 → 両者一致点に余りを追加配分

    戻り値: JSON文字列
      {"safe_allocation","aggressive_allocation","synthesis_allocation",
       "conflict_points","summary","budget"}
    """
    try:
        safe = json.loads(safe_bets)
        agg_full = json.loads(aggressive_bets)
    except json.JSONDecodeError as e:
        return json.dumps({"error": f"入力JSONエラー: {e}"}, ensure_ascii=False)

    # safe_bets はリストでもラッパー形式でも受け付ける
    if isinstance(safe, dict) and "recommended_bets" in safe:
        safe_list = safe["recommended_bets"]
        safe_candidates = safe.get("candidates", [])
    elif isinstance(safe, list):
        safe_list = safe
        safe_candidates = []
    else:
        return json.dumps({"error": "safe_bets はリスト形式 または {recommended_bets:[...]} を含むdictで指定"}, ensure_ascii=False)

    if not isinstance(agg_full, dict) or "recommended_bets" not in agg_full:
        return json.dumps({"error": "aggressive_bets は get_aggressive_prediction の戻り値（recommended_bets を含むdict）を指定"}, ensure_ascii=False)

    agg_list = agg_full["recommended_bets"]
    agg_candidates = agg_full.get("candidates", [])

    if budget < 1000:
        return json.dumps({"error": "予算は1000円以上を指定してください"}, ensure_ascii=False)

    # ── 1. 両者の dict 化 ──
    safe_dict = {b["combination"]: b for b in safe_list if "combination" in b}
    agg_dict  = {b["combination"]: b for b in agg_list if "combination" in b}

    common    = sorted(set(safe_dict) & set(agg_dict))
    safe_only = sorted(set(safe_dict) - set(agg_dict))
    agg_only  = sorted(set(agg_dict) - set(safe_dict))

    # ── 2. 統合配分（仮配分） ──
    synthesis = []
    for c in common:
        avg_raw = (int(safe_dict[c].get("amount", 0)) + int(agg_dict[c].get("amount", 0))) / 2
        amount = max(100, int(avg_raw // 100) * 100)
        synthesis.append({
            "combination": c,
            "odds": float(safe_dict[c].get("odds", agg_dict[c].get("odds", 0))),
            "amount": amount,
            "adopted_from": "両者一致",
        })
    for c in safe_only:
        amount = max(100, int(safe_dict[c].get("amount", 0)) // 2 // 100 * 100)
        synthesis.append({
            "combination": c,
            "odds": float(safe_dict[c].get("odds", 0)),
            "amount": amount,
            "adopted_from": "堅実派採用",
        })
    for c in agg_only:
        amount = max(100, int(agg_dict[c].get("amount", 0)) // 2 // 100 * 100)
        synthesis.append({
            "combination": c,
            "odds": float(agg_dict[c].get("odds", 0)),
            "amount": amount,
            "adopted_from": "穴派採用",
        })

    # ── 3. 予算調整 ──
    total = sum(b["amount"] for b in synthesis)
    if total > budget and total > 0:
        # 比率で按分削減（100円未満は100円に切り上げ）
        ratio = budget / total
        for b in synthesis:
            b["amount"] = max(100, int(b["amount"] * ratio) // 100 * 100)
    elif total < budget:
        # 余りを両者一致点に均等追加（一致点がなければ堅実派のオッズ最低点）
        target_bets = [b for b in synthesis if b["adopted_from"] == "両者一致"]
        if not target_bets:
            sf = [b for b in synthesis if b["adopted_from"] == "堅実派採用"]
            sf.sort(key=lambda x: x["odds"])
            target_bets = sf[:1]
        if target_bets:
            extra = budget - total
            per = extra // len(target_bets) // 100 * 100
            for b in target_bets:
                b["amount"] += per

    # ── 4. 対立ポイント抽出 ──
    def _is_in_bets(boat: int, bets: list) -> bool:
        bn = str(boat)
        return any(bn in b.get("combination", "").split("-") for b in bets)

    conflict_points = []
    cand_by_boat = {c["boat_no"]: c for c in agg_candidates}
    safe_cand_by_boat = {c.get("boat_no"): c for c in safe_candidates}

    for boat in (1, 2, 3, 4, 5, 6):
        in_safe = _is_in_bets(boat, safe_list)
        in_agg  = _is_in_bets(boat, agg_list)
        if in_safe == in_agg:
            continue  # 評価が一致なら対立なし

        agg_role = cand_by_boat.get(boat, {}).get("role", "（不明）")
        agg_reason = cand_by_boat.get(boat, {}).get("rationale", "")
        safe_label = safe_cand_by_boat.get(boat, {}).get("label", "採用" if in_safe else "切り")

        if in_safe and not in_agg:
            issue = f"堅実派は買い目に含めるが、穴派は『{agg_role}』と評価。{agg_reason}"
            safe_view = safe_label
            agg_view = agg_role
        else:
            issue = f"堅実派は買い目に含めない一方、穴派は『{agg_role}』として採用。{agg_reason}"
            safe_view = "切り"
            agg_view = agg_role

        conflict_points.append({
            "boat_no": boat,
            "safe_view": safe_view,
            "aggressive_view": agg_view,
            "issue": issue,
        })

    # ── 5. サマリ ──
    final_total = sum(b["amount"] for b in synthesis)
    profits = [int(b["amount"] * b["odds"]) - final_total for b in synthesis]
    summary = (
        f"統合: 両者一致{len(common)}点 / 堅実派のみ{len(safe_only)}点 / 穴派のみ{len(agg_only)}点。"
        f"対立ポイント{len(conflict_points)}件。合計投資{final_total:,}円。"
    )

    return json.dumps({
        "safe_allocation": safe_list,
        "aggressive_allocation": agg_list,
        "synthesis_allocation": synthesis,
        "synthesis_total": final_total,
        "synthesis_min_profit": min(profits) if profits else 0,
        "synthesis_max_profit": max(profits) if profits else 0,
        "conflict_points": conflict_points,
        "summary": summary,
        "budget": budget,
    }, ensure_ascii=False, indent=2)


# ── Tool 8〜12: 収支記録 ──────────────────────────────

@mcp.tool()
def record_bets(
    venue: int,
    race_no: int,
    date: str,
    bets: str,
    memo: str = "",
    strategy: str = "synthesis",
) -> str:
    """
    買い目を一括記録する。
    venue: 会場ID（1〜24）
    race_no: レース番号（1〜12）
    date: 日付（"today" または "YYYYMMDD"）
    bets: JSON文字列 例: '[{"combination":"1-2-3","amount":600,"odds":5.1}, ...]'
    memo: メモ（任意）
    strategy: 採用エンジン。"safe"（堅実派単独）/ "aggressive"（穴派単独）/ "synthesis"（統合）
              のいずれか。デフォルト "synthesis"。
    """
    target_date = _resolve_date(date)
    race_id = f"{target_date}_{venue:02d}_{race_no:02d}"

    if strategy not in ALLOWED_STRATEGIES:
        return (
            f'【エラー】strategy は {"/".join(ALLOWED_STRATEGIES)} のいずれかを指定してください'
            f'（指定値: "{strategy}"）'
        )

    try:
        bets_list = json.loads(bets)
    except json.JSONDecodeError as e:
        return f"【エラー】betsのJSON形式が正しくありません。\n詳細: {e}"

    if not bets_list:
        return "【エラー】買い目が1件もありません。"

    try:
        ws = _get_sheet()
        header_added = _ensure_strategy_column(ws)
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
            strategy,  # 11列目（0-indexed）
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
        f"  採用エンジン: {strategy}",
        f"  記録点数: {len(rows)}点",
        "",
        f"  {'組み合わせ':<10}  {'賭金':>6}  {'オッズ':>6}",
        "  " + "-" * 30,
    ]
    for b in bets_list:
        lines.append(f"  {b.get('combination',''):<10}  {b.get('amount',0):>5,}円  {b.get('odds',0):>5.1f}倍")
    lines.append("")
    lines.append(f"  合計賭け金: {total_amount:,}円")
    if header_added:
        lines.append("")
        lines.append("  ※ シートに strategy 列を自動追加しました（既存行は空）。")
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

    # race_id単位で集計（strategy情報も保持）
    races: dict[str, dict] = {}
    # strategy別の bet 単位集計（同一レースで複数エンジン記録があり得るため bet 単位）
    strategy_stats: dict[str, dict] = {
        s: {"invested": 0, "payout": 0, "hit_bets": 0, "total_bets": 0}
        for s in ALLOWED_STRATEGIES + ("unknown",)
    }
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

        # strategy 列（11番目、0-indexed）を取得。古い行は空 → "unknown"
        strat = row[STRATEGY_COL_INDEX] if len(row) > STRATEGY_COL_INDEX and row[STRATEGY_COL_INDEX] else "unknown"
        if strat not in strategy_stats:
            strat = "unknown"
        strategy_stats[strat]["invested"] += amount
        strategy_stats[strat]["payout"]   += payout
        strategy_stats[strat]["total_bets"] += 1
        if payout > 0:
            strategy_stats[strat]["hit_bets"] += 1

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

    # ── エンジン別集計（safe / aggressive / synthesis / unknown） ──
    has_strategy_data = any(s["total_bets"] > 0 for k, s in strategy_stats.items() if k != "unknown")
    if has_strategy_data or strategy_stats["unknown"]["total_bets"] > 0:
        lines.append("")
        lines.append("【エンジン別】（買い目単位の的中率・回収率）")
        for strat_name in ALLOWED_STRATEGIES + ("unknown",):
            s = strategy_stats[strat_name]
            if s["total_bets"] == 0:
                continue
            s_net = s["payout"] - s["invested"]
            s_roi = s["payout"] / s["invested"] * 100 if s["invested"] > 0 else 0
            s_hit = s["hit_bets"] / s["total_bets"] * 100 if s["total_bets"] > 0 else 0
            label = {
                "safe": "堅実派", "aggressive": "穴派",
                "synthesis": "統合派", "unknown": "未分類（旧データ）",
            }[strat_name]
            lines.append(
                f"  {label:<14}: {s['total_bets']:>3}点  的中{s['hit_bets']}点({s_hit:.0f}%)"
                f"  収支{s_net:+,}円  回収率{s_roi:.0f}%"
            )

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
        "=" * 60,
        f"  直近{min(limit, len(recent))}件の投票記録",
        "=" * 60,
        f"  {'日付':<10}  {'会場':<6}  {'R':<3}  {'組合せ':<8}  {'賭金':>6}  {'オッズ':>6}  {'結果':<4}  {'収支':>8}  {'戦略':<10}",
        "  " + "-" * 58,
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
        strat    = row[STRATEGY_COL_INDEX] if len(row) > STRATEGY_COL_INDEX and row[STRATEGY_COL_INDEX] else "-"

        try:
            profit = int(profit_s) if profit_s not in ("", "-") else None
        except ValueError:
            profit = None

        profit_disp = f"{profit:+,}円" if profit is not None else "-"
        lines.append(
            f"  {d:<10}  {venue:<6}  {rno:<3}  {combo:<8}  {amount:>5}円  {odds_str:>5}倍  {result:<4}  {profit_disp:>8}  {strat:<10}"
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

# シート列構成（strategyは末尾追加で既存の0〜10列のインデックスを維持）
STRATEGY_COL_INDEX = 11  # 0-indexed = L列
STRATEGY_HEADER = "strategy"
ALLOWED_STRATEGIES = ("safe", "aggressive", "synthesis")


def _ensure_strategy_column(ws) -> bool:
    """1行目ヘッダーに strategy 列がなければ末尾に追加する。追加した場合 True。"""
    headers = ws.row_values(1)
    if STRATEGY_HEADER in headers:
        return False
    col_idx = len(headers) + 1  # 1-indexed
    ws.update_cell(1, col_idx, STRATEGY_HEADER)
    return True


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

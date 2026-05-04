#!/usr/bin/env python3
"""残り22会場のテンプレートYAMLを生成するスクリプト（初回のみ実行）"""

import os

venues = {
    1: ("桐生", "01_kiryu"),
    2: ("戸田", "02_toda"),
    3: ("江戸川", "03_edogawa"),
    5: ("多摩川", "05_tamagawa"),
    6: ("浜名湖", "06_hamanako"),
    7: ("蒲郡", "07_gamagori"),
    8: ("常滑", "08_tokoname"),
    9: ("津", "09_tsu"),
    10: ("三国", "10_mikuni"),
    11: ("びわこ", "11_biwako"),
    13: ("尼崎", "13_amagasaki"),
    14: ("鳴門", "14_naruto"),
    15: ("丸亀", "15_marugame"),
    16: ("児島", "16_kojima"),
    17: ("宮島", "17_miyajima"),
    18: ("徳山", "18_tokuyama"),
    19: ("下関", "19_shimonoseki"),
    20: ("若松", "20_wakamatsu"),
    21: ("芦屋", "21_ashiya"),
    22: ("福岡", "22_fukuoka"),
    23: ("唐津", "23_karatsu"),
    24: ("大村", "24_omura"),
}

template = """# {name}競艇場ルール
venue_id: {vid}
venue_name: {name}
water_type: "要確認"    # freshwater / seawater / brackish / river
course_length: 0         # 要確認（メートル）
stable_board_used: false # 要確認
night_race: false        # 要確認
region: "要確認"

characteristics:
  - "（未記入）"

cautions:
  - "（未記入）"

recommended_strategy:
  - "（未記入）"
"""

out_dir = os.path.join(os.path.dirname(__file__), "rules", "venues")
for vid, (name, slug) in venues.items():
    path = os.path.join(out_dir, f"{slug}.yaml")
    if not os.path.exists(path):
        with open(path, "w", encoding="utf-8") as f:
            f.write(template.format(vid=vid, name=name))
        print(f"作成: {path}")
    else:
        print(f"スキップ（既存）: {path}")

print("完了")

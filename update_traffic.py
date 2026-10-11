import csv
import datetime
import json
import os
import unicodedata
import urllib.parse
import urllib.request

BBOX = "130.386,31.293,130.730,31.753"

# 日本時間 (UTC+9) の現在時刻を取得
now_utc = datetime.datetime.now(datetime.timezone.utc)
now_jst = now_utc + datetime.timedelta(hours=9)

# 5分値用（様式1・3: 観測後約20分後に提供されるため25分前、5分単位）
t5 = now_jst - datetime.timedelta(minutes=25)
code_5m = f"{t5.strftime('%Y%m%d%H')}{(t5.minute // 5) * 5:02d}"

# 1時間値用（様式2・4: 観測後毎時約20分後に提供されるため、前時00分）
t1h = now_jst - datetime.timedelta(hours=1, minutes=20)
code_1h = f"{t1h.strftime('%Y%m%d%H')}00"


def norm(key):
  """項目名の表記ゆれ（全角/半角の括弧など）を吸収する"""
  return unicodedata.normalize("NFKC", key).replace(" ", "").replace("\u3000", "")


def getp(p, name):
  """正規化した項目名で値を取得する。無ければ None"""
  target = norm(name)
  for k, v in p.items():
    if norm(k) == target:
      return v
  return None


# 交通量そのもの・位置情報などの項目（これ以外は「フラグ類」として丸ごと保存する）
NON_FLAG_KEYS = {
    norm(k)
    for k in [
        "地方整備局等番号", "開発建設部／都道府県コード", "常時観測点コード",
        "収集時間フラグ（5分間／1時間）", "観測年月日", "時間帯",
        "道路種別", "時間コード",
        "上り・小型交通量", "上り・大型交通量", "上り・車種判別不能交通量",
        "下り・小型交通量", "下り・大型交通量", "下り・車種判別不能交通量",
        "上り・自動車交通量", "下り・自動車交通量",
        "上り・小型大型判別不能交通量", "下り・小型大型判別不能交通量",
        "上り・自動車交通量（集計値）", "下り・自動車交通量（集計値）",
        "上り・小型交通量（集計値）", "上り・大型交通量（集計値）",
        "下り・小型交通量（集計値）", "下り・大型交通量（集計値）",
        "上り・小型大型判別不能交通量（集計値）", "下り・小型大型判別不能交通量（集計値）",
    ]
}


def flags_of(p):
  """停電・異常・欠測・カメラ状態などのフラグ類を、項目名のまま全て保存する"""
  return {k: v for k, v in p.items() if norm(k) not in NON_FLAG_KEYS}


def fetch(layer, time_code):
  cql = (
      f"道路種別=3 AND 時間コード={time_code} AND"
      f" BBOX(ジオメトリ,{BBOX},'EPSG:4326')"
  )
  params = {
      "service": "WFS",
      "version": "2.0.0",
      "request": "GetFeature",
      "typeNames": layer,
      "srsName": "EPSG:4326",
      "outputFormat": "application/json",
      "exceptions": "application/json",
      "cql_filter": cql,
  }
  url = (
      "https://api.jartic-open-traffic.org/geoserver?"
      + urllib.parse.urlencode(params)
  )
  req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
  try:
    with urllib.request.urlopen(req, timeout=15) as r:
      return json.loads(r.read().decode("utf-8")).get("features", [])
  except Exception as e:
    print(f"[{layer}] エラー: {e}")
    return []


# ---- 履歴の保存（月ごとのCSV: history/YYYYMM.csv）----
HISTORY_DIR = "history"
HISTORY_COLUMNS = [
    "generated_at", "time_5m", "time_1h", "code", "type",
    "f1_up", "f1_down", "f2_up", "f2_down",
    "f3_up", "f3_down", "f4_up", "f4_down", "abnormal",
]


def dir_total(f, d):
  """方向別の合計台数。様式が無い/値が空なら空文字（0台と区別するため）"""
  if not f:
    return ""
  tot = f.get(f"{d}_tot")
  if tot is not None:
    return tot
  s, l = f.get(f"{d}_s"), f.get(f"{d}_l")
  if s is None and l is None:
    return ""
  return (s or 0) + (l or 0)


def abnormal_of(st):
  """値が「1」(異常)のフラグ名を '|' でつなぐ（様式1・2・3のフラグが対象）"""
  names = []
  for key in ("f1", "f2", "f3"):
    for k, v in ((st.get(key) or {}).get("flags") or {}).items():
      if str(v) == "1" and k not in names:
        names.append(k)
  return "|".join(names)


def save_history(station_list):
  """1回の取得結果を、月ごとのCSVに追記する（同じ時刻・同じ地点は二重に書かない）"""
  os.makedirs(HISTORY_DIR, exist_ok=True)
  path = os.path.join(HISTORY_DIR, f"{code_5m[:6]}.csv")
  new_file = not os.path.exists(path)
  seen = set()
  if not new_file:
    with open(path, encoding="utf-8-sig", newline="") as fh:
      for r in csv.DictReader(fh):
        seen.add((r["time_5m"], r["code"]))
  rows = []
  for st in station_list:
    if (st["time_5m"], st["code"]) in seen:
      continue
    rows.append([
        st["generated_at"], st["time_5m"], st["time_1h"], st["code"], st["type"],
        dir_total(st["f1"], "up"), dir_total(st["f1"], "down"),
        dir_total(st["f2"], "up"), dir_total(st["f2"], "down"),
        dir_total(st["f3"], "up"), dir_total(st["f3"], "down"),
        dir_total(st["f4"], "up"), dir_total(st["f4"], "down"),
        abnormal_of(st),
    ])
  if not rows:
    print("履歴: 追記なし（同じ時刻のデータは保存済み）")
    return
  # 新規作成時だけBOM付きにして、Excelでも文字化けしないようにする
  with open(path, "a", encoding="utf-8-sig" if new_file else "utf-8", newline="") as fh:
    w = csv.writer(fh)
    if new_file:
      w.writerow(HISTORY_COLUMNS)
    w.writerows(rows)
  print(f"履歴: {len(rows)} 行を {path} に追記しました。")


def main():
  print(f"JARTIC API リクエスト開始: 5分={code_5m} / 1時間={code_1h}")
  f1 = fetch("t_travospublic_measure_5m", code_5m)
  f2 = fetch("t_travospublic_measure_1h", code_1h)
  f3 = fetch("t_travospublic_measure_5m_img", code_5m)
  f4 = fetch("t_travospublic_measure_1h_img", code_1h)
  print(f"取得件数: 様式1={len(f1)} 様式2={len(f2)} 様式3={len(f3)} 様式4={len(f4)}")

  generated_at = now_jst.strftime("%Y-%m-%d %H:%M:%S JST")
  stations = {}

  def get_st(f, t):
    c = str(f.get("properties", {}).get("常時観測点コード"))
    if c not in stations:
      coords = f.get("geometry", {}).get("coordinates", [])
      if f.get("geometry", {}).get("type") == "MultiPoint" and coords:
        coords = coords[0]
      stations[c] = {
          "code": c,
          "type": t,
          "lng": coords[0],
          "lat": coords[1],
          "time_5m": code_5m,
          "time_1h": code_1h,
          "generated_at": generated_at,
          "f1": None,
          "f2": None,
          "f3": None,
          "f4": None,
      }
    return stations[c]

  # 様式1・2（常設トラカン）: 台数 + 停電/ループ異常/超音波異常/欠測フラグ
  for key, feats in (("f1", f1), ("f2", f2)):
    for f in feats:
      p = f["properties"]
      get_st(f, "常設トラカン")[key] = {
          "up_s": p.get("上り・小型交通量"),
          "up_l": p.get("上り・大型交通量"),
          "down_s": p.get("下り・小型交通量"),
          "down_l": p.get("下り・大型交通量"),
          "flags": flags_of(p),
      }

  # 様式3・4（CCTVトラカン）: 台数 + カメラ状態等のフラグ
  # 項目名は括弧の全角/半角の違いを吸収して取得する
  for key, feats, suffix in (("f3", f3, "（集計値）"), ("f4", f4, "")):
    for f in feats:
      p = f["properties"]
      get_st(f, "CCTVトラカン")[key] = {
          "up_tot": getp(p, f"上り・自動車交通量{suffix}"),
          "down_tot": getp(p, f"下り・自動車交通量{suffix}"),
          "up_s": getp(p, f"上り・小型交通量{suffix}"),
          "up_l": getp(p, f"上り・大型交通量{suffix}"),
          "down_s": getp(p, f"下り・小型交通量{suffix}"),
          "down_l": getp(p, f"下り・大型交通量{suffix}"),
          "flags": flags_of(p),
      }

  with open("traffic_data.json", "w", encoding="utf-8") as out:
    json.dump(list(stations.values()), out, ensure_ascii=False, indent=2)

  print(f"更新完了: {len(stations)} 地点を保存しました。")

  # 履歴の保存に失敗しても、上の traffic_data.json の更新には影響させない
  try:
    save_history(list(stations.values()))
  except Exception as e:
    print(f"履歴の保存に失敗しました: {e}")


if __name__ == "__main__":
  main()

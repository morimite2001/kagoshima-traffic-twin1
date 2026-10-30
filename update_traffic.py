import csv
import datetime
import glob
import json
import os
import time
import unicodedata
import urllib.parse
import urllib.request

# 南端を 31.293 → 31.285 に広げ、南端ぎりぎりにある地点(9811950)を含める
BBOX = "130.386,31.285,130.730,31.753"

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


def fetch(layer, time_code, time_to=None, raise_on_error=False):
  """time_to を指定すると、time_code〜time_to の範囲（5分刻み）をまとめて取得する"""
  if time_to:
    cond = f"時間コード>={time_code} AND 時間コード<={time_to}"
  else:
    cond = f"時間コード={time_code}"
  cql = (
      f"道路種別=3 AND {cond} AND"
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
    with urllib.request.urlopen(req, timeout=30) as r:
      data = json.loads(r.read().decode("utf-8"))
    if raise_on_error and "features" not in data:
      # 取得量が大きすぎる場合などは、features の無いエラー応答になる
      raise RuntimeError(f"想定外の応答: {str(data)[:200]}")
    return data.get("features", [])
  except Exception as e:
    print(f"[{layer}] エラー: {e}")
    if raise_on_error:
      raise
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
  s, l, u = f.get(f"{d}_s"), f.get(f"{d}_l"), f.get(f"{d}_u")
  if s is None and l is None and u is None:
    return ""
  return (s or 0) + (l or 0) + (u or 0)


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


# ---- 過去1時間の5分間交通量（グラフ用: traffic_series.json）----
SERIES_WINDOW_MIN = 60


def series_point(p, kind, suffix="（集計値）"):
  """1件のレコードから、時刻と上り・下りの合計台数(5分間)を取り出す。空なら None"""
  if kind == "fixed":
    d = {
        "up_s": p.get("上り・小型交通量"), "up_l": p.get("上り・大型交通量"),
        "up_u": p.get("上り・車種判別不能交通量"),
        "down_s": p.get("下り・小型交通量"), "down_l": p.get("下り・大型交通量"),
        "down_u": p.get("下り・車種判別不能交通量"),
    }
  else:
    d = {
        "up_tot": getp(p, f"上り・自動車交通量{suffix}"),
        "down_tot": getp(p, f"下り・自動車交通量{suffix}"),
        "up_s": getp(p, f"上り・小型交通量{suffix}"),
        "up_l": getp(p, f"上り・大型交通量{suffix}"),
        "up_u": getp(p, f"上り・小型大型判別不能交通量{suffix}"),
        "down_s": getp(p, f"下り・小型交通量{suffix}"),
        "down_l": getp(p, f"下り・大型交通量{suffix}"),
        "down_u": getp(p, f"下り・小型大型判別不能交通量{suffix}"),
    }
  up, down = dir_total(d, "up"), dir_total(d, "down")
  return {
      "t": str(p.get("時間コード")),
      "up": None if up == "" else up,
      "down": None if down == "" else down,
  }


def build_series(generated_at):
  """直近1時間分(13点)の5分値を、様式1(常設)と様式3(CCTV)から範囲指定でまとめて取得する"""
  t5f = t5.replace(minute=(t5.minute // 5) * 5, second=0, microsecond=0)
  start = t5f - datetime.timedelta(minutes=SERIES_WINDOW_MIN)
  code_start = start.strftime("%Y%m%d%H%M")
  sources = (
      ("fixed", "常設トラカン", fetch("t_travospublic_measure_5m", code_start, code_5m)),
      ("cctv", "CCTVトラカン", fetch("t_travospublic_measure_5m_img", code_start, code_5m)),
  )
  stations = {}
  for kind, type_name, feats in sources:
    for f in feats:
      p = f.get("properties", {})
      if p.get("時間コード") is None or p.get("常時観測点コード") is None:
        continue
      st = stations.setdefault(
          str(p["常時観測点コード"]), {"type": type_name, "points": {}})
      pt = series_point(p, kind)
      st["points"][pt["t"]] = pt
  out = {}
  for code, st in stations.items():
    out[code] = {
        "type": st["type"],
        "points": [st["points"][k] for k in sorted(st["points"])],
    }
  return {
      "generated_at": generated_at,
      "latest": code_5m,
      "interval_min": 5,
      "window_min": SERIES_WINDOW_MIN,
      "stations": out,
  }


# ---- 1時間値の履歴（欠けた時刻を自動で埋める: history/hourly_YYYYMM.csv）----
# 実行が飛ばされて記録が欠けても、APIが保持している1時間値（約3か月分）から取り直して埋める。
HOURLY_COLUMNS = ["time_1h", "code", "type", "up", "down", "abnormal", "fetched_at"]
HOURLY_RECENT_HOURS = 3      # 毎回取り直す直近の時間数（速報値が後で更新されることがあるため）
HOURLY_BACKFILL_DAYS = 30    # 何日前まで遡って欠けを埋めるか
HOURLY_CHUNK_HOURS = 24      # 1回のリクエストで取得する最大の時間数
HOURLY_MAX_CHUNKS = 6        # 1回の実行で処理する欠けの塊の最大数（初回の穴埋めは数回に分けて進む）
HOURLY_SETTLE_HOURS = 3      # この時間を過ぎても返ってこない時刻は「データなし」とみなして再取得しない
HOURLY_TRIED_FILE = os.path.join(HISTORY_DIR, "hourly_tried.txt")


def hour_code(dt):
  return dt.strftime("%Y%m%d%H") + "00"


def parse_hour(code):
  return datetime.datetime.strptime(code, "%Y%m%d%H%M")


def load_hourly():
  """history/hourly_*.csv をすべて読み込む。{(time_1h, code): 行} を返す"""
  data = {}
  for path in sorted(glob.glob(os.path.join(HISTORY_DIR, "hourly_20*.csv"))):
    with open(path, encoding="utf-8-sig", newline="") as fh:
      for r in csv.DictReader(fh):
        data[(r["time_1h"], r["code"])] = r
  return data


def save_hourly_months(data, months):
  """変更のあった月のファイルだけを、時刻・地点コード順に書き直す"""
  for month in sorted(months):
    keys = sorted(k for k in data if k[0][:6] == month)
    path = os.path.join(HISTORY_DIR, f"hourly_{month}.csv")
    with open(path, "w", encoding="utf-8-sig", newline="") as fh:
      w = csv.writer(fh)
      w.writerow(HOURLY_COLUMNS)
      for k in keys:
        r = data[k]
        w.writerow([r[c] for c in HOURLY_COLUMNS])


def fetch_hourly(code_from, code_to, fetched_at):
  """常設(様式2)とCCTV(様式4)の1時間値を、範囲指定でまとめて取得する。失敗時は例外"""
  out = []
  for kind, type_name, layer in (
      ("fixed", "常設トラカン", "t_travospublic_measure_1h"),
      ("cctv", "CCTVトラカン", "t_travospublic_measure_1h_img"),
  ):
    for f in fetch(layer, code_from, code_to, raise_on_error=True):
      p = f.get("properties", {})
      if p.get("時間コード") is None or p.get("常時観測点コード") is None:
        continue
      pt = series_point(p, kind, suffix="")
      abnormal = "|".join(k for k, v in flags_of(p).items() if str(v) == "1")
      out.append({
          "time_1h": pt["t"],
          "code": str(p["常時観測点コード"]),
          "type": type_name,
          "up": "" if pt["up"] is None else pt["up"],
          "down": "" if pt["down"] is None else pt["down"],
          "abnormal": abnormal,
          "fetched_at": fetched_at,
      })
    time.sleep(1)  # APIへの負荷を抑える
  return out


def update_hourly(generated_at):
  os.makedirs(HISTORY_DIR, exist_ok=True)
  data = load_hourly()
  tried = set()
  if os.path.exists(HOURLY_TRIED_FILE):
    with open(HOURLY_TRIED_FILE, encoding="utf-8") as fh:
      tried = {line.strip() for line in fh if line.strip()}
  dirty_months = set()
  latest_h = parse_hour(code_1h)

  def upsert(rows):
    for r in rows:
      data[(r["time_1h"], r["code"])] = r
      dirty_months.add(r["time_1h"][:6])

  # 1) 直近の数時間は毎回取り直して上書きする（1時間集計の速報値が後で更新されることがあるため）
  recent_from = latest_h - datetime.timedelta(hours=HOURLY_RECENT_HOURS - 1)
  rows = fetch_hourly(hour_code(recent_from), code_1h, generated_at)
  upsert(rows)
  print(f"1時間値(直近): {len(rows)} 件を更新しました。")

  # 2) それより前で、記録が1件も無い時刻を探し、新しい方から順に取り直して埋める
  present = {t for (t, _c) in data}
  start_h = latest_h - datetime.timedelta(days=HOURLY_BACKFILL_DAYS)
  boundary = recent_from - datetime.timedelta(hours=1)
  missing = []
  h = start_h
  while h <= boundary:
    c = hour_code(h)
    if c not in present and c not in tried:
      missing.append(h)
    h += datetime.timedelta(hours=1)
  print(f"1時間値: 記録の欠けている時刻は {len(missing)} 時間分です。")

  filled = 0
  newly_tried = set()
  chunks = 0
  missing.sort(reverse=True)
  while missing and chunks < HOURLY_MAX_CHUNKS:
    newest = missing[0]
    window_start = newest - datetime.timedelta(hours=HOURLY_CHUNK_HOURS - 1)
    window = [m for m in missing if window_start <= m <= newest]
    code_from, code_to = hour_code(min(window)), hour_code(newest)
    chunks += 1
    try:
      rows = fetch_hourly(code_from, code_to, generated_at)
    except Exception as e:
      print(f"1時間値の穴埋めに失敗しました({code_from}〜{code_to}): {e}")
      break  # 失敗した時は、ここで中断して次回に回す（「取得済み」とは記録しない）
    upsert(rows)
    got_hours = {r["time_1h"] for r in rows}
    filled += len(rows)
    for m in window:
      c = hour_code(m)
      if c not in got_hours and m < latest_h - datetime.timedelta(hours=HOURLY_SETTLE_HOURS):
        newly_tried.add(c)  # 取りに行ったが返ってこなかった時刻。次回以降は再取得しない
    missing = [m for m in missing if m not in set(window)]
  if chunks:
    print(f"1時間値(穴埋め): {chunks} 回のリクエストで {filled} 件を追加しました。残り {len(missing)} 時間分。")

  if dirty_months:
    save_hourly_months(data, dirty_months)
  if newly_tried:
    with open(HOURLY_TRIED_FILE, "a", encoding="utf-8") as fh:
      for c in sorted(newly_tried):
        fh.write(c + "\n")


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
          # 車種判別不能（小型・大型に分けられなかった台数）も合計に含める
          "up_u": p.get("上り・車種判別不能交通量"),
          "down_u": p.get("下り・車種判別不能交通量"),
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
          "up_u": getp(p, f"上り・小型大型判別不能交通量{suffix}"),
          "down_u": getp(p, f"下り・小型大型判別不能交通量{suffix}"),
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

  # グラフ用の過去1時間データ。失敗しても、上の更新には影響させない
  try:
    series = build_series(generated_at)
    with open("traffic_series.json", "w", encoding="utf-8") as out:
      json.dump(series, out, ensure_ascii=False, separators=(",", ":"))
    n_pts = sum(len(v["points"]) for v in series["stations"].values())
    print(f"グラフ用データ: {len(series['stations'])} 地点・{n_pts} 点を保存しました。")
  except Exception as e:
    print(f"グラフ用データの保存に失敗しました: {e}")

  # 1時間値の履歴（欠けた時刻の穴埋め）。失敗しても、上の更新には影響させない
  try:
    update_hourly(generated_at)
  except Exception as e:
    print(f"1時間値の履歴の更新に失敗しました: {e}")


if __name__ == "__main__":
  main()

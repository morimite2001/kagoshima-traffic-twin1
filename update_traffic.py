import datetime
import json
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


if __name__ == "__main__":
  main()

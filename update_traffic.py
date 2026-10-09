import datetime
import json
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


print(f"JARTIC API リクエスト開始: 5分={code_5m} / 1時間={code_1h}")
f1 = fetch("t_travospublic_measure_5m", code_5m)
f2 = fetch("t_travospublic_measure_1h", code_1h)
f3 = fetch("t_travospublic_measure_5m_img", code_5m)
f4 = fetch("t_travospublic_measure_1h_img", code_1h)

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
        "f1": None,
        "f2": None,
        "f3": None,
        "f4": None,
    }
  return stations[c]


for f in f1:
  p = f["properties"]
  get_st(f, "常設トラカン")["f1"] = {
      "up_s": p.get("上り・小型交通量"),
      "up_l": p.get("上り・大型交通量"),
      "down_s": p.get("下り・小型交通量"),
      "down_l": p.get("下り・大型交通量"),
  }
for f in f2:
  p = f["properties"]
  get_st(f, "常設トラカン")["f2"] = {
      "up_s": p.get("上り・小型交通量"),
      "up_l": p.get("上り・大型交通量"),
      "down_s": p.get("下り・小型交通量"),
      "down_l": p.get("下り・大型交通量"),
  }
for f in f3:
  p = f["properties"]
  get_st(f, "CCTVトラカン")["f3"] = {
      "up_tot": p.get("上り・自動車交通量(集計値)"),
      "down_tot": p.get("下り・自動車交通量(集計値)"),
      "up_s": p.get("上り・小型交通量(集計値)"),
      "up_l": p.get("上り・大型交通量(集計値)"),
      "down_s": p.get("下り・小型交通量(集計値)"),
      "down_l": p.get("下り・大型交通量(集計値)"),
  }
for f in f4:
  p = f["properties"]
  get_st(f, "CCTVトラカン")["f4"] = {
      "up_tot": p.get("上り・自動車交通量"),
      "down_tot": p.get("下り・自動車交通量"),
      "up_s": p.get("上り・小型交通量"),
      "up_l": p.get("上り・大型交通量"),
      "down_s": p.get("下り・小型交通量"),
      "down_l": p.get("下り・大型交通量"),
  }

with open("traffic_data.json", "w", encoding="utf-8") as out:
  json.dump(list(stations.values()), out, ensure_ascii=False, indent=2)

print(f"更新完了: {len(stations)} 地点を保存しました。")
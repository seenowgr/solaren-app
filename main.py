"""
Solaren — Cloud Backend
FastAPI server που τρέχει στο Render.com
Συνδέει το dashboard με Sungrow / Huawei / GoodWe / Fronius APIs
"""
import os, time, json, hashlib, logging, requests
from datetime import datetime, timedelta
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from dotenv import load_dotenv

load_dotenv()
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("solaren")

app = FastAPI(title="Solaren API")

# CORS — επιτρέπει στο dashboard να καλεί τον server
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Σερβίρει το dashboard HTML ──
app.mount("/static", StaticFiles(directory="."), name="static")

@app.get("/")
def root():
    return FileResponse("index.html")

# ══════════════════════════════
#  SUNGROW
# ══════════════════════════════
_sungrow_token = None
_sungrow_expires = None

def sungrow_login():
    global _sungrow_token, _sungrow_expires
    r = requests.post("https://gateway.isolarcloud.com.hk/openapi/login", json={
        "appkey": os.getenv("SUNGROW_APP_ID"),
        "token":  os.getenv("SUNGROW_TOKEN"),
    }, timeout=10)
    data = r.json()
    if data.get("result_code") == "1":
        _sungrow_token = data["result_data"]["token"]
        _sungrow_expires = datetime.now() + timedelta(hours=2)

def get_sungrow_token():
    if not _sungrow_token or datetime.now() > (_sungrow_expires or datetime.min):
        sungrow_login()
    return _sungrow_token

@app.get("/api/sungrow/sites")
def sungrow_sites():
    try:
        token = get_sungrow_token()
        r = requests.post(
            "https://gateway.isolarcloud.com.hk/openapi/getPowerStationList",
            json={"token": token, "page_size": 100, "cur_page": 1},
            headers={"token": token}, timeout=15
        )
        stations = r.json().get("result_data", {}).get("pageList", [])
        return {"ok": True, "sites": [
            {
                "id":     str(s["ps_id"]),
                "name":   s.get("ps_name", f"Sungrow-{s['ps_id']}"),
                "brand":  "sungrow",
                "kw":     round(float(s.get("curr_power", 0)) / 1000, 2),
                "cap":    round(float(s.get("installed_power_map", 0)), 1),
                "status": "ok" if s.get("status") == 1 else "error",
                "err":    s.get("alarm_msg"),
            } for s in stations
        ]}
    except Exception as e:
        log.error(f"Sungrow error: {e}")
        return {"ok": False, "error": str(e), "sites": []}

# ══════════════════════════════
#  HUAWEI FUSIONSOLAR
# ══════════════════════════════
_huawei_token = None
_huawei_expires = None

def get_huawei_token():
    global _huawei_token, _huawei_expires
    if _huawei_token and datetime.now() < (_huawei_expires or datetime.min):
        return _huawei_token
    r = requests.post(
        "https://eu5.fusionsolar.huawei.com/thirdData/login",
        json={"userName": os.getenv("HUAWEI_USER"), "systemCode": os.getenv("HUAWEI_PASS")},
        timeout=10
    )
    if r.json().get("success"):
        _huawei_token = r.cookies.get("XSRF-TOKEN")
        _huawei_expires = datetime.now() + timedelta(hours=1)
    return _huawei_token

@app.get("/api/huawei/debug")
def huawei_debug():
    """Δείχνει την ακριβή απάντηση του Huawei API για debugging"""
    try:
        token = get_huawei_token()
        r = requests.post(
            "https://eu5.fusionsolar.huawei.com/thirdData/getStationList",
            json={}, headers={"XSRF-TOKEN": token}, timeout=15
        )
        data = r.json()
        stations = data.get("data", [])
        # Επιστρέφει τα πρώτα 2 για να δούμε τα πεδία
        return {
            "total": len(stations),
            "sample": stations[:2] if stations else [],
            "raw_keys": list(stations[0].keys()) if stations else []
        }
    except Exception as e:
        return {"error": str(e)}

@app.get("/api/huawei/sites")
def huawei_sites():
    try:
        token = get_huawei_token()
        r = requests.post(
            "https://eu5.fusionsolar.huawei.com/thirdData/getStationList",
            json={}, headers={"XSRF-TOKEN": token}, timeout=15
        )
        data = r.json()
        stations = data.get("data", [])
        if not isinstance(stations, list):
            stations = []
        sites = []
        for s in stations:
            # Huawei uses different field names depending on API version
            site_id = s.get("dn") or s.get("stationCode") or s.get("plantCode") or str(s.get("id",""))
            name    = s.get("stationName") or s.get("plantName") or f"Huawei-{site_id}"
            # Power: try multiple field paths
            kpi     = s.get("realKpi") or s.get("kpiInfo") or {}
            power   = kpi.get("activePower") or kpi.get("radiationIntensity") or s.get("activePower") or 0
            cap     = s.get("capacity") or s.get("installedCapacity") or 0
            status_val = s.get("status") or s.get("healthState") or 1
            if status_val in (1, "1", "normal"): status = "ok"
            elif status_val in (2, "2", "warning"): status = "warn"
            else: status = "error"
            sites.append({
                "id":    str(site_id),
                "name":  name,
                "brand": "huawei",
                "kw":    round(float(power), 2),
                "cap":   round(float(cap), 1),
                "status": status,
                "err":   None,
            })
        return {"ok": True, "sites": sites, "raw_count": len(stations)}
    except Exception as e:
        log.error(f"Huawei error: {e}")
        return {"ok": False, "error": str(e), "sites": []}

# ══════════════════════════════
#  GOODWE SEMS
# ══════════════════════════════
_goodwe_jwt = None
_goodwe_uid = None

def get_goodwe_token():
    global _goodwe_jwt, _goodwe_uid
    if _goodwe_jwt:
        return _goodwe_jwt, _goodwe_uid
    r = requests.post(
        "https://www.semsportal.com/api/v1/Common/CrossLogin",
        json={"account": os.getenv("GOODWE_ACCOUNT"), "pwd": os.getenv("GOODWE_TOKEN"),
              "is_local": True, "agreement_agreement": 1},
        headers={"Token": json.dumps({"uid":"","user_id":"","timestamp":"",
                                      "token":"","client":"ios","version":"","language":"en"})},
        timeout=10
    )
    data = r.json()
    if data.get("code") == "100":
        _goodwe_uid = data["data"]["uid"]
        _goodwe_jwt = data["data"]["token"]
    return _goodwe_jwt, _goodwe_uid

def goodwe_header():
    jwt, uid = get_goodwe_token()
    return {"Token": json.dumps({
        "uid": uid, "user_id": uid,
        "timestamp": str(int(time.time())),
        "token": jwt, "client": "ios",
        "version": "v2.1.0", "language": "en"
    })}

@app.get("/api/goodwe/sites")
def goodwe_sites():
    try:
        r = requests.post(
            "https://www.semsportal.com/api/v2/PowerStation/GetMonitorDetailByPowerstationId",
            json={"powerstation_id": "all"}, headers=goodwe_header(), timeout=15
        )
        stations = r.json().get("data", {}).get("powerstation_list", [])
        return {"ok": True, "sites": [
            {
                "id":    str(s["id"]),
                "name":  s.get("name", f"GoodWe-{s['id']}"),
                "brand": "goodwe",
                "kw":    round(float(s.get("pac", 0)) / 1000, 2),
                "cap":   round(float(s.get("capacity", 0)), 1),
                "status":"ok" if int(s.get("status",1))==1 else ("warn" if int(s.get("status",1))==2 else "error"),
                "err":   s.get("alarm_msg"),
            } for s in stations
        ]}
    except Exception as e:
        log.error(f"GoodWe error: {e}")
        return {"ok": False, "error": str(e), "sites": []}

# ══════════════════════════════
#  FRONIUS SOLAR API
# ══════════════════════════════
@app.get("/api/fronius/sites")
def fronius_sites():
    try:
        api_key = os.getenv("FRONIUS_API_KEY")
        base    = os.getenv("FRONIUS_BASE_URL", "https://api.solarweb.com/swqapi")
        headers = {"AccessKeyId": os.getenv("FRONIUS_ACCESS_KEY_ID",""),
                   "AccessKeyValue": os.getenv("FRONIUS_ACCESS_KEY_VALUE",""),
                   "Content-Type": "application/json"}
        r = requests.get(f"{base}/pvsystems", headers=headers, timeout=15)
        systems = r.json().get("Data", [])
        sites = []
        for s in systems:
            sid = s.get("PvSystemId")
            # Get live data
            live = requests.get(f"{base}/pvsystems/{sid}/aggsdata/day",
                                headers=headers, timeout=10).json()
            channels = live.get("Data", {}).get("Channels", [])
            power = next((c.get("Value",0) for c in channels if "Power" in c.get("ChannelName","")), 0)
            sites.append({
                "id":    str(sid),
                "name":  s.get("Name", f"Fronius-{sid}"),
                "brand": "fronius",
                "kw":    round(float(power)/1000, 2) if power else 0,
                "cap":   round(float(s.get("PeakPower", 0)), 1),
                "status":"ok" if s.get("IsActive") else "error",
                "err":   None if s.get("IsActive") else "System offline",
            })
        return {"ok": True, "sites": sites}
    except Exception as e:
        log.error(f"Fronius error: {e}")
        return {"ok": False, "error": str(e), "sites": []}

# ══════════════════════════════
#  UNIFIED ENDPOINT
# ══════════════════════════════
@app.get("/api/all-sites")
def all_sites():
    """Ένα endpoint που επιστρέφει όλες τις εγκαταστάσεις από όλα τα brands"""
    all_data = []
    for brand_fn in [sungrow_sites, huawei_sites, goodwe_sites, fronius_sites]:
        result = brand_fn()
        if result.get("ok"):
            all_data.extend(result["sites"])
        else:
            log.warning(f"Brand failed: {result.get('error')}")

    # Ανίχνευση σφαλμάτων
    alerts = [
        {
            "site_id":   s["id"],
            "site_name": s["name"],
            "brand":     s["brand"],
            "severity":  "critical" if s["status"] == "error" else "warning",
            "message":   s.get("err") or "Χαμηλή απόδοση",
            "timestamp": datetime.now().isoformat(),
        }
        for s in all_data if s["status"] in ("error", "warn")
    ]

    return {
        "ok":        True,
        "timestamp": datetime.now().isoformat(),
        "sites":     all_data,
        "alerts":    alerts,
        "summary": {
            "total":   len(all_data),
            "ok":      sum(1 for s in all_data if s["status"] == "ok"),
            "errors":  sum(1 for s in all_data if s["status"] == "error"),
            "warnings":sum(1 for s in all_data if s["status"] == "warn"),
            "total_kw":round(sum(s["kw"] for s in all_data), 2),
        }
    }

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", 8000)))

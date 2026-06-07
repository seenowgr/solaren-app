"""
SolarOps — Alert System
========================
Τρέχει κάθε 5 λεπτά (cron job ή scheduler).
Ελέγχει κάθε inverter από Sungrow / Huawei / GoodWe
και στέλνει email + push notification αν βρει σφάλμα.

Απαιτήσεις:
  pip install requests python-dotenv pywebpush
"""

import os, time, json, logging, smtplib, hashlib
from datetime import datetime, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from dataclasses import dataclass, field
from typing import Optional
from dotenv import load_dotenv
import requests

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("solar-alerts")

# ══════════════════════════════════════════════
#  ΡΥΘΜΙΣΕΙΣ  (βάλε τα στο αρχείο .env)
# ══════════════════════════════════════════════

# Email (Gmail ή άλλο SMTP)
EMAIL_FROM    = os.getenv("EMAIL_FROM")          # π.χ. alerts@gmail.com
EMAIL_PASS    = os.getenv("EMAIL_PASS")          # App Password Gmail
EMAIL_TO      = os.getenv("EMAIL_TO")            # π.χ. info@company.gr
SMTP_HOST     = os.getenv("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT     = int(os.getenv("SMTP_PORT", 587))

# Push Notifications (Firebase FCM)
FCM_SERVER_KEY = os.getenv("FCM_SERVER_KEY")    # Από Firebase Console
FCM_DEVICE_TOKEN = os.getenv("FCM_DEVICE_TOKEN")  # Token κινητού

# API Keys
SUNGROW_TOKEN  = os.getenv("SUNGROW_TOKEN")
SUNGROW_APP_ID = os.getenv("SUNGROW_APP_ID")
HUAWEI_USER    = os.getenv("HUAWEI_USER")
HUAWEI_PASS    = os.getenv("HUAWEI_PASS")
GOODWE_TOKEN   = os.getenv("GOODWE_TOKEN")
GOODWE_ACCOUNT = os.getenv("GOODWE_ACCOUNT")

# Κατώφλια για ειδοποιήσεις
LOW_OUTPUT_THRESHOLD = 0.30   # Αν η παραγωγή πέσει >30% κάτω από αναμενόμενη
OFFLINE_MINUTES      = 15     # Αν δεν υπάρχει δεδομένο για X λεπτά → offline

# ══════════════════════════════════════════════
#  ΔΟΜΕΣ ΔΕΔΟΜΕΝΩΝ
# ══════════════════════════════════════════════

@dataclass
class InverterStatus:
    site_id:     str
    site_name:   str
    brand:       str            # sungrow / huawei / goodwe
    power_kw:    float          # Τρέχουσα παραγωγή kW
    status:      str            # ok / warning / error / offline
    error_code:  Optional[str] = None
    error_msg:   Optional[str] = None
    last_seen:   Optional[datetime] = None
    expected_kw: Optional[float] = None   # Εκτιμώμενη παραγωγή βάσει ώρας/καιρού

@dataclass
class Alert:
    site_id:   str
    site_name: str
    brand:     str
    severity:  str   # critical / warning
    message:   str
    timestamp: datetime = field(default_factory=datetime.now)

# ══════════════════════════════════════════════
#  SUNGROW API
# ══════════════════════════════════════════════

class SungrowAPI:
    BASE = "https://gateway.isolarcloud.com.hk/openapi"

    def __init__(self):
        self.token = SUNGROW_TOKEN
        self.app_id = SUNGROW_APP_ID
        self.session_token = None

    def _login(self):
        r = requests.post(f"{self.BASE}/login", json={
            "appkey": self.app_id,
            "token": self.token
        }, timeout=10)
        data = r.json()
        if data.get("result_code") == "1":
            self.session_token = data["result_data"]["token"]

    def get_all_sites(self) -> list[InverterStatus]:
        if not self.session_token:
            self._login()
        results = []
        try:
            r = requests.post(f"{self.BASE}/getPowerStationList", json={
                "token": self.session_token,
                "page_size": 100, "cur_page": 1
            }, headers={"token": self.session_token}, timeout=15)
            stations = r.json().get("result_data", {}).get("pageList", [])

            for st in stations:
                power = float(st.get("curr_power", 0)) / 1000  # W → kW
                status_code = st.get("status", 0)
                status = "ok" if status_code == 1 else "error" if status_code == 0 else "warning"
                results.append(InverterStatus(
                    site_id=str(st["ps_id"]),
                    site_name=st.get("ps_name", f"Sungrow-{st['ps_id']}"),
                    brand="sungrow", power_kw=power, status=status,
                    error_code=str(status_code) if status != "ok" else None,
                    error_msg=st.get("alarm_msg"),
                    last_seen=datetime.now()
                ))
        except Exception as e:
            log.error(f"Sungrow API error: {e}")
        return results

# ══════════════════════════════════════════════
#  HUAWEI FUSIONSOLAR API
# ══════════════════════════════════════════════

class HuaweiAPI:
    BASE = "https://eu5.fusionsolar.huawei.com/thirdData"

    def __init__(self):
        self.user = HUAWEI_USER
        self.password = HUAWEI_PASS
        self.token = None
        self.token_expires = None

    def _login(self):
        r = requests.post(f"{self.BASE}/login", json={
            "userName": self.user,
            "systemCode": self.password
        }, timeout=10)
        data = r.json()
        if data.get("success"):
            self.token = r.cookies.get("XSRF-TOKEN")
            self.token_expires = datetime.now() + timedelta(hours=1)

    def _headers(self):
        if not self.token or datetime.now() > (self.token_expires or datetime.min):
            self._login()
        return {"XSRF-TOKEN": self.token}

    def get_all_sites(self) -> list[InverterStatus]:
        results = []
        try:
            r = requests.post(f"{self.BASE}/getStationList",
                json={}, headers=self._headers(), timeout=15)
            stations = r.json().get("data", [])

            for st in stations:
                power = float(st.get("capacity", 0))
                real_power = float(st.get("realKpi", {}).get("radiationIntensity", power))
                status_val = st.get("status", 1)
                status = {1:"ok", 2:"warning"}.get(status_val, "error")
                results.append(InverterStatus(
                    site_id=str(st["dn"]),
                    site_name=st.get("stationName", f"Huawei-{st['dn']}"),
                    brand="huawei", power_kw=real_power, status=status,
                    error_code=str(status_val) if status != "ok" else None,
                    last_seen=datetime.now()
                ))
        except Exception as e:
            log.error(f"Huawei API error: {e}")
        return results

# ══════════════════════════════════════════════
#  GOODWE SEMS API
# ══════════════════════════════════════════════

class GoodweAPI:
    BASE = "https://www.semsportal.com/api"

    def __init__(self):
        self.token = GOODWE_TOKEN
        self.account = GOODWE_ACCOUNT
        self.uid = None
        self.jwt = None

    def _login(self):
        r = requests.post(f"{self.BASE}/v1/Common/CrossLogin",
            json={"account": self.account, "pwd": self.token,
                  "is_local": True, "agreement_agreement": 1},
            headers={"Token": json.dumps({"uid":"","user_id":"","timestamp":"",
                                           "token":"","client":"ios","version":"","language":"en"})},
            timeout=10)
        data = r.json()
        if data.get("code") == "100":
            self.uid = data["data"]["uid"]
            self.jwt = data["data"]["token"]

    def _token_header(self):
        if not self.jwt:
            self._login()
        return {"Token": json.dumps({
            "uid": self.uid, "user_id": self.uid,
            "timestamp": str(int(time.time())),
            "token": self.jwt, "client": "ios",
            "version": "v2.1.0", "language": "en"
        })}

    def get_all_sites(self) -> list[InverterStatus]:
        results = []
        try:
            r = requests.post(f"{self.BASE}/v2/PowerStation/GetMonitorDetailByPowerstationId",
                json={"powerstation_id": "all"}, headers=self._token_header(), timeout=15)
            stations = r.json().get("data", {}).get("powerstation_list", [])

            for st in stations:
                power = float(st.get("pac", 0)) / 1000  # W → kW
                status_code = int(st.get("status", 1))
                status = {1:"ok", 2:"warning"}.get(status_code, "error")
                results.append(InverterStatus(
                    site_id=str(st["id"]),
                    site_name=st.get("name", f"GoodWe-{st['id']}"),
                    brand="goodwe", power_kw=power, status=status,
                    error_code=str(status_code) if status != "ok" else None,
                    error_msg=st.get("alarm_msg"),
                    last_seen=datetime.now()
                ))
        except Exception as e:
            log.error(f"GoodWe API error: {e}")
        return results

# ══════════════════════════════════════════════
#  ALERT DETECTOR
# ══════════════════════════════════════════════

# Αποθηκεύουμε τα alerts που έχουν ήδη σταλθεί
# για να μη στέλνουμε το ίδιο alert κάθε 5 λεπτά
_sent_alerts: dict[str, datetime] = {}
RESEND_AFTER_HOURS = 4  # Ξανά-στέλνει το ίδιο alert μετά από 4 ώρες

def _alert_key(site_id: str, message: str) -> str:
    return hashlib.md5(f"{site_id}:{message}".encode()).hexdigest()

def should_send(site_id: str, message: str) -> bool:
    key = _alert_key(site_id, message)
    last = _sent_alerts.get(key)
    if last and datetime.now() - last < timedelta(hours=RESEND_AFTER_HOURS):
        return False
    _sent_alerts[key] = datetime.now()
    return True

def detect_alerts(inverters: list[InverterStatus]) -> list[Alert]:
    alerts = []
    hour = datetime.now().hour
    is_daylight = 7 <= hour <= 19

    for inv in inverters:
        # 1. Εντελώς εκτός λειτουργίας
        if inv.status == "error":
            msg = inv.error_msg or f"Inverter offline (κωδικός: {inv.error_code or 'N/A'})"
            alerts.append(Alert(inv.site_id, inv.site_name, inv.brand, "critical", msg))

        # 2. Μηδενική παραγωγή ενώ έχει ήλιο
        elif is_daylight and inv.power_kw == 0:
            alerts.append(Alert(inv.site_id, inv.site_name, inv.brand, "critical",
                "Μηδενική παραγωγή κατά τη διάρκεια ηλιοφάνειας"))

        # 3. Χαμηλή παραγωγή
        elif inv.status == "warning":
            if inv.expected_kw and inv.expected_kw > 0:
                pct_drop = (inv.expected_kw - inv.power_kw) / inv.expected_kw
                if pct_drop > LOW_OUTPUT_THRESHOLD:
                    alerts.append(Alert(inv.site_id, inv.site_name, inv.brand, "warning",
                        f"Χαμηλή παραγωγή: {inv.power_kw:.1f} kW αντί {inv.expected_kw:.1f} kW "
                        f"({pct_drop*100:.0f}% κάτω από αναμενόμενο)"))
            else:
                alerts.append(Alert(inv.site_id, inv.site_name, inv.brand, "warning",
                    f"Χαμηλή απόδοση: {inv.power_kw:.1f} kW"))

    return alerts

# ══════════════════════════════════════════════
#  EMAIL NOTIFICATION
# ══════════════════════════════════════════════

EMAIL_TEMPLATE = """
<!DOCTYPE html>
<html><head><meta charset="UTF-8">
<style>
  body {{ font-family: Arial, sans-serif; background:#0f1a14; color:#e8f5ec; margin:0; padding:20px; }}
  .card {{ background:#111c15; border-radius:12px; padding:24px; max-width:560px; margin:0 auto; }}
  .header {{ border-bottom:1px solid rgba(74,200,120,.2); padding-bottom:16px; margin-bottom:20px; }}
  .logo {{ color:#4ac878; font-size:20px; font-weight:700; }}
  .badge {{ display:inline-block; padding:3px 10px; border-radius:6px; font-size:12px; font-weight:700; }}
  .critical {{ background:rgba(224,82,82,.2); color:#e05252; }}
  .warning  {{ background:rgba(245,166,35,.2); color:#f5a623; }}
  .site-row {{ background:rgba(255,255,255,.03); border-radius:8px; padding:14px; margin:10px 0; border-left:3px solid {border_color}; }}
  .site-name {{ font-size:15px; font-weight:700; margin-bottom:4px; }}
  .site-msg  {{ font-size:13px; color:#7fa98a; }}
  .site-brand {{ font-size:11px; color:#4d7057; margin-top:4px; }}
  .footer {{ margin-top:20px; font-size:11px; color:#4d7057; text-align:center; }}
</style></head>
<body>
<div class="card">
  <div class="header">
    <div class="logo">⚡ SolarOps</div>
    <div style="color:#7fa98a;font-size:13px;margin-top:4px">{timestamp}</div>
  </div>
  <div style="font-size:16px;margin-bottom:16px">
    <span class="badge {severity_class}">{severity_label}</span>
    — {alert_count} ειδοποίηση(ες)
  </div>
  {alerts_html}
  <div class="footer">SolarOps Monitoring · <a href="#" style="color:#4ac878">Άνοιγμα Dashboard</a></div>
</div>
</body></html>
"""

def send_email(alerts: list[Alert]):
    if not EMAIL_FROM or not EMAIL_TO:
        log.warning("Email δεν έχει ρυθμιστεί — παράλειψη")
        return

    has_critical = any(a.severity == "critical" for a in alerts)
    severity_class = "critical" if has_critical else "warning"
    severity_label = "🚨 Κρίσιμο Σφάλμα" if has_critical else "⚠️ Προειδοποίηση"
    border_color = "#e05252" if has_critical else "#f5a623"

    alerts_html = ""
    for a in alerts:
        brand_icons = {"sungrow":"🔵","huawei":"🔴","goodwe":"🟢"}
        alerts_html += f"""
        <div class="site-row">
          <div class="site-name">{a.site_name}</div>
          <div class="site-msg">{a.message}</div>
          <div class="site-brand">{brand_icons.get(a.brand,'⚡')} {a.brand.capitalize()} · {a.timestamp.strftime('%H:%M')}</div>
        </div>"""

    html = EMAIL_TEMPLATE.format(
        timestamp=datetime.now().strftime("%d/%m/%Y %H:%M"),
        severity_class=severity_class,
        severity_label=severity_label,
        alert_count=len(alerts),
        alerts_html=alerts_html,
        border_color=border_color
    )

    subject_prefix = "🚨 [ΚΡΙΣΙΜΟ]" if has_critical else "⚠️ [ΠΡΟΕΙΔΟΠΟΙΗΣΗ]"
    sites_str = ", ".join(set(a.site_name for a in alerts[:3]))
    if len(alerts) > 3:
        sites_str += f" +{len(alerts)-3} ακόμα"
    subject = f"{subject_prefix} SolarOps — {sites_str}"

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = EMAIL_FROM
    msg["To"]      = EMAIL_TO
    msg.attach(MIMEText(html, "html", "utf-8"))

    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
            server.starttls()
            server.login(EMAIL_FROM, EMAIL_PASS)
            server.sendmail(EMAIL_FROM, EMAIL_TO, msg.as_string())
        log.info(f"✉️  Email στάλθηκε: {len(alerts)} alerts")
    except Exception as e:
        log.error(f"Email error: {e}")

# ══════════════════════════════════════════════
#  PUSH NOTIFICATION (Firebase FCM)
# ══════════════════════════════════════════════

def send_push(alerts: list[Alert]):
    if not FCM_SERVER_KEY or not FCM_DEVICE_TOKEN:
        log.warning("FCM δεν έχει ρυθμιστεί — παράλειψη")
        return

    has_critical = any(a.severity == "critical" for a in alerts)
    title = "🚨 SolarOps — Κρίσιμο!" if has_critical else "⚠️ SolarOps — Προειδοποίηση"

    if len(alerts) == 1:
        body = f"{alerts[0].site_name}: {alerts[0].message}"
    else:
        body = f"{len(alerts)} εγκαταστάσεις με πρόβλημα"

    payload = {
        "to": FCM_DEVICE_TOKEN,
        "priority": "high",
        "notification": {
            "title": title,
            "body": body,
            "sound": "default",
            "badge": len(alerts),
            "click_action": "FLUTTER_NOTIFICATION_CLICK"
        },
        "data": {
            "alerts": json.dumps([{
                "site_id": a.site_id,
                "site_name": a.site_name,
                "brand": a.brand,
                "severity": a.severity,
                "message": a.message
            } for a in alerts]),
            "type": "solar_alert"
        }
    }

    try:
        r = requests.post(
            "https://fcm.googleapis.com/fcm/send",
            json=payload,
            headers={
                "Authorization": f"key={FCM_SERVER_KEY}",
                "Content-Type": "application/json"
            },
            timeout=10
        )
        if r.json().get("success") == 1:
            log.info(f"📱 Push notification στάλθηκε: {len(alerts)} alerts")
        else:
            log.error(f"FCM error: {r.json()}")
    except Exception as e:
        log.error(f"Push error: {e}")

# ══════════════════════════════════════════════
#  ΚΥΡΙΟ LOOP
# ══════════════════════════════════════════════

def run_check():
    log.info("═══ Έλεγχος inverters ═══")

    # Μάζεψε δεδομένα από όλα τα APIs
    all_inverters: list[InverterStatus] = []
    all_inverters.extend(SungrowAPI().get_all_sites())
    all_inverters.extend(HuaweiAPI().get_all_sites())
    all_inverters.extend(GoodweAPI().get_all_sites())
    log.info(f"Ελέγχθηκαν {len(all_inverters)} inverters")

    # Ανίχνευσε προβλήματα
    raw_alerts = detect_alerts(all_inverters)
    log.info(f"Βρέθηκαν {len(raw_alerts)} alerts")

    # Φιλτράρισε όσα έχουν ήδη σταλθεί πρόσφατα
    new_alerts = [a for a in raw_alerts if should_send(a.site_id, a.message)]
    log.info(f"Νέα alerts για αποστολή: {len(new_alerts)}")

    if not new_alerts:
        log.info("✅ Καμία νέα ειδοποίηση")
        return

    # Στείλε ειδοποιήσεις
    send_email(new_alerts)
    send_push(new_alerts)

    for a in new_alerts:
        log.info(f"  [{a.severity.upper()}] {a.site_name} ({a.brand}): {a.message}")

if __name__ == "__main__":
    run_check()

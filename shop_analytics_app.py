"""
Shop Analytics Hourly - panel Streamlit
=======================================
Dua mode, otomatis:

* LOKAL (laptop Windows, ada shop_analytics_hourly.py di folder ini)
  Panel kontrol: mulai/berhenti worker, pantau log, lihat hasil.
    py -m pip install -r requirements.txt
    py -m streamlit run shop_analytics_app.py

* WEB (Streamlit Community Cloud)
  Dashboard saja: membaca data dari Google Sheet yang diisi worker di laptop.
  Pengambilan data TIDAK bisa jalan di cloud (butuh Chrome di laptop yang login Seller Center).
  Isi Settings -> Secrets di Streamlit Cloud dengan isi .streamlit/secrets.toml (jangan di-commit).
"""
import os
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
import streamlit as st

BASE = Path(__file__).resolve().parent
WORKER = BASE / "shop_analytics_hourly.py"
PID_FILE = BASE / "worker.pid"
LOG_FILE = BASE / "worker.log"

DEFAULT_SHEET_ID = "1ZXO2y7M8Ggpr4J_4ixSk5tJNSzWTJQTjf579uRpELwg"
DEFAULT_SHEET_TAB = "Sheet1"

NUM_COLS = [
    "GMV", "Items Sold", "SKU Orders", "Orders",
    "Affiliate GMV", "Affiliate - LIVE streams", "Affiliate - Videos", "Affiliate - Product cards",
    "Seller GMV", "Seller - LIVE streams", "Seller - Videos", "Seller - Product cards",
]

# di cloud (Linux) tidak ada Chrome laptop -> mode dashboard saja
CLOUD = os.name != "nt"

st.set_page_config(page_title="Shop Analytics Hourly", page_icon="📈", layout="wide")


# ----------------------------------------------------------------- proses worker
def read_pid():
    try:
        return int(PID_FILE.read_text().strip())
    except Exception:
        return None


def is_running(pid):
    if not pid:
        return False
    if os.name == "nt":
        out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                             capture_output=True, text=True).stdout
        return str(pid) in out
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def stop_worker(pid):
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True)
    else:
        try:
            os.kill(pid, 15)
        except OSError:
            pass
    PID_FILE.unlink(missing_ok=True)


def build_cmd(cdp, minute, stop_hour, wait, out_path):
    return [sys.executable, "-u", str(WORKER), "--cdp", cdp, "--minute", str(minute),
            "--stop-hour", str(stop_hour), "--wait", str(wait), "--out", str(out_path)]


def start_worker(cmd):
    kw = {}
    if os.name == "nt":  # lepas dari proses Streamlit supaya tetap jalan
        kw["creationflags"] = 0x00000008 | 0x00000200
    with open(LOG_FILE, "w", encoding="utf-8") as lf:
        p = subprocess.Popen(cmd, cwd=BASE, stdout=lf, stderr=subprocess.STDOUT, **kw)
    PID_FILE.write_text(str(p.pid))


def check_chrome(cdp):
    try:
        with urllib.request.urlopen(cdp.rstrip("/") + "/json/version", timeout=3) as r:
            return True, r.read().decode("utf-8", "ignore")[:200]
    except Exception as e:
        return False, str(e)


def launch_chrome():
    cands = [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        os.path.expandvars(r"%LocalAppData%\Google\Chrome\Application\chrome.exe"),
    ]
    exe = next((c for c in cands if os.path.exists(c)), None)
    if not exe:
        return False
    subprocess.Popen([exe, "--remote-debugging-port=9222", r"--user-data-dir=C:\chrome-nivea"])
    return True


# ---------------------------------------------------------------------- data
def prepare(df):
    if df is None or df.empty or "Target Hour" not in df.columns:
        return None
    th = df["Target Hour"]
    as_num = pd.to_numeric(th, errors="coerce")
    # Google Sheet bisa mengembalikan tanggal sebagai serial number (hari sejak 1899-12-30)
    df["Target Hour"] = pd.to_datetime(th.astype(str), errors="coerce").where(
        as_num.isna(), pd.to_datetime(as_num, unit="D", origin="1899-12-30").dt.round("min"))
    for c in NUM_COLS:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    df = df.dropna(subset=["Target Hour"]).sort_values("Target Hour")
    if df.empty:
        return None
    # 1 baris per jam: pilih yang paling dekat ke jam bulat (Updated On - Target Hour terkecil).
    # Baris isi-ulang dari tren per jam = tepat jam bulat (selisih 0).
    upd = pd.to_datetime(df.get("Updated On", pd.Series(index=df.index, dtype=str)).astype(str),
                         format="%b %d, %Y %I:%M %p", errors="coerce")
    lag = (upd - df["Target Hour"]).dt.total_seconds().fillna(0).abs()
    if "Fresh" in df.columns:  # data lama (TIDAK) paling belakang
        lag = lag + df["Fresh"].astype(str).str.startswith("TIDAK") * 1e9
    df = df.assign(_lag=lag).sort_values(["Target Hour", "_lag"])
    df = df.drop_duplicates(subset=["Target Hour"], keep="first").drop(columns="_lag")
    df["Tanggal"] = df["Target Hour"].dt.date
    df["Jam"] = df["Target Hour"].dt.strftime("%H:00")
    df["GMV jam ini (selisih)"] = df.groupby("Tanggal")["GMV"].diff()
    return df


def load_csv(path):
    if not Path(path).exists():
        return None
    return prepare(pd.read_csv(path, encoding="utf-8-sig"))


def secret(section, key=None, default=None):
    try:
        v = st.secrets[section]
        return v[key] if key else v
    except Exception:
        return default


@st.cache_data(ttl=60, show_spinner="Membaca Google Sheet ...")
def load_sheet(sheet_id, tab):
    import gspread
    from google.oauth2.credentials import Credentials

    info = secret("gcp_oauth")
    if info:
        creds = Credentials.from_authorized_user_info(dict(info))
    else:  # lokal tanpa secrets.toml: pakai token hasil login worker
        creds = Credentials.from_authorized_user_file(str(BASE / "authorized_user.json"))
    ws = gspread.authorize(creds).open_by_key(sheet_id).worksheet(tab)
    rows = ws.get_all_values(value_render_option="UNFORMATTED_VALUE",
                             date_time_render_option="SERIAL_NUMBER")
    if len(rows) < 2:
        return None
    return pd.DataFrame(rows[1:], columns=rows[0])


def rp(v):
    return "-" if pd.isna(v) else "Rp{:,.0f}".format(v).replace(",", ".")


def num(v):
    return "-" if pd.isna(v) else "{:,.0f}".format(v).replace(",", ".")


# ------------------------------------------------------------------- sidebar
sheet_id = secret("sheet", "id", DEFAULT_SHEET_ID)
sheet_tab = secret("sheet", "tab", DEFAULT_SHEET_TAB)

st.sidebar.header("Pengaturan")
if CLOUD:
    source = "Google Sheet"
else:
    source = st.sidebar.radio("Sumber data", ["CSV lokal", "Google Sheet"], horizontal=True)
    cdp = st.sidebar.text_input("Alamat Chrome (CDP)", "http://localhost:9222")
    minute = st.sidebar.number_input("Menit pengambilan tiap jam", 0, 59, 1)
    stop_hour = st.sidebar.number_input("Berhenti pada jam (24 = tengah malam, 0 = tanpa batas)", 0, 24, 24)
    wait = st.sidebar.number_input("Maks menit menunggu data ter-update", 1, 30, 10)
    out_name = st.sidebar.text_input("File hasil (CSV)", "shop_analytics_hourly.csv")
    out_path = (BASE / out_name).resolve()
auto = st.sidebar.checkbox("Auto-refresh tiap 60 detik" if CLOUD else "Auto-refresh tiap 15 detik", value=False)
if source == "Google Sheet":
    if st.sidebar.button("🔄 Muat ulang data Sheet"):
        load_sheet.clear()
    st.sidebar.link_button("📄 Buka Google Sheet", f"https://docs.google.com/spreadsheets/d/{sheet_id}/edit")
if CLOUD:
    st.sidebar.info("Versi web hanya **menampilkan data**. Tombol Chrome, mulai/berhenti pengambilan "
                    "dan log ada di panel **laptop** (`py -m streamlit run shop_analytics_app.py`), "
                    "karena pengambilan data butuh Chrome di laptop yang login Seller Center.")

st.title("📈 Shop Analytics — snapshot per jam")

# ------------------------------------------------------------- Chrome & kontrol
running = False
if not CLOUD:
    c1, c2 = st.columns(2)
    with c1:
        st.subheader("1. Chrome")
        ok, info = check_chrome(cdp)
        if ok:
            st.success("Chrome terhubung (port debugging aktif).")
        else:
            st.error("Chrome belum terhubung ke port debugging.")
            if st.button("Buka Chrome khusus (port 9222)"):
                if launch_chrome():
                    st.info("Chrome dibuka. Login ke Seller Center di jendela itu, lalu klik Refresh.")
                else:
                    st.warning("Chrome tidak ditemukan di lokasi umum. Buka manual dengan perintah di panduan.")
        if st.button("Cek ulang koneksi"):
            st.rerun()

    with c2:
        st.subheader("2. Pengambilan data")
        pid = read_pid()
        running = is_running(pid)
        if running:
            nxt = datetime.now().replace(minute=int(minute), second=0, microsecond=0)
            if nxt <= datetime.now():
                nxt += timedelta(hours=1)
            st.success(f"Mode jaga BERJALAN (PID {pid}). Pengambilan berikutnya ±{nxt:%H:%M}.")
            if st.button("⏹ Hentikan"):
                stop_worker(pid)
                st.rerun()
        else:
            st.info("Mode jaga tidak berjalan.")
            b1, b2 = st.columns(2)
            if b1.button("🧪 Tes ambil sekarang"):
                with st.spinner("Mengambil data... (bisa sampai beberapa menit)"):
                    try:
                        r = subprocess.run(build_cmd(cdp, minute, stop_hour, wait, out_path) + ["--once"],
                                           cwd=BASE, capture_output=True, text=True,
                                           timeout=(int(wait) + 2) * 60 + 120)
                        st.code((r.stdout + r.stderr).strip() or "(tanpa output)")
                    except subprocess.TimeoutExpired:
                        st.error("Waktu habis. Cek apakah halaman Seller Center sudah login.")
            if b2.button("▶ Mulai mode jaga"):
                start_worker(build_cmd(cdp, minute, stop_hour, wait, out_path))
                time.sleep(1)
                st.rerun()

    with st.expander("Log pengambilan terakhir", expanded=running):
        if LOG_FILE.exists():
            lines = LOG_FILE.read_text(encoding="utf-8", errors="ignore").splitlines()[-30:]
            st.code("\n".join(lines) or "(kosong)")
        else:
            st.caption("Belum ada log.")
    st.divider()
else:
    st.caption("Dashboard web: data dibaca dari Google Sheet yang diisi otomatis oleh worker di laptop.")

# ----------------------------------------------------------------------- hasil
st.subheader("Hasil")
df, err = None, None
if source == "Google Sheet":
    try:
        df = prepare(load_sheet(sheet_id, sheet_tab))
    except Exception as e:
        err = e
else:
    df = load_csv(out_path)

if err is not None:
    st.error(f"Gagal membaca Google Sheet: {err}")
    if CLOUD:
        st.caption("Cek isi Settings → Secrets di Streamlit Cloud (bagian [gcp_oauth] dan [sheet]).")
elif df is None:
    where = "Google Sheet masih kosong" if source == "Google Sheet" else "CSV lokal masih kosong"
    st.warning(f"{where}. Jalankan worker di laptop (Tes ambil sekarang / mode jaga) dulu.")
else:
    newest = df.iloc[-1]
    age = datetime.now() - newest["Target Hour"].to_pydatetime() if not CLOUD else None
    st.sidebar.metric("Data terakhir", f"{newest['Target Hour']:%d %b %H:00}")
    st.sidebar.caption(f"{len(df)} jam tercatat")
    if age is not None and age > timedelta(hours=2):
        st.warning(f"Data terakhir {newest['Target Hour']:%d %b %H:00} — worker mungkin berhenti.")
    dates = sorted(df["Tanggal"].unique(), reverse=True)
    pick = st.selectbox("Tanggal", dates, index=0)
    d = df[df["Tanggal"] == pick].copy()

    if "Fresh" in d.columns and (d["Fresh"].astype(str).str.startswith("TIDAK")).any():
        st.warning("Ada baris dengan data lama (kolom Fresh = TIDAK). Cek 'Updated On' pada tabel.")

    last = d.iloc[-1]
    prev = d.iloc[-2] if len(d) > 1 else None

    def delta(col):
        if prev is None or pd.isna(last[col]) or pd.isna(prev[col]):
            return None
        return num(last[col] - prev[col])

    m = st.columns(4)
    m[0].metric(f"GMV (s.d. {last['Jam']})", rp(last["GMV"]), delta("GMV"))
    m[1].metric("Items sold", num(last["Items Sold"]), delta("Items Sold"))
    m[2].metric("Affiliate GMV", rp(last["Affiliate GMV"]), delta("Affiliate GMV"))
    m[3].metric("Seller GMV", rp(last["Seller GMV"]), delta("Seller GMV"))

    g1, g2 = st.columns(2)
    with g1:
        st.caption("GMV kumulatif per jam")
        st.line_chart(d.set_index("Jam")["GMV"])
    with g2:
        st.caption("Affiliate vs Seller (kumulatif)")
        st.bar_chart(d.set_index("Jam")[["Affiliate GMV", "Seller GMV"]])

    st.caption("GMV yang masuk per jam (selisih dari jam sebelumnya)")
    st.bar_chart(d.set_index("Jam")["GMV jam ini (selisih)"])

    show = d.drop(columns=["Tanggal", "Jam"]).sort_values("Target Hour", ascending=False)
    st.dataframe(show, width="stretch", hide_index=True)
    all_show = df.drop(columns=["Tanggal", "Jam"])
    st.download_button("⬇ Download CSV (semua tanggal)",
                       all_show.to_csv(index=False).encode("utf-8-sig"),
                       file_name="shop_analytics_hourly.csv", mime="text/csv")

if auto:
    time.sleep(60 if CLOUD else 15)
    st.rerun()

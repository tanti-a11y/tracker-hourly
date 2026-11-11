"""
TikTok Shop - Shop analytics hourly snapshot
============================================
Tiap jam pada menit :01 (mis. 15:01) skrip ini:
  1. Reload halaman Shop analytics (data-overview).
  2. Tunggu sampai "Updated on" sudah >= jam bulat tsb (mis. 15:00); kalau belum, reload tiap 1 menit.
  3. Ambil Key metrics (GMV, Items sold, SKU orders, Orders).
  4. Scroll ke "GMV breakdown", klik "By order source", ambil Affiliate/Seller + LIVE/Videos/Product cards.
  5. Tambahkan 1 baris ke CSV.

PERSIAPAN: sama seperti skrip SKU (Chrome dibuka dengan --remote-debugging-port=9222 dan sudah login).
  py -m pip install playwright

PAKAI
  py shop_analytics_hourly.py --once         # ambil 1x sekarang (TES dulu)
  py shop_analytics_hourly.py                # mode jaga: jalan terus, ambil tiap HH:01
  py shop_analytics_hourly.py --out snapshot_nivea.csv --minute 1

Syarat: laptop tidak sleep & Chrome (port 9222) tetap menyala selama mode jaga berjalan.
Hentikan dengan Ctrl+C.
"""
import argparse
import csv
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta

from playwright.sync_api import sync_playwright

URL = "https://seller-id.tokopedia.com/compass/data-overview?shop_region=ID&platform_data_source=shop"

# Google Sheet "Tracker Otomatis" (login via Google OAuth, butuh oauth_client.json = OAuth client "Desktop app" di folder skrip ini)
SHEET_ID = "1ZXO2y7M8Ggpr4J_4ixSk5tJNSzWTJQTjf579uRpELwg"
SHEET_TAB = "Sheet1"
HERE = os.path.dirname(os.path.abspath(__file__))

COLUMNS = [
    "Captured At", "Target Hour", "Updated On", "Fresh",
    "GMV", "Items Sold", "SKU Orders", "Orders",
    "Affiliate GMV", "Affiliate - LIVE streams", "Affiliate - Videos", "Affiliate - Product cards",
    "Seller GMV", "Seller - LIVE streams", "Seller - Videos", "Seller - Product cards",
]


def to_int(s):
    d = re.sub(r"[^\d]", "", s or "")
    return int(d) if d else ""


# ------------------------------------------------------------- parser (teks halaman)
def parse_updated_on(text):
    m = re.search(r"Updated on:\s*([A-Za-z]{3,9}) (\d{1,2}), (\d{4})\s+(\d{1,2}):(\d{2})\s*(AM|PM)", text)
    if not m:
        return None, ""
    raw = m.group(0).replace("Updated on:", "").strip()
    try:
        dt = datetime.strptime(f"{m.group(1)[:3]} {m.group(2)} {m.group(3)} {m.group(4)}:{m.group(5)} {m.group(6)}",
                               "%b %d %Y %I:%M %p")
    except ValueError:
        return None, raw
    return dt, raw


def parse_key_metrics(text):
    i = text.find("Updated on")
    seg = text[i:] if i >= 0 else text
    j = seg.find("Today GMV")
    if j > 0:
        seg = seg[:j]
    out = {}
    m = re.search(r"GMV\s*Rp\s*([\d.,]+)", seg)
    out["GMV"] = to_int(m.group(1)) if m else ""
    for key, label in (("Items Sold", "Items sold"), ("SKU Orders", "SKU orders"), ("Orders", "Orders")):
        m = re.search(r"(?<![A-Za-z])" + re.escape(label) + r"\s*([\d.,]+)", seg)
        out[key] = to_int(m.group(1)) if m else ""
    return out


def parse_breakdown(text):
    i = text.find("GMV breakdown")
    if i < 0:
        return {}
    lines = [l.strip() for l in text[i:].splitlines() if l.strip()]
    res, group = {}, None

    def rp_in(next_lines):
        for l in next_lines[:3]:
            m = re.match(r"^Rp\s*([\d.]+)$", l)
            if m:
                return to_int(m.group(1))
        return None

    for k, line in enumerate(lines):
        nxt = lines[k + 1:]
        if line in ("Affiliate", "Seller"):
            v = rp_in(nxt)
            if v is not None:  # baris grup (bukan legenda donat yang diikuti persen)
                group = line
                res[f"{line} GMV"] = v
            continue
        for sub in ("LIVE streams", "Videos", "Product cards"):
            if line.startswith(sub) and group:
                v = rp_in(nxt) if not re.search(r"Rp\s*[\d.]+$", line) else to_int(line.split("Rp")[-1])
                if v is not None:
                    res.setdefault(f"{group} - {sub}", v)
    return res


# ------------------------------------------------------------------ scrape
def scrape_once(ctx, target_hour, max_wait_min):
    page = ctx.new_page()
    try:
        fresh, text, upd_dt, upd_raw = False, "", None, ""
        for attempt in range(max_wait_min + 1):
            page.goto(URL, wait_until="domcontentloaded")
            page.get_by_text("Key metrics").first.wait_for(timeout=45000)
            page.wait_for_timeout(3000)
            text = page.locator("body").inner_text()
            upd_dt, upd_raw = parse_updated_on(text)
            if upd_dt and upd_dt >= target_hour:
                fresh = True
                break
            print(f"   data belum update ({upd_raw or '??'}), coba lagi 60 dtk "
                  f"({attempt + 1}/{max_wait_min})")
            page.wait_for_timeout(60000)

        km = parse_key_metrics(text)

        # scroll ke GMV breakdown -> klik By order source
        page.get_by_text("GMV breakdown", exact=True).first.scroll_into_view_if_needed()
        page.get_by_text("By order source", exact=True).first.click()
        page.wait_for_timeout(2000)
        text2 = page.locator("body").inner_text()
        bd = parse_breakdown(text2)
        if not bd:
            os.makedirs("debug", exist_ok=True)
            page.screenshot(path=os.path.join("debug", f"{datetime.now():%H%M%S}_breakdown.png"))

        row = {c: "" for c in COLUMNS}
        row.update(km)
        row.update(bd)
        row["Captured At"] = f"{datetime.now():%Y-%m-%d %H:%M:%S}"
        row["Target Hour"] = f"{target_hour:%Y-%m-%d %H:00}"
        row["Updated On"] = upd_raw
        row["Fresh"] = "ya" if fresh else "TIDAK (data lama)"
        return row
    finally:
        page.close()


def write_row(path, row):
    new = not os.path.exists(path)
    with open(path, "a", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS)
        if new:
            w.writeheader()
        w.writerow(row)


# ------------------------------------------------------------ Google Sheets
def open_worksheet(sheet_id, tab):
    """Login Google OAuth (pertama kali buka browser), token disimpan di authorized_user.json."""
    import gspread
    gc = gspread.oauth(
        credentials_filename=os.path.join(HERE, "oauth_client.json"),
        authorized_user_filename=os.path.join(HERE, "authorized_user.json"),
    )
    ws = gc.open_by_key(sheet_id).worksheet(tab)
    if ws.row_values(1) != COLUMNS:
        ws.update([COLUMNS], "A1")
    return ws


def send_to_sheet(ws, row, pending_path):
    """Kirim baris ke Google Sheet. Kalau gagal, simpan di file pending & dicoba lagi jam berikutnya."""
    queue = []
    if os.path.exists(pending_path):
        with open(pending_path, encoding="utf-8") as f:
            queue = [json.loads(l) for l in f if l.strip()]
    queue.append(row)
    left = []
    try:
        ws.append_rows([[r.get(c, "") for c in COLUMNS] for r in queue], value_input_option="USER_ENTERED")
    except Exception as e:
        print("   Sheet GAGAL (disimpan, dicoba lagi nanti):", e)
        left = queue
    if left:
        with open(pending_path, "w", encoding="utf-8") as f:
            f.writelines(json.dumps(r) + "\n" for r in left)
    else:
        if os.path.exists(pending_path):
            os.remove(pending_path)
        print(f"   Sheet OK ({len(queue)} baris terkirim)")


def next_run(minute):
    now = datetime.now()
    t = now.replace(minute=minute, second=0, microsecond=0)
    if t <= now:
        t += timedelta(hours=1)
    return t


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cdp", default="http://localhost:9222")
    ap.add_argument("--out", default="shop_analytics_hourly.csv")
    ap.add_argument("--minute", type=int, default=1, help="menit pengambilan tiap jam (default 1 = HH:01)")
    ap.add_argument("--wait", type=int, default=10, help="maks menit menunggu data ter-update")
    ap.add_argument("--once", action="store_true", help="ambil sekali sekarang lalu selesai")
    ap.add_argument("--sheet-id", default=SHEET_ID, help="ID Google Sheet tujuan")
    ap.add_argument("--sheet-tab", default=SHEET_TAB, help="nama tab di Google Sheet")
    ap.add_argument("--no-sheet", action="store_true", help="jangan kirim ke Google Sheet (CSV saja)")
    ap.add_argument("--stop-hour", type=int, default=0,
                    help="berhenti pada jam ini (24 = tengah malam, 0 = tanpa batas)")
    args = ap.parse_args()
    pending_path = os.path.splitext(args.out)[0] + ".sheet_pending.jsonl"

    stop_at = None
    if args.stop_hour and not args.once:
        base = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        stop_at = base + timedelta(hours=args.stop_hour)
        if stop_at <= datetime.now():
            stop_at += timedelta(days=1)
        print(f"Mode jaga berhenti pada {stop_at:%Y-%m-%d %H:%M}")

    ws = None
    if not args.no_sheet:
        ws = open_worksheet(args.sheet_id, args.sheet_tab)
        print(f"Google Sheet terhubung: tab '{args.sheet_tab}'")

    with sync_playwright() as pw:
        browser = pw.chromium.connect_over_cdp(args.cdp)
        ctx = browser.contexts[0]

        while True:
            if not args.once:
                t = next_run(args.minute)
                if stop_at and t >= stop_at:
                    print("Batas jam berhenti tercapai.")
                    break
                print(f"Menunggu {t:%H:%M} ...")
                while datetime.now() < t:
                    time.sleep(5)
            target_hour = datetime.now().replace(minute=0, second=0, microsecond=0)
            print(f"[{datetime.now():%H:%M:%S}] ambil data jam {target_hour:%H:00}")
            try:
                row = scrape_once(ctx, target_hour, args.wait)
                write_row(args.out, row)
                print("   OK:", {k: row[k] for k in ("Updated On", "GMV", "Items Sold", "Affiliate GMV", "Seller GMV")})
                if ws is not None:
                    send_to_sheet(ws, row, pending_path)
            except Exception as e:
                print("   GAGAL:", e)
            if args.once:
                break
    print("Selesai." if args.once else "")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)

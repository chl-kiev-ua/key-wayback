"""
Автоматичне архівування публічних сторінок КЛЮЧа у Wayback Machine (SPN2 API).

Запускається з GitHub Actions за розкладом — НЕ на сервері КЛЮЧа і НЕ в Києві,
тому не залежить ні від доступу до продакшну, ні від відключень світла.

Логіка одного запуску:
  1. Зібрати список URL: priority-urls.txt → sitemap.xml → (якщо sitemap немає) обхід сайту.
  2. Впорядкувати: пріоритетні → ще жодного разу не архівовані → найдавніше архівовані.
  3. Надіслати до MAX_URLS сторінок у Save Page Now 2 (не більше MAX_CONCURRENT одночасно).
     Сторінки, що вже мають знімок, свіжіший за IF_NOT_ARCHIVED_WITHIN, архів пропускає сам.
  4. Дописати результати в archive-log/captures.csv (журнал = доказ, що й коли збережено).

Таким чином за кілька днів проходиться весь сайт, далі — ротація по колу.

Змінні середовища:
  IA_ACCESS_KEY, IA_SECRET_KEY  — S3-ключі з https://archive.org/account/s3.php (обов'язково)
  SITE_URL                      — за замовчуванням https://key.chl.kiev.ua
  MAX_URLS                      — скільки сторінок за один запуск (300)
  MAX_CONCURRENT                — одночасних захоплень (4; ліміт архіву для авторизованих — 12)
  IF_NOT_ARCHIVED_WITHIN        — не перезнімати, якщо є знімок, свіжіший за це (30d)
  PRIORITY_NOT_ARCHIVED_WITHIN  — те саме для priority-urls.txt (1d — щоденний актуальний вигляд)
  TIME_BUDGET_MIN               — зупинити подачу нових URL через N хвилин (50)

Кожен запуск також дописує archive-log/availability.csv — фактичний журнал доступності сайту.
  CRAWL_LIMIT                   — максимум сторінок при обході без sitemap (3000)
  DRY_RUN=1                     — лише зібрати й показати список, нічого не надсилати
"""

import csv
import os
import sys
import time
import datetime as dt
import xml.etree.ElementTree as ET
from collections import deque
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse, urldefrag

import requests

SITE = os.environ.get("SITE_URL", "https://key.chl.kiev.ua").rstrip("/")
ACCESS = os.environ.get("IA_ACCESS_KEY", "")
SECRET = os.environ.get("IA_SECRET_KEY", "")
MAX_URLS = int(os.environ.get("MAX_URLS") or 300)
MAX_CONCURRENT = int(os.environ.get("MAX_CONCURRENT") or 4)
NOT_WITHIN = os.environ.get("IF_NOT_ARCHIVED_WITHIN") or "30d"
# Сторінки з priority-urls.txt перезнімаються значно частіше — щоб бачити актуальний стан проду
PRIORITY_NOT_WITHIN = os.environ.get("PRIORITY_NOT_ARCHIVED_WITHIN") or "1d"
AVAIL_PATH = "archive-log/availability.csv"
TIME_BUDGET = int(os.environ.get("TIME_BUDGET_MIN") or 50) * 60
CRAWL_LIMIT = int(os.environ.get("CRAWL_LIMIT") or 3000)
DRY_RUN = os.environ.get("DRY_RUN") == "1"

LOG_PATH = "archive-log/captures.csv"
PRIORITY_PATH = "priority-urls.txt"
UA = "key-wayback/1.0 (+https://github.com/chl-kiev-ua/key-wayback)"

# Службові й персональні розділи — не архівуємо (там або нічого публічного, або дані користувачів)
EXCLUDE_PREFIXES = ("/admin", "/django-admin", "/accounts", "/api", "/graphql", "/static", "/__debug__")
EXCLUDE_QUERY = True  # сторінки з ?query=..., ?page=... — пропускаємо, щоб не плодити дублі

SPN_SAVE = "https://web.archive.org/save"
SPN_STATUS = "https://web.archive.org/save/status/"

session = requests.Session()
session.headers["User-Agent"] = UA


def log(msg):
    print(f"[{dt.datetime.utcnow():%H:%M:%S}] {msg}", flush=True)


# ---------- 1. Збір URL ----------

def normalize(url):
    url, _ = urldefrag(url)
    p = urlparse(url)
    if p.netloc != urlparse(SITE).netloc:
        return None
    if any(p.path.startswith(x) for x in EXCLUDE_PREFIXES):
        return None
    if EXCLUDE_QUERY and p.query:
        return None
    return f"{p.scheme}://{p.netloc}{p.path or '/'}"


def read_priority():
    urls = []
    if os.path.exists(PRIORITY_PATH):
        with open(PRIORITY_PATH, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    urls.append(urljoin(SITE + "/", line))
    return urls


def read_seeds():
    """URL з папки seeds/: будь-які .csv/.txt — напр. експорт звіту «Сторінки» з Search Console.
    Формат не важливий: беремо все, що схоже на посилання на наш домен."""
    import glob
    import re
    pattern = re.compile(re.escape(SITE) + r"[^\s\",;<>]*")
    urls = []
    for path in sorted(glob.glob("seeds/*.csv") + glob.glob("seeds/*.txt")):
        with open(path, encoding="utf-8-sig", errors="ignore") as f:
            urls += pattern.findall(f.read())
    return urls


def read_sitemap(url, depth=0):
    """Повертає список URL зі sitemap або sitemap index. None — якщо sitemap немає."""
    try:
        r = session.get(url, timeout=60)
    except requests.RequestException as e:
        log(f"sitemap недоступний: {e}")
        return None
    if r.status_code != 200 or b"<" not in r.content[:200]:
        return None
    try:
        root = ET.fromstring(r.content)
    except ET.ParseError:
        return None
    ns = {"s": "http://www.sitemaps.org/schemas/sitemap/0.9"}
    if root.tag.endswith("sitemapindex") and depth < 2:
        out = []
        for loc in root.findall("s:sitemap/s:loc", ns):
            out += read_sitemap(loc.text.strip(), depth + 1) or []
        return out
    return [loc.text.strip() for loc in root.findall("s:url/s:loc", ns)]


class LinkParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links = []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            href = dict(attrs).get("href")
            if href:
                self.links.append(href)


def crawl(start_urls):
    """Запасний варіант, якщо sitemap.xml немає: обхід сайту в ширину."""
    log(f"sitemap не знайдено — обходжу сайт (ліміт {CRAWL_LIMIT})")
    seen, queue, found = set(), deque([SITE + "/"] + list(start_urls)), []
    while queue and len(found) < CRAWL_LIMIT:
        url = queue.popleft()
        if url in seen:
            continue
        seen.add(url)
        try:
            r = session.get(url, timeout=30)
        except requests.RequestException:
            continue
        if r.status_code != 200 or "text/html" not in r.headers.get("Content-Type", ""):
            continue
        found.append(url)
        p = LinkParser()
        p.feed(r.text)
        for href in p.links:
            n = normalize(urljoin(url, href))
            if n and n not in seen:
                queue.append(n)
        time.sleep(0.3)  # не навантажувати продакшн
    return found


def site_is_up():
    """Перевіряє доступність і дописує результат у журнал доступності.
    Журнал ведеться завжди — і коли сайт працює, і коли ні: це фактичний запис відключень."""
    t0 = time.time()
    try:
        r = session.get(SITE + "/", timeout=30)
        up, detail = r.status_code < 500, f"HTTP {r.status_code}"
    except requests.RequestException as e:
        up, detail = False, type(e).__name__
    ms = int((time.time() - t0) * 1000)
    os.makedirs(os.path.dirname(AVAIL_PATH), exist_ok=True)
    new = not os.path.exists(AVAIL_PATH)
    with open(AVAIL_PATH, "a", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["checked_at_utc", "status", "detail", "response_ms"])
        w.writerow([dt.datetime.utcnow().strftime("%Y-%m-%d %H:%M"), "up" if up else "down", detail, ms])
    return up


# ---------- 2. Журнал і порядок ----------

def read_log():
    """url -> дата останнього успішного захоплення (ISO)."""
    last = {}
    if os.path.exists(LOG_PATH):
        with open(LOG_PATH, encoding="utf-8") as f:
            for row in csv.DictReader(f):
                if row["status"] in ("success", "already_recent"):
                    last[row["url"]] = max(last.get(row["url"], ""), row["date_utc"])
    return last


def append_log(rows):
    os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
    new = not os.path.exists(LOG_PATH)
    with open(LOG_PATH, "a", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["date_utc", "url", "status", "wayback_url", "detail"])
        if new:
            w.writeheader()
        w.writerows(rows)


def order(priority, discovered, last):
    pri = list(dict.fromkeys(priority))
    rest = [u for u in dict.fromkeys(discovered) if u not in set(pri)]
    rest.sort(key=lambda u: last.get(u, ""))  # ніколи не архівовані ("") — першими
    return pri + rest


# ---------- 3. Save Page Now 2 ----------

def auth_headers():
    return {"Accept": "application/json", "Authorization": f"LOW {ACCESS}:{SECRET}"}


class DailyLimit(Exception):
    pass


def submit(url, not_within):
    """Повертає (job_id, None) або (None, detail)."""
    detail = ""
    for attempt in range(3):
        try:
            r = session.post(
                SPN_SAVE,
                headers=auth_headers(),
                data={"url": url, "if_not_archived_within": not_within, "skip_first_archive": "1"},
                timeout=60,
            )
        except requests.RequestException as e:
            time.sleep(20)
            detail = str(e)
            continue
        if r.status_code == 429:
            log("архів просить зачекати (429) — пауза 60 с")
            time.sleep(60)
            detail = "429"
            continue
        try:
            data = r.json()
        except ValueError:
            return None, f"HTTP {r.status_code}"
        if "job_id" in data:
            return data["job_id"], None
        msg = data.get("message", "") or data.get("status_ext", "")
        if "daily" in msg.lower() or "too-many-daily" in msg:
            raise DailyLimit(msg)
        return None, msg[:200]
    return None, detail


def check(job_id):
    try:
        r = session.get(SPN_STATUS + job_id, headers=auth_headers(), timeout=60)
        return r.json()
    except (requests.RequestException, ValueError):
        return {"status": "pending"}


def run(queue, priority_set=frozenset()):
    start = time.time()
    pending = {}  # job_id -> (url, submitted_at)
    rows = []
    today = dt.datetime.utcnow().strftime("%Y-%m-%d")
    stats = {"success": 0, "already_recent": 0, "error": 0}
    stop_submitting = False

    def record(url, status, wb="", detail=""):
        stats[status] = stats.get(status, 0) + 1
        rows.append({"date_utc": today, "url": url, "status": status, "wayback_url": wb, "detail": detail})

    while queue or pending:
        # подаємо нові, поки є вільні слоти й час
        while queue and not stop_submitting and len(pending) < MAX_CONCURRENT:
            if time.time() - start > TIME_BUDGET:
                log("вичерпано бюджет часу — нові URL не подаю")
                stop_submitting = True
                break
            url = queue.popleft()
            try:
                job, detail = submit(url, PRIORITY_NOT_WITHIN if url in priority_set else NOT_WITHIN)
            except DailyLimit as e:
                log(f"досягнуто денного ліміту архіву: {e} — решта переноситься на наступний запуск")
                stop_submitting = True
                break
            if job:
                pending[job] = (url, time.time())
            elif "already" in (detail or "").lower() or "same snapshot" in (detail or "").lower():
                record(url, "already_recent", detail=detail)
            else:
                record(url, "error", detail=detail)
            time.sleep(2)

        if stop_submitting:
            queue.clear()

        # перевіряємо статуси
        time.sleep(8)
        for job, (url, t0) in list(pending.items()):
            s = check(job)
            st = s.get("status")
            if st == "success":
                wb = f"https://web.archive.org/web/{s.get('timestamp', '')}/{s.get('original_url', url)}"
                record(url, "success", wb)
                del pending[job]
            elif st == "error":
                ext = s.get("status_ext", "") or s.get("message", "")
                if "too-many-daily" in ext:
                    stop_submitting = True
                record(url, "error", detail=ext[:200])
                del pending[job]
            elif time.time() - t0 > 600:
                record(url, "error", detail="timeout 10 min")
                del pending[job]

    append_log(rows)
    log(f"готово: успішно {stats['success']}, вже свіжі {stats['already_recent']}, помилки {stats['error']}")
    return stats


def main():
    if not site_is_up():
        # Падіння запуску → GitHub надішле лист: це заодно найпростіший сигнал «КЛЮЧ недоступний»
        log(f"САЙТ НЕДОСТУПНИЙ: {SITE} — архівування пропущено, спробую наступним запуском")
        sys.exit(1)

    priority = read_priority()
    seeds = read_seeds()
    if seeds:
        log(f"seeds/: {len(seeds)} URL (Search Console тощо)")
    discovered = read_sitemap(SITE + "/sitemap.xml")
    if discovered:
        log(f"sitemap: {len(discovered)} URL")
    else:
        discovered = crawl(seeds)
    discovered = [n for n in (normalize(u) for u in discovered + seeds) if n]

    last = read_log()
    ordered = order(priority, discovered, last)
    batch = ordered[:MAX_URLS]
    never = sum(1 for u in ordered if u not in last)
    log(f"усього {len(ordered)} URL, ще не архівовано нами: {never}; у цьому запуску: {len(batch)}")

    if DRY_RUN:
        for u in batch:
            print(u)
        return

    if not (ACCESS and SECRET):
        log("немає IA_ACCESS_KEY / IA_SECRET_KEY у секретах репозиторію")
        sys.exit(2)

    run(deque(batch), frozenset(priority))


if __name__ == "__main__":
    main()

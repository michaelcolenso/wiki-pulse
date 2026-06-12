#!/usr/bin/env python3
"""Wikipedia Pageviews Collector — fetches daily top articles and stores in SQLite."""

import json
import sqlite3
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

API_BASE = "https://wikimedia.org/api/rest_v1/metrics/pageviews/top/en.wikipedia/all-access"
DB_PATH = Path(__file__).parent / "data" / "pageviews.db"
SKIP_PREFIXES = ("Special:", "Wikipedia:", "File:", "Template:", "Category:",
                 "Help:", "Portal:", "Talk:", "User:", "Draft:", "MediaWiki:",
                 "Main_Page", "Main Page")  # Main_Page is noise

USER_AGENT = "WikiPulse/1.0 (https://github.com/michaelcolenso/wiki-pulse; contact@michaelcolenso.com)"


def get_db():
    db = sqlite3.connect(str(DB_PATH))
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA synchronous=NORMAL")
    return db


def init_db(db):
    db.executescript("""
        CREATE TABLE IF NOT EXISTS daily_views (
            date TEXT NOT NULL,
            article TEXT NOT NULL,
            views INTEGER NOT NULL,
            rank INTEGER NOT NULL,
            PRIMARY KEY (date, article)
        );
        CREATE INDEX IF NOT EXISTS idx_daily_views_date ON daily_views(date);
        CREATE INDEX IF NOT EXISTS idx_daily_views_article ON daily_views(article);
        CREATE INDEX IF NOT EXISTS idx_daily_views_views ON daily_views(views);

        CREATE TABLE IF NOT EXISTS baselines (
            article TEXT PRIMARY KEY,
            avg_30d REAL,
            avg_90d REAL,
            median_30d INTEGER,
            median_90d INTEGER,
            max_90d INTEGER,
            sample_count_30d INTEGER,
            sample_count_90d INTEGER,
            last_spike_date TEXT,
            last_spike_multiple REAL,
            last_updated TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_baselines_avg ON baselines(avg_30d);

        CREATE TABLE IF NOT EXISTS daily_spikes (
            date TEXT NOT NULL,
            article TEXT NOT NULL,
            views INTEGER NOT NULL,
            avg_30d REAL,
            spike_multiple REAL,
            rank INTEGER,
            PRIMARY KEY (date, article)
        );
        CREATE TABLE IF NOT EXISTS collection_log (
            date TEXT PRIMARY KEY,
            articles_collected INTEGER,
            started_at TEXT,
            completed_at TEXT,
            error TEXT
        );
    """)


def fetch_day(target_date):
    """Fetch top articles for a specific date from Wikimedia API."""
    date_str = target_date.strftime("%Y/%m/%d")
    url = f"{API_BASE}/{date_str}"
    
    req = Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    
    try:
        with urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read())
    except HTTPError as e:
        if e.code == 404:
            print(f"  No data available for {target_date} (404)")
            return None
        raise
    
    if not data.get("items"):
        print(f"  No items in response for {target_date}")
        return None
    
    articles = data["items"][0].get("articles", [])
    return articles


def is_valid_article(name):
    """Filter out special pages and non-articles."""
    if not name or name.startswith(SKIP_PREFIXES):
        return False
    # Also skip obvious non-articles
    if ":" in name and name.startswith(SKIP_PREFIXES):
        return False
    return True


def collect_day(db, target_date):
    """Collect one day's top articles into the database."""
    date_key = target_date.isoformat()
    
    # Check if already collected
    existing = db.execute("SELECT 1 FROM collection_log WHERE date = ?", (date_key,)).fetchone()
    if existing:
        print(f"  Already collected {date_key}, skipping")
        return 0
    
    articles = fetch_day(target_date)
    if articles is None:
        db.execute(
            "INSERT OR REPLACE INTO collection_log VALUES (?, 0, ?, ?, 'no_data')",
            (date_key, datetime.now().isoformat(), datetime.now().isoformat())
        )
        db.commit()
        return 0
    
    # Filter and insert
    valid = [(a["article"], a["views"], a["rank"]) 
             for a in articles if is_valid_article(a["article"])]
    
    db.executemany(
        "INSERT OR IGNORE INTO daily_views VALUES (?, ?, ?, ?)",
        [(date_key, article, views, rank) for article, views, rank in valid]
    )
    
    db.execute(
        "INSERT OR REPLACE INTO collection_log VALUES (?, ?, ?, ?, NULL)",
        (date_key, len(valid), datetime.now().isoformat(), datetime.now().isoformat())
    )
    db.commit()
    
    return len(valid)


def backfill(db, days=30):
    """Collect the last N days of data."""
    today = date.today()
    total = 0
    for i in range(1, days + 1):
        target = today - timedelta(days=i)
        print(f"Collecting {target}...")
        count = collect_day(db, target)
        total += count
        if i < days:
            time.sleep(0.5)  # Be polite to the API
    return total


def main():
    init_db(get_db())  # Initialize schema
    db = get_db()
    init_db(db)
    
    if len(sys.argv) > 1 and sys.argv[1] == "--backfill":
        days = int(sys.argv[2]) if len(sys.argv) > 2 else 30
        print(f"Backfilling {days} days...")
        total = backfill(db, days)
        print(f"Done! Collected {total} articles across {days} days.")
        return
    
    # Default: collect yesterday
    yesterday = date.today() - timedelta(days=1)
    print(f"Collecting top articles for {yesterday}...")
    count = collect_day(db, yesterday)
    print(f"Done! Collected {count} articles for {yesterday}.")
    
    db.close()


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Spike Detection Engine — computes baselines and identifies anomalous pageviews."""

import json
import sqlite3
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

DB_PATH = Path(__file__).parent / "data" / "pageviews.db"
SPIKE_JSON = Path(__file__).parent / "dashboard" / "spikes.json"
HISTORY_JSON = Path(__file__).parent / "dashboard" / "history.json"

# Thresholds
MIN_BASELINE_VIEWS = 500       # Don't flag articles with tiny baselines
SPIKE_THRESHOLD_LOW = 3.0      # "Notable" spike (3x baseline)
SPIKE_THRESHOLD_MED = 8.0      # "Big" spike (8x baseline)
SPIKE_THRESHOLD_HIGH = 25.0    # "Massive" spike (25x baseline)
MIN_SPIKE_VIEWS = 2000         # Minimum absolute views to be considered a spike
MIN_SAMPLE_DAYS = 3            # Minimum data points needed to establish a baseline
LOW_SAMPLE_MULTIPLIER = 5.0    # For sparse articles (<14 data pts), use higher threshold


def get_db():
    return sqlite3.connect(str(DB_PATH))


def compute_baselines(db):
    """Update baseline statistics for all articles with recent data."""
    today = date.today()
    cutoff_90d = (today - timedelta(days=90)).isoformat()
    cutoff_30d = (today - timedelta(days=30)).isoformat()
    now_iso = datetime.now().isoformat()
    
    db.execute("DELETE FROM baselines")
    
    # Use simple averages (not median) — SQLite median via OFFSET is too slow at scale.
    # For spike detection, mean is good enough at this stage.
    db.execute("""
        INSERT INTO baselines (article, avg_30d, avg_90d, median_30d, median_90d, 
                                max_90d, sample_count_30d, sample_count_90d, last_updated)
        SELECT 
            article,
            ROUND(AVG(CASE WHEN date >= ? THEN views END), 1),
            ROUND(AVG(CASE WHEN date >= ? THEN views END), 1),
            NULL,  -- median not computed (performance)
            NULL,
            MAX(CASE WHEN date >= ? THEN views END),
            COUNT(CASE WHEN date >= ? THEN 1 END),
            COUNT(CASE WHEN date >= ? THEN 1 END),
            ?
        FROM daily_views dv
        WHERE date >= ?
        GROUP BY article
        HAVING COUNT(CASE WHEN date >= ? THEN 1 END) >= 3
    """, (cutoff_30d, cutoff_90d, cutoff_90d, cutoff_30d, cutoff_90d, now_iso, cutoff_90d, cutoff_30d))
    
    db.commit()


def detect_spikes(db, target_date=None):
    """Detect spike articles for a given date (default: yesterday)."""
    if target_date is None:
        target_date = (date.today() - timedelta(days=1)).isoformat()
    
    # Delete existing spikes for this date
    db.execute("DELETE FROM daily_spikes WHERE date = ?", (target_date,))
    
    # Find articles with anomalous views compared to their 30-day baseline
    rows = db.execute("""
        SELECT 
            dv.article,
            dv.views,
            dv.rank,
            b.avg_30d,
            b.avg_90d,
            b.max_90d,
            b.sample_count_30d,
            ROUND(CAST(dv.views AS REAL) / NULLIF(b.avg_30d, 0), 1) as spikex
        FROM daily_views dv
        LEFT JOIN baselines b ON dv.article = b.article
        WHERE dv.date = ?
          AND dv.views >= ?
          AND b.avg_30d IS NOT NULL
          AND b.avg_30d >= ?
        ORDER BY spikex DESC NULLS LAST
    """, (target_date, MIN_SPIKE_VIEWS, MIN_BASELINE_VIEWS))
    
    spikes = []
    for article, views, rank, avg_30d, avg_90d, max_90d, samples, spike_score in rows:
        if spike_score is None or spike_score < SPIKE_THRESHOLD_LOW:
            continue
        
        # For articles with sparse history (<14 data points), require a stronger signal
        # to avoid false positives from small sample sizes
        if samples < 14 and spike_score < LOW_SAMPLE_MULTIPLIER:
            continue
        
        tier = "low"
        if spike_score >= SPIKE_THRESHOLD_HIGH:
            tier = "high"
        elif spike_score >= SPIKE_THRESHOLD_MED:
            tier = "med"
        
        spikes.append({
            "article": article.replace("_", " "),
            "article_url": f"https://en.wikipedia.org/wiki/{article}",
            "views": views,
            "views_fmt": f"{views:,}",
            "rank": rank,
            "spike_multiple": spike_score,
            "spike_tier": tier,
            "baseline_30d": int(avg_30d),
            "baseline_90d": int(avg_90d) if avg_90d else None,
            "max_90d": max_90d,
            "sample_days": samples,
        })
    
    # Insert into daily_spikes table
    if spikes:
        db.executemany(
            "INSERT OR REPLACE INTO daily_spikes VALUES (?, ?, ?, ?, ?, ?)",
            [(target_date, s["article"], s["views"], s["baseline_30d"], 
              s["spike_multiple"], s["rank"]) for s in spikes]
        )
        db.commit()
    
    return spikes


def export_json(db, spikes, target_date):
    """Export spike data and historical trends to JSON for the dashboard."""
    # Current spikes
    payload = {
        "date": target_date,
        "fetched_at": datetime.now().isoformat(),
        "spikes": spikes,
    }
    
    with open(SPIKE_JSON, "w") as f:
        json.dump(payload, f, indent=2)
    
    # Historical spike feed (last 14 days)
    rows = db.execute("""
        SELECT ds.date, ds.article, ds.views, ds.spike_multiple, ds.rank,
               b.avg_30d
        FROM daily_spikes ds
        LEFT JOIN baselines b ON ds.article = b.article
        WHERE ds.date >= ?
        ORDER BY ds.date DESC, ds.spike_multiple DESC
        LIMIT 200
    """, ((date.today() - timedelta(days=14)).isoformat(),)).fetchall()
    
    history = []
    for d, article, views, sm, rank, avg_30d in rows:
        history.append({
            "date": d,
            "article": article,
            "article_url": f"https://en.wikipedia.org/wiki/{article.replace(' ', '_')}",
            "views": views,
            "views_fmt": f"{views:,}",
            "spike_multiple": round(sm, 1) if sm else None,
            "rank": rank,
            "baseline": round(avg_30d) if avg_30d else None,
        })
    
    with open(HISTORY_JSON, "w") as f:
        json.dump({"history": history}, f, indent=2)
    
    print(f"  Exported {len(spikes)} spikes to {SPIKE_JSON}")
    print(f"  Exported {len(history)} historical entries to {HISTORY_JSON}")


def main():
    db = get_db()
    
    target_date = None
    if len(sys.argv) > 1:
        target_date = sys.argv[1]  # YYYY-MM-DD
    
    print("Computing baselines...")
    compute_baselines(db)
    
    print("Detecting spikes...")
    if target_date:
        spikes = detect_spikes(db, target_date)
    else:
        yesterday = (date.today() - timedelta(days=1)).isoformat()
        spikes = detect_spikes(db, yesterday)
        target_date = yesterday
    
    # Group by tier
    high = [s for s in spikes if s["spike_tier"] == "high"]
    med = [s for s in spikes if s["spike_tier"] == "med"]
    low = [s for s in spikes if s["spike_tier"] == "low"]
    
    print(f"\n  🚨 MASSIVE (25x+):  {len(high)}")
    for s in high:
        print(f"    {s['article']} — {s['views_fmt']} views ({s['spike_multiple']}x baseline)")
    
    print(f"\n  📈 BIG (8x+):      {len(med)}")
    for s in med[:10]:
        print(f"    {s['article']} — {s['views_fmt']} views ({s['spike_multiple']}x baseline)")
    
    print(f"\n  👀 Notable (3x+):  {len(low)}")
    for s in low[:5]:
        print(f"    {s['article']} — {s['views_fmt']} views ({s['spike_multiple']}x baseline)")
    
    # Export
    export_json(db, spikes, target_date)
    
    # Show some stats
    stat = db.execute("""
        SELECT COUNT(DISTINCT date) as days, COUNT(*) as total, 
               MIN(date), MAX(date)
        FROM daily_views
    """).fetchone()
    print(f"\n  DB stats: {stat[0]} days, {stat[1]:,} rows ({stat[2]} → {stat[3]})")
    
    db.close()


if __name__ == "__main__":
    main()

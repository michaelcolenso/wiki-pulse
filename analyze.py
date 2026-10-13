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
MIN_SAMPLE_DAYS = 3            # Minimum data points for a standard 30-day baseline
LOW_SAMPLE_MULTIPLIER = 5.0    # For sparse low-volume articles (<14 data pts, <10k base), use higher threshold
HIGH_BASELINE_FLOOR = 10000    # Baselines >=10k are already in the global top-1000; 3.0x is a real surge
NEW_ENTRANT_MAX_RANK = 100     # Admit new entrants (<3 prior days) if they break into the global top 100
NEW_ENTRANT_MIN_VIEWS = 30000  # ...or clear 30,000 daily views
NEW_ENTRANT_MULTIPLIER = 4.0   # Brand-new entrants (0 prior days) must clear 4x the top-1000 daily floor


def get_db():
    return sqlite3.connect(str(DB_PATH))


def compute_baselines(db, target_date=None):
    """Update baseline statistics for all articles with data strictly prior to target_date."""
    if target_date is None:
        ref = date.today() - timedelta(days=1)
    else:
        ref = date.fromisoformat(target_date)
    target_iso = ref.isoformat()
    cutoff_90d = (ref - timedelta(days=90)).isoformat()
    cutoff_30d = (ref - timedelta(days=30)).isoformat()
    now_iso = datetime.now().isoformat()
    
    db.execute("DELETE FROM baselines")
    
    # Exclude target_date (< target_iso) so a one-day spike does not inflate its own
    # denominator, and keep articles with >=1 prior appearance so sparse/new-entrant
    # articles still have their prior average recorded alongside sample_count_30d.
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
        WHERE date >= ? AND date < ?
        GROUP BY article
    """, (cutoff_30d, cutoff_90d, cutoff_90d, cutoff_30d, cutoff_90d, now_iso, cutoff_90d, target_iso))
    
    db.commit()


def get_top1000_floor(db, target_date):
    """Average #1000 daily view count over the 30 days preceding target_date.

    Any article absent from the prior top-1000 had daily views below this ceiling,
    so it serves as a conservative upper-bound baseline for brand-new entrants.
    """
    ref = date.fromisoformat(target_date)
    cutoff_30d = (ref - timedelta(days=30)).isoformat()
    row = db.execute("""
        SELECT ROUND(AVG(min_views), 1)
        FROM (
            SELECT date, MIN(views) AS min_views
            FROM daily_views
            WHERE date >= ? AND date < ?
            GROUP BY date
        )
    """, (cutoff_30d, target_date)).fetchone()
    return float(row[0]) if row and row[0] else 8000.0


def detect_spikes(db, target_date=None):
    """Detect spike articles for a given date (default: yesterday)."""
    if target_date is None:
        target_date = (date.today() - timedelta(days=1)).isoformat()
    
    # Delete existing spikes for this date
    db.execute("DELETE FROM daily_spikes WHERE date = ?", (target_date,))
    
    top1000_floor = get_top1000_floor(db, target_date)
    
    # Pull candidate articles for target_date, including new entrants with no prior baseline row
    rows = db.execute("""
        SELECT 
            dv.article,
            dv.views,
            dv.rank,
            b.avg_30d,
            b.avg_90d,
            b.max_90d,
            COALESCE(b.sample_count_30d, 0) as sample_count_30d,
            COALESCE(b.sample_count_90d, 0) as sample_count_90d
        FROM daily_views dv
        LEFT JOIN baselines b ON dv.article = b.article
        WHERE dv.date = ?
          AND dv.views >= ?
    """, (target_date, MIN_SPIKE_VIEWS)).fetchall()
    
    spikes = []
    for article, views, rank, avg_30d, avg_90d, max_90d, samples_30d, samples_90d in rows:
        if samples_30d >= MIN_SAMPLE_DAYS and avg_30d is not None and avg_30d >= MIN_BASELINE_VIEWS:
            effective_baseline = float(avg_30d)
            # Only apply the 5.0x sparse-sample penalty when the baseline is small (<10k)
            # and absolute views are below 50k; established top-1000 pages (like holiday or
            # major figure pages) surging 3x+ to tens of thousands of views are real spikes.
            if samples_30d < 14 and effective_baseline < HIGH_BASELINE_FLOOR and views < 50000:
                min_multiple = LOW_SAMPLE_MULTIPLIER
            else:
                min_multiple = SPIKE_THRESHOLD_LOW
        else:
            # New entrant (<3 appearances in prior 30-day top-1000): admit only if it breaks
            # into the global top 100 or clears 30,000 views.
            if (rank is None or rank > NEW_ENTRANT_MAX_RANK) and views < NEW_ENTRANT_MIN_VIEWS:
                continue
            effective_baseline = max(float(avg_30d or 0), float(avg_90d or 0), top1000_floor)
            min_multiple = SPIKE_THRESHOLD_LOW if (samples_30d >= 1 or samples_90d >= 1) else NEW_ENTRANT_MULTIPLIER
        
        spike_score = round(views / effective_baseline, 1)
        if spike_score < min_multiple:
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
            "baseline_30d": int(round(effective_baseline)),
            "baseline_90d": int(round(avg_90d)) if avg_90d else None,
            "max_90d": max_90d,
            "sample_days": samples_30d,
        })
    
    spikes.sort(key=lambda s: (s["spike_multiple"], s["views"]), reverse=True)
    
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
    else:
        target_date = (date.today() - timedelta(days=1)).isoformat()
    
    print("Computing baselines...")
    compute_baselines(db, target_date)
    
    print("Detecting spikes...")
    spikes = detect_spikes(db, target_date)
    
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

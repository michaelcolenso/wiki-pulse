#!/usr/bin/env python3
"""WikiPulse Realtime Monitor — listens to Wikipedia's recentchange SSE stream and
detects edit bursts that precede pageview spikes."""

import json
import sqlite3
import signal
import sys
import time
from collections import defaultdict, deque
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock, Thread
from urllib.request import Request, urlopen

PROJECT_DIR = Path(__file__).parent
DB_PATH = PROJECT_DIR / "data" / "pageviews.db"
REALTIME_JSON = PROJECT_DIR / "dashboard" / "realtime.json"
STREAM_URL = "https://stream.wikimedia.org/v2/stream/recentchange"

# Burst thresholds
BURST_WINDOW_15M = 15 * 60      # 15 minutes in seconds
BURST_WINDOW_1H  = 60 * 60      # 1 hour
BURST_WINDOW_6H  = 6 * 60 * 60  # 6 hours

BURST_15M_MAJOR = 5    # 5+ non-minor edits in 15 min → notable
BURST_15M_ANY   = 10   # 10+ any edits in 15 min → notable
BURST_1H_MAJOR  = 12   # 12+ non-minor edits in 1 hour → breaking
BURST_6H_MAJOR  = 20   # 20+ non-minor edits in 6 hours → massive

# Cooldown: don't re-alert for same article within this many seconds
ALERT_COOLDOWN = 30 * 60  # 30 minutes

USER_AGENT = "WikiPulse/1.0 (https://github.com/michaelcolenso/wiki-pulse)"

# Track article edit windows: article -> deque of (timestamp, is_minor)
edit_windows: dict[str, deque] = defaultdict(lambda: deque(maxlen=500))
# Track when we last alerted for an article
last_alert: dict[str, float] = {}
# Tiers: article -> current tier
current_tiers: dict[str, str] = {}
# Lock for thread safety
lock = Lock()
# Running flag
running = True


def get_db():
    db = sqlite3.connect(str(DB_PATH))
    db.execute("PRAGMA journal_mode=WAL")
    return db


def init_db(db):
    db.execute("""
        CREATE TABLE IF NOT EXISTS edit_history (
            timestamp INTEGER NOT NULL,
            article TEXT NOT NULL,
            event_type TEXT NOT NULL,
            is_minor INTEGER NOT NULL DEFAULT 0,
            user_name TEXT
        )
    """)
    db.execute("CREATE INDEX IF NOT EXISTS idx_edit_ts ON edit_history(timestamp)")
    db.execute("CREATE INDEX IF NOT EXISTS idx_edit_article ON edit_history(article)")
    db.commit()


def prune_windows():
    """Remove old entries from all article windows."""
    now = time.time()
    with lock:
        for article in list(edit_windows.keys()):
            dq = edit_windows[article]
            # Remove entries older than 6 hours
            while dq and (now - dq[0][0]) > BURST_WINDOW_6H:
                dq.popleft()
            # Remove empty deques
            if not dq:
                del edit_windows[article]
                if article in current_tiers:
                    del current_tiers[article]


def count_edits(article: str, window_sec: float, major_only: bool = True) -> int:
    """Count edits for an article within the given time window."""
    dq = edit_windows.get(article)
    if not dq:
        return 0
    
    cutoff = time.time() - window_sec
    count = 0
    for ts, is_minor in dq:
        if ts < cutoff:
            continue
        if major_only and is_minor:
            continue
        count += 1
    return count


def detect_burst(article: str) -> tuple[str | None, int, int]:
    """Detect if an article has an edit burst. Returns (tier, edit_count, window_size)."""
    major_15m = count_edits(article, BURST_WINDOW_15M, major_only=True)
    any_15m = count_edits(article, BURST_WINDOW_15M, major_only=False)
    major_1h = count_edits(article, BURST_WINDOW_1H, major_only=True)
    major_6h = count_edits(article, BURST_WINDOW_6H, major_only=True)
    
    if major_6h >= BURST_6H_MAJOR:
        return ("high", major_6h, 360)
    if major_1h >= BURST_1H_MAJOR:
        return ("med", major_1h, 60)
    if major_15m >= BURST_15M_MAJOR:
        return ("med", major_15m, 15)
    if any_15m >= BURST_15M_ANY:
        return ("low", any_15m, 15)
    
    return (None, 0, 0)


def should_alert(article: str, tier: str) -> bool:
    """Check if we should alert for this article/tier combination."""
    now = time.time()
    current = current_tiers.get(article)
    
    # Always alert if tier upgraded (low→med, med→high)
    tier_order = {"low": 0, "med": 1, "high": 2}
    if current and tier_order.get(tier, -1) <= tier_order.get(current, -1):
        # Same or lower tier — check cooldown
        last = last_alert.get(article, 0)
        if now - last < ALERT_COOLDOWN:
            return False
    
    return True


def write_realtime_json():
    """Write current burst state to JSON for dashboard."""
    with lock:
        bursts = []
        for article, tier in sorted(current_tiers.items()):
            any_15m = count_edits(article, BURST_WINDOW_15M, major_only=False)
            major_1h = count_edits(article, BURST_WINDOW_1H, major_only=True)
            bursts.append({
                "article": article.replace("_", " "),
                "article_url": f"https://en.wikipedia.org/wiki/{article}",
                "tier": tier,
                "edits_15m": any_15m,
                "edits_1h": major_1h,
                "detected_at": datetime.now(timezone.utc).isoformat(),
            })
        
        payload = {
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "listening_since": getattr(write_realtime_json, "start_time", None),
            "active_bursts": len(bursts),
            "bursts": bursts[:30],  # top 30
        }
    
    with open(REALTIME_JSON, "w") as f:
        json.dump(payload, f, indent=2)


def flush_to_db(db, batch):
    """Write accumulated edits to SQLite."""
    if not batch:
        return
    db.executemany(
        "INSERT INTO edit_history VALUES (?, ?, ?, ?, ?)",
        batch
    )
    db.commit()
    batch.clear()


def process_event(db, data: dict):
    """Process a single recentchange event."""
    wiki = data.get("wiki")
    if wiki != "enwiki":
        return
    
    ns = data.get("namespace")
    if ns != 0:  # Only main articles (not Talk, User, Draft, etc.)
        return
    
    title = data.get("title", "")
    if not title:
        return
    
    event_type = data.get("type", "edit")
    is_bot = data.get("bot", False)
    is_minor = data.get("minor", False)
    timestamp = data.get("timestamp", int(time.time()))
    user = data.get("user", "")
    
    # Skip bot edits
    if is_bot:
        return
    
    # Skip category-only changes (type="categorize")
    if event_type == "categorize":
        return
    
    # Record in window
    with lock:
        edit_windows[title].append((timestamp, is_minor))
    
    # Detect burst
    tier, count, window_min = detect_burst(title)
    
    if tier and should_alert(title, tier):
        with lock:
            current_tiers[title] = tier
            last_alert[title] = time.time()
        
        display_name = title.replace("_", " ")
        tier_emoji = {"high": "🚨", "med": "📈", "low": "👀"}
        label = {"high": "MASSIVE BURST", "med": "Edit surge", "low": "Heating up"}
        
        # New page is separately interesting
        prefix = "🆕 NEW PAGE: " if event_type == "new" else ""
        
        print(f"\n{tier_emoji[tier]} [{label[tier]}] {prefix}{display_name}")
        print(f"   {count} edits in {window_min}min · {datetime.now().strftime('%H:%M:%S UTC')}")
        print(f"   https://en.wikipedia.org/wiki/{title}")
        sys.stdout.flush()
        
        write_realtime_json()
    
    # Return event for batch DB flush
    return (timestamp, title, event_type, 1 if is_minor else 0, user)


def listen_stream():
    """Main SSE listener loop."""
    db = get_db()
    init_db(db)
    
    batch = []
    last_flush = time.time()
    last_prune = time.time()
    last_json = time.time()
    event_count = 0
    start_time = datetime.now(timezone.utc).isoformat()
    write_realtime_json.start_time = start_time
    
    print(f"📡 WikiPulse Realtime Monitor starting...")
    print(f"   Listening: {STREAM_URL}")
    print(f"   Output:    {REALTIME_JSON}")
    print(f"   DB:        {DB_PATH}")
    print()
    
    while running:
        try:
            req = Request(STREAM_URL, headers={
                "User-Agent": USER_AGENT,
                "Accept": "text/event-stream",
            })
            
            with urlopen(req, timeout=90) as resp:
                print(f"✓ Connected to EventStreams ({resp.status})")
                
                buffer = ""
                for chunk in iter(lambda: resp.read(4096), b""):
                    if not running:
                        break
                    
                    buffer += chunk.decode("utf-8", errors="replace")
                    
                    # Parse SSE lines
                    while "\n\n" in buffer:
                        message, buffer = buffer.split("\n\n", 1)
                        
                        data_line = None
                        for line in message.split("\n"):
                            if line.startswith("data:"):
                                data_line = line[5:].strip()
                                break
                        
                        if data_line:
                            try:
                                event = json.loads(data_line)
                                result = process_event(db, event)
                                if result:
                                    batch.append(result)
                                event_count += 1
                            except json.JSONDecodeError:
                                pass
                    
                    # Periodic maintenance
                    now = time.time()
                    
                    if len(batch) >= 500 or (now - last_flush) > 60:
                        flush_to_db(db, batch)
                        last_flush = now
                    
                    if now - last_prune > 300:  # Every 5 min
                        prune_windows()
                        last_prune = now
                    
                    if now - last_json > 30:  # Every 30 sec
                        write_realtime_json()
                        last_json = now
                    
                    # Heartbeat
                    if event_count % 5000 == 0 and event_count > 0:
                        with lock:
                            tracked = len(edit_windows)
                        print(f"  ♥ {event_count:,} events processed · tracking {tracked} articles")
                        sys.stdout.flush()
        
        except Exception as e:
            if not running:
                break
            print(f"⚠ Connection lost: {e}")
            print(f"  Reconnecting in 5s...")
            sys.stdout.flush()
            time.sleep(5)
    
    # Cleanup
    flush_to_db(db, batch)
    db.close()
    print("✓ Monitor stopped.")


def main():
    def shutdown(signum, frame):
        global running
        running = False
        print("\n⏸ Shutting down...")
    
    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)
    
    listen_stream()


if __name__ == "__main__":
    main()

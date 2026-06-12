#!/usr/bin/env python3
"""Context Note Generator — enriches spikes.json with human-readable "why it's spiking" notes.

Fetches Wikipedia page summaries for each spike article, then uses heuristics to
generate a short explanation of what's driving the traffic. Falls back gracefully.
"""

import json
import re
import sys
import time
import urllib.request
import urllib.parse
from pathlib import Path

SPIKE_JSON = Path(__file__).parent / "dashboard" / "spikes.json"
WIKI_SUMMARY_API = "https://en.wikipedia.org/api/rest_v1/page/summary/"

# ── Signal dictionaries for heuristic classification ──

DEATH_SIGNALS = [
    r"\bdied\b", r"\bdeath\b", r"\bpassed away\b", r"\bmurdered\b",
    r"\bkilled\b", r"\bassassinated\b", r"\bfuneral\b", r"\bmemorial\b",
    r"\bobituary\b", r"was a[n]?\b.*\bwho\b"
]

RELEASE_SIGNALS = [
    r"\breleased\b", r"\bpremiered?\b", r"\bdebut\b", r"\blaunch\b",
    r"\balbum\b", r"\bsingle\b", r"\bfilm\b", r"\bmovie\b", r"\bseries\b",
    r"\bseason\b", r"\bepisode\b"
]

SPORT_SIGNALS = [
    r"\b(?:NBA|NFL|MLB|NHL|Premier League|La Liga|Serie A)\b",
    r"\bchampionship\b", r"\bplayoffs?\b", r"\btournament\b",
    r"\b(?:final|finals)\b", r"\bFIFA\b", r"\bWorld Cup\b",
    r"\b(?:soccer|football|basketball|tennis|baseball)\b",
    r"\bGrand Prix\b", r"\bUFC\b", r"\bWWE\b"
]

POLITICS_SIGNALS = [
    r"\belection\b", r"\bpresident\b", r"\bprime minister\b",
    r"\bcongress\b", r"\bsenate\b", r"\bparliament\b", r"\bgovernor\b",
    r"\bsupreme court\b", r"\bscandal\b", r"\bresigned\b"
]

DISASTER_SIGNALS = [
    r"\bearthquake\b", r"\btsunami\b", r"\bhurricane\b", r"\btornado\b",
    r"\bflood\b", r"\bwildfire\b", r"\beruption\b", r"\bcrash\b",
    r"\bexplosion\b", r"\battack\b", r"\bshooting\b"
]

ANNOUNCEMENT_SIGNALS = [
    r"\bannounced\b", r"\brevealed\b", r"\bconfirmed\b", r"\bunveiled\b",
    r"\bawarded\b", r"\bnominated\b", r"\bwon\b"
]


def fetch_summary(article):
    """Fetch Wikipedia page summary for a given article title."""
    encoded = urllib.parse.quote(article.replace(" ", "_"))
    url = WIKI_SUMMARY_API + encoded
    
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "WikiPulse/1.0 (wikispike.xyz)"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())
            return {
                "description": data.get("description", ""),
                "extract": data.get("extract", ""),
            }
    except Exception:
        return None


def detect_signals(text):
    """Scan text for signal categories. Returns dict of category → [matched phrases]."""
    text_lower = text.lower()
    signals = {}
    
    categories = {
        "death": DEATH_SIGNALS,
        "release": RELEASE_SIGNALS,
        "sport": SPORT_SIGNALS,
        "politics": POLITICS_SIGNALS,
        "disaster": DISASTER_SIGNALS,
        "announcement": ANNOUNCEMENT_SIGNALS,
    }
    
    for category, patterns in categories.items():
        for pattern in patterns:
            match = re.search(pattern, text_lower)
            if match:
                signals.setdefault(category, []).append(match.group(0))
    
    return signals


def classify_article_type(description, extract):
    """Determine what kind of article this is from its description."""
    text = (description + " " + extract).lower()
    
    if re.search(r"\b(?:singer|musician|rapper|band|guitarist|drummer|composer)\b", text):
        return "musician"
    if re.search(r"\b(?:actor|actress|filmmaker|director|comedian)\b", text):
        return "entertainer"
    if re.search(r"\b(?:athlete|player|coach|champion|boxer|runner)\b", text):
        return "athlete"
    if re.search(r"\b(?:politician|senator|president|minister|governor)\b", text):
        return "politician"
    if re.search(r"\b(?:film|movie|television series|TV series|show)\b", text):
        return "media"
    if re.search(r"\b(?:song|album|single|record)\b", text):
        return "music"
    if re.search(r"\b(?:team|club|league|championship|tournament)\b", text):
        return "sports_entity"
    if re.search(r"\b(?:company|corporation|organization|foundation)\b", text):
        return "organization"
    if re.search(r"\b(?:country|city|state|province|region|nation)\b", text):
        return "place"
    if re.search(r"\b(?:war|battle|conflict|revolution|movement)\b", text):
        return "historical_event"
    
    return "general"


def generate_context_note(article, spike_multiple, description, extract, article_type, signals):
    """Generate a human-readable context note explaining the spike."""
    magnitude = "massive" if spike_multiple >= 25 else ("significant" if spike_multiple >= 8 else "notable")
    
    # Priority-ordered context generation
    
    # 1. Death-related spike
    if "death" in signals:
        return f"{'Recently passed away' if article_type == 'person' else 'Recent death connected to this topic'}, driving a {magnitude} surge in Wikipedia searches."
    
    # 2. Sports events
    if "sport" in signals and article_type in ("athlete", "sports_entity"):
        if article_type == "athlete":
            return f"Featured in a major sporting event, triggering a {magnitude} spike in pageviews."
        else:
            return f"Major sporting event involving this team/competition is driving a {magnitude} surge in searches."
    
    # 3. Disaster/breaking news
    if "disaster" in signals:
        return f"Related to a breaking news event, causing a {magnitude} spike in Wikipedia pageviews."
    
    # 4. Release/premiere
    if "release" in signals and article_type in ("musician", "entertainer", "music", "media"):
        return f"Recent release or premiere is driving a {magnitude} surge in searches."
    
    # 5. Political event
    if "politics" in signals:
        return f"In the news for political developments, triggering a {magnitude} spike in pageviews."
    
    # 6. Announcement
    if "announcement" in signals:
        return f"Recent announcement or reveal is driving a {magnitude} surge in interest."
    
    # 7. Type-based fallback with description
    type_notes = {
        "musician": f"In the spotlight — a {magnitude} increase in Wikipedia searches suggests a recent event or release.",
        "entertainer": f"Making headlines — a {magnitude} surge in pageviews points to trending news or appearance.",
        "athlete": f"In the game — a {magnitude} spike in searches suggests recent athletic performance or news.",
        "politician": f"In the political spotlight — a {magnitude} increase in Wikipedia searches.",
        "media": f"Trending in entertainment — a {magnitude} surge in pageviews.",
        "sports_entity": f"On the field — a {magnitude} spike in searches for this team or event.",
        "place": f"In the news — a {magnitude} increase in searches for this location.",
        "organization": f"Making waves — a {magnitude} surge in Wikipedia pageviews.",
        "historical_event": f"Relevant again — a {magnitude} spike in searches for this historical topic.",
    }
    
    fallback = type_notes.get(article_type)
    if fallback:
        return fallback
    
    # 8. If we have a description, use it as a weak signal
    if description:
        return f"{description}. Currently experiencing a {magnitude} surge in Wikipedia pageviews."
    
    return f"Experiencing a {magnitude} surge in Wikipedia pageviews."


def enrich_spikes():
    """Read spikes.json, generate context notes, write back."""
    if not SPIKE_JSON.exists():
        print(f"  spikes.json not found at {SPIKE_JSON}", file=sys.stderr)
        return
    
    with open(SPIKE_JSON) as f:
        data = json.load(f)
    
    spikes = data.get("spikes", [])
    if not spikes:
        print("  No spikes to enrich.")
        return
    
    print(f"  Generating context notes for {len(spikes)} spikes...")
    
    enriched = 0
    for s in spikes:
        article = s.get("article", "")
        if not article:
            continue
        
        summary = fetch_summary(article)
        if not summary:
            s["context_note"] = None
            continue
        
        description = summary.get("description", "")
        extract = summary.get("extract", "")
        
        signals = detect_signals(description + " " + extract)
        article_type = classify_article_type(description, extract)
        
        note = generate_context_note(
            article, s.get("spike_multiple", 0),
            description, extract, article_type, signals
        )
        
        s["context_note"] = note
        enriched += 1
        
        if enriched % 5 == 0:
            print(f"    ... {enriched}/{len(spikes)}")
        
        time.sleep(0.15)  # Be nice to Wikipedia's API
    
    # Also update history.json with context notes
    with open(SPIKE_JSON, "w") as f:
        json.dump(data, f, indent=2)
    
    print(f"  Enriched {enriched}/{len(spikes)} spikes with context notes → {SPIKE_JSON}")


if __name__ == "__main__":
    enrich_spikes()

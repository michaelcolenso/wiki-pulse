#!/usr/bin/env python3
"""Context Note Generator — fetches Wikipedia summaries and builds context data.

Generates heuristic fallback context_notes and saves structured context data
as context-data.json for Hermes to enrich with LLM-powered explanations.

Two output files:
  spikes.json       — heuristic context_notes (standalone fallback)
  context-data.json — raw data for Hermes LLM enrichment
"""

import json
import re
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

PROJECT_DIR = Path(__file__).parent
SPIKE_JSON = PROJECT_DIR / "dashboard" / "spikes.json"
CONTEXT_DATA_JSON = PROJECT_DIR / "dashboard" / "context-data.json"
WIKI_SUMMARY_API = "https://en.wikipedia.org/api/rest_v1/page/summary/"

# ── Signal dictionaries for heuristic classification (fallback) ──

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
    """Scan text for signal categories. Returns dict of category -> [matched phrases]."""
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


def build_cluster_context(article, all_spikes, summaries):
    """Build a brief context string of other articles spiking in the same cluster today.

    Uses description + extract + article title to find related articles sharing
    teams, leagues, events, or proper nouns.
    """
    this_summary = summaries.get(article, {})
    this_body = (this_summary.get("description", "") + " " + this_summary.get("extract", "")).lower()
    # Also use article title for entity extraction (catches cases where extract is sparse)
    # Do NOT lowercase the title — we need original case for proper noun detection
    this_title_text = article + " " + this_body

    # Extract proper nouns and known acronyms
    this_entities = set()
    for term in this_title_text.split():
        if term[0].isupper() and len(term) > 3:
            this_entities.add(term.lower())
    for match in re.findall(r'\b(NBA|NFL|MLB|NHL|EPL|UFC|WWE|FIFA|UEFA|F1|WBC|ICC)\b', this_title_text, re.IGNORECASE):
        this_entities.add(match.lower())

    related = []
    for s in all_spikes:
        other = s.get("article", "")
        if other == article:
            continue
        other_summary = summaries.get(other, {})
        other_body = (other_summary.get("description", "") + " " + other_summary.get("extract", "")).lower()
        other_title_text = other + " " + other_body

        other_entities = set()
        for term in other_title_text.split():
            if term[0].isupper() and len(term) > 3:
                other_entities.add(term.lower())
        for match in re.findall(r'\b(NBA|NFL|MLB|NHL|EPL|UFC|WWE|FIFA|UEFA|F1|WBC|ICC)\b', other_title_text, re.IGNORECASE):
            other_entities.add(match.lower())

        shared = this_entities & other_entities
        if shared:
            other_extract = other_summary.get("extract", "")
            snippet = other_extract[:500] if other_extract else ""
            if snippet:
                first_sentences = ". ".join(snippet.split(". ")[:4])
                if not first_sentences.endswith("."):
                    first_sentences += "."
                related.append(f"{other}: {first_sentences}")
            else:
                related.append(f"{other} ({other_summary.get('description', '')})")

    if not related:
        return ""

    return "Also spiking today: " + "; ".join(related[:3])


def generate_context_note_fallback(article, spike_multiple, description, extract, article_type, signals):
    """Heuristic fallback — template-based context generation."""
    magnitude = "massive" if spike_multiple >= 25 else ("significant" if spike_multiple >= 8 else "notable")

    if "death" in signals:
        death_summary = _extract_death_detail(article, extract)
        if death_summary:
            return death_summary
        return f"{'Recently passed away' if article_type == 'person' else 'Recent death connected to this topic'}, driving a {magnitude} surge in Wikipedia searches."

    if "sport" in signals and article_type in ("athlete", "sports_entity"):
        sport_detail = _extract_sport_detail(article, description, extract)
        if sport_detail:
            return sport_detail
        if article_type == "athlete":
            return f"Featured in a major sporting event, triggering a {magnitude} spike in pageviews."
        else:
            return f"Major sporting event involving this team/competition is driving a {magnitude} surge in searches."

    if "disaster" in signals:
        return f"Related to a breaking news event, causing a {magnitude} spike in Wikipedia pageviews."

    if "release" in signals and article_type in ("musician", "entertainer", "music", "media"):
        return f"Recent release or premiere is driving a {magnitude} surge in searches."

    if "politics" in signals:
        return f"In the news for political developments, triggering a {magnitude} spike in pageviews."

    if "announcement" in signals:
        return f"Recent announcement or reveal is driving a {magnitude} surge in interest."

    type_notes = {
        "musician": f"In the spotlight \u2014 a {magnitude} increase in Wikipedia searches suggests a recent event or release.",
        "entertainer": f"Making headlines \u2014 a {magnitude} surge in pageviews points to trending news or appearance.",
        "athlete": f"In the game \u2014 a {magnitude} spike in searches suggests recent athletic performance or news.",
        "politician": f"In the political spotlight \u2014 a {magnitude} increase in Wikipedia searches.",
        "media": f"Trending in entertainment \u2014 a {magnitude} surge in pageviews.",
        "sports_entity": f"On the field \u2014 a {magnitude} spike in searches for this team or event.",
        "place": f"In the news \u2014 a {magnitude} increase in searches for this location.",
        "organization": f"Making waves \u2014 a {magnitude} surge in Wikipedia pageviews.",
        "historical_event": f"Relevant again \u2014 a {magnitude} spike in searches for this historical topic.",
    }

    fallback = type_notes.get(article_type)
    if fallback:
        return fallback

    if description:
        return f"{description}. Currently experiencing a {magnitude} surge in Wikipedia pageviews."

    return f"Experiencing a {magnitude} surge in Wikipedia pageviews."


def _extract_death_detail(article, extract):
    """Try to extract a specific death-related statement from the extract."""
    lines = extract.split(". ")
    for line in lines:
        if re.search(r"\b(died|death|passed away|killed|assassinated)\b", line, re.IGNORECASE):
            short = line.strip()[:180]
            if short.endswith(","):
                short = short[:-1]
            return short
    return None


def _extract_sport_detail(article, description, extract):
    """Try to extract a specific sport-related statement from the extract."""
    lines = extract.split(". ")
    for line in lines:
        if re.search(r"\b(coach|manager|head coach|player for|plays for|member of|owner of)\b", line, re.IGNORECASE):
            short = line.strip()[:180]
            if short and not short.endswith("."):
                short += "."
            return short
    return None


def enrich_spikes():
    """Read spikes.json, fetch Wikipedia summaries, write heuristic notes + context data."""
    if not SPIKE_JSON.exists():
        print(f"  spikes.json not found at {SPIKE_JSON}", file=sys.stderr)
        return

    with open(SPIKE_JSON) as f:
        data = json.load(f)

    spikes = data.get("spikes", [])
    if not spikes:
        print("  No spikes to enrich.")
        return

    date = data.get("date", "unknown")
    print(f"  Fetching Wikipedia summaries for {len(spikes)} spikes ({date})...")

    # Clear existing context_notes so we regenerate everything
    for s in spikes:
        s.pop("context_note", None)

    # Pass 1: fetch all summaries
    summaries = {}
    for s in spikes:
        article = s.get("article", "")
        if not article:
            continue
        summary = fetch_summary(article)
        if summary:
            summaries[article] = summary
            s["_description"] = summary.get("description", "")
            s["_extract"] = summary.get("extract", "")
        else:
            s["context_note"] = None

    print(f"    Fetched {len(summaries)}/{len(spikes)} summaries")

    # Pass 2: generate heuristic context notes (standalone fallback)
    enriched_heuristic = 0
    enriched_llm_ready = 0
    context_entries = []

    for s in spikes:
        article = s.get("article", "")
        if not article or s.get("context_note") is not None:
            continue

        description = s.get("_description", "")
        extract = s.get("_extract", "")
        if not description and not extract:
            continue
        spike_multiple = s.get("spike_multiple", 0)

        # Build cluster context for this article
        cluster_text = build_cluster_context(article, spikes, summaries)

        signals = detect_signals(description + " " + extract)
        article_type = classify_article_type(description, extract)

        # Heuristic fallback note (always written)
        note = generate_context_note_fallback(
            article, spike_multiple,
            description, extract, article_type, signals
        )
        s["context_note"] = note
        enriched_heuristic += 1

        # Build structured entry for Hermes LLM enrichment
        context_entries.append({
            "article": article,
            "article_url": s.get("article_url", ""),
            "views": s.get("views", 0),
            "views_fmt": s.get("views_fmt", ""),
            "rank": s.get("rank", 0),
            "spike_multiple": spike_multiple,
            "spike_tier": s.get("spike_tier", ""),
            "baseline_30d": s.get("baseline_30d", 0),
            "wikipedia_description": description,
            "wikipedia_extract": extract[:2000],
            "cluster_context": cluster_text,
        })
        enriched_llm_ready += 1

        if enriched_heuristic % 5 == 0:
            print(f"    ... {enriched_heuristic}/{len(spikes)} heuristic notes generated")

        time.sleep(0.15)

    # Save context-data.json for Hermes enrichment
    context_data = {
        "date": date,
        "fetched_at": data.get("fetched_at", ""),
        "total_spikes": len(spikes),
        "entries": context_entries,
    }
    with open(CONTEXT_DATA_JSON, "w") as f:
        json.dump(context_data, f, indent=2)

    # Save heuristic notes to spikes.json
    for s in spikes:
        s.pop("_description", None)
        s.pop("_extract", None)

    with open(SPIKE_JSON, "w") as f:
        json.dump(data, f, indent=2)

    print(f"  Heuristic notes written to {SPIKE_JSON}")
    print(f"  Context data written to {CONTEXT_DATA_JSON} ({len(context_entries)} entries)")
    print(f"\n  -> Hermes enrichment: run post_process with the enricher prompt on context-data.json")

    if spikes:
        print(f"\n  Sample heuristic notes:")
        for s in spikes[:5]:
            print(f"    \u2022 {s['article']}: {s.get('context_note', 'N/A')}")


if __name__ == "__main__":
    enrich_spikes()

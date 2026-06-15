#!/usr/bin/env python3
"""Context Note Generator — enriches spikes.json with specific "why it's spiking" explanations.

Fetches Wikipedia page summaries for each spike article, then uses an LLM (Anthropic Claude)
to generate a concise, specific explanation. Falls back to heuristics when the LLM is unavailable.
"""

import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

SPIKE_JSON = Path(__file__).parent / "dashboard" / "spikes.json"
WIKI_SUMMARY_API = "https://en.wikipedia.org/api/rest_v1/page/summary/"
LLM_MODEL = "claude-sonnet-4-20250514"

# ── Heuristic signal dictionaries (fallback) ──

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
    
    Uses description and extract text to find related articles sharing teams, leagues,
    events, or proper nouns.
    """
    this_summary = summaries.get(article, {})
    this_text = (this_summary.get("description", "") + " " + this_summary.get("extract", "")).lower()
    
    # Extract team/league/event proper nouns from the article's text
    # Look for NBA teams, leagues, competitions, proper names
    this_entities = set()
    for term in this_text.split():
        # Catch proper nouns: NBA team names, league names, event names
        if term[0].isupper() and len(term) > 3:
            this_entities.add(term.lower())
    # Also catch known league acronyms
    for match in re.findall(r'\b(NBA|NFL|MLB|NHL|EPL|UFC|WWE|FIFA|UEFA|F1|WBC|ICC)\b', this_text, re.IGNORECASE):
        this_entities.add(match.lower())
    
    related = []
    for s in all_spikes:
        other = s.get("article", "")
        if other == article:
            continue
        other_summary = summaries.get(other, {})
        other_text = (other_summary.get("description", "") + " " + other_summary.get("extract", "")).lower()
        
        other_entities = set()
        for term in other_text.split():
            if term[0].isupper() and len(term) > 3:
                other_entities.add(term.lower())
        for match in re.findall(r'\b(NBA|NFL|MLB|NHL|EPL|UFC|WWE|FIFA|UEFA|F1|WBC|ICC)\b', other_text, re.IGNORECASE):
            other_entities.add(match.lower())
        
        # Check if they share meaningful entities
        shared = this_entities & other_entities
        if shared:
            other_extract = other_summary.get("extract", "")
            # Include enough extract to capture key event mentions
            snippet = other_extract[:500] if other_extract else ""
            if snippet:
                # Grab first few sentences to capture event mentions  
                first_sentences = ". ".join(snippet.split(". ")[:4])
                if not first_sentences.endswith("."):
                    first_sentences += "."
                related.append(f"{other}: {first_sentences}")
            else:
                related.append(f"{other} ({other_summary.get('description', '')})")
    
    if not related:
        return ""
    
    return "Also spiking today: " + "; ".join(related[:3])


def generate_context_note_llm(article, description, extract, api_key, spike_multiple, cluster_context=""):
    """Use Anthropic Claude to generate a specific, informative context note."""
    if not api_key:
        return None

    truncated_extract = extract[:1500] if extract else ""

    cluster_section = f"\n{cluster_context}\n" if cluster_context else ""

    prompt = f"""You are the context writer for WikiPulse (wikispike.xyz), a site that tracks trending Wikipedia pages.

The article below is spiking at {spike_multiple}x its normal pageview baseline today.

Article: {article}
Wikipedia description: {description}
Wikipedia extract: {truncated_extract}
{cluster_section}
Write ONE tight factual sentence: identify WHAT the subject is, then say WHY it's surging.

Rules:
- ONE sentence only. Direct, journalistic tone. No markdown, no quotes, no "people are searching for" framing.
- Start with who/what this is (be specific: "head coach of the New York Knicks" not "American basketball coach"), then the reason.
- If the extract mentions a notable achievement, award, championship, or event — USE IT explicitly.
- If other articles are spiking simultaneously (listed above) and they mention a specific event (like a championship win), you CAN and SHOULD connect your article to that same event. This is not fabrication — it's using the spike cluster as a cross-reference.
- Example: if this article says "head coach of the Knicks" and another spiking article says "won the NBA championship with the Knicks in 2026", you can say "head coach of the New York Knicks, surging after the team won the NBA championship."
- NEVER invent facts not supported by the extract or the cluster context.
- Keep it under 200 characters."""

    try:
        import anthropic
        client = anthropic.Anthropic(api_key=api_key)
        response = client.messages.create(
            model=LLM_MODEL,
            max_tokens=100,
            temperature=0.2,
            messages=[{"role": "user", "content": prompt}],
        )
        note = response.content[0].text.strip()
        # Clean up stray quotes
        note = note.strip('"').strip("'").strip()
        if note.endswith('"'):
            note = note[:-1].strip()
        if note.endswith("'"):
            note = note[:-1].strip()
        return note
    except Exception as e:
        print(f"    LLM error for '{article}': {e}", file=sys.stderr)
        return None


def generate_context_note_fallback(article, spike_multiple, description, extract, article_type, signals):
    """Heuristic fallback — original template-based context generation."""
    magnitude = "massive" if spike_multiple >= 25 else ("significant" if spike_multiple >= 8 else "notable")

    # 1. Death-related spike
    if "death" in signals:
        death_summary = _extract_death_detail(article, extract)
        if death_summary:
            return death_summary
        return f"{'Recently passed away' if article_type == 'person' else 'Recent death connected to this topic'}, driving a {magnitude} surge in Wikipedia searches."

    # 2. Sports events
    if "sport" in signals and article_type in ("athlete", "sports_entity"):
        sport_detail = _extract_sport_detail(article, description, extract)
        if sport_detail:
            return sport_detail
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

    # 8. If we have a description, use it as a weak signal
    if description:
        return f"{description}. Currently experiencing a {magnitude} surge in Wikipedia pageviews."

    return f"Experiencing a {magnitude} surge in Wikipedia pageviews."


def _extract_death_detail(article, extract):
    """Try to extract a specific death-related statement from the extract."""
    lines = extract.split(". ")
    for line in lines:
        if re.search(r"\b(died|death|passed away|killed|assassinated)\b", line, re.IGNORECASE):
            # Grab a concise summary
            short = line.strip()[:180]
            if short.endswith(","):
                short = short[:-1]
            return short
    return None


def _extract_sport_detail(article, description, extract):
    """Try to extract a specific sport-related statement from the extract."""
    lines = extract.split(". ")
    for line in lines:
        # Look for lines mentioning a team, coach, or role relationship
        if re.search(r"\b(coach|manager|head coach|player for|plays for|member of|owner of)\b", line, re.IGNORECASE):
            short = line.strip()[:180]
            if short and not short.endswith("."):
                short += "."
            return short
    return None


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

    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    use_llm = bool(api_key)

    print(f"  Generating context notes for {len(spikes)} spikes...")
    if use_llm:
        print(f"  Using LLM ({LLM_MODEL}) for context generation.")
    else:
        print(f"  No ANTHROPIC_API_KEY found \u2014 using heuristic fallback.")

    # Pass 1: fetch all summaries and build description lookup
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

    # Pass 2: generate context notes with optional cluster context
    enriched = 0
    for s in spikes:
        article = s.get("article", "")
        if not article or s.get("context_note") is not None:
            if s.get("context_note") is None:
                continue
            if not s.get("_description"):
                continue

        description = s.get("_description", "")
        extract = s.get("_extract", "")

        # Try LLM first, fall back to heuristics
        if use_llm:
            cluster = build_cluster_context(article, spikes, summaries)
            note = generate_context_note_llm(article, description, extract, api_key, s.get("spike_multiple", 0), cluster)
            if note:
                s["context_note"] = note
                enriched += 1
                if enriched % 5 == 0:
                    print(f"    ... {enriched}/{len(spikes)} (LLM)")
                time.sleep(0.1)  # Rate limit
                continue

        # Fallback: heuristic generation
        signals = detect_signals(description + " " + extract)
        article_type = classify_article_type(description, extract)

        note = generate_context_note_fallback(
            article, s.get("spike_multiple", 0),
            description, extract, article_type, signals
        )

        s["context_note"] = note
        enriched += 1

        if enriched % 5 == 0:
            print(f"    ... {enriched}/{len(spikes)}")

        time.sleep(0.15)

    # Clean up internal keys
    for s in spikes:
        s.pop("_description", None)
        s.pop("_extract", None)

    with open(SPIKE_JSON, "w") as f:
        json.dump(data, f, indent=2)

    print(f"  Enriched {enriched}/{len(spikes)} spikes with context notes \u2192 {SPIKE_JSON}")
    if use_llm and enriched > 0:
        print(f"\n  Sample context notes:")
        for s in spikes[:5]:
            print(f"    \u2022 {s['article']}: {s.get('context_note', 'N/A')}")
    elif enriched > 0:
        print(f"\n  Sample context notes (heuristic):")
        for s in spikes[:5]:
            print(f"    \u2022 {s['article']}: {s.get('context_note', 'N/A')}")


if __name__ == "__main__":
    enrich_spikes()

"""Allocate a shared character budget without changing retrieval or quality gates."""
import re
from difflib import SequenceMatcher
from .providers.base import numeric_terms


def _words(text: str) -> tuple[str, ...]:
    # Preserve order and repetitions: swapping values between entities changes facts.
    return tuple(re.findall(r"[a-z0-9_]+|[\u4e00-\u9fff]+", text.casefold()))


def _clip(text: str, allowance: int) -> str:
    if len(text) <= allowance:
        return text
    if allowance < 2:
        return "…"[:allowance]
    head = text[:allowance-1]
    ends = [m.end() for m in re.finditer(r"[。！？.!?]\s*", head)]
    if ends and ends[-1] >= len(head)//2:
        head = head[:ends[-1]]
    return head + "…"


def excerpt_plan(items: list[dict], budget: int) -> list[list[dict]]:
    candidates = []
    for item in items:
        unique = []
        for raw in item['evidence']:
            entry = dict(raw, channels=[raw['source']])
            text = re.sub(r'\s+', ' ', entry['snippet']).strip()
            if not text:
                continue
            entry['snippet'] = text
            for previous in unique:
                if (numeric_terms(text) == numeric_terms(previous['snippet'])
                        and _words(text) == _words(previous['snippet'])
                        and entry.get('published') == previous.get('published')
                        and entry.get('dates') == previous.get('dates')
                        and SequenceMatcher(None, text[:4000], previous['snippet'][:4000], autojunk=False).ratio() >= .92):
                    previous['channels'] = list(dict.fromkeys(previous['channels'] + entry['channels']))
                    previous['engines'] = list(dict.fromkeys(previous.get('engines', []) + entry.get('engines', [])))
                    break
            else:
                unique.append(entry)
        candidates.append(unique)
    for entries in candidates:
        for entry in entries:
            if entry.get("published"):
                entry["snippet"] = "发布 " + entry["published"] + " | " + entry["snippet"]
            else:
                dates = entry.get("dates", {})
                source_time = dates.get("source_date", "") or dates.get("page_age", "")
                if source_time:
                    entry["snippet"] = "来源时间 " + source_time + " | " + entry["snippet"]
    output = [[] for _ in items]
    # Round one reserves a fair primary excerpt for every independent page.
    remaining = budget
    for index, entries in enumerate(candidates):
        if not entries or remaining <= 0:
            continue
        allowance = max(1, remaining // max(1, sum(bool(v) for v in candidates[index:])))
        entry = dict(entries[0])
        entry['snippet'] = _clip(entry['snippet'], allowance)
        output[index].append(entry)
        remaining -= len(entry['snippet'])
    supplements = []
    # Spend unused primary reservations on higher-ranked pages before supplements.
    for index, entries in enumerate(candidates):
        if not entries or not output[index] or remaining <= 0:
            continue
        primary = output[index][0]
        previous_length = len(primary['snippet'])
        primary['snippet'] = _clip(entries[0]['snippet'], previous_length + remaining)
        remaining -= len(primary['snippet']) - previous_length
    for index, entries in enumerate(candidates):
        for entry in entries[1:]:
            novelty = len(numeric_terms(entry['snippet'])) + bool(entry.get('published')) + bool(re.search(r'[“"「]', entry['snippet']))
            supplements.append((novelty, index, entry))
    for _, index, raw in sorted(supplements, key=lambda row: (-row[0], row[1])):
        if remaining <= 0:
            break
        entry = dict(raw)
        entry['snippet'] = _clip(entry['snippet'], remaining)
        output[index].append(entry)
        remaining -= len(entry['snippet'])
    return output

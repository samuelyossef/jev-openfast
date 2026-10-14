"""User requirements and bounded observation references shared by every task helper."""

import copy
import re
import time
from datetime import date
from decimal import Decimal, InvalidOperation

AMOUNT = r"(?:\d{1,3}(?:\s*\.\s*\d{3})+|\d+)(?:\s*[,.]\s*\d{1,2})?(?!\d|[.,]\s*\d|\s+\d)"
PRICE_RANGE = re.compile(rf"R\$\s*({AMOUNT})\s*(?:a|até|[-–])\s*(?:R\$\s*)?({AMOUNT})", re.I)
PRICE = re.compile(rf"R\$\s*({AMOUNT})", re.I)
QUANTITY = re.compile(
    r"\b(\d+|dois|duas|três|quatro|cinco|two|three|four|five)\s+"
    r"(?:livros|produtos|resultados|opções|artigos|vídeos|books|products|results|options|articles|videos)\b",
    re.I,
)
COUNTS = {"dois": 2, "duas": 2, "três": 3, "quatro": 4, "cinco": 5, "two": 2, "three": 3, "four": 4, "five": 5}
DATE_TEXT = r"(?:\d{4}-\d{2}-\d{2}|\d{2}/\d{2}/\d{4})"
DATES = re.compile(rf"\b({DATE_TEXT})\b")
DATE_RANGE = re.compile(rf"\b(?:entre|between|de|from)\s+({DATE_TEXT})\s+(?:e|and|a|até|to)\s+({DATE_TEXT})", re.I)
REFERENCE_WORDS = set(
    "a o as os um uma de da do das dos e em no na nos nas para por com que esse essa este esta "
    "abra busque encontre mostre liste favor the a an and in on to for please open find search show list".split()
)


def meaningful_words(value):
    return set(re.findall(r"https?://\S+|\w+", value.casefold())) - REFERENCE_WORDS


def amount(value):
    value = "".join(value.split())
    if "," in value or re.search(r"\.\d{3}(?:\.|$)", value):
        value = value.replace(".", "")
    return Decimal(value.replace(",", "."))


def source_span(quote, source):
    positions = [index for index, char in enumerate(source) if not char.isspace()]
    normalized = "".join(source[index] for index in positions)
    needle = "".join(quote.split())
    offset = normalized.find(needle)
    if not needle or offset < 0:
        return None
    return source[positions[offset]:positions[offset + len(needle) - 1] + 1]


def parsed_date(value):
    try:
        if "/" in value:
            day, month, year = value.split("/")
            value = f"{year}-{month}-{day}"
        return date.fromisoformat(value)
    except ValueError:
        return None


def new_task(goal="", turn_id=None):
    return {
        "turn_id": turn_id,
        "original_goal": goal,
        "resolved_task": goal,
        "requirements": [{"id": "r1", "description": goal, "source_quote": goal,
                          "source_turn_id": turn_id or "", "status": "unknown", "evidence": []}] if goal else [],
        "coverage_valid": False,
        "observations": [],
        "actions": [],
        "guidance": None,
        "escalations": [],
        "stale": False,
    }


def set_requirements(task, proposed, conversation=()):
    goal = task["original_goal"]
    sources = {
        message.get("source_turn_id", ""): message["content"]
        for message in conversation
        if message["role"] == "user" and message.get("source_turn_id")
    }
    sources[task["turn_id"] or ""] = goal
    requirements = [{"description": goal, "source_quote": goal, "source_turn_id": task["turn_id"] or ""}]
    valid = bool(proposed)
    covered = set()
    for item in proposed:
        source_id = item.get("source_turn_id") or task["turn_id"] or ""
        quote = source_span(item.get("source_quote", ""), sources.get(source_id, ""))
        if not quote or not item.get("description", "").strip():
            valid = False
            continue
        candidate = {"description": item["description"], "source_quote": quote, "source_turn_id": source_id}
        if source_id == (task["turn_id"] or ""):
            covered.update(meaningful_words(quote))
        if candidate not in requirements:
            requirements.append(candidate)
    for match in PRICE_RANGE.finditer(goal):
        low, high = map(amount, match.groups())
        requirements.append(
            {
                "description": match.group(),
                "source_quote": match.group(),
                "source_turn_id": task["turn_id"] or "",
                "constraint": {
                    "kind": "price_range",
                    "min": str(low),
                    "max": str(high),
                    "filter": bool(re.search(r"\b(?:filtro|filtrar|filter)\b", goal, re.I)),
                },
            }
        )
        valid = valid and low <= high
    for match in QUANTITY.finditer(goal):
        raw = match[1].lower()
        count = int(raw) if raw.isdigit() else COUNTS[raw]
        requirements.append(
            {
                "description": match.group(),
                "source_quote": match.group(),
                "source_turn_id": task["turn_id"] or "",
                "constraint": {"kind": "quantity", "min": count},
            }
        )
    intervals = list(DATE_RANGE.finditer(goal))
    for match in intervals:
        start, end = map(parsed_date, match.groups())
        if start and end and start <= end:
            requirements.append(
                {
                    "description": match.group(),
                    "source_quote": match.group(),
                    "source_turn_id": task["turn_id"] or "",
                    "constraint": {"kind": "date_range", "min": start.isoformat(), "max": end.isoformat()},
                }
            )
        else:
            valid = False
    for match in DATES.finditer(goal):
        if any(interval.start() <= match.start() < interval.end() for interval in intervals):
            continue
        value = parsed_date(match.group())
        if value:
            prefix = goal[max(0, match.start() - 20) : match.start()].lower()
            relation = (
                "after"
                if re.search(r"(?:depois de|após|after)\s*$", prefix)
                else (
                    "before"
                    if re.search(r"(?:antes de|before)\s*$", prefix)
                    else (
                        "from"
                        if re.search(r"(?:desde|a partir de|from)\s*$", prefix)
                        else ("until" if re.search(r"(?:até|until)\s*$", prefix) else "equal")
                    )
                )
            )
            requirements.append(
                {
                    "description": goal[max(0, match.start() - 12) : match.end()],
                    "source_quote": match.group(),
                    "source_turn_id": task["turn_id"] or "",
                    "constraint": {"kind": "date", "value": value.isoformat(), "relation": relation},
                }
            )
        else:
            valid = False
    previous = {(item["description"], item["source_quote"]): item for item in task["requirements"]}
    task["requirements"] = [
        {
            **item,
            "id": f"r{index + 1}",
            "status": "unknown",
            "evidence": [],
            **{
                key: previous.get((item["description"], item["source_quote"]), {}).get(key, default)
                for key, default in (("status", "unknown"), ("evidence", []))
            },
        }
        for index, item in enumerate(requirements)
    ]
    task["coverage_valid"] = valid and meaningful_words(goal) <= covered


def remember(task, page):
    if not task["original_goal"] or page.get("protected"):
        return
    observation = {
        "url": page["url"],
        "title": page["title"],
        "text": page["text"][:1500],
        "blocks": copy.deepcopy(page.get("blocks", [])[:8]),
        "stale": False,
    }
    previous = task["observations"][-1] if task["observations"] else None
    if previous and all(previous[key] == observation[key] for key in ("url", "text", "blocks")):
        return
    task["observations"].append({**observation, "observed_at": time.time()})
    del task["observations"][:-8]


def model_context(task, *, observations=False):
    keys = ("original_goal", "resolved_task", "requirements", "coverage_valid", "guidance")
    context = copy.deepcopy({key: task[key] for key in keys})
    if not observations:
        context["requirements"] = [
            {key: value for key, value in item.items() if key not in {"source_quote", "source_turn_id"}}
            for item in context["requirements"]
        ]
    if observations:
        context["observation_references"] = copy.deepcopy(task["observations"])
    return context


def update_checks(task, checks):
    by_id = {check.get("requirement_id"): check for check in checks}
    for requirement in task["requirements"]:
        check = by_id.get(requirement["id"], {})
        requirement.update(status=check.get("status", "unknown"), evidence=check.get("evidence", []))


def numeric_evidence(requirement, evidence, page, constraints=()):
    """Only associated, quoted records can support numeric constraints; ambiguous data stays unknown."""
    constraint = requirement.get("constraint")
    if not constraint:
        return True

    def normalize(value):
        return "".join(value.split())

    blocks = [
        block["text"]
        for block in page.get("blocks", [])
        if any(normalize(quote) in normalize(block["text"]) for quote in evidence)
    ]

    def eligible(block, low, high):
        try:
            values = {amount(value) for value in PRICE.findall(block)}
        except InvalidOperation:
            return False
        return len(values) == 1 and low <= next(iter(values)) <= high

    if constraint["kind"] == "quantity":
        price = next((item for item in constraints if item["kind"] == "price_range"), None)
        if price and not price.get("filter"):
            blocks = [block for block in blocks if eligible(block, amount(price["min"]), amount(price["max"]))]
        return len(set(blocks)) >= constraint["min"]
    if constraint["kind"] == "price_range":
        low, high = amount(constraint["min"]), amount(constraint["max"])
        if constraint.get("filter"):
            active = [
                element
                for element in page.get("elements", [])
                if element.get("checked") == "true"
                or element.get("selected") == "true"
                or element.get("role") in {"spinbutton", "textbox", "combobox"}
            ]
            values = [
                amount(match.group())
                for element in active
                for match in re.finditer(AMOUNT, str(element.get("value", "")) + " " + element.get("label", ""))
            ]
            return low in values and high in values
        quantity = max((item["min"] for item in constraints if item["kind"] == "quantity"), default=1)
        # Multiple distinct prices (discounts, instalments or several items) are ambiguous.
        return len({block for block in blocks if eligible(block, low, high)}) >= quantity
    values = [parsed_date(match.group()) for quote in evidence for match in DATES.finditer(quote)]
    if constraint["kind"] == "date_range":
        return any(
            value and parsed_date(constraint["min"]) <= value <= parsed_date(constraint["max"]) for value in values
        )
    expected = parsed_date(constraint["value"])
    return any(
        value
        and {
            "equal": value == expected,
            "before": value < expected,
            "after": value > expected,
            "from": value >= expected,
            "until": value <= expected,
        }[constraint["relation"]]
        for value in values
    )

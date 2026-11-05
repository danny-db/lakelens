"""Generate app/rate_card.json from Databricks' published Foundation Model APIs pricing pages.

Run from the repo root when the pricing pages change (needs internet access):
    python3 tools/gen_rate_card.py

The pages render their DBU tables as real HTML tables, so they are parsed, never read by eye.
Each model keeps the standard pay-per-token rate for text tokens, using the short-context
tier where a page splits one. The $ per DBU is not here: it depends on the cloud, region and
tier, so the app reads it from system.billing.list_prices at run time.
"""
import html.parser
import json
import os
import re
import urllib.request
from datetime import date

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "app", "rate_card.json")

PAGES = {
    "proprietary": "https://www.databricks.com/product/pricing/proprietary-foundation-model-serving",
    "open": "https://www.databricks.com/product/pricing/foundation-model-serving",
}

# Billing SKU family by model-name prefix on the proprietary page. Databricks resells each
# vendor under its own <TIER>_<VENDOR>_MODEL_SERVING SKU.
VENDOR_SKU = {"GPT": "OPENAI_MODEL_SERVING", "Claude": "ANTHROPIC_MODEL_SERVING",
              "Gemini": "GEMINI_MODEL_SERVING", "Grok": "XAI_MODEL_SERVING"}
# Models on the open page bill under the regional SRTI SKU, except these. Kimi K3 bills
# ENTERPRISE_MOONSHOT_MODEL_SERVING in system.billing.usage since that SKU appeared (Aug 2026).
OPEN_PAGE_VENDOR_SKU = {"Kimi K3": "MOONSHOT_MODEL_SERVING"}
SRTI = "SERVERLESS_REAL_TIME_INFERENCE"

# Page names whose serving endpoint id does not follow databricks-<slug of the name>.
ENDPOINT_ALIASES = {
    "DeepSeek V4.1 Flash (0910)": "databricks-deepseek-v4-1-flash",
    "DeepSeek V4 Flash": "databricks-deepseek-v4-flash-0731",
    "DeepSeek V4 Pro": "databricks-deepseek-v4-pro-0813",
    "Gemini 3.0 Flash": "databricks-gemini-3-flash",
    "Llama 3.3 70B": "databricks-meta-llama-3-3-70b-instruct",
    "Llama 3.1 8B": "databricks-meta-llama-3-1-8b-instruct",
    "Qwen 3 80B Instruct": "databricks-qwen3-next-80b-a3b-instruct",
    "Qwen 3.5 122B": "databricks-qwen35-122b-a10b",
}
# Rows kept per model: the plain row, else the short-context or text-token row.
BASE_TIERS = ("", "Short context", "Text tokens")
RESIDENCY = "⌖"


class _Page(html.parser.HTMLParser):
    """Every <table> as rows of cell text, plus every visible text node outside tables."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tables, self.texts, self._open, self._skip = [], [], [], 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self._skip += 1
        elif tag == "table":
            self._open.append({"rows": [], "row": None, "cell": None})
        elif self._open and tag == "tr":
            self._open[-1]["row"] = []
        elif self._open and tag in ("td", "th"):
            self._open[-1]["cell"] = []

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self._skip -= 1
        if not self._open:
            return
        t = self._open[-1]
        if tag in ("td", "th") and t["cell"] is not None and t["row"] is not None:
            t["row"].append(" ".join("".join(t["cell"]).split()))
            t["cell"] = None
        elif tag == "tr" and t["row"] is not None:
            t["rows"].append(t["row"])
            t["row"] = None
        elif tag == "table":
            self.tables.append(self._open.pop()["rows"])

    def handle_data(self, data):
        if self._open and self._open[-1]["cell"] is not None:
            self._open[-1]["cell"].append(data)
        elif not self._open and not self._skip and data.strip():
            self.texts.append(" ".join(data.split()))


def _parse(page: str) -> _Page:
    p = _Page()
    p.feed(page)
    return p


def _num(cell: str):
    cell = cell.replace(",", "").strip()
    return float(cell) if re.fullmatch(r"\d+(\.\d+)?", cell) else None


def _names(label: str) -> list[str]:
    """'GPT-5.4 Pro, 5.5 Pro ⌖' -> ['GPT-5.4 Pro', 'GPT-5.5 Pro']."""
    parts = [p.strip() for p in re.sub(rf"[*{RESIDENCY}]", "", label).split(",")]
    first = parts[0]
    prefix = first[:re.search(r"\d", first).start()] if re.search(r"\d", first) else first
    return [first] + [prefix + p for p in parts[1:]]


def _endpoint(name: str) -> str:
    return ENDPOINT_ALIASES.get(name) or "databricks-" + re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def _sku_family(source: str, name: str) -> str:
    if source == "open":
        return OPEN_PAGE_VENDOR_SKU.get(name, SRTI)
    family = next((f for p, f in VENDOR_SKU.items() if name.startswith(p)), None)
    if not family:
        raise SystemExit(f"{name!r} has no billing SKU family: add its vendor to VENDOR_SKU")
    return family


def pay_per_token_rows(source: str, page: _Page) -> list[dict]:
    """Rows of the standard pay-per-token tables: one per model with input and output rates."""
    out = []
    for table in page.tables:
        title = " ".join(table[0]) if table else ""
        if "Pay Per Token" not in title or "Priority" in title:
            continue
        has_tier = "" in table[0]
        cols = table[1]
        model = None
        for row in table[2:]:
            if has_tier and len(row) == len(cols) + 2:
                model, tier, values = row[0], row[1], row[2:]
            elif has_tier and len(row) == len(cols) + 1:
                tier, values = row[0], row[1:]
            else:
                model, tier, values = row[0], "", row[1:]
            rates = dict(zip(cols, (_num(v) for v in values)))
            if tier not in BASE_TIERS or rates.get("Input") is None or rates.get("Output") is None:
                continue
            for name in _names(model):
                if "Image" in name:
                    continue
                out.append({
                    "id": _endpoint(name), "name": name, "source": source,
                    "page_row": model + (f" / {tier}" if tier else ""),
                    "sku_family": _sku_family(source, name),
                    "dbu_in": rates["Input"], "dbu_out": rates["Output"],
                    "dbu_cache_read": rates.get("Cache read"),
                    "residency_uplift": RESIDENCY in model,
                })
    seen, unique = set(), []
    for r in out:
        if r["id"] not in seen:
            seen.add(r["id"])
            unique.append(r)
    return unique


def footnotes(page: _Page) -> list[str]:
    """The page's own footnotes that change a rate: residency uplift and time-limited promotions."""
    return [t for t in page.texts
            if t.startswith(f"{RESIDENCY} When regional processing") or re.match(r"\*+ (NOTE: )?The .*rates", t)]


def main():
    models, notes = [], []
    for source, url in PAGES.items():
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=60) as r:
            if r.status != 200:
                raise SystemExit(f"{url} returned {r.status}")
            page = _parse(r.read().decode("utf-8"))
        models += pay_per_token_rows(source, page)
        notes += footnotes(page)
    card = {
        "retrieved": date.today().isoformat(),
        "sources": PAGES,
        "basis": "Standard pay-per-token DBU per 1M text tokens; the short-context tier where a page splits one.",
        "notes": list(dict.fromkeys(notes)),
        "models": sorted(models, key=lambda m: m["id"]),
    }
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(card, f, indent=1, ensure_ascii=False)
        f.write("\n")
    print(f"wrote {OUT}: {len(card['models'])} models, {len(card['notes'])} notes")


if __name__ == "__main__":
    main()

from abc import ABC, abstractmethod
from pathlib import Path
import json
import re
import requests
import wikitextparser as wtp

FLAVOURS = {
    "DS": "Don't Starve",
    "ROG": "Reign of Giants",
    "SW": "Shipwrecked",
    "H": "Hamlet",
}

# --- Item extraction strategies ---
# Each subclass extracts item names from a raw crafting-table cell string.


class ItemExtractor(ABC):
    @abstractmethod
    def __call__(self, cell: str) -> list: ...


class Pic32Extractor(ItemExtractor):
    """Handles {{Pic32|Item Name}} markup."""

    _RE = re.compile(r"\{\{Pic32\|([^}]+)\}\}")

    def __call__(self, cell: str) -> list:
        return self._RE.findall(cell)


class FileLinksExtractor(ItemExtractor):
    """Handles [[File:...|link=Page#Section|Caption]] markup."""

    _RE = re.compile(r"\[\[File:[^\]]*\|link=([^#|\]]+)(?:#([^|\]]+))?(?:\|([^|\]]+))?")

    def __call__(self, cell: str) -> list:
        results = []
        for m in self._RE.finditer(cell):
            page = m.group(1).strip()
            section = m.group(2).strip() if m.group(2) else None
            caption = m.group(3).strip() if m.group(3) else None
            results.append(caption or section or page)
        return results


ITEM_EXTRACTORS: list[ItemExtractor] = [Pic32Extractor(), FileLinksExtractor()]

IGNORE_TABS = ["Books", "Tinkering"]


# --- Ingredient extraction strategies ---
# Each subclass extracts ingredient dicts from an infobox template + flavour key.
# Tried in order; first non-empty result is used.


class IngredientExtractor(ABC):
    @abstractmethod
    def __call__(self, infobox, flavour: str) -> list: ...


class IngredientsFromArgs(IngredientExtractor):
    """ingredient1/multiplier1 numbered args (most items)."""

    def __call__(self, infobox, flavour: str) -> list:
        ingredients = []
        i = 1
        while True:
            ing_arg = infobox.get_arg(f"ingredient{i}")
            if not ing_arg:
                break
            item_name = ing_arg.value.strip()
            if not item_name:
                i += 1
                continue
            mul_arg = infobox.get_arg(f"multiplier{i}")
            count = 1
            if mul_arg:
                m = re.search(r"\d+", mul_arg.value)
                count = int(m.group()) if m else 1
            ingredients.append({"item": item_name, "count": count})
            i += 1
        return ingredients


class IngredientsFromCraftingText(IngredientExtractor):
    """crafting_text arg with DLC-variant recipes separated by <br> or <hr>."""

    _PIC_ITEM_RE = re.compile(r"\{\{[Pp]ic(?:24)?\|(?:\d+\|)?([^}|]+)\}\}\s*[×x]\s*(\d+)")

    _DLC_PREFERENCES = {
        "DS": ["DS"],
        "ROG": ["RoG", "All DLC", "DS"],
        "SW": ["SW", "All DLC", "DS"],
        "H": ["Ham", "All DLC", "DS"],
    }

    def __call__(self, infobox, flavour: str) -> list:
        ct_arg = infobox.get_arg("crafting_text")
        if not ct_arg:
            return []
        preferred = self._DLC_PREFERENCES.get(flavour, [])
        # Index segments by their DLC tag so we can look up in preference order,
        # not document order (e.g. prefer RoG over DS even if DS appears first).
        segment_by_tag: dict[str, str] = {}
        for segment in re.split(r"<br>|<hr>", ct_arg.value):
            dlc_m = re.match(r"\s*\{\{([^|}]+)[|}]", segment)
            if dlc_m:
                segment_by_tag[dlc_m.group(1).strip().lower()] = segment
        for p in preferred:
            segment = segment_by_tag.get(p.lower())
            if segment:
                return [
                    {"item": m.group(1).strip(), "count": int(m.group(2))}
                    for m in self._PIC_ITEM_RE.finditer(segment)
                ]
        return []


class IngredientsFromCraftInfobox(IngredientExtractor):
    """crafting_infobox arg containing Craft Infobox sub-templates, one per
    DLC/character (e.g. Electrical Doodad). Skips tabs listed in IGNORE_TABS."""

    _args = IngredientsFromArgs()

    def __call__(self, infobox, flavour: str) -> list:
        ci_arg = infobox.get_arg("crafting_infobox")
        if not ci_arg:
            return []
        for sub in wtp.parse(ci_arg.value).templates:
            if sub.name.strip() != "Craft Infobox":
                continue
            tab_arg = sub.get_arg("tab")
            if tab_arg and tab_arg.value.strip() in IGNORE_TABS:
                continue
            ings = self._args(sub, flavour)
            if ings:
                return ings
        return []


INGREDIENT_EXTRACTORS: list[IngredientExtractor] = [
    IngredientsFromArgs(),
    IngredientsFromCraftingText(),
    IngredientsFromCraftInfobox()
]


def fetch_crafting_wikitext(wikitext_file, flavour):
    if not wikitext_file.exists():
        response = requests.get(
            "https://dontstarve.wiki.gg/api.php",
            timeout=30,
            params={
                "action": "parse",
                "page": f"Crafting/{FLAVOURS[flavour]}",
                "prop": "wikitext",
                "format": "json",
            },
        )
        response.raise_for_status()
        wikitext_file.write_text(
            response.json()["parse"]["wikitext"]["*"], encoding="utf-8"
        )


def extract_craftables(wikitext, json_file):
    table = wtp.parse(wikitext).tables[0]
    rows = table.data()

    def key_from_header(h):
        # Keep text-only <br> parts, drop parts with wiki markup ({{...}} or [[...]])
        parts = [
            p.strip() for p in h.split("<br>") if not any(c in p for c in ("{{", "[["))
        ]
        return "_".join(parts).lower().replace(" ", "_")

    keys = [key_from_header(h) for h in rows[0]]

    records = []
    for row in rows[1:]:
        # Tab name: display text of the last wikilink in the first cell.
        # table.data() returns raw cell strings, so we re-parse to access wikilinks.
        tab_links = wtp.parse(row[0]).wikilinks
        if not tab_links:
            continue
        tab_name = (tab_links[-1].text or tab_links[-1].title).strip()
        if tab_name in IGNORE_TABS:
            continue

        record = {"tab": tab_name}
        for key, cell in zip(keys[1:], row[1:]):
            items = [item for extract in ITEM_EXTRACTORS for item in extract(cell)]
            if items:
                record[key] = items
        records.append(record)

    json_file.write_text(
        json.dumps(records, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    print(f"Wrote {len(records)} item categories to {json_file}")


def fetch_item_wikitexts(craftable_items, items_wikitext_file):
    all_items = sorted(
        {
            item
            for row in craftable_items
            for key, val in row.items()
            if key != "tab"
            for item in val
        }
    )
    print(f"Found {len(all_items)} unique items")

    # Cache raw wikitext for each item (one JSON file, keyed by item name)
    items_cache = (
        json.loads(items_wikitext_file.read_text(encoding="utf-8"))
        if items_wikitext_file.exists()
        else {}
    )

    # Re-fetch items whose cached wikitext is a redirect stub
    missing = [i for i in all_items if i not in items_cache]
    for i in range(0, len(missing), 50):
        batch = missing[i : i + 50]
        response = requests.get(
            "https://dontstarve.wiki.gg/api.php",
            timeout=30,
            params={
                "action": "query",
                "prop": "revisions",
                "rvprop": "content",
                "rvslots": "main",
                "redirects": 1,
                "titles": "|".join(batch),
                "format": "json",
            },
        )
        response.raise_for_status()
        data = response.json()["query"]
        # Map redirect target → original item names (e.g. "Farm" → ["Basic Farm"])
        redirect_map: dict[str, list[str]] = {}
        for r in data.get("redirects", []):
            redirect_map.setdefault(r["to"], []).append(r["from"])
        for page in data["pages"].values():
            if "revisions" in page:
                page_wikitext = page["revisions"][0]["slots"]["main"]["*"]
                for name in redirect_map.get(page["title"], [page["title"]]):
                    items_cache[name] = page_wikitext
        items_wikitext_file.write_text(
            json.dumps(items_cache, indent=2, ensure_ascii=False), encoding="utf-8"
        )


def extract_recipes(items_wikitext, recipes_file):
    def parse_ingredients(name: str, wikitext: str, flavour: str) -> list:
        infoboxes = [t for t in wtp.parse(wikitext).templates if "Infobox" in t.name]
        # Prefer the infobox whose |name| arg matches the item (handles pages with
        # multiple infoboxes, e.g. Straw Roll page also containing Fur Roll).
        def box_name_matches(ib):
            n = ib.get_arg("name")
            return n and n.value.strip().lower() == name.lower()
        matched = [ib for ib in infoboxes if box_name_matches(ib)]
        candidates = matched if matched else infoboxes
        for infobox in candidates:
            for extract in INGREDIENT_EXTRACTORS:
                ings = extract(infobox, flavour)
                if ings:
                    return ings
        return []

    recipes = {}
    for name, wikitext in items_wikitext.items():
        ings = parse_ingredients(name, wikitext, flavour)
        if ings:
            normalized_ings = [
                {"item": i["item"].lower(), "count": i["count"]} for i in ings
            ]
            recipes[name.lower()] = {"ingredients": normalized_ings}

    recipes_file.write_text(
        json.dumps(recipes, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    print(f"Wrote {len(recipes)} recipes to {recipes_file}")


if __name__ == "__main__":
    flavour = "ROG"

    tmp = Path("_scraps")
    target = Path("resources")
    crafting_wikitext_file = tmp / f"{flavour}_crafting.wikitext"
    fetch_crafting_wikitext(crafting_wikitext_file, flavour)

    crafting_wikitext = crafting_wikitext_file.read_text(encoding="utf-8")
    crafting_json_file = target / f"{flavour}_crafting.json"
    extract_craftables(crafting_wikitext, crafting_json_file)

    craftable_items = (
        json.loads(crafting_json_file.read_text(encoding="utf-8"))
        if crafting_json_file.exists()
        else {}
    )
    items_wikitext_file = tmp / f"{flavour}_items_wikitext.json"
    fetch_item_wikitexts(craftable_items, items_wikitext_file)

    items_wikitext = (
        json.loads(items_wikitext_file.read_text(encoding="utf-8"))
        if items_wikitext_file.exists()
        else {}
    )
    recipes_file = target / f"{flavour}_recipes.json"
    extract_recipes(items_wikitext, recipes_file)

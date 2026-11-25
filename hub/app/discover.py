"""Ideas: game shelves the kid can browse on "Get more games" without knowing what to search for.

A short, hand-picked list of well-liked games for kids, grouped by what they're like. The kid browses
it and asks for one (it becomes an ordinary request with the exact store attached); a parent can also
approve straight from the same list on the dashboard. Nothing here is approved by being on the list.

Each kid has a rating cap for ideas (kv "ideas_rating:<kid>", default E10+, or "off"): games rated
above it are not shown. Ratings come from Steam's store data when the hub has it (cached a day, see
main.steam_app); the rating written here is only the fallback.

The list is the same for every family, so it works the same for a hub in the cloud. A family's own
"what my brother has" shelf comes from their own catalogue.
"""
from __future__ import annotations

RATING_ORDER = ["E", "E10+", "T", "M", "AO"]
CAPS = ["off", "E", "E10+", "T", "M"]
DEFAULT_CAP = "E10+"

# (shelf title, [(launcher, launch_ref, title, fallback ESRB)]). Steam ids are the store app id.
SHELVES: list[tuple[str, list[tuple[str, str, str, str]]]] = [
    ("Build and create", [
        ("steam", "413150", "Stardew Valley", "E10+"),
        ("steam", "220200", "Kerbal Space Program", "E"),
        ("steam", "493340", "Planet Coaster", "E"),
        ("steam", "703080", "Planet Zoo", "E"),
        ("steam", "1291340", "Townscaper", "E"),
        ("steam", "433340", "Slime Rancher", "E10+"),
        ("steam", "427520", "Factorio", "E10+"),
        ("steam", "105600", "Terraria", "T"),
    ]),
    ("Play together", [
        ("steam", "728880", "Overcooked! 2", "E"),
        ("steam", "996770", "Moving Out", "E"),
        ("steam", "477160", "Human: Fall Flat", "E10+"),
        ("steam", "1016920", "Unrailed!", "E"),
        ("steam", "431240", "Golf With Your Friends", "E"),
        ("steam", "1097150", "Fall Guys", "E"),
        ("steam", "291550", "Brawlhalla", "E10+"),
    ]),
    ("Puzzles and adventures", [
        ("steam", "620", "Portal 2", "E10+"),
        ("steam", "1055540", "A Short Hike", "E"),
        ("steam", "837470", "Untitled Goose Game", "E"),
        ("steam", "736260", "Baba Is You", "E"),
        ("steam", "387290", "Ori and the Blind Forest: Definitive Edition", "E"),
        ("steam", "1135690", "Unpacking", "E10+"),
        ("steam", "504230", "Celeste", "E10+"),
    ]),
    ("Racing and sports", [
        ("steam", "1271700", "Hot Wheels Unleashed", "E"),
        ("steam", "389140", "Horizon Chase Turbo", "E"),
        ("steam", "711540", "Lonely Mountains: Downhill", "E"),
        ("steam", "1378990", "Crash Bandicoot 4: It's About Time", "E10+"),
    ]),
    ("On Battle.net", [
        ("battlenet", "RTRO", "Blizzard Arcade Collection", "E10+"),
        ("battlenet", "Pro", "Overwatch 2", "T"),
        ("battlenet", "WTCG", "Hearthstone", "T"),
        ("battlenet", "S2", "StarCraft II", "T"),
    ]),
]


def rating_ok(esrb: str, cap: str) -> bool:
    """True when a game rated `esrb` may be shown under `cap`. Unrated games are only shown at M."""
    if cap == "off":
        return False
    esrb = (esrb or "").upper()
    if esrb not in RATING_ORDER:
        return cap == "M"
    return RATING_ORDER.index(esrb) <= RATING_ORDER.index(cap)


def all_ideas() -> list[dict]:
    """Every idea once, in shelf order, with its shelf name."""
    return [{"shelf": shelf, "launcher": l, "launch_ref": ref, "title": t, "esrb": r}
            for shelf, games in SHELVES for (l, ref, t, r) in games]

"""Writes the shell's "art" demo data: every Battle.net and Epic game with the art the hub gives it, plus one game whose
picture can't load, so CI screenshots show each picture on real Windows tiles (Demo.cs reads KIDSPC_DEMO_ART).

    python -m hub.tests.art_demo art-demo.json
"""
import json
import sys

from hub.app import gamesearch

games = [{"launcher": "battlenet", "ref": b["code"], "title": b["title"], "esrb": b["esrb"]} for b in gamesearch.BATTLENET]
games += [{"launcher": "epic", "ref": e["app"], "title": e["title"], "esrb": e["esrb"]} for e in gamesearch.EPIC]
tiles = [{"id": i + 1, "title": g["title"], "launcher": g["launcher"], "launch_ref": g["ref"], "rating": g["esrb"],
          "image_url": gamesearch.art_for(g["launcher"], g["ref"])} for i, g in enumerate(games)]
tiles.append({"id": len(tiles) + 1, "title": "A picture that won't load", "launcher": "steam", "launch_ref": "1",
              "rating": "E", "image_url": "https://shared.cloudflare.steamstatic.com/store_item_assets/steam/apps/1/header.jpg"})


def shelf(title: str, launcher: str) -> dict:
    return {"title": title, "games": [{"ref": f"{g['launcher']}:{g['ref']}", "name": g["title"], "esrb": g["esrb"],
                                       "image_url": gamesearch.art_for(g["launcher"], g["ref"]),
                                       "store": gamesearch.LABELS[launcher]} for g in games if g["launcher"] == launcher]}


data = {"games": tiles, "discover": {"cap": "M", "shelves": [shelf("On Battle.net", "battlenet"), shelf("On Epic Games", "epic")]}}
with open(sys.argv[1] if len(sys.argv) > 1 else "art-demo.json", "w") as f:
    json.dump(data, f, indent=1)

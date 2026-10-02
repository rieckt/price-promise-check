import json
import math
from pathlib import Path

from .config import load_config

DATA = Path(__file__).with_name("data")


def distance_km(origin, target):
    lat1, lon1, lat2, lon2 = map(math.radians, (*origin, *target))
    h = math.sin((lat2-lat1)/2)**2 + math.cos(lat1)*math.cos(lat2)*math.sin((lon2-lon1)/2)**2
    return 6371.0088 * 2 * math.asin(min(1, math.sqrt(h)))


def catalogue():
    settings = load_config()
    value = json.loads((DATA / "retailers.json").read_text(encoding="utf-8"))
    if settings["market_id"] != value["market"]["id"]:
        value["market"] = settings["market"] or (
            {"id": settings["market_id"], "name": "MediaMarkt " + settings["market_id"],
             "address": "", "coordinates": None} if settings["market_id"] else None)
        value["local_candidates"] = []
        value["local_catalogue_status"] = "not_configured" if settings["market_id"] is None else "not_available"
        return value
    value["local_catalogue_status"] = "available"
    # The curated catalogue owns market coordinates; private fields cannot override it.
    origin = value["market"]["coordinates"]
    for shop in value["local_candidates"]:
        distance = distance_km(origin, shop["coordinates"])
        shop["distance_km"] = round(distance, 1)
        shop["within_30km_airline"] = distance <= 30
        shop["radius_assessment"] = "borderline" if abs(distance - 30) < 1 else (
            "inside" if distance <= 30 else "outside")
        shop["coordinate_precision"] = shop.get("coordinate_precision", "address")
        shop["offer_eligibility"] = "not_checked"
        shop["price_adapter"] = "not_implemented"
    value["local_candidates"].sort(key=lambda s: s["distance_km"])
    return value

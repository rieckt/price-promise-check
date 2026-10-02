"""Private local settings. Only public market fields cross the output boundary."""
import json
import math
import os
import re
from pathlib import Path


class ConfigError(ValueError):
    pass


def config_path():
    base = Path(os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config"))
    return base / "pricecheck" / "config.json"


def validate_market_id(value):
    if not isinstance(value, str) or not re.fullmatch(r"\d{1,10}", value, re.ASCII):
        raise ConfigError("market_id must be a string containing 1 to 10 digits")
    return value


def validate_market(value, market_id):
    if not isinstance(value, dict):
        raise ConfigError("market must be an object")
    public = {"id": market_id}
    for field in ("name", "address"):
        text = value.get(field, "")
        if not isinstance(text, str) or len(text) > 300 or any(ord(c) < 32 for c in text):
            raise ConfigError("Invalid public market field: " + field)
        public[field] = text
    coordinates = value.get("coordinates")
    if coordinates is not None:
        if (not isinstance(coordinates, list) or len(coordinates) != 2
                or any(type(c) not in (int, float) or not math.isfinite(c) for c in coordinates)
                or not -90 <= coordinates[0] <= 90 or not -180 <= coordinates[1] <= 180):
            raise ConfigError("Market coordinates must be finite [latitude, longitude]")
    public["coordinates"] = coordinates
    return public


def load_config():
    path = config_path()
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {"schema_version": 1, "market_id": None, "market": None}
    except (OSError, UnicodeError):
        raise ConfigError("Cannot read local configuration") from None
    try:
        value = json.loads(raw)
    except ValueError:
        raise ConfigError("Local configuration contains invalid JSON") from None
    if not isinstance(value, dict) or type(value.get("schema_version")) is not int or value["schema_version"] != 1:
        raise ConfigError("Unsupported local configuration schema; expected schema_version 1")
    market_id = validate_market_id(value.get("market_id"))
    market = validate_market(value["market"], market_id) if "market" in value else None
    # Private address and unknown fields are never returned to CLI consumers.
    return {"schema_version": 1, "market_id": market_id, "market": market}


def initialize(market_id, market=None):
    validate_market_id(market_id)
    value = {"schema_version": 1, "market_id": market_id}
    if market is not None:
        value["market"] = validate_market(market, market_id)
    path = config_path()
    try:
        path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(path, flags, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    except FileExistsError:
        raise ConfigError("Local configuration already exists; edit it manually to preserve private settings") from None
    except OSError:
        raise ConfigError("Cannot create local configuration") from None

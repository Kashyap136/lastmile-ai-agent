"""Recipient organisation profiles.

Recipients in the demo dataset are clearly labelled DEMO data - never
presented as real organisations. Structures mirror what a real verified
directory would contain.
"""

from __future__ import annotations

import json
import os
from functools import lru_cache
from typing import Any

from pydantic import BaseModel, Field

from .common import DataSource, FoodCategory, StorageType

_DEMO_DATA_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "lastmile",
    "data",
    "demo_recipients.json",
)


class RecipientProfile(BaseModel):
    recipient_id: str
    name: str
    org_type: str  # shelter | soup_kitchen | senior_center | food_bank | community_fridge
    address_text: str
    zone: str
    latitude: float
    longitude: float
    capacity_units: float  # max daily units we can responsibly route in demo
    accepts_categories: list[FoodCategory]
    accepts_ambient: bool = False  # can they take unrefrigerated food?
    min_notice_minutes: int = Field(ge=0)  # scheduling headroom they need
    service_windows: list[list[str]] = Field(default_factory=list)  # ["HH:MM","HH:MM"] daily
    contact_name: str = ""
    verified_data_source: DataSource = DataSource.DEMO

    @classmethod
    def from_raw(cls, raw: dict[str, Any]) -> "RecipientProfile":
        return cls(
            recipient_id=raw["recipient_id"],
            name=raw["name"],
            org_type=raw["org_type"],
            address_text=raw["address_text"],
            zone=raw["zone"],
            latitude=float(raw["latitude"]),
            longitude=float(raw["longitude"]),
            capacity_units=float(raw["capacity_units"]),
            accepts_categories=[FoodCategory(c) for c in raw["accepts_categories"]],
            accepts_ambient=bool(raw.get("accepts_ambient", False)),
            min_notice_minutes=int(raw.get("min_notice_minutes", 0)),
            service_windows=raw.get("service_windows", []),
            contact_name=raw.get("contact_name", ""),
        )

    def is_demo(self) -> bool:
        return self.verified_data_source == DataSource.DEMO


@lru_cache(maxsize=4)
def load_demo_recipients(path: str | None = None) -> list[RecipientProfile]:
    """Load the labelled demonstration recipient dataset.

    The file is an explicitly-labelled demo dataset - organisations must
    never be presented as real to users.
    """
    file_path = path or _DEMO_DATA_PATH
    with open(file_path, "r", encoding="utf-8") as fh:
        doc = json.load(fh)
    if isinstance(doc, dict):  # {"meta": {...}, "recipients": [...]}
        if not doc.get("meta", {}).get("is_demo_data"):
            raise ValueError(f"{file_path} is missing the is_demo_data label")
        raw_list = doc["recipients"]
    else:
        raw_list = doc
    return [RecipientProfile.from_raw(raw) for raw in raw_list]


class StorageConstraint(BaseModel):
    required_storage: StorageType
    consistent: bool
    note: str = ""

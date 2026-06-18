"""Bulk import people into InspireFace from a JSON file.

This script:
- Reads people records from `data/attendance-people-data.json` (default)
- Downloads/caches JPEG images to `data/enrollment_images/`
- Upserts each person into the local SQLite DB
- Enrolls each person's face into InspireFace FeatureHub and creates the mapping

Usage:
  uv run python scripts/bulk_import_people.py \
    --json data/attendance-people-data.json \
    [--start 0] [--limit 100] [--force]

Notes:
- Skips people who already have a mapping in `FaceIdentityMap` unless `--force` is used
- Reuses a single `FaceRecognizer` session for performance
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, Optional

import os
from urllib.parse import urlparse

import cv2
import requests as req

from src.config import DATA_DIR, ENROLLMENT_IMAGES_DIR
from src.core.face_recognizer import FaceRecognizer
from src.schema import FaceIdentityMap, Person, db, ensure_db_schema
from src.utils import ist_timestamp


def _download_image_if_needed(picture_url: str) -> tuple[str, bool]:
    """Ensure the image is downloaded locally; return (local_path, downloaded_now)."""
    parsed_url = urlparse(picture_url)
    filename = os.path.basename(parsed_url.path)
    if not filename.lower().endswith(".jpg"):
        raise ValueError("Only .jpg images are supported")

    local_path = os.path.join(ENROLLMENT_IMAGES_DIR, filename)

    if os.path.exists(local_path):
        return local_path, False

    response = req.get(picture_url, timeout=15)
    if response.status_code != 200:
        raise RuntimeError(f"Failed to download image (status={response.status_code})")

    content_type = response.headers.get("Content-Type", "")
    if "image/jpeg" not in content_type.lower():
        raise ValueError("URL does not point to a JPEG image")

    with open(local_path, "wb") as f:
        f.write(response.content)

    return local_path, True


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Bulk import people into InspireFace from a JSON file.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--json",
        dest="json_path",
        type=str,
        default=str(DATA_DIR / "attendance-people-data.json"),
        help="Path to the people JSON file",
    )
    parser.add_argument(
        "--start",
        dest="start_index",
        type=int,
        default=0,
        help="Start index in the JSON array",
    )
    parser.add_argument(
        "--limit",
        dest="limit_count",
        type=int,
        default=None,
        help="Max number of records to process (None = all)",
    )
    parser.add_argument(
        "--force",
        dest="force",
        action="store_true",
        help="Re-enroll even if a mapping already exists",
    )
    return parser.parse_args()


def _load_people(json_path: str) -> list[Dict[str, Any]]:
    json_file = Path(json_path)
    if not json_file.exists():
        raise FileNotFoundError(f"JSON file not found: {json_file}")
    with json_file.open("r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, list):
        raise ValueError("Top-level JSON must be an array of people objects")
    return data


def _upsert_person_record(person: Dict[str, Any], local_filename: str) -> None:
    """Insert or replace a `Person` row from the incoming payload."""
    synced_at = ist_timestamp()
    person_id = person.get("personId")
    name = person.get("preferredName") or person.get("name")
    user_type = person.get("userType")

    if not person_id or not name or not user_type:
        raise ValueError("personId, preferredName|name and userType are required")

    # Admission/room are present for Cadet; may be absent for Employee
    Person.insert(
        uniqueId=person_id,
        name=name,
        admissionNumber=person.get("admissionNumber"),
        roomId=person.get("roomId"),
        pictureFileName=local_filename,
        personType=user_type,
        syncedAt=synced_at,
    ).on_conflict_replace().execute()


def main() -> int:
    args = _parse_args()

    try:
        people = _load_people(args.json_path)
    except Exception as exc:
        print(f"[bulk-import] Failed to load JSON: {exc}")
        return 1

    # Ensure tables exist and keep the DB open for the bulk operation
    ensure_db_schema()
    if db.is_closed():
        db.connect(reuse_if_open=True)

    recognizer = None
    try:
        recognizer = FaceRecognizer()
    except SystemExit:
        # FaceRecognizer may call exit(1) internally on fatal errors
        return 1
    except Exception as exc:
        print(f"[bulk-import] Failed to initialise FaceRecognizer: {exc}")
        return 1

    start_index = max(0, int(args.start_index or 0))
    end_index: Optional[int] = None
    if args.limit_count is not None and args.limit_count >= 0:
        end_index = start_index + args.limit_count

    processed = 0
    enrolled = 0
    skipped = 0
    failed = 0

    for idx, person in enumerate(people[start_index:end_index], start=start_index):
        person_id = person.get("personId")
        picture_url = person.get("picture") or person.get("pictureUrl")
        display_name = person.get("preferredName") or person.get("name") or "<unknown>"

        if not person_id or not picture_url:
            print(f"[{idx}] Skipping invalid record (missing id/url): {display_name}")
            skipped += 1
            continue

        if not args.force:
            try:
                existing = FaceIdentityMap.get_or_none(
                    FaceIdentityMap.personId == person_id
                )
            except Exception:
                existing = None
            if existing is not None:
                print(f"[{idx}] Skip already enrolled: {display_name} ({person_id})")
                skipped += 1
                continue

        try:
            local_path, _ = _download_image_if_needed(picture_url)
        except Exception as exc:
            print(f"[{idx}] Image fetch failed for {display_name}: {exc}")
            failed += 1
            continue

        try:
            _upsert_person_record(person, Path(local_path).name)
        except Exception as exc:
            print(f"[{idx}] DB upsert failed for {display_name}: {exc}")
            failed += 1
            continue

        try:
            frame = cv2.imread(str(local_path))
            if frame is None:
                raise ValueError("cv2.imread returned None")
            enroll_result = recognizer.add_face(frame, person_id)
            if enroll_result is None:
                raise RuntimeError("No face detected to enroll")
        except Exception as exc:
            print(f"[{idx}] Enrollment failed for {display_name}: {exc}")
            failed += 1
            continue

        enrolled += 1
        processed += 1
        print(f"[{idx}] Enrolled: {display_name} ({person_id})")

    # Close DB after bulk operation
    try:
        if not db.is_closed():
            db.close()
    except Exception:
        pass

    total = len(people[start_index:end_index])
    print(
        f"\n[bulk-import] Done. total={total} enrolled={enrolled} skipped={skipped} failed={failed}"
    )
    return 0 if failed == 0 else 2


if __name__ == "__main__":
    sys.exit(main())

from unittest.mock import MagicMock

from src.core.face_recognizer import FaceRecognizer


def _make_recognizer() -> FaceRecognizer:
    recognizer = object.__new__(FaceRecognizer)
    recognizer._identity_cache = {}
    recognizer.logger = MagicMock()
    return recognizer


def test_resolve_identity_returns_cached_value_without_db_call():
    recognizer = _make_recognizer()
    cached = {
        "personId": "p7",
        "name": "Alice",
        "admissionNumber": "A007",
    }
    recognizer._identity_cache[7] = cached

    result = recognizer.resolve_identity(7)

    assert result == cached
    assert recognizer._identity_cache[7] == cached


def test_invalidate_identity_by_hub_id_removes_only_that_entry():
    recognizer = _make_recognizer()
    recognizer._identity_cache[5] = {"personId": "p5", "name": "Bob", "admissionNumber": "A005"}
    recognizer._identity_cache[6] = {"personId": "p6", "name": "Carol", "admissionNumber": "A006"}

    recognizer.invalidate_identity(hub_id=5)

    assert 5 not in recognizer._identity_cache
    assert recognizer._identity_cache[6]["personId"] == "p6"


def test_invalidate_identity_by_person_id_removes_all_matching_entries():
    recognizer = _make_recognizer()
    recognizer._identity_cache[1] = {"personId": "p1", "name": "Dan", "admissionNumber": "A001"}
    recognizer._identity_cache[2] = {"personId": "p1", "name": "Dan", "admissionNumber": "A001"}
    recognizer._identity_cache[3] = {"personId": "p2", "name": "Eve", "admissionNumber": "A002"}

    recognizer.invalidate_identity(person_id="p1")

    assert 1 not in recognizer._identity_cache
    assert 2 not in recognizer._identity_cache
    assert recognizer._identity_cache[3]["personId"] == "p2"
#!/usr/bin/env python3
"""Dispatch incoming Tally form submissions to the appropriate worker script."""

import argparse
import json
import pathlib
import subprocess
import sys


BASE_DIR = pathlib.Path(__file__).resolve().parent


def _find_field(fields, predicate):
    for field in fields:
        try:
            if predicate(field):
                return field
        except Exception:
            continue
    return None


def _extract_select_text(field):
    value = field.get("value")
    if isinstance(value, list):
        if not value:
            return None
        selected = value[0]
        for opt in field.get("options", []):
            if opt.get("id") == selected:
                return opt.get("text")
        return selected
    if isinstance(value, str):
        return value
    return None


def _make_test_payload(kind: str) -> dict:
    mapping = {
        "audio": ("audio", "Présentiel (fichier audio)"),
        "video": ("video", "Distanciel (fichier vidéo)"),
        "capsule": ("capsule", "Capsule vidéo sur le site de la faculté"),
    }
    value_id, label = mapping[kind]
    return {
        "data": {
            "fields": [
                {"key": "question_gdaklO", "value": "Test Faculty"},
                {"key": "question_y2MdQg", "value": "Test Class"},
                {"key": "question_0BdD9y", "value": 1},
                {"key": "question_a59ab9", "value": "Test Chapter"},
                {
                    "label": "Type de cours ?",
                    "value": [value_id],
                    "options": [{"id": value_id, "text": label}],
                },
            ]
        }
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Dispatch Tally form submissions to workers")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--test-audio", action="store_true", help="run with placeholder audio payload")
    group.add_argument("--test-video", action="store_true", help="run with placeholder video payload")
    group.add_argument("--test-capsule", action="store_true", help="run with placeholder capsule payload")
    args = parser.parse_args()

    if args.test_audio:
        payload = _make_test_payload("audio")
    elif args.test_video:
        payload = _make_test_payload("video")
    elif args.test_capsule:
        payload = _make_test_payload("capsule")
    else:
        payload = json.load(sys.stdin)
    fields = payload.get("data", {}).get("fields")
    if not isinstance(fields, list):
        raise ValueError("Invalid Tally payload: missing fields")

    # Validate required fields
    faculty_field = _find_field(fields, lambda f: f.get("key") == "question_gdaklO" and f.get("value"))
    if not faculty_field:
        raise ValueError("Missing \u201cFormation ?\u201d field")

    class_keys = [
        "question_y2MdQg",
        "question_1dX6bp",
        "question_MNXMrM",
        "question_JpqMVo",
        "question_gdavQO",
        "question_y2Mz0g",
        "question_XJLzRg",
    ]
    class_field = None
    for key in class_keys:
        class_field = _find_field(fields, lambda f, k=key: f.get("key") == k and f.get("value"))
        if class_field:
            break
    if not class_field:
        raise ValueError("Missing class dropdown selection")

    number_field = _find_field(
        fields,
        lambda f: f.get("key") == "question_0BdD9y" and isinstance(f.get("value"), (int, float)),
    )
    if not number_field:
        raise ValueError("Missing \u201cNum\u00e9ro de CM\u201d field")

    chapter_field = _find_field(
        fields,
        lambda f: f.get("key") == "question_a59ab9" and isinstance(f.get("value"), str) and f.get("value").strip(),
    )
    if not chapter_field:
        raise ValueError("Missing \u201cTitre du cours\u201d field")

    type_field = None
    for f in fields:
        label = (f.get("label") or f.get("question") or "").lower()
        if "type de cours" in label:
            type_field = f
            break
    if not type_field:
        raise ValueError("Missing \u201cType de cours ?\u201d field")

    type_text = _extract_select_text(type_field)
    if not type_text:
        raise ValueError("Invalid \u201cType de cours ?\u201d field")
    type_text = type_text.strip()

    if type_text == "Pr\u00e9sentiel (fichier audio)":
        script = "rt_audio.py"
    elif type_text == "Distanciel (fichier vid\u00e9o)":
        script = "zoom.py"
    elif type_text == "Capsule vid\u00e9o sur le site de la facult\u00e9":
        script = "download.py"
    else:
        raise ValueError(f"Unknown course type: {type_text}")

    target = BASE_DIR / script
    subprocess.run(
        [sys.executable, str(target)],
        input=json.dumps(payload).encode(),
        check=True,
    )


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # pragma: no cover - simple error reporting
        print(f"[mastermind] error: {exc}", file=sys.stderr)
        sys.exit(1)

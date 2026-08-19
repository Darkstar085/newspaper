import json
from pathlib import Path

STATE_FILE = Path("state.json")


def load_state():
    if not STATE_FILE.exists():
        return {"sent": {}, "hashes": {}}

    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        print("⚠ state.json could not be read; starting with empty state.")
        return {"sent": {}, "hashes": {}}

    data.setdefault("sent", {})
    data.setdefault("hashes", {})
    return data


def save_state(state):
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(
        json.dumps(state, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    tmp.replace(STATE_FILE)


def edition_key(name, date):
    return f"{name.lower()}:{date}"


def already_sent(state, name, date):
    return edition_key(name, date) in state["sent"]


def record_sent(state, name, date, pdf_hash, pages=None, size_mb=None, uploader="bot"):
    key = edition_key(name, date)

    state["sent"][key] = {
        "sent_at": __import__("datetime").datetime.utcnow().isoformat() + "Z",
        "sha256": pdf_hash,
        "pages": pages,
        "size_mb": round(size_mb, 2) if size_mb is not None else None,
        "uploader": uploader,
    }
    state["hashes"][pdf_hash] = key
    save_state(state)


def hash_owner(state, pdf_hash):
    return state["hashes"].get(pdf_hash)

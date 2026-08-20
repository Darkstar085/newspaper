import json
from datetime import datetime
from pathlib import Path

STATE_FILE = Path("state.json")


def _paper_from_key(key):
    return key.split(":", 1)[0].lower() if ":" in key else None


def _date_from_key(key):
    return key.rsplit(":", 1)[-1] if ":" in key else ""


def _latest_only(data):
    sent = data.setdefault("sent", {})
    hashes = data.setdefault("hashes", {})

    latest_keys = {}
    for key in sent:
        paper = _paper_from_key(key)
        if paper and (paper not in latest_keys or _date_from_key(key) > _date_from_key(latest_keys[paper])):
            latest_keys[paper] = key

    keep = set(latest_keys.values())
    data["sent"] = {key: value for key, value in sent.items() if key in keep}
    data["hashes"] = {pdf_hash: key for pdf_hash, key in hashes.items() if key in keep}
    return data


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
    cleaned = _latest_only(data)
    if cleaned != data:
        save_state(cleaned)
        print("🧹 Removed stale delivery-state entries; keeping latest edition per paper.")
    return cleaned


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


def _remove_previous_paper_state(state, name, keep_key):
    paper = name.lower()
    old_keys = [key for key in state["sent"] if _paper_from_key(key) == paper and key != keep_key]
    old_hashes = set()
    for key in old_keys:
        record = state["sent"].pop(key, {})
        old_hash = record.get("sha256")
        if old_hash:
            old_hashes.add(old_hash)

    for pdf_hash in old_hashes:
        if state["hashes"].get(pdf_hash) in old_keys:
            state["hashes"].pop(pdf_hash, None)


def record_sent(state, name, date, pdf_hash, pages=None, size_mb=None, uploader="bot", duplicate_of=None):
    key = edition_key(name, date)
    _remove_previous_paper_state(state, name, key)

    record = {
        "sent_at": datetime.utcnow().isoformat() + "Z",
        "sha256": pdf_hash,
        "pages": pages,
        "size_mb": round(size_mb, 2) if size_mb is not None else None,
        "uploader": uploader,
    }
    if duplicate_of:
        record["duplicate_of"] = duplicate_of
        owner_record = state["sent"].get(duplicate_of, {})
        record["sent_at"] = owner_record.get("sent_at", record["sent_at"])

    state["sent"][key] = record
    state["hashes"][pdf_hash] = key
    save_state(state)


def hash_owner(state, pdf_hash):
    return state["hashes"].get(pdf_hash)

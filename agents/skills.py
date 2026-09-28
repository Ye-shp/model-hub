"""Load only the selected task playbook; never inject the entire skill library."""
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1] / "skills"


def load_skill(name: str) -> dict:
    if not re.fullmatch(r"[a-z0-9-]{1,64}", name):
        raise ValueError("Invalid skill name")
    path = ROOT / name / "SKILL.md"
    if not path.is_file():
        raise ValueError("Unknown skill")
    text = path.read_text(encoding="utf-8")
    _, header, body = text.split("---", 2)
    meta = dict(line.split(":", 1) for line in header.strip().splitlines() if ":" in line)
    return {"id": name, "name": meta["name"].strip(), "description": meta["description"].strip(), "instructions": body.strip()}


def catalog() -> list[dict]:
    return [{k: v for k, v in load_skill(p.parent.name).items() if k != "instructions"} for p in sorted(ROOT.glob("*/SKILL.md"))]

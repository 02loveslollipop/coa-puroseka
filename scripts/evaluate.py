"""Record real base/enriched traces alongside independently executed reference SQL."""

import asyncio
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from fastapi.encoders import jsonable_encoder
from app.config import artifact, ARTIFACTS
from app.database import physical_schema
from app.inference import infer
from app.query import execute_query


async def run():
    examples = artifact("few_shots.json")
    schema = set(physical_schema())
    report = []
    # References establish database results; no LLM judge or automatic success claim.
    for example in examples[:3]:
        reference = execute_query(example["sql"], schema)
        entry = {
            "question": example["question"],
            "reference": reference,
            "interpretation": example["note"],
            "runs": [],
        }
        for mode in ("base", "enriched"):
            result = await infer(example["question"], mode, [])
            entry["runs"].append(result)
            print(mode, result["status"], result.get("sql"), flush=True)
        report.append(entry)
        (ARTIFACTS / "evaluation.json").write_text(
            json.dumps(jsonable_encoder(report), ensure_ascii=False, indent=2)
        )
    print(
        "Saved reference results and real agent traces in artifacts/evaluation.json",
        flush=True,
    )


if __name__ == "__main__":
    asyncio.run(run())

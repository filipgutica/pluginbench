"""Source-bound synthetic parser receipts shared by Fallow assessment tests."""

import json
from pathlib import Path


def syntax_proof(
    report: dict[str, object], snapshot: Path, config: Path, inputs: list[str]
) -> None:
    import hashlib

    def digest(data: bytes) -> str:
        return "sha256:" + hashlib.sha256(data).hexdigest()

    config.write_text(json.dumps({"entry": ["src/index.ts"], "production": True}))
    argv = [
        "fallow",
        "--root",
        str(snapshot),
        "--config",
        str(config),
        "--format",
        "json",
        "--quiet",
        "--explain",
        "--no-cache",
    ]
    report["discovery"] = {"files": inputs, "file_count": len(inputs)}
    report["syntax_validation"] = {
        "schema_version": 1,
        "parser": "oxc-parser",
        "version": "0.126.0",
        "commit": "836fe9d51c11981162461216f37a067fc14441a7",
        "producer_version": "3.18.0",
        "parser_options": "fallow-3.18.0-default-primary",
        "config_digest": digest(config.read_bytes()),
        "lock_digest": digest(b"lock"),
        "package_integrity": "sha512-FktCvLby/mOHyuijZt22+nOt10dS24gGUZE3XwIbUg7Kf4+rer3/5T7RgwzazlNuVsCjPloZ3p8E+4ONT3A8Kw==",
        "binding": {"native": {"version": "0.126.0", "integrity": "sha512:binding"}},
        "dependency_hashes": {"parser": digest(b"parser")},
        "validator_files": {"1": digest(b"script")},
        "producer_execution": [
            {"command": argv, "exit_code": 0},
            {"command": argv + ["list", "--files"], "exit_code": 0},
        ],
        "validator_execution": {"exit_code": 0},
        "files": [
            {
                "path": path,
                "source_digest": digest((snapshot / path).read_bytes()),
                "status": "valid",
                "errors": [],
            }
            for path in inputs
        ],
    }

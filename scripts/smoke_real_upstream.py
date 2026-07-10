from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import httpx


def compact_body(text: str) -> str:
    cleaned = text.replace("\n", " ")
    for marker in ("Bearer ", "sk-"):
        if marker in cleaned:
            return "[redacted body contained secret-like marker]"
    return cleaned[:220]


def load_auth_header() -> str | None:
    token = os.getenv("CTC_SMOKE_PROXY_TOKEN") or os.getenv("CTC_PROXY_TOKEN")
    if token:
        return f"Bearer {token}"
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description="Small CTC smoke test against a running CTC proxy.")
    parser.add_argument("--ctc-base", default="http://127.0.0.1:8787/v1")
    parser.add_argument("--model", default=os.getenv("CTC_SMOKE_MODEL", "gpt-5.5"))
    parser.add_argument("--stream", action="store_true")
    parser.add_argument("--tool-output-test", action="store_true")
    parser.add_argument("--auth-required", action="store_true")
    args = parser.parse_args()

    headers = {"content-type": "application/json"}
    auth = load_auth_header()
    if auth:
        headers["authorization"] = auth
    elif args.auth_required:
        print("CTC proxy token is not set; refusing auth-required smoke test.", file=sys.stderr)
        return 2

    base = args.ctc_base.rstrip("/")
    with httpx.Client(timeout=120, trust_env=False) as client:
        models = client.get(f"{base}/models", headers=headers)
        print("models_status", models.status_code)
        print("models_body_prefix", compact_body(models.text))

        if args.tool_output_test:
            noisy = "\n".join(
                [
                    "running command",
                    "file /workspace/example.py line 10",
                    "WARNING something minor",
                    "Traceback (most recent call last):",
                    "Exception: synthetic failure line for compression retention",
                ]
                + [f"repeated line {i % 5}" for i in range(1200)]
                + ["summary: total=1200 failed=1 passed=1199"]
            )
            payload = {
                "model": args.model,
                "stream": args.stream,
                "input": [
                    {"role": "user", "content": "Say ok."},
                    {"type": "function_call_output", "call_id": "call_smoke", "output": noisy},
                ],
            }
        else:
            payload = {
                "model": args.model,
                "stream": args.stream,
                "input": "Reply with exactly: CTC OK",
            }
        if args.stream:
            with client.stream("POST", f"{base}/responses", headers=headers, json=payload) as res:
                print("responses_status", res.status_code)
                total = 0
                for chunk in res.iter_bytes():
                    total += len(chunk)
                    if total > 500:
                        break
                print("stream_bytes_prefix", total)
        else:
            res = client.post(f"{base}/responses", headers=headers, json=payload)
            print("responses_status", res.status_code)
            print("responses_body_prefix", compact_body(res.text))
            if res.status_code >= 400:
                return 3

        if models.status_code >= 400:
            return 3

    db_hint = Path(os.getenv("CTC_DB_PATH", "ctc.sqlite3"))
    print("db_path", db_hint.name)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

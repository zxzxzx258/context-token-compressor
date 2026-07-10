from __future__ import annotations

import argparse
import time
from typing import Any

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

app = FastAPI()
REQUESTS: list[dict[str, Any]] = []


@app.get("/v1/models")
async def models():
    return {"object": "list", "data": [{"id": "mock-gpt-5.5", "object": "model"}]}


@app.post("/v1/responses")
async def responses(request: Request):
    body = await request.json()
    REQUESTS.append({"headers": dict(request.headers), "body": body})
    if body.get("stream"):
        def events():
            yield 'event: response.created\ndata: {"type":"response.created"}\n\n'
            time.sleep(0.01)
            yield 'event: response.output_text.delta\ndata: {"type":"response.output_text.delta","delta":"ok"}\n\n'
            yield 'event: response.completed\ndata: {"type":"response.completed","response":{"id":"resp_mock","output":[]}}\n\n'
            yield "data: [DONE]\n\n"

        return StreamingResponse(events(), media_type="text/event-stream")
    return JSONResponse(
        {
            "id": "resp_mock",
            "object": "response",
            "model": body.get("model", "mock-gpt-5.5"),
            "output": [{"type": "message", "content": [{"type": "output_text", "text": "ok"}]}],
        }
    )


@app.get("/_captured")
async def captured():
    return {"requests": REQUESTS}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8799)
    args = parser.parse_args()
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()

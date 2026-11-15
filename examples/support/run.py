"""A complete support task through the public API, with optional failure injection."""

import argparse
import asyncio
import json
from pathlib import Path

from agentic_runtime import Identity, Runtime, SQLiteStore

from .domain import SupportDatabase, support_agent


async def main(directory, fault):
    Path(directory).mkdir(parents=True, exist_ok=True)
    identity = Identity(user_id="demo-user", workspace_id="demo-workspace")
    database = SupportDatabase(Path(directory) / "support.sqlite", fault=fault)
    database.seed(identity)
    runtime = Runtime(SQLiteStore(Path(directory) / "runtime.sqlite"), [support_agent(database)])
    task = runtime.submit("support", "Resolve ticket T-100 and send the resolution", identity)
    task = await runtime.run(task.id)
    await runtime.respond(
        task.id,
        task.pending.id,
        identity,
        {"resolution": "Your invoice download link has been refreshed."},
    )
    task = await runtime.run(task.id)
    await runtime.respond(
        task.id, task.pending.id, identity, True, input_digest=task.pending.input_digest
    )
    task = await runtime.run(task.id)
    print(json.dumps(runtime.result(task.id, identity).model_dump(mode="json"), indent=2))
    print(f"Recorded sends: {database.count_sends()}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--directory", default=".demo-cli")
    parser.add_argument("--fault", default="none", choices=["none", "lost_response", "unknown"])
    args = parser.parse_args()
    asyncio.run(main(args.directory, args.fault))

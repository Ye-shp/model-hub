"""Single controller, bounded parallel model calls, durable jobs and explicit recovery."""
from __future__ import annotations

import asyncio
from contextlib import contextmanager, suppress
import os

import store
import workspace as ws


@contextmanager
def controller_lock():
    store.DATA.mkdir(parents=True, exist_ok=True)
    handle = (store.DATA / "controller.lock").open("a+b")
    try:
        if os.name == "nt":
            import msvcrt
            if handle.tell() == 0:
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        raise RuntimeError("Another controller is using this data folder. Run only one controller.") from None
    try:
        yield
    finally:
        handle.close()  # OS releases the lock, including after a crashed process.


async def execute(job: dict, gate: asyncio.Semaphore, runner):
    ws.event(job["id"], "started", f"Attempt {job['attempts']}; {job['profile']} profile")
    task = asyncio.create_task(runner(job, gate))
    try:
        while not task.done():
            await asyncio.wait({task}, timeout=1)
            current = ws.query("SELECT status FROM jobs WHERE id=?", (job["id"],))[0]["status"]
            if current != "running":
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
                return
            with ws.connection() as db, db:
                db.execute("UPDATE jobs SET heartbeat_at=? WHERE id=? AND status='running'", (store.now(), job["id"]))
        result = await task
        if ws.query("SELECT status FROM jobs WHERE id=?", (job["id"],))[0]["status"] != "running":
            return
        if job["skill"] != "cowork":  # Cowork replies live in the chat; its files are shared explicitly
            artifact = ws.write_artifact(job["project"], job["id"], "task-result.md", result.encode())
            ws.save_note(job["project"], f"Task {job['id'][:8]}",
                         f"Completed: {job['task'][:1000]}\nResult artifact: {artifact['id']}\n{result[:3500]}",
                         "checkpoint", [f"artifact:{artifact['id']}"])
        ws.finish_job(job["id"], "completed", result=result)
    except asyncio.CancelledError:
        task.cancel()
        with suppress(asyncio.CancelledError, Exception):
            await task
        ws.finish_job(job["id"], "interrupted", error="Controller stopped; saved progress is retained.")
        raise
    except TimeoutError:
        ws.finish_job(job["id"], "interrupted", error="Task time budget reached. Review saved progress, then Resume if needed.")
    except Exception as error:
        # Provider exceptions can contain full URLs/headers. Keep the UI message short and redacted.
        import hub
        message = str(error)
        if hub.HUB_KEY:
            message = message.replace(hub.HUB_KEY, "[redacted]")
        ws.finish_job(job["id"], "failed", error=f"{type(error).__name__}: {message[:1200]}")


async def serve(runner, configured, slots: int | None = None):
    # Jobs that run at once, and model calls in flight across all of them (each GPU serves 3 at a time).
    slots = slots or int(os.environ.get("AGENT_SLOTS", "4"))
    gate = asyncio.Semaphore(int(os.environ.get("AGENT_MODEL_CALLS", "6")))
    running = set()
    try:
        while True:
            # One bad job or a transient "database is locked" must not silently stop the worker.
            try:
                done = {task for task in running if task.done()}
                for task in done:
                    try:
                        await task
                    except Exception as error:  # execute() records its own failures; this is a backstop
                        print(f"[worker] job task ended with {type(error).__name__}: {error}", flush=True)
                running -= done
                if configured() and len(running) < slots:
                    job = ws.claim_job()
                    if job:
                        running.add(asyncio.create_task(execute(job, gate, runner)))
                        continue
            except asyncio.CancelledError:
                raise
            except Exception as error:
                print(f"[worker] loop error {type(error).__name__}: {error}; retrying in 5 s", flush=True)
                await asyncio.sleep(5)
                continue
            await asyncio.sleep(.5)
    finally:
        for task in running:
            task.cancel()
        await asyncio.gather(*running, return_exceptions=True)

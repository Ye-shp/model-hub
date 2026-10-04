"""Durable, model-free audience analytics service: python agents/audience_worker.py.

The SQLite queue owns scheduling, retry state and expiring leases. Killing/restarting
this process does not lose pending observations, and lease tokens fence late workers.
"""
from __future__ import annotations

import asyncio
import logging
import signal

import hub  # Load agents/.env before toolbox/store select the persistent data directory.
import audience_metrics

LOG = logging.getLogger("audience_worker")


def _repository(repository=None):
    if repository is None:
        import audience
        return audience
    return repository


def retry_delay(attempts: int, requested: int | None = None) -> int:
    """Bound exponential delays; honour safe provider cooldowns."""
    delay = min(21600, 60 * 2 ** min(max(int(attempts) - 1, 0), 9))
    return min(21600, max(delay, int(requested or 0)))


async def collect_once(*, collector=None, repository=None, lease_seconds: int = 180,
                       timeout_seconds: float = 120) -> dict:
    """Claim and collect one due observation; injectable without a live account."""
    if timeout_seconds <= 0 or lease_seconds < timeout_seconds + 30:
        raise ValueError("The lease must outlast collection timeout by at least 30 seconds")
    repository = _repository(repository)
    task = repository.claim_due(lease_seconds=lease_seconds)
    if task is None:
        return {"status": "idle"}
    task_id, lease_token = task["id"], task["lease_token"]
    try:
        result = await asyncio.wait_for((collector or audience_metrics.collect)(task), timeout=timeout_seconds)
        repository.finish_collection(task_id, lease_token, result["metrics"], result["source"],
                                     observed_at=result.get("observed_at"), warnings=result.get("warnings", []))
    except asyncio.CancelledError:
        # Synchronous DB fencing makes this cancellation cleanup atomic. A hard kill
        # is recovered by lease expiration instead. Never persist raw exception text.
        try:
            repository.fail_collection(task_id, lease_token, "Collection interrupted; retry scheduled.", retry_after_seconds=60)
        except Exception:
            # If SQLite is unavailable or this lease expired, restart recovery owns it.
            LOG.warning("Interrupted observation could not be released; lease recovery will retry it.")
        raise
    except Exception as error:
        if isinstance(error, audience_metrics.MetricsError):
            message = f"{error.code}: {error}"
            requested = error.retry_after_seconds
        elif isinstance(error, (TimeoutError, asyncio.TimeoutError)):
            message, requested = "Analytics collection timed out; retry scheduled.", None
        else:
            message, requested = "Analytics collection failed; retry scheduled.", None
        delay = retry_delay(task.get("attempts", 1), requested)
        try:
            repository.fail_collection(task_id, lease_token, message, retry_after_seconds=delay)
        except ValueError:
            # A replaced/expired lease cannot write a failure over its new owner.
            return {"status": "lease_lost", "task_id": task_id}
        return {"status": "retry", "task_id": task_id, "retry_after_seconds": delay}
    return {"status": "collected", "task_id": task_id}


async def serve(*, collector=None, repository=None, poll_seconds: float = 30,
                stop_event: asyncio.Event | None = None) -> None:
    """Run independently of Qwen, Open WebUI, and individual chat timeouts."""
    if poll_seconds <= 0:
        raise ValueError("Polling interval must be positive")
    repository = _repository(repository)
    repository.init()
    stop_event = stop_event or asyncio.Event()
    while not stop_event.is_set():
        try:
            result = await collect_once(collector=collector, repository=repository)
        except asyncio.CancelledError:
            raise
        except Exception:
            # DB/configuration exceptions can also contain secrets: no traceback.
            LOG.error("Audience worker could not process the queue; retrying.")
            result = {"status": "retry"}
        if result["status"] in {"collected", "lease_lost"} or result.get("task_id") is not None:
            continue
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=poll_seconds)
        except asyncio.TimeoutError:
            pass


async def _main() -> None:
    task = asyncio.current_task()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, task.cancel)
        except (NotImplementedError, RuntimeError):
            pass
    try:
        await serve()
    except asyncio.CancelledError:
        pass


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    asyncio.run(_main())

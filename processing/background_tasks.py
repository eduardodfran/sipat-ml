from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger(__name__)

_active_tasks: set[asyncio.Task] = set()
_shutdown_event = asyncio.Event()
_SHUTDOWN_TIMEOUT = 30.0


def _track_task(task: asyncio.Task) -> None:
    _active_tasks.discard(task)
    if task.cancelled():
        logger.warning("Background task cancelled: %s", task.get_name())
    elif task.exception():
        logger.error("Background task failed: %s - %s", task.get_name(), task.exception())


async def submit_background_task(coro, name: str = "background_task") -> asyncio.Task:
    """Submit a background task that will be tracked for graceful shutdown."""
    if _shutdown_event.is_set():
        raise RuntimeError("Server is shutting down, cannot accept new tasks")
    task = asyncio.create_task(coro, name=name)
    _active_tasks.add(task)
    task.add_done_callback(_track_task)
    return task


def signal_shutdown() -> None:
    """Signal that the server is shutting down."""
    _shutdown_event.set()


async def wait_for_tasks() -> None:
    """Wait for active tasks to complete, with a timeout."""
    logger.info("Shutdown initiated, waiting for %d active task(s)", len(_active_tasks))
    if _active_tasks:
        done, pending = await asyncio.wait(
            _active_tasks,
            timeout=_SHUTDOWN_TIMEOUT,
            return_when=asyncio.ALL_COMPLETED,
        )
        if pending:
            logger.warning(
                "Cancelling %d task(s) after %.0fs timeout",
                len(pending),
                _SHUTDOWN_TIMEOUT,
            )
            for task in pending:
                task.cancel()
            await asyncio.wait(pending, timeout=5.0)
    logger.info("Shutdown complete")


def get_active_task_count() -> int:
    """Return the number of active background tasks."""
    return len(_active_tasks)

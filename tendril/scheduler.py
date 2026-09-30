"""Background loop that wakes due goals."""

from __future__ import annotations

import asyncio
import logging
import time

from .agent import Agent
from .store import Store

log = logging.getLogger("tendril.scheduler")


class Scheduler:
    def __init__(self, store: Store, agent: Agent, tick_seconds: float, max_concurrent: int):
        self.store = store
        self.agent = agent
        self.tick_seconds = tick_seconds
        self.slots = asyncio.Semaphore(max_concurrent)
        self.inflight: dict[int, asyncio.Task] = {}
        self._poke = asyncio.Event()

    def poke(self) -> None:
        """Check for due goals now instead of waiting for the next tick."""
        self._poke.set()

    async def run_forever(self) -> None:
        self.store.reset_running()
        while True:
            for goal in self.store.due_goals(time.time()):
                if goal["id"] not in self.inflight:
                    self.inflight[goal["id"]] = asyncio.create_task(self._run(goal["id"]))
            try:
                await asyncio.wait_for(self._poke.wait(), self.tick_seconds)
            except asyncio.TimeoutError:
                pass
            self._poke.clear()

    async def _run(self, goal_id: int) -> None:
        try:
            async with self.slots:
                await self.agent.run(goal_id)
        finally:
            self.inflight.pop(goal_id, None)
            self.poke()  # a wake request may have arrived while it was running

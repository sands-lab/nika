from __future__ import annotations

import asyncio
import random
from typing import Iterable, Optional

from nika.net_env.base import NetworkEnvBase
from nika.net_env.net_env_pool import get_net_env_instance
from nika.service.kathara import KatharaAPIALL


class WebBrowsingTrafficGenerator:
    def __init__(
        self,
        scenario_name: NetworkEnvBase | str | None = None,
        request_delay_range: tuple[float, float] = (1.0, 5.0),
        pages_per_session_range: tuple[int, int] = (3, 10),
        loop_forever: bool = True,
        **kwargs,
    ):
        if scenario_name is None:
            self.net_env = get_net_env_instance(
                "campus_lan", workload="static", **kwargs
            )
        elif isinstance(scenario_name, str):
            self.net_env = get_net_env_instance(scenario_name, **kwargs)
        else:
            self.net_env = scenario_name
        self.kathara_api = KatharaAPIALL(lab_name=self.net_env.lab.name)
        self.clients: list[str] = self.net_env.hosts
        self.web_servers: list[str] = self.net_env.servers["web"]
        self.web_domains: list[str] = []
        # make sure urls in right format
        for url in self.net_env.web_urls:
            if url.startswith("http"):
                if url.endswith("/"):
                    self.web_domains.append(url)
                else:
                    self.web_domains.append(f"{url}/")
            else:
                self.web_domains.append(f"http://{url}/")

        self.request_delay_range = request_delay_range
        self.pages_per_session_range = pages_per_session_range
        self.loop_forever = loop_forever

        self._traffic_task: Optional[asyncio.Task] = None

    async def _browse_once(self, src_host: str, web_domain: str):
        cmd = f"ab -n 500 -c 200 -k {web_domain}"
        return await self.kathara_api._run_cmd_async(src_host, cmd)

    async def _client_session(self, src_host: str):
        while True:
            num_pages = random.randint(*self.pages_per_session_range)

            if num_pages >= len(self.web_domains):
                domains_to_visit: Iterable[str] = random.sample(
                    self.web_domains, k=len(self.web_domains)
                )
            else:
                domains_to_visit = random.sample(self.web_domains, k=num_pages)

            for web_domain in domains_to_visit:
                await self._browse_once(src_host, web_domain)

                delay = random.uniform(*self.request_delay_range)
                await asyncio.sleep(delay)

            if not self.loop_forever:
                break

            session_pause = random.uniform(5.0, 15.0)
            await asyncio.sleep(session_pause)

    async def _generate_traffic_async(self):
        tasks = [
            asyncio.create_task(self._client_session(client)) for client in self.clients
        ]
        await asyncio.gather(*tasks)

    async def generate_traffic(self):
        """
        Directly generate traffic in a blocking manner.
        """
        await self._generate_traffic_async()

    def start_traffic_background(self) -> asyncio.Task:
        """
        Start generating traffic in the background.
        """
        loop = asyncio.get_running_loop()
        self._traffic_task = loop.create_task(self._generate_traffic_async())
        return self._traffic_task

    def stop_traffic(self):
        if self._traffic_task and not self._traffic_task.done():
            self._traffic_task.cancel()

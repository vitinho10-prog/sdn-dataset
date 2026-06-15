#!/usr/bin/env python3
"""
traffic.py - Automated SDN Traffic Generator
=============================================
Generates realistic, varied traffic patterns inside a Mininet network
to produce rich training data for deep learning models.

Traffic Patterns:
  1. background  - Low, steady background traffic (1-5 Mbps)
  2. burst       - Short high-bandwidth bursts (50-90 Mbps)
  3. elephant    - Long-duration large flows (30-80 Mbps, 30-120s)
  4. congestion  - Deliberate congestion scenario (multiple flows saturating a link)

Usage (inside Mininet Python API or from external script):
    from traffic import TrafficGenerator
    gen = TrafficGenerator(net)
    gen.run_scenario("congestion", duration=60)

    # Or run the full benchmark sequence:
    gen.run_benchmark(total_duration=300)

Standalone (requires Mininet running):
    sudo python3 traffic.py --scenario all --duration 300
"""

import argparse
import logging
import random
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import List, Optional

LOG = logging.getLogger("sdn.traffic")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s %(message)s"
)

# ─── Host Configuration ────────────────────────────────────────────────────────

ALL_HOSTS = [f"10.0.0.{i}" for i in range(1, 9)]

HOST_PAIRS = [
    ("10.0.0.1", "10.0.0.5"),
    ("10.0.0.2", "10.0.0.6"),
    ("10.0.0.3", "10.0.0.7"),
    ("10.0.0.4", "10.0.0.8"),
    ("10.0.0.1", "10.0.0.3"),   # intra-pod
    ("10.0.0.5", "10.0.0.7"),   # intra-pod (other side)
    ("10.0.0.2", "10.0.0.8"),   # cross-pod
]

IPERF_PORT_BASE = 5200


# ─── Flow Descriptor ──────────────────────────────────────────────────────────

@dataclass
class Flow:
    src: str
    dst: str
    bandwidth_mbps: float
    duration_sec: float
    parallel: int = 1
    udp: bool = False
    port: int = 5201
    name: str = ""
    start_delay: float = 0.0

    def __str__(self):
        proto = "UDP" if self.udp else "TCP"
        return (f"Flow[{self.name}] {self.src}→{self.dst} "
                f"{self.bandwidth_mbps:.1f}Mbps {proto} {self.duration_sec:.0f}s")


# ─── iperf3 Runner ─────────────────────────────────────────────────────────────

class IperfRunner:
    """
    Manages iperf3 server and client processes.
    Servers are started once per destination host.
    """

    def __init__(self, net=None):
        self.net      = net
        self._servers = {}
        self._clients = []
        self._lock    = threading.Lock()

    def start_server(self, host_ip: str, port: int = 5201):
        """Start an iperf3 server on the given host."""
        key = (host_ip, port)
        with self._lock:
            if key in self._servers:
                return
        cmd  = ["iperf3", "-s", "-p", str(port), "-D", "--one-off"]
        proc = self._exec_on_host(host_ip, cmd, background=True)
        with self._lock:
            self._servers[key] = proc
        LOG.debug("iperf3 server started on %s:%d", host_ip, port)

    def run_client(self, flow: Flow) -> Optional[subprocess.Popen]:
        """Start an iperf3 client for the given flow. Returns the process."""
        self.start_server(flow.dst, flow.port)
        time.sleep(0.2)
        cmd = [
            "iperf3",
            "-c", flow.dst,
            "-p", str(flow.port),
            "-b", f"{flow.bandwidth_mbps}M",
            "-t", str(int(flow.duration_sec)),
            "-P", str(flow.parallel),
            "-J",
        ]
        if flow.udp:
            cmd.append("-u")
        proc = self._exec_on_host(flow.src, cmd, background=True)
        with self._lock:
            self._clients.append((flow, proc))
        LOG.info("Started %s", flow)
        return proc

    def _exec_on_host(self, host_ip: str, cmd: List[str], background=False):
        """Execute a command on a Mininet host or via subprocess."""
        if self.net:
            host = self._get_mn_host(host_ip)
            if host:
                return host.popen(cmd) if background else host.cmd(*cmd)
        if background:
            return subprocess.Popen(cmd,
                                    stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL)
        return subprocess.run(cmd, capture_output=True, text=True)

    def _get_mn_host(self, host_ip: str):
        if not self.net:
            return None
        for h in self.net.hosts:
            if h.IP() == host_ip:
                return h
        return None

    def stop_all(self):
        """Kill all active iperf3 processes."""
        with self._lock:
            for _, proc in self._clients:
                try: proc.terminate()
                except Exception: pass
            for proc in self._servers.values():
                try: proc.terminate()
                except Exception: pass
            self._clients.clear()
            self._servers.clear()
        LOG.info("All iperf3 processes stopped")

    def cleanup_finished(self):
        with self._lock:
            self._clients = [(f, p) for f, p in self._clients if p.poll() is None]


# ─── Traffic Scenario Generators ──────────────────────────────────────────────

class ScenarioBuilder:
    """Builds lists of Flow objects for various traffic scenarios."""

    @staticmethod
    def background(duration=300) -> List[Flow]:
        """
        Low-bandwidth background noise across all pairs.
        Simula serviços sempre ativos: monitoramento, heartbeats, DNS.
        """
        flows = []
        for i, (src, dst) in enumerate(HOST_PAIRS[:4]):
            flows.append(Flow(
                src=src, dst=dst,
                bandwidth_mbps=random.uniform(1, 5),
                duration_sec=duration,
                port=IPERF_PORT_BASE + i,
                name=f"bg_{i}",
                start_delay=i * 0.5
            ))
        return flows

    @staticmethod
    def burst(duration=60) -> List[Flow]:
        """
        Short high-bandwidth bursts repeated throughout duration.
        Simula downloads, backups pontuais, transferências rápidas.
        """
        flows = []
        t = 0
        flow_id = 0
        while t < duration:
            src, dst = random.choice(HOST_PAIRS)
            bw  = random.uniform(50, 90)
            dur = random.uniform(3, 8)
            gap = random.uniform(2, 6)
            flows.append(Flow(
                src=src, dst=dst,
                bandwidth_mbps=bw,
                duration_sec=dur,
                port=IPERF_PORT_BASE + 10 + (flow_id % 10),
                name=f"burst_{flow_id}",
                start_delay=t
            ))
            t += dur + gap
            flow_id += 1
        return flows

    @staticmethod
    def elephant(duration=300) -> List[Flow]:
        """
        Few large long-lived flows.
        Simula replicação de banco de dados, transferência de VMs, backup contínuo.
        """
        flows = []
        for i, (src, dst) in enumerate(HOST_PAIRS[:3]):
            flows.append(Flow(
                src=src, dst=dst,
                bandwidth_mbps=random.uniform(30, 80),
                duration_sec=random.uniform(duration * 0.5, duration * 0.9),
                port=IPERF_PORT_BASE + 20 + i,
                name=f"elephant_{i}",
                start_delay=random.uniform(0, 10)
            ))
        return flows

    @staticmethod
    def congestion(duration=60) -> List[Flow]:
        """
        Multiple flows saturating the same bottleneck link.
        Simula congestionamento real em data centers.
        """
        flows = []
        congestion_pairs = [
            ("10.0.0.1", "10.0.0.5"),
            ("10.0.0.1", "10.0.0.6"),
            ("10.0.0.2", "10.0.0.5"),
            ("10.0.0.2", "10.0.0.7"),
            ("10.0.0.3", "10.0.0.6"),
        ]
        for i, (src, dst) in enumerate(congestion_pairs):
            flows.append(Flow(
                src=src, dst=dst,
                bandwidth_mbps=random.uniform(20, 40),
                duration_sec=duration,
                parallel=2,
                port=IPERF_PORT_BASE + 50 + i,
                name=f"cong_{i}",
                start_delay=i * 0.3
            ))
        return flows


# ─── Traffic Generator ─────────────────────────────────────────────────────────

class TrafficGenerator:
    """Orchestrates multiple flow scenarios over a Mininet network."""

    SCENARIOS = {
        "background": ScenarioBuilder.background,
        "burst":      ScenarioBuilder.burst,
        "elephant":   ScenarioBuilder.elephant,
        "congestion": ScenarioBuilder.congestion,
    }

    def __init__(self, net=None):
        self.net    = net
        self.runner = IperfRunner(net)
        self._stop  = threading.Event()

    def run_scenario(self, scenario_name: str, duration: int = 120):
        """Run a named traffic scenario."""
        builder = self.SCENARIOS.get(scenario_name)
        if not builder:
            raise ValueError(
                f"Unknown scenario: {scenario_name}. "
                f"Choose from: {list(self.SCENARIOS.keys())}"
            )
        flows = builder(duration)
        LOG.info("Scenario '%s': %d flows, duration=%ds",
                 scenario_name, len(flows), duration)
        self._execute_flows(flows, duration)

    def run_benchmark(self, total_duration=300):
        """
        Run all scenarios sequentially.
        Time budget split among scenarios.
        """
        scenarios = [
            ("background", 0.20),   # 20% do tempo
            ("burst",      0.25),   # 25% do tempo
            ("elephant",   0.25),   # 25% do tempo
            ("congestion", 0.30),   # 30% do tempo
        ]

        LOG.info("Starting full benchmark: %ds total", total_duration)

        for name, fraction in scenarios:
            if self._stop.is_set():
                break
            dur = int(total_duration * fraction)
            LOG.info("=== Scenario: %s (%ds) ===", name.upper(), dur)
            self.run_scenario(name, dur)
            LOG.info("Scenario %s complete. Cooldown 3s...", name)
            time.sleep(3)

        LOG.info("Benchmark complete.")

    def _execute_flows(self, flows: List[Flow], total_duration: float):
        """Schedule and execute flows based on their start_delay."""
        start_ts = time.time()
        threads  = []
        launched = set()

        try:
            while not self._stop.is_set():
                now = time.time() - start_ts
                if now >= total_duration:
                    break
                for i, flow in enumerate(flows):
                    if i not in launched and now >= flow.start_delay:
                        t = threading.Thread(
                            target=self._run_flow_sync,
                            args=(flow,),
                            daemon=True
                        )
                        t.start()
                        threads.append(t)
                        launched.add(i)
                self.runner.cleanup_finished()
                time.sleep(0.1)
        finally:
            self.runner.stop_all()
            for t in threads:
                t.join(timeout=2)

    def _run_flow_sync(self, flow: Flow):
        """Execute a single flow synchronously."""
        try:
            proc = self.runner.run_client(flow)
            if proc:
                proc.wait(timeout=flow.duration_sec + 10)
        except subprocess.TimeoutExpired:
            try: proc.terminate()
            except Exception: pass
        except Exception as e:
            LOG.debug("Flow %s error: %s", flow.name, e)

    def stop(self):
        self._stop.set()
        self.runner.stop_all()


# ─── CLI Entry Point ───────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="SDN Traffic Generator")
    parser.add_argument(
        "--scenario",
        default="congestion",
        choices=list(TrafficGenerator.SCENARIOS.keys()) + ["all"],
        help="Traffic scenario to run"
    )
    parser.add_argument("--duration", default=120, type=int,
                        help="Duration in seconds")
    parser.add_argument("--benchmark", action="store_true",
                        help="Run all scenarios sequentially")
    args = parser.parse_args()

    gen = TrafficGenerator(net=None)

    try:
        if args.benchmark or args.scenario == "all":
            gen.run_benchmark(total_duration=args.duration)
        else:
            gen.run_scenario(args.scenario, duration=args.duration)
    except KeyboardInterrupt:
        LOG.info("Interrupted by user")
    finally:
        gen.stop()


if __name__ == "__main__":
    main()

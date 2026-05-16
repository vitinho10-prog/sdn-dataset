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
  4. mice        - Many small short flows (0.5-2 Mbps, 1-5s)
  5. congestion  - Deliberate congestion scenario (multiple flows saturating a link)
  6. sweep       - Linear ramp from 5 Mbps to 95 Mbps and back

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
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

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
        """
        net: Mininet network object (optional).
        When None, uses subprocess with SSH/direct exec.
        """
        self.net      = net
        self._servers = {}   # {(host_ip, port): process}
        self._clients = []   # list of active client procs
        self._lock    = threading.Lock()

    def start_server(self, host_ip: str, port: int = 5201):
        """Start an iperf3 server on the given host."""
        key = (host_ip, port)
        with self._lock:
            if key in self._servers:
                return   # already running

        cmd = ["iperf3", "-s", "-p", str(port), "-D", "--one-off"]
        proc = self._exec_on_host(host_ip, cmd, background=True)
        with self._lock:
            self._servers[key] = proc
        LOG.debug("iperf3 server started on %s:%d", host_ip, port)

    def run_client(self, flow: Flow) -> Optional[subprocess.Popen]:
        """Start an iperf3 client for the given flow. Returns the process."""
        self.start_server(flow.dst, flow.port)
        time.sleep(0.2)   # let server bind

        cmd = [
            "iperf3",
            "-c", flow.dst,
            "-p", str(flow.port),
            "-b", f"{flow.bandwidth_mbps}M",
            "-t", str(int(flow.duration_sec)),
            "-P", str(flow.parallel),
            "-J",   # JSON output
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
                if background:
                    return host.popen(cmd)
                else:
                    return host.cmd(*cmd)

        # Fallback: direct subprocess (for testing without Mininet)
        if background:
            return subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL
            )
        else:
            return subprocess.run(
                cmd,
                capture_output=True,
                text=True
            )

    def _get_mn_host(self, host_ip: str):
        """Find a Mininet host by IP."""
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
                try:
                    proc.terminate()
                except Exception:
                    pass
            for proc in self._servers.values():
                try:
                    proc.terminate()
                except Exception:
                    pass
            self._clients.clear()
            self._servers.clear()
        LOG.info("All iperf3 processes stopped")

    def cleanup_finished(self):
        """Remove completed client processes from tracking list."""
        with self._lock:
            active = [(f, p) for f, p in self._clients if p.poll() is None]
            self._clients = active


# ─── Traffic Scenario Generators ──────────────────────────────────────────────

class ScenarioBuilder:
    """
    Builds lists of Flow objects for various traffic scenarios.
    """

    @staticmethod
    def background(duration=300) -> List[Flow]:
        """Low-bandwidth background noise across all pairs."""
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
        """Short high-bandwidth bursts repeated throughout duration."""
        flows = []
        t = 0
        flow_id = 0
        while t < duration:
            src, dst = random.choice(HOST_PAIRS)
            bw   = random.uniform(50, 90)
            dur  = random.uniform(3, 8)
            gap  = random.uniform(2, 6)
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
        """Few large long-lived flows (elephant flows)."""
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
    def mice(duration=120) -> List[Flow]:
        """Many tiny short flows (mice flows)."""
        flows = []
        t = 0
        flow_id = 0
        while t < duration:
            n_flows = random.randint(3, 8)
            for j in range(n_flows):
                src, dst = random.choice(HOST_PAIRS)
                flows.append(Flow(
                    src=src, dst=dst,
                    bandwidth_mbps=random.uniform(0.5, 2),
                    duration_sec=random.uniform(1, 5),
                    port=IPERF_PORT_BASE + 30 + (flow_id % 20),
                    name=f"mice_{flow_id}",
                    start_delay=t + j * 0.1
                ))
                flow_id += 1
            t += random.uniform(2, 8)
        return flows

    @staticmethod
    def congestion(duration=60) -> List[Flow]:
        """
        Multiple flows saturating the same bottleneck link.
        Forces all traffic through spine→agg→lf1 path.
        """
        flows = []
        # All flows from h1,h2,h3 → h5,h6,h7 (cross-pod, same uplink)
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
                bandwidth_mbps=random.uniform(20, 40),  # aggregate > link capacity
                duration_sec=duration,
                parallel=2,
                port=IPERF_PORT_BASE + 50 + i,
                name=f"cong_{i}",
                start_delay=i * 0.3
            ))
        return flows

    @staticmethod
    def sweep(duration=120) -> List[Flow]:
        """
        Ramp bandwidth from 5 to 95 Mbps and back.
        Creates clear utilization gradient in the dataset.
        """
        flows = []
        step_dur   = 5.0
        bw_values  = list(range(5, 100, 10)) + list(range(90, 0, -10))
        flow_id    = 0
        t = 0

        for bw in bw_values:
            if t >= duration:
                break
            src, dst = random.choice(HOST_PAIRS[:4])
            flows.append(Flow(
                src=src, dst=dst,
                bandwidth_mbps=float(bw),
                duration_sec=step_dur,
                port=IPERF_PORT_BASE + 70 + (flow_id % 10),
                name=f"sweep_{flow_id}",
                start_delay=t
            ))
            t += step_dur
            flow_id += 1
        return flows

    @staticmethod
    def mixed(duration=300) -> List[Flow]:
        """Combination of background + bursts + elephants + mice."""
        flows = []
        flows.extend(ScenarioBuilder.background(duration))
        flows.extend(ScenarioBuilder.burst(duration // 3))
        flows.extend(ScenarioBuilder.elephant(duration))
        flows.extend(ScenarioBuilder.mice(duration // 2))
        return flows


# ─── Traffic Generator ─────────────────────────────────────────────────────────

class TrafficGenerator:
    """
    Orchestrates multiple flow scenarios over a Mininet network.
    """

    SCENARIOS = {
        "background":  ScenarioBuilder.background,
        "burst":       ScenarioBuilder.burst,
        "elephant":    ScenarioBuilder.elephant,
        "mice":        ScenarioBuilder.mice,
        "congestion":  ScenarioBuilder.congestion,
        "sweep":       ScenarioBuilder.sweep,
        "mixed":       ScenarioBuilder.mixed,
    }

    def __init__(self, net=None):
        self.net    = net
        self.runner = IperfRunner(net)
        self._stop  = threading.Event()

    def run_scenario(self, scenario_name: str, duration: int = 120):
        """Run a named traffic scenario."""
        builder = self.SCENARIOS.get(scenario_name)
        if not builder:
            raise ValueError(f"Unknown scenario: {scenario_name}. "
                             f"Choose from: {list(self.SCENARIOS.keys())}")

        flows = builder(duration)
        LOG.info("Scenario '%s': %d flows, duration=%ds",
                 scenario_name, len(flows), duration)
        self._execute_flows(flows, duration)

    def run_benchmark(self, total_duration=600):
        """
        Run all scenarios sequentially for comprehensive dataset coverage.
        Time budget is split among scenarios.
        """
        scenarios = [
            ("background", 0.15),
            ("sweep",      0.10),
            ("burst",      0.15),
            ("elephant",   0.15),
            ("mice",       0.10),
            ("congestion", 0.15),
            ("mixed",      0.20),
        ]

        LOG.info("Starting full benchmark: %ds total", total_duration)

        for name, fraction in scenarios:
            if self._stop.is_set():
                break
            dur = int(total_duration * fraction)
            LOG.info("=== Scenario: %s (%ds) ===", name.upper(), dur)
            self.run_scenario(name, dur)
            LOG.info("Scenario %s complete. Cooldown 5s...", name)
            time.sleep(5)

        LOG.info("Benchmark complete.")

    def _execute_flows(self, flows: List[Flow], total_duration: float):
        """
        Schedule and execute flows based on their start_delay.
        Uses threading for concurrent execution.
        """
        start_ts  = time.time()
        threads   = []
        launched  = set()

        try:
            while not self._stop.is_set():
                now = time.time() - start_ts
                if now >= total_duration:
                    break

                # Launch flows whose start_delay has passed
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
            LOG.info("Stopping all flows...")
            self.runner.stop_all()
            for t in threads:
                t.join(timeout=2)

    def _run_flow_sync(self, flow: Flow):
        """Execute a single flow synchronously (blocking for flow.duration_sec)."""
        try:
            proc = self.runner.run_client(flow)
            if proc:
                proc.wait(timeout=flow.duration_sec + 10)
        except subprocess.TimeoutExpired:
            try:
                proc.terminate()
            except Exception:
                pass
        except Exception as e:
            LOG.debug("Flow %s error: %s", flow.name, e)

    def stop(self):
        self._stop.set()
        self.runner.stop_all()

    def generate_random_scenario(self, duration=60) -> List[Flow]:
        """
        Generate a fully random traffic scenario with:
        - Random number of flows (3-10)
        - Random bandwidth per flow
        - Random start delays
        - Mix of protocols
        """
        n_flows = random.randint(3, 10)
        flows   = []
        used_ports = set()

        for i in range(n_flows):
            src, dst = random.choice(HOST_PAIRS)
            bw       = random.choice([
                random.uniform(1, 10),    # mice
                random.uniform(10, 50),   # medium
                random.uniform(50, 95),   # elephant
            ])
            dur      = random.uniform(duration * 0.3, duration * 0.9)
            port     = random.randint(5300, 5500)
            while port in used_ports:
                port = random.randint(5300, 5500)
            used_ports.add(port)

            flows.append(Flow(
                src=src, dst=dst,
                bandwidth_mbps=round(bw, 1),
                duration_sec=round(dur, 1),
                udp=random.random() < 0.3,   # 30% UDP
                port=port,
                name=f"random_{i}",
                start_delay=random.uniform(0, duration * 0.3)
            ))

        LOG.info("Generated random scenario: %d flows", n_flows)
        return flows

    def run_random(self, duration=60):
        """Execute a random scenario."""
        flows = self.generate_random_scenario(duration)
        self._execute_flows(flows, duration)


# ─── Congestion Event Logger ───────────────────────────────────────────────────

class CongestionEventLogger:
    """
    Monitors throughput and logs congestion events for labeling.
    Useful to add ground-truth labels to the dataset.
    """

    def __init__(self, threshold_pct=80.0, output_path="congestion_events.jsonl"):
        self.threshold   = threshold_pct
        self.output_path = output_path
        self._events     = []
        self._fh         = open(output_path, "w", buffering=1)

    def check(self, link_id: str, utilization: float, ts: float):
        """Log congestion if threshold exceeded."""
        if utilization >= self.threshold:
            event = {
                "ts":          ts,
                "link_id":     link_id,
                "utilization": utilization,
                "label":       "congested",
            }
            self._events.append(event)
            self._fh.write(json.dumps(event) + "\n")
            return True
        return False

    def close(self):
        self._fh.close()
        LOG.info("Congestion events logged: %d events → %s",
                 len(self._events), self.output_path)


# ─── CLI Entry Point ───────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="SDN Traffic Generator")
    parser.add_argument(
        "--scenario",
        default="mixed",
        choices=list(TrafficGenerator.SCENARIOS.keys()) + ["all", "random"],
        help="Traffic scenario to run"
    )
    parser.add_argument("--duration", default=120, type=int,
                        help="Duration in seconds")
    parser.add_argument("--benchmark", action="store_true",
                        help="Run full benchmark sequence")
    args = parser.parse_args()

    gen = TrafficGenerator(net=None)   # standalone mode

    try:
        if args.benchmark:
            gen.run_benchmark(total_duration=args.duration)
        elif args.scenario == "all":
            gen.run_benchmark(total_duration=args.duration)
        elif args.scenario == "random":
            gen.run_random(duration=args.duration)
        else:
            gen.run_scenario(args.scenario, duration=args.duration)
    except KeyboardInterrupt:
        LOG.info("Interrupted by user")
    finally:
        gen.stop()


if __name__ == "__main__":
    main()
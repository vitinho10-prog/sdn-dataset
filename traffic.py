#!/usr/bin/env python3
"""
traffic.py - SDN Traffic Generator
====================================
Gera padrões de tráfego variados dentro do Mininet
para produzir dados ricos para modelos LSTM/GRU.

Padrões disponíveis:
  1. background  - Tráfego baixo e constante (1-5 Mbps)
  2. burst       - Picos curtos de alta banda (50-90 Mbps)
  3. elephant    - Fluxos grandes e longos (30-80 Mbps)
  4. congestion  - Congestionamento intencional do link

Uso:
    from traffic import TrafficGenerator
    gen = TrafficGenerator(net)
    gen.run_scenario("congestion", duration=60)
    gen.run_benchmark(total_duration=300)
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

# ─── Configuração dos hosts ────────────────────────────────────────────────────

HOST_PAIRS = [
    ("10.0.0.1", "10.0.0.5"),
    ("10.0.0.2", "10.0.0.6"),
    ("10.0.0.3", "10.0.0.7"),
    ("10.0.0.4", "10.0.0.8"),
    ("10.0.0.1", "10.0.0.3"),
    ("10.0.0.2", "10.0.0.8"),
]

IPERF_PORT_BASE = 5200


# ─── Descritor de fluxo ────────────────────────────────────────────────────────

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


# ─── Executor iperf3 ───────────────────────────────────────────────────────────

class IperfRunner:
    """Gerencia processos iperf3 servidor e cliente."""

    def __init__(self, net=None):
        self.net      = net
        self._servers = {}
        self._clients = []
        self._lock    = threading.Lock()

    def start_server(self, host_ip: str, port: int = 5201):
        """Inicia servidor iperf3 no host destino."""
        key = (host_ip, port)
        with self._lock:
            if key in self._servers:
                return
        cmd  = ["iperf3", "-s", "-p", str(port), "-D", "--one-off"]
        proc = self._exec_on_host(host_ip, cmd, background=True)
        with self._lock:
            self._servers[key] = proc

    def run_client(self, flow: Flow) -> Optional[subprocess.Popen]:
        """Inicia cliente iperf3 para o fluxo dado."""
        self.start_server(flow.dst, flow.port)
        time.sleep(0.2)
        cmd = [
            "iperf3",
            "-c", flow.dst,
            "-p", str(flow.port),
            "-b", f"{flow.bandwidth_mbps}M",
            "-t", str(int(flow.duration_sec)),
            "-P", str(flow.parallel),
        ]
        if flow.udp:
            cmd.append("-u")
        proc = self._exec_on_host(flow.src, cmd, background=True)
        with self._lock:
            self._clients.append((flow, proc))
        LOG.info("Fluxo iniciado: %s", flow)
        return proc

    def _exec_on_host(self, host_ip: str, cmd: List[str], background=False):
        """Executa comando no host Mininet ou via subprocess."""
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
        """Para todos os processos iperf3 ativos."""
        with self._lock:
            for _, proc in self._clients:
                try: proc.terminate()
                except Exception: pass
            for proc in self._servers.values():
                try: proc.terminate()
                except Exception: pass
            self._clients.clear()
            self._servers.clear()
        LOG.info("Todos os fluxos parados.")

    def cleanup_finished(self):
        with self._lock:
            self._clients = [(f, p) for f, p in self._clients if p.poll() is None]


# ─── Construtores de cenários ──────────────────────────────────────────────────

class ScenarioBuilder:
    """Constrói listas de fluxos para cada cenário de tráfego."""

    @staticmethod
    def background(duration=300) -> List[Flow]:
        """
        Tráfego de fundo: baixo e constante.
        Simula serviços sempre ativos (monitoramento, heartbeats).
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
        Picos curtos de alta banda.
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
        Poucos fluxos grandes e longos.
        Simula replicação de banco de dados, transferência de VMs.
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
        Múltiplos fluxos saturando o mesmo link.
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


# ─── Gerador de tráfego ────────────────────────────────────────────────────────

class TrafficGenerator:
    """Orquestra os cenários de tráfego sobre a rede Mininet."""

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
        """Executa um cenário de tráfego pelo nome."""
        builder = self.SCENARIOS.get(scenario_name)
        if not builder:
            raise ValueError(
                f"Cenário desconhecido: {scenario_name}. "
                f"Escolha entre: {list(self.SCENARIOS.keys())}"
            )
        flows = builder(duration)
        LOG.info("Cenário '%s': %d fluxos, duração=%ds",
                 scenario_name, len(flows), duration)
        self._execute_flows(flows, duration)

    def run_benchmark(self, total_duration=300):
        """
        Executa todos os cenários em sequência.
        Divide o tempo total proporcionalmente entre os cenários.
        """
        scenarios = [
            ("background", 0.20),   # 20% do tempo
            ("burst",      0.25),   # 25% do tempo
            ("elephant",   0.25),   # 25% do tempo
            ("congestion", 0.30),   # 30% do tempo
        ]

        LOG.info("Iniciando benchmark completo: %ds total", total_duration)

        for name, fraction in scenarios:
            if self._stop.is_set():
                break
            dur = int(total_duration * fraction)
            LOG.info("=== Cenário: %s (%ds) ===", name.upper(), dur)
            self.run_scenario(name, dur)
            LOG.info("Cenário %s completo. Pausa 3s...", name)
            time.sleep(3)

        LOG.info("Benchmark completo.")

    def _execute_flows(self, flows: List[Flow], total_duration: float):
        """Agenda e executa fluxos com base no start_delay de cada um."""
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
        """Executa um único fluxo de forma síncrona."""
        try:
            proc = self.runner.run_client(flow)
            if proc:
                proc.wait(timeout=flow.duration_sec + 10)
        except subprocess.TimeoutExpired:
            try: proc.terminate()
            except Exception: pass
        except Exception as e:
            LOG.debug("Erro no fluxo %s: %s", flow.name, e)

    def stop(self):
        self._stop.set()
        self.runner.stop_all()


# ─── Entrada CLI ───────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="SDN Traffic Generator")
    parser.add_argument(
        "--scenario",
        default="congestion",
        choices=list(TrafficGenerator.SCENARIOS.keys()) + ["all"],
        help="Cenário de tráfego"
    )
    parser.add_argument("--duration", default=120, type=int,
                        help="Duração em segundos")
    parser.add_argument("--benchmark", action="store_true",
                        help="Executar todos os cenários em sequência")
    args = parser.parse_args()

    gen = TrafficGenerator(net=None)

    try:
        if args.benchmark or args.scenario == "all":
            gen.run_benchmark(total_duration=args.duration)
        else:
            gen.run_scenario(args.scenario, duration=args.duration)
    except KeyboardInterrupt:
        LOG.info("Interrompido pelo usuário")
    finally:
        gen.stop()


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
scenarios.py - Gerador de Cenários de Tráfego para SDN
=========================================================
Substitui o conceito de "Fase 1..6" fixas por um catálogo de
cenários parametrizados, sorteados aleatoriamente a cada execução,
com suporte a:

  - Pares de hosts aleatórios (não sempre h1->h5, h3->h7)
  - Banda, duração e número de fluxos aleatórios
  - Mix de protocolos (TCP/UDP/ICMP) configurável
  - Bursts aleatórios
  - Tráfego de fundo contínuo (background)
  - Eventos de rede: queda de link, novo fluxo, host sai/volta,
    variação de delay e packet loss via tc netem
  - Reprodutibilidade via seed

Uso:
    from scenarios import SCENARIOS, ScenarioExecutor
    import random

    scenario = random.choice(list(SCENARIOS.values()))
    executor = ScenarioExecutor(net, scenario, seed=42)
    executor.run()
"""

import random
import time
import threading
import subprocess
import itertools
from dataclasses import dataclass, field
from typing import Dict, Tuple, List, Optional

IPERF_PORT_BASE = 5300


# ─── Definição de Cenário ──────────────────────────────────────────────────────

@dataclass
class Scenario:
    """Descreve os parâmetros de um cenário de tráfego."""
    name: str
    n_flows: Tuple[int, int]                       # (min, max) fluxos simultâneos
    bandwidth_range: Tuple[float, float]            # (min, max) Mbps
    duration_range: Tuple[float, float]              # (min, max) segundos por fluxo
    protocol_mix: Dict[str, float] = field(default_factory=lambda: {
        "TCP": 0.60, "UDP": 0.25, "ICMP": 0.10, "MIXED": 0.05
    })
    background: bool = True                          # tráfego de fundo contínuo
    burst_probability: float = 0.15                  # chance de um fluxo virar burst
    congestion_probability: float = 0.10              # chance de forçar mesmo caminho
    link_failure_probability: float = 0.05            # chance de derrubar um link
    netem_probability: float = 0.10                   # chance de aplicar delay/loss


# ─── Catálogo de Cenários ──────────────────────────────────────────────────────

SCENARIOS: Dict[str, Scenario] = {
    "LOW_LOAD": Scenario(
        name="LOW_LOAD",
        n_flows=(1, 3),
        bandwidth_range=(1, 15),
        duration_range=(15, 40),
        background=True,
        burst_probability=0.05,
        congestion_probability=0.0,
        link_failure_probability=0.0,
    ),
    "MEDIUM_LOAD": Scenario(
        name="MEDIUM_LOAD",
        n_flows=(2, 5),
        bandwidth_range=(15, 60),
        duration_range=(20, 60),
        background=True,
        burst_probability=0.15,
        congestion_probability=0.10,
        link_failure_probability=0.02,
    ),
    "HIGH_LOAD": Scenario(
        name="HIGH_LOAD",
        n_flows=(4, 8),
        bandwidth_range=(60, 200),
        duration_range=(30, 90),
        background=True,
        burst_probability=0.25,
        congestion_probability=0.30,
        link_failure_probability=0.05,
    ),
    "BURST": Scenario(
        name="BURST",
        n_flows=(2, 6),
        bandwidth_range=(10, 300),
        duration_range=(3, 15),
        background=True,
        burst_probability=0.80,
        congestion_probability=0.15,
        link_failure_probability=0.0,
    ),
    "IDLE": Scenario(
        name="IDLE",
        n_flows=(0, 1),
        bandwidth_range=(0.5, 3),
        duration_range=(10, 30),
        background=False,
        burst_probability=0.0,
        congestion_probability=0.0,
        link_failure_probability=0.0,
    ),
    "MIX_TCP": Scenario(
        name="MIX_TCP",
        n_flows=(3, 7),
        bandwidth_range=(10, 120),
        duration_range=(20, 70),
        protocol_mix={"TCP": 1.0, "UDP": 0.0, "ICMP": 0.0, "MIXED": 0.0},
        background=True,
        burst_probability=0.15,
        congestion_probability=0.15,
        link_failure_probability=0.03,
    ),
    "MIX_UDP": Scenario(
        name="MIX_UDP",
        n_flows=(3, 7),
        bandwidth_range=(10, 120),
        duration_range=(20, 70),
        protocol_mix={"TCP": 0.0, "UDP": 1.0, "ICMP": 0.0, "MIXED": 0.0},
        background=True,
        burst_probability=0.20,
        congestion_probability=0.15,
        link_failure_probability=0.03,
    ),
    "ELEPHANT_FLOW": Scenario(
        name="ELEPHANT_FLOW",
        n_flows=(1, 2),
        bandwidth_range=(80, 250),
        duration_range=(60, 120),
        background=True,
        burst_probability=0.05,
        congestion_probability=0.20,
        link_failure_probability=0.05,
    ),
    "MANY_MICE": Scenario(
        name="MANY_MICE",
        n_flows=(6, 10),
        bandwidth_range=(0.5, 5),
        duration_range=(1, 6),
        background=True,
        burst_probability=0.10,
        congestion_probability=0.05,
        link_failure_probability=0.0,
    ),
    "CONGESTION": Scenario(
        name="CONGESTION",
        n_flows=(5, 10),
        bandwidth_range=(30, 300),
        duration_range=(30, 90),
        background=True,
        burst_probability=0.30,
        congestion_probability=0.70,     # a maioria força o mesmo caminho
        link_failure_probability=0.10,
    ),
    "FAILURE": Scenario(
        name="FAILURE",
        n_flows=(3, 6),
        bandwidth_range=(10, 100),
        duration_range=(30, 90),
        background=True,
        burst_probability=0.15,
        congestion_probability=0.20,
        link_failure_probability=0.60,   # cenário focado em falhas
        netem_probability=0.40,
    ),
    "BACKGROUND_TRAFFIC": Scenario(
        name="BACKGROUND_TRAFFIC",
        n_flows=(0, 2),
        bandwidth_range=(1, 10),
        duration_range=(40, 100),
        background=True,
        burst_probability=0.05,
        congestion_probability=0.0,
        link_failure_probability=0.0,
    ),
}


# ─── Injeção de Eventos de Rede ────────────────────────────────────────────────

class NetworkEventInjector:
    """
    Aplica eventos de rede durante a execução de um cenário:
      - Queda e recuperação de link (net.configLinkStatus)
      - Variação de delay/loss via tc netem
      - Host saindo e voltando (queda do link de acesso do host)
    """

    def __init__(self, net, log_fn=print):
        self.net = net
        self.log = log_fn

    def fail_link(self, node1: str, node2: str, downtime: float):
        """Derruba um link por `downtime` segundos e depois restaura."""
        def _worker():
            try:
                self.log(f"  [EVENTO] Link {node1}<->{node2} DOWN")
                self.net.configLinkStatus(node1, node2, 'down')
                time.sleep(downtime)
                self.net.configLinkStatus(node1, node2, 'up')
                self.log(f"  [EVENTO] Link {node1}<->{node2} UP novamente")
            except Exception as e:
                self.log(f"  [EVENTO] Erro ao manipular link {node1}<->{node2}: {e}")

        threading.Thread(target=_worker, daemon=True).start()

    def host_leave_and_return(self, host_name: str, leaf_name: str, downtime: float):
        """Simula um host saindo da rede e retornando depois."""
        self.fail_link(host_name, leaf_name, downtime)

    def apply_netem(self, iface: str, delay_ms: Optional[float] = None,
                    loss_pct: Optional[float] = None, duration: float = 20.0):
        """
        Aplica delay/loss em uma interface via tc netem, mantém por
        `duration` segundos e depois remove.
        """
        def _worker():
            params = []
            if delay_ms:
                params += ["delay", f"{delay_ms}ms"]
            if loss_pct:
                params += ["loss", f"{loss_pct}%"]
            if not params:
                return

            add_cmd = ["tc", "qdisc", "add", "dev", iface, "root", "netem"] + params
            del_cmd = ["tc", "qdisc", "del", "dev", iface, "root"]

            try:
                self.log(f"  [EVENTO] netem em {iface}: {' '.join(params)}")
                subprocess.run(add_cmd, capture_output=True)
                time.sleep(duration)
                subprocess.run(del_cmd, capture_output=True)
                self.log(f"  [EVENTO] netem removido de {iface}")
            except Exception as e:
                self.log(f"  [EVENTO] Erro netem em {iface}: {e}")

        threading.Thread(target=_worker, daemon=True).start()


# ─── Executor de Cenário ───────────────────────────────────────────────────────

class ScenarioExecutor:
    """
    Executa um Scenario sobre uma rede Mininet já ativa (`net`),
    gerando fluxos, bursts, tráfego de fundo e eventos de rede
    de acordo com os parâmetros do cenário.
    """

    # Topologia lógica: switch de acesso (leaf) de cada host, usada para
    # eventos de "host sai/volta". Ajuste se a topologia mudar.
    HOST_LEAF = {
        "h1": "lf1", "h2": "lf1",
        "h3": "lf2", "h4": "lf2",
        "h5": "lf3", "h6": "lf3",
        "h7": "lf4", "h8": "lf4",
    }

    # Links internos candidatos a falha (spine/agg), evita isolar hosts
    INTERNAL_LINKS = [
        ("sp1", "ag1"), ("sp1", "ag2"),
        ("ag1", "lf1"), ("ag1", "lf2"),
        ("ag2", "lf3"), ("ag2", "lf4"),
    ]

    def __init__(self, net, scenario: Scenario, seed: Optional[int] = None,
                 log_fn=print):
        self.net      = net
        self.scenario = scenario
        self.log      = log_fn
        self.rng      = random.Random(seed)
        self.events   = NetworkEventInjector(net, log_fn=log_fn)
        self._bg_threads: List[threading.Thread] = []
        self._stop_bg  = threading.Event()

    # ── Helpers ─────────────────────────────────────────────────────────────

    def _pick_protocol(self) -> str:
        mix = self.scenario.protocol_mix
        protos, weights = zip(*mix.items())
        return self.rng.choices(protos, weights=weights, k=1)[0]

    def _pick_pair(self, hosts: List[str]) -> Tuple[str, str]:
        return tuple(self.rng.sample(hosts, 2))

    def _pick_congested_pair(self, hosts: List[str]) -> Tuple[str, str]:
        """Escolhe um par que force tráfego pelo mesmo caminho (mesmo leaf de origem)."""
        by_leaf: Dict[str, List[str]] = {}
        for h, lf in self.HOST_LEAF.items():
            if h in hosts:
                by_leaf.setdefault(lf, []).append(h)
        leaves_with_pairs = [lf for lf, hs in by_leaf.items() if len(hs) >= 1]
        if not leaves_with_pairs:
            return self._pick_pair(hosts)
        src_leaf = self.rng.choice(leaves_with_pairs)
        src = self.rng.choice(by_leaf[src_leaf])
        remaining = [h for h in hosts if h != src]
        dst = self.rng.choice(remaining)
        return src, dst

    def _run_iperf(self, src_name, dst_ip, bw_mbps, duration, proto, port):
        """Executa um fluxo iperf3 (background thread já é gerenciada pelo caller)."""
        src = self.net.get(src_name)
        dst_flag = "-u" if proto == "UDP" else ""
        cmd = (f"iperf3 -c {dst_ip} -b {bw_mbps}M -t {int(duration)} "
               f"-p {port} {dst_flag} &")
        src.cmd(cmd)

    def _run_icmp(self, src_name, dst_ip, duration):
        src = self.net.get(src_name)
        count = max(1, int(duration))
        src.cmd(f"ping -c {count} -i 1 {dst_ip} > /dev/null 2>&1 &")

    def _ensure_servers(self, hosts: List[str]):
        for h in hosts:
            host = self.net.get(h)
            for p in range(IPERF_PORT_BASE, IPERF_PORT_BASE + 5):
                host.cmd(f"iperf3 -s -D -p {p} 2>/dev/null")

    # ── Tráfego de fundo ────────────────────────────────────────────────────

    def _start_background(self, hosts: List[str], total_duration: float):
        """Inicia fluxos pequenos e contínuos simulando tráfego de fundo real."""
        if not self.scenario.background:
            return

        n_bg = self.rng.randint(2, 4)
        self.log(f"  [BACKGROUND] iniciando {n_bg} fluxos de fundo")

        for i in range(n_bg):
            src, dst = self._pick_pair(hosts)
            bw = round(self.rng.uniform(1, 8), 1)
            port = IPERF_PORT_BASE + 90 + i
            dst_ip = self.net.get(dst).IP()
            self._run_iperf(src, dst_ip, bw, total_duration, "TCP", port)
            self.log(f"    bg: {src}->{dst} {bw}Mbps por {total_duration:.0f}s")

    # ── Execução principal ──────────────────────────────────────────────────

    def run(self, hosts: Optional[List[str]] = None):
        """Executa o cenário completo: sorteia fluxos, bursts e eventos."""
        s = self.scenario
        hosts = hosts or list(self.HOST_LEAF.keys())

        total_duration = self.rng.uniform(*s.duration_range)
        n_flows = self.rng.randint(*s.n_flows)

        self.log(f"\n=== Cenário: {s.name} | fluxos={n_flows} | "
                 f"duração_base={total_duration:.0f}s ===")

        self._ensure_servers(hosts)
        self._start_background(hosts, total_duration)

        # Eventos de falha de link
        if self.rng.random() < s.link_failure_probability:
            n1, n2 = self.rng.choice(self.INTERNAL_LINKS)
            downtime = self.rng.uniform(5, 20)
            delay_start = self.rng.uniform(0, total_duration * 0.5)
            threading.Timer(delay_start, self.events.fail_link,
                            args=(n1, n2, downtime)).start()

        # Evento de host saindo e voltando
        if self.rng.random() < s.link_failure_probability * 0.5:
            h = self.rng.choice(hosts)
            lf = self.HOST_LEAF[h]
            downtime = self.rng.uniform(5, 15)
            delay_start = self.rng.uniform(0, total_duration * 0.6)
            threading.Timer(delay_start, self.events.host_leave_and_return,
                            args=(h, lf, downtime)).start()

        # Evento de netem (delay/loss) em um link interno aleatório
        if self.rng.random() < s.netem_probability:
            n1, n2 = self.rng.choice(self.INTERNAL_LINKS)
            iface = f"{n1}-eth1"   # aproximação; ajuste fino depende da topologia
            delay_ms = self.rng.choice([None, 20, 50, 100])
            loss_pct = self.rng.choice([None, 1, 5, 10])
            if delay_ms or loss_pct:
                self.events.apply_netem(iface, delay_ms, loss_pct,
                                        duration=min(20, total_duration))

        # Fluxos principais
        for i in range(n_flows):
            if s.congestion_probability > 0 and self.rng.random() < s.congestion_probability:
                src, dst = self._pick_congested_pair(hosts)
            else:
                src, dst = self._pick_pair(hosts)

            proto = self._pick_protocol()
            bw    = round(self.rng.uniform(*s.bandwidth_range), 1)
            dur   = round(self.rng.uniform(
                        max(2, s.duration_range[0]),
                        min(total_duration, s.duration_range[1])), 1)
            port  = IPERF_PORT_BASE + i

            is_burst = self.rng.random() < s.burst_probability
            if is_burst:
                bw  = round(self.rng.uniform(max(bw, 50), 300), 1)
                dur = round(self.rng.uniform(2, 8), 1)

            dst_ip = self.net.get(dst).IP()

            if proto == "ICMP":
                self._run_icmp(src, dst_ip, dur)
            elif proto == "MIXED":
                self._run_iperf(src, dst_ip, bw, dur, "TCP", port)
                self._run_iperf(dst, self.net.get(src).IP(), bw * 0.5, dur, "UDP", port + 1)
            else:
                self._run_iperf(src, dst_ip, bw, dur, proto, port)

            tag = "BURST" if is_burst else proto
            self.log(f"  fluxo[{i}] {src}->{dst} {bw}Mbps {tag} {dur}s")

        return total_duration


# ─── Runner de Experimentos ────────────────────────────────────────────────────

class ExperimentRunner:
    """
    Executa N experimentos em sequência, cada um sorteando um cenário
    do catálogo, e registra um manifesto (CSV) com metadados de cada
    execução para correlacionar depois com o dataset coletado.
    """

    def __init__(self, net, scenarios: Dict[str, Scenario] = SCENARIOS,
                 seed: Optional[int] = None, manifest_path: str = "experiments_manifest.csv"):
        self.net       = net
        self.scenarios = scenarios
        self.rng       = random.Random(seed)
        self.manifest_path = manifest_path
        self._init_manifest()

    def _init_manifest(self):
        with open(self.manifest_path, "w") as f:
            f.write("experiment_id,scenario,start_ts,duration_s,seed\n")

    def _log_experiment(self, exp_id, scenario_name, start_ts, duration, seed):
        with open(self.manifest_path, "a") as f:
            f.write(f"{exp_id},{scenario_name},{start_ts:.3f},{duration:.1f},{seed}\n")

    def run(self, n_experiments: int, cooldown: float = 3.0):
        """Roda `n_experiments` cenários sorteados aleatoriamente em sequência."""
        names = list(self.scenarios.keys())

        for exp_id in range(1, n_experiments + 1):
            scenario_name = self.rng.choice(names)
            scenario = self.scenarios[scenario_name]
            exp_seed = self.rng.randint(0, 10_000_000)

            print(f"\n{'#'*70}")
            print(f"# Experimento {exp_id}/{n_experiments} — Cenário: {scenario_name} "
                  f"(seed={exp_seed})")
            print(f"{'#'*70}")

            executor = ScenarioExecutor(self.net, scenario, seed=exp_seed)
            start_ts = time.time()
            duration = executor.run()

            # Aguarda o cenário terminar antes do próximo (fluxos já foram
            # disparados em background, então aguardamos a duração + margem)
            time.sleep(duration + 2)

            self._log_experiment(exp_id, scenario_name, start_ts, duration, exp_seed)
            time.sleep(cooldown)

        print(f"\n{'='*70}")
        print(f"  {n_experiments} experimentos concluídos.")
        print(f"  Manifesto salvo em: {self.manifest_path}")
        print(f"{'='*70}\n")
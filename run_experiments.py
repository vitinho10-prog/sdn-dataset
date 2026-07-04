#!/usr/bin/env python3
"""
run_experiments.py - Orquestrador de Experimentos Aleatorios
================================================================
Substitui o roteiro fixo de "Fase 1..6" por uma sequencia de N
experimentos, cada um sorteando um cenario do catalogo (scenarios.py)
com fluxos, protocolos, largura de banda, duracao e eventos de rede
aleatorios (porem reprodutíveis via --seed).

Uso (com Ryu ja rodando no Terminal 1):
    sudo ~/miniconda/envs/sdn/bin/python run_experiments.py \\
        --n-experiments 20 --seed 42

Rode ANTES o Terminal 3 (collector.py) e/ou Terminal 5 (pcap_collector.py)
para capturar o dataset gerado por estes experimentos.
"""

import argparse
import sys
import time

sys.path.insert(0, "/home/beatriz/meus-projetos-p4/metricas")

from mininet.net import Mininet
from mininet.node import RemoteController, OVSSwitch
from mininet.link import Link
from mininet.log import setLogLevel
from mininet.cli import CLI

from scenarios import SCENARIOS, ExperimentRunner


def build_network():
    """Monta a mesma topologia Fat-tree usada no restante do projeto."""
    net = Mininet(controller=None, switch=OVSSwitch, link=Link, autoSetMacs=True)
    net.addController('c0', controller=RemoteController, ip='127.0.0.1', port=6633)

    sp1 = net.addSwitch('sp1', protocols='OpenFlow13', dpid='0000000000000001')
    ag1 = net.addSwitch('ag1', protocols='OpenFlow13', dpid='0000000000000002')
    ag2 = net.addSwitch('ag2', protocols='OpenFlow13', dpid='0000000000000003')
    lf1 = net.addSwitch('lf1', protocols='OpenFlow13', dpid='0000000000000004')
    lf2 = net.addSwitch('lf2', protocols='OpenFlow13', dpid='0000000000000005')
    lf3 = net.addSwitch('lf3', protocols='OpenFlow13', dpid='0000000000000006')
    lf4 = net.addSwitch('lf4', protocols='OpenFlow13', dpid='0000000000000007')

    hosts = [net.addHost('h%d' % i, ip='10.0.0.%d/24' % i) for i in range(1, 9)]

    net.addLink(sp1, ag1); net.addLink(sp1, ag2)
    net.addLink(ag1, lf1); net.addLink(ag1, lf2)
    net.addLink(ag2, lf3); net.addLink(ag2, lf4)

    for i, lf in enumerate([lf1, lf1, lf2, lf2, lf3, lf3, lf4, lf4]):
        net.addLink(lf, hosts[i])

    return net


def main():
    parser = argparse.ArgumentParser(description="Gerador de experimentos SDN")
    parser.add_argument("--n-experiments", type=int, default=20,
                        help="Número de experimentos a executar")
    parser.add_argument("--seed", type=int, default=None,
                        help="Seed para reprodutibilidade")
    parser.add_argument("--cooldown", type=float, default=3.0,
                        help="Pausa entre experimentos (segundos)")
    parser.add_argument("--cli", action="store_true",
                        help="Abrir Mininet CLI ao final")
    parser.add_argument("--manifest", default="experiments_manifest.csv",
                        help="Caminho do CSV de manifesto")
    args = parser.parse_args()

    setLogLevel("warning")

    print("=" * 70)
    print("  Construindo topologia Fat-tree...")
    print("=" * 70)
    net = build_network()
    net.start()

    for h in net.hosts:
        h.cmd("sysctl -w net.ipv6.conf.all.disable_ipv6=1 2>/dev/null")

    print("Aguardando 5s para os switches conectarem ao Ryu...")
    time.sleep(5)

    print(f"\nCenários disponíveis: {list(SCENARIOS.keys())}")

    runner = ExperimentRunner(net, scenarios=SCENARIOS, seed=args.seed,
                              manifest_path=args.manifest)
    runner.run(n_experiments=args.n_experiments, cooldown=args.cooldown)

    if args.cli:
        CLI(net)

    net.stop()


if __name__ == "__main__":
    main()
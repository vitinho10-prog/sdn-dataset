#!/usr/bin/env python3
import json, time, argparse
from mininet.topo import Topo
from mininet.net import Mininet
from mininet.node import RemoteController, OVSSwitch
from mininet.link import Link
from mininet.log import setLogLevel, info
from mininet.cli import CLI

# verificar designs de topologia e tentar escolher um q tenha em mais artigos 
class FatTreeTopo(Topo):
    def __init__(self, **opts):
        self.link_registry = []
        super().__init__(**opts)

    def build(self):
        spines = [self.addSwitch(f"sp{i}", protocols="OpenFlow13", failMode="standalone") for i in range(1,3)]
        aggs   = [self.addSwitch(f"ag{i}", protocols="OpenFlow13", failMode="standalone") for i in range(1,5)]
        leaves = [self.addSwitch(f"lf{i}", protocols="OpenFlow13", failMode="standalone") for i in range(1,5)]

        host_idx = 1
        for lf in leaves:
            for _ in range(2):
                h = self.addHost(f"h{host_idx}", ip=f"10.0.0.{host_idx}/24")
                self.addLink(lf, h)
                host_idx += 1

        for sp in spines:
            for ag in aggs:
                self.addLink(sp, ag)

        for ag, lf_group in zip(aggs, [leaves[0:2], leaves[0:2], leaves[2:4], leaves[2:4]]):
            for lf in lf_group:
                self.addLink(ag, lf)

    def export_topology_json(self, path="topology.json"):
        with open(path, "w") as f:
            json.dump({}, f)

def build_network():
    topo = FatTreeTopo()
    net  = Mininet(topo=topo, controller=None, switch=OVSSwitch,
                   link=Link, autoSetMacs=True)
    net.addController("c0", controller=RemoteController,
                      ip="127.0.0.1", port=6633)
    return net, topo

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--cli",  action="store_true")
    parser.add_argument("--test", action="store_true")
    args = parser.parse_args()

    setLogLevel("warning")
    net, topo = build_network()
    net.start()

    for h in net.hosts:
        h.cmd("sysctl -w net.ipv6.conf.all.disable_ipv6=1 2>/dev/null")
    for sw in net.switches:
        sw.cmd("sysctl -w net.ipv6.conf.all.disable_ipv6=1 2>/dev/null")

    info("Aguardando 8s...\n")
    time.sleep(8)

    if args.test:
        print(net.get("h1").cmd("ping -c 3 -W 2 10.0.0.2"))
        net.pingAll()

    if args.cli:
        CLI(net)

    net.stop()

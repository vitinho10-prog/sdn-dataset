#!/usr/bin/env python3
"""
pcap_collector.py - Captura PCAP dentro dos namespaces Mininet
===============================================================
Captura tráfego real dentro de cada host Mininet usando nsenter + tcpdump.
Foca em fluxo e vazão por par de IPs.

Uso:
    sudo python3 pcap_collector.py --duration 60 --output-dir ./pcap
"""

import subprocess
import time
import json
from pathlib import Path
from datetime import datetime
from collections import defaultdict


HOSTS = ["h1", "h2", "h3", "h4", "h5", "h6", "h7", "h8"]


def get_host_pid(host_name: str):
    """Encontra o PID do processo bash do host Mininet."""
    try:
        result = subprocess.run(
            ["pgrep", "-f", f"mininet:{host_name}"],
            capture_output=True, text=True
        )
        pids = result.stdout.strip().split()
        return pids[0] if pids else None
    except Exception:
        return None


class PcapCapture:
    def __init__(self, output_dir: Path, duration: int):
        self.output_dir = output_dir
        self.duration   = duration
        self._procs     = {}
        output_dir.mkdir(parents=True, exist_ok=True)

    def start_capture(self, host_name: str):
        pcap_path = self.output_dir / f"{host_name}.pcap"
        pid = get_host_pid(host_name)

        if not pid:
            print(f"  ✗ {host_name}: PID não encontrado — Mininet rodando?")
            return

        iface = f"{host_name}-eth0"
        cmd = [
            "nsenter", "-t", pid, "--net",
            "tcpdump",
            "-i", iface,
            "-w", str(pcap_path),
            "-n",
            "--snapshot-length=0",
        ]

        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL
            )
            self._procs[host_name] = (proc, pcap_path)
            print(f"  ✓ {host_name} (PID={pid}) → {pcap_path.name}")
        except Exception as e:
            print(f"  ✗ {host_name}: {e}")

    def start_all(self):
        print("\n=== Iniciando capturas PCAP ===")
        for h in HOSTS:
            self.start_capture(h)
        print(f"  {len(self._procs)} capturas ativas\n")

    def stop_all(self):
        print("\n=== Parando capturas PCAP ===")
        for host_name, (proc, path) in self._procs.items():
            try:
                proc.terminate()
                proc.wait(timeout=3)
                size = path.stat().st_size if path.exists() else 0
                print(f"  ✓ {host_name}: {path.name} ({size/1024:.1f} KB)")
            except Exception as e:
                print(f"  ✗ {host_name}: {e}")

    def get_pcap_files(self):
        return [(h, p) for h, (_, p) in self._procs.items()
                if p.exists() and p.stat().st_size > 24]  # > header PCAP


class PcapAnalyzer:
    def __init__(self, output_dir: Path):
        self.output_dir = output_dir

    def analyze_all(self, pcap_files):
        print("\n=== Analisando capturas PCAP ===")
        all_flows = defaultdict(lambda: {
            "packets": 0, "bytes": 0,
            "start_time": None, "end_time": None,
            "protocol": "TCP"
        })

        for host_name, pcap_path in pcap_files:
            print(f"  Analisando {host_name} ({pcap_path.stat().st_size/1024:.0f} KB)...")
            flows = self._analyze_pcap(pcap_path)
            print(f"    → {len(flows)} fluxos encontrados")
            for key, stats in flows.items():
                f = all_flows[key]
                f["packets"]  += stats["packets"]
                f["bytes"]    += stats["bytes"]
                f["protocol"]  = stats["protocol"]
                if f["start_time"] is None or stats["start_time"] < f["start_time"]:
                    f["start_time"] = stats["start_time"]
                if f["end_time"] is None or stats["end_time"] > f["end_time"]:
                    f["end_time"] = stats["end_time"]

        return self._build_report(all_flows)

    def _analyze_pcap(self, pcap_path: Path):
        flows = defaultdict(lambda: {
            "packets": 0, "bytes": 0,
            "start_time": None, "end_time": None,
            "protocol": "TCP"
        })

        cmd = ["tcpdump", "-r", str(pcap_path), "-n", "-q", "-tt"]
        try:
            result = subprocess.run(cmd, capture_output=True,
                                    text=True, timeout=60)
        except Exception as e:
            print(f"    Erro ao ler PCAP: {e}")
            return flows

        for line in result.stdout.splitlines():
            p = self._parse_line(line)
            if not p:
                continue
            ts, proto, src, dst, length = p

            # Normalizar: chave bidirecional por IP (sem porta)
            src_ip = src.rsplit(".", 1)[0]
            dst_ip = dst.rsplit(".", 1)[0]
            if src_ip > dst_ip:
                src_ip, dst_ip = dst_ip, src_ip

            key = f"{src_ip} → {dst_ip} [{proto}]"
            f = flows[key]
            f["packets"]  += 1
            f["bytes"]    += length
            f["protocol"]  = proto
            if f["start_time"] is None:
                f["start_time"] = ts
            f["end_time"] = ts

        return flows

    def _parse_line(self, line):
        try:
            parts = line.split()
            if len(parts) < 5:
                return None
            ts = float(parts[0])

            proto = "TCP"
            if " UDP " in line or " udp " in line:
                proto = "UDP"
            elif " ICMP" in line or " icmp" in line:
                proto = "ICMP"

            src = dst = ""
            for i, p in enumerate(parts):
                if p == "IP":
                    if i + 3 < len(parts):
                        src = parts[i+1]
                        dst = parts[i+3].rstrip(":")
                    break

            if not src or not dst:
                return None

            # Formato real do tcpdump -q -tt:
            # "... IP src > dst: tcp 65160"  -> length é o último número
            # "... IP src > dst: udp 0"      -> length é o último número
            # "... IP src > dst: ICMP echo request, ..." -> sem length numerico
            length = 0
            for i, p in enumerate(parts):
                if p in ("tcp", "udp", "TCP", "UDP"):
                    if i + 1 < len(parts):
                        try:
                            length = int(parts[i+1])
                        except ValueError:
                            length = 0
                    if p.lower() == "udp":
                        proto = "UDP"
                    break

            return ts, proto, src, dst, length
        except Exception:
            return None

    def _build_report(self, all_flows):
        report = {
            "timestamp":   datetime.now().isoformat(),
            "total_flows": len(all_flows),
            "flows":       []
        }
        total_bytes = total_packets = 0

        for key, stats in sorted(all_flows.items(),
                                  key=lambda x: x[1]["bytes"],
                                  reverse=True):
            dur  = 0.0
            if stats["start_time"] and stats["end_time"]:
                dur = max(stats["end_time"] - stats["start_time"], 0.001)
            mbps = (stats["bytes"] * 8) / (dur * 1_000_000) if dur > 0 else 0

            report["flows"].append({
                "flow":       key,
                "protocol":   stats["protocol"],
                "packets":    stats["packets"],
                "bytes":      stats["bytes"],
                "duration_s": round(dur, 3),
                "vazao_mbps": round(mbps, 3),
            })
            total_bytes   += stats["bytes"]
            total_packets += stats["packets"]

        report["total_bytes"]   = total_bytes
        report["total_packets"] = total_packets
        report["total_mbps"]    = round(
            sum(f["vazao_mbps"] for f in report["flows"]), 2)
        return report

    def save_report(self, report, run_id):
        json_path = self.output_dir / f"flows_{run_id}.json"
        csv_path  = self.output_dir / f"flows_{run_id}.csv"

        with open(json_path, "w") as f:
            json.dump(report, f, indent=2)

        with open(csv_path, "w") as f:
            f.write("flow,protocol,packets,bytes,duration_s,vazao_mbps\n")
            for flow in report["flows"]:
                f.write(f"{flow['flow']},{flow['protocol']},"
                        f"{flow['packets']},{flow['bytes']},"
                        f"{flow['duration_s']},{flow['vazao_mbps']}\n")

        print(f"\n{'='*65}")
        print(f"  RELATÓRIO DE FLUXOS E VAZÃO")
        print(f"{'='*65}")
        print(f"  Total de fluxos  : {report['total_flows']}")
        print(f"  Total de pacotes : {report['total_packets']:,}")
        print(f"  Total de bytes   : {report['total_bytes']:,}")
        print(f"  Vazão total      : {report['total_mbps']} Mbps")
        print(f"\n  Top 10 fluxos por vazão:")
        print(f"  {'Fluxo':<40} {'Vazão Mbps':>12} {'Pacotes':>10} {'Bytes':>15}")
        print(f"  {'-'*78}")
        for flow in report["flows"][:10]:
            print(f"  {flow['flow']:<40} "
                  f"{flow['vazao_mbps']:>11.2f}  "
                  f"{flow['packets']:>10,}  "
                  f"{flow['bytes']:>14,}")
        print(f"\n  Arquivos salvos:")
        print(f"    {json_path}")
        print(f"    {csv_path}")
        print(f"{'='*65}\n")


def main():
    import argparse
    parser = argparse.ArgumentParser(description="SDN PCAP Collector")
    parser.add_argument("--duration",   default=60,      type=int)
    parser.add_argument("--output-dir", default="./pcap", type=str)
    args = parser.parse_args()

    output_dir = Path(args.output_dir)
    run_id     = datetime.now().strftime("%Y%m%d_%H%M%S")

    print(f"{'='*65}")
    print(f"  SDN PCAP Collector")
    print(f"  Run ID  : {run_id}")
    print(f"  Duração : {args.duration}s")
    print(f"{'='*65}")

    if subprocess.run(["which", "tcpdump"],
                      capture_output=True).returncode != 0:
        print("ERRO: tcpdump não encontrado. sudo apt install tcpdump")
        return

    capture  = PcapCapture(output_dir, args.duration)
    analyzer = PcapAnalyzer(output_dir)

    capture.start_all()

    print(f"Capturando por {args.duration} segundos...")
    try:
        for i in range(args.duration):
            time.sleep(1)
            if (i+1) % 10 == 0:
                print(f"  {i+1}/{args.duration}s...")
    except KeyboardInterrupt:
        print("\nInterrompido")

    capture.stop_all()

    pcap_files = capture.get_pcap_files()
    if not pcap_files:
        print("\nNenhum PCAP com dados.")
        return

    report = analyzer.analyze_all(pcap_files)
    analyzer.save_report(report, run_id)


if __name__ == "__main__":
    main()

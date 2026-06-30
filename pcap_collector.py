#!/usr/bin/env python3
"""
pcap_collector.py - Captura de Tráfego em Tempo Real com Janelas Temporais
============================================================================
Versão 2: captura via tshark em tempo real (sem PCAP intermediário),
agrega em janelas fixas de 1 segundo, extrai features ricas por fluxo
e exporta em formato pronto para LSTM/GRU.

Implementa os ajustes solicitados:
  1. Janelas temporais fixas (WINDOW=1s) em vez de média total
  2. tshark em vez de tcpdump (parsing mais simples e direto)
  3. Série temporal completa por fluxo (não agregado só no final)
  4. Dataset em formato wide (timestamp, flow1, flow2, ...) pronto p/ LSTM
  5. Features ricas: mbps, pps, avg_pkt, std_pkt, tcp_ratio, udp_ratio,
     flow_count, active_hosts
  6. Captura em tempo real: tshark -> métricas por segundo -> CSV
     (sem etapa de "salvar pcap e analisar depois")
  7. Suporte a múltiplos cenários de tráfego (ver traffic.py)
  8. Séries completas por fluxo salvas em JSON
  9. Exporta janelas deslizantes (SEQ_LEN) em .npy para treino direto

Uso:
    sudo python3 pcap_collector.py --duration 60 --output-dir ./pcap
    sudo python3 pcap_collector.py --duration 60 --interfaces h1-eth0,h2-eth0
"""

import argparse
import json
import subprocess
import threading
import time
import math
import os
from pathlib import Path
from datetime import datetime
from collections import defaultdict

import numpy as np

# ─── Configuração ──────────────────────────────────────────────────────────────

WINDOW = 1.0          # tamanho da janela temporal em segundos (ajuste #1)
SEQ_LEN = 30          # tamanho da janela deslizante para LSTM (ajuste #9)
HOSTS = ["h1", "h2", "h3", "h4", "h5", "h6", "h7", "h8"]


# ─── Localização dos hosts no namespace Mininet ───────────────────────────────

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


# ─── Leitor de Pacotes em Tempo Real (ajuste #2 e #6) ─────────────────────────

class TsharkLiveReader:
    """
    Roda `tshark` em modo live dentro do namespace de um host,
    lendo a saída linha a linha (sem salvar .pcap intermediário).

    Cada linha do tshark vira um evento de pacote, que é
    imediatamente repassado para o agregador de janelas.
    """

    FIELDS = [
        "frame.time_epoch",
        "ip.src",
        "ip.dst",
        "frame.len",
        "ip.proto",        # 6=TCP, 17=UDP, 1=ICMP
    ]

    def __init__(self, host_name: str, on_packet):
        self.host_name = host_name
        self.on_packet = on_packet
        self._proc = None
        self._thread = None
        self._stop = threading.Event()

    def start(self):
        pid = get_host_pid(self.host_name)
        if not pid:
            print(f"  ✗ {self.host_name}: PID não encontrado")
            return False

        iface = f"{self.host_name}-eth0"
        field_args = []
        for f in self.FIELDS:
            field_args += ["-e", f]

        cmd = [
            "nsenter", "-t", pid, "--net",
            "tshark",
            "-i", iface,
            "-l",                 # line-buffered (saida em tempo real)
            "-T", "fields",
            "-E", "separator=|",
            *field_args,
        ]

        try:
            self._proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                bufsize=1,
            )
        except Exception as e:
            print(f"  ✗ {self.host_name}: erro ao iniciar tshark: {e}")
            return False

        self._thread = threading.Thread(target=self._read_loop, daemon=True)
        self._thread.start()
        print(f"  ✓ {self.host_name} (PID={pid}) [tshark live]")
        return True

    def _read_loop(self):
        for line in self._proc.stdout:
            if self._stop.is_set():
                break
            self._parse_line(line)

    def _parse_line(self, line: str):
        parts = line.rstrip("\n").split("|")
        if len(parts) < 5:
            return
        ts_str, src, dst, len_str, proto = parts[:5]

        if not ts_str or not src or not dst:
            return

        try:
            ts = float(ts_str)
            length = int(len_str) if len_str else 0
        except ValueError:
            return

        proto_name = {"6": "TCP", "17": "UDP", "1": "ICMP"}.get(proto, "OTHER")

        self.on_packet({
            "ts": ts,
            "src": src,
            "dst": dst,
            "length": length,
            "proto": proto_name,
        })

    def stop(self):
        self._stop.set()
        if self._proc:
            try:
                self._proc.terminate()
                self._proc.wait(timeout=3)
            except Exception:
                try:
                    self._proc.kill()
                except Exception:
                    pass


# ─── Agregador de Janelas Temporais (ajustes #1, #3, #5) ──────────────────────

class WindowAggregator:
    """
    Recebe eventos de pacote em tempo real e agrega em janelas fixas
    de WINDOW segundos, por fluxo (par de IPs, direção normalizada).

    Para cada (fluxo, janela) calcula:
      - bytes totais
      - mbps (vazão)
      - pps (pacotes por segundo)
      - avg_pkt / std_pkt (tamanho médio e desvio do pacote)
      - tcp_ratio / udp_ratio
      - flow_count (fluxos ativos na janela, global)
      - active_hosts (hosts distintos ativos na janela, global)
    """

    def __init__(self, window=WINDOW):
        self.window = window
        self._lock = threading.Lock()

        # Buffer de pacotes brutos por janela: {window_idx: [pkt, pkt, ...]}
        self._raw_windows = defaultdict(list)

        # Série temporal final por fluxo: {flow_key: [ {t, mbps, pps, ...}, ... ]}
        self.series = defaultdict(list)

        self._start_ts = None
        self._closed_until = -1

    def add_packet(self, pkt: dict):
        with self._lock:
            if self._start_ts is None:
                self._start_ts = pkt["ts"]

            window_idx = int((pkt["ts"] - self._start_ts) // self.window)
            self._raw_windows[window_idx].append(pkt)

    def flush_closed_windows(self, current_ts: float):
        """
        Fecha e processa janelas cujo tempo já passou,
        gerando as linhas de série temporal (chamado periodicamente).
        """
        if self._start_ts is None:
            return

        with self._lock:
            current_idx = int((current_ts - self._start_ts) // self.window)
            # Processa todas as janelas completas (com margem de 1 janela)
            for idx in sorted(k for k in self._raw_windows if k < current_idx):
                if idx <= self._closed_until:
                    continue
                self._process_window(idx)
                self._closed_until = idx
                del self._raw_windows[idx]

    def _normalize_flow(self, src, dst):
        """Normaliza o par (src,dst) para um fluxo bidirecional único."""
        return f"{src}-{dst}" if src <= dst else f"{dst}-{src}"

    def _process_window(self, window_idx: int):
        """Calcula as features de cada fluxo dentro de uma janela."""
        packets = self._raw_windows[window_idx]
        if not packets:
            return

        t = window_idx * self.window

        # Agrupar pacotes por fluxo dentro desta janela
        by_flow = defaultdict(list)
        for pkt in packets:
            flow_key = self._normalize_flow(pkt["src"], pkt["dst"])
            by_flow[flow_key].append(pkt)

        active_hosts = set()
        for pkt in packets:
            active_hosts.add(pkt["src"])
            active_hosts.add(pkt["dst"])

        flow_count = len(by_flow)

        for flow_key, pkts in by_flow.items():
            lengths = [p["length"] for p in pkts]
            n_pkts  = len(pkts)
            n_bytes = sum(lengths)

            mbps = (n_bytes * 8) / (self.window * 1_000_000)
            pps  = n_pkts / self.window

            avg_pkt = float(np.mean(lengths)) if lengths else 0.0
            std_pkt = float(np.std(lengths))  if lengths else 0.0

            n_tcp = sum(1 for p in pkts if p["proto"] == "TCP")
            n_udp = sum(1 for p in pkts if p["proto"] == "UDP")
            tcp_ratio = n_tcp / n_pkts if n_pkts else 0.0
            udp_ratio = n_udp / n_pkts if n_pkts else 0.0

            self.series[flow_key].append({
                "t":            round(t, 2),
                "bytes":        n_bytes,
                "mbps":         round(mbps, 4),
                "pps":          round(pps, 2),
                "avg_pkt":      round(avg_pkt, 1),
                "std_pkt":      round(std_pkt, 1),
                "tcp_ratio":    round(tcp_ratio, 3),
                "udp_ratio":    round(udp_ratio, 3),
                "flow_count":   flow_count,
                "active_hosts": len(active_hosts),
            })

    def finalize(self, end_ts: float):
        """Processa todas as janelas remanescentes ao final da captura."""
        with self._lock:
            if self._start_ts is None:
                return
            current_idx = int((end_ts - self._start_ts) // self.window) + 1
            for idx in sorted(self._raw_windows.keys()):
                if idx <= self._closed_until:
                    continue
                self._process_window(idx)
                self._closed_until = idx
            self._raw_windows.clear()


# ─── Captura Orquestrada ───────────────────────────────────────────────────────

class LiveCapture:
    """Orquestra múltiplos TsharkLiveReader (um por host) + 1 WindowAggregator."""

    def __init__(self, hosts=HOSTS, window=WINDOW):
        self.hosts      = hosts
        self.aggregator = WindowAggregator(window=window)
        self.readers    = {}

    def start_all(self):
        print("\n=== Iniciando captura em tempo real (tshark) ===")
        for h in self.hosts:
            reader = TsharkLiveReader(h, on_packet=self.aggregator.add_packet)
            if reader.start():
                self.readers[h] = reader
        print(f"  {len(self.readers)} capturas ativas\n")

    def stop_all(self):
        print("\n=== Parando capturas ===")
        for h, reader in self.readers.items():
            reader.stop()
            print(f"  ✓ {h} parado")


# ─── Exportador de Dataset (ajustes #4, #8, #9) ───────────────────────────────

class DatasetExporter:
    """
    Exporta a série temporal coletada em múltiplos formatos:
      - JSON com série completa por fluxo (ajuste #8)
      - CSV "long" (timestamp, flow, mbps, pps, ...)
      - CSV "wide" (timestamp, flow1_mbps, flow2_mbps, ...) pronto p/ LSTM (ajuste #4)
      - Janelas deslizantes .npy (SEQ_LEN) para treino direto (ajuste #9)
    """

    FEATURES = ["mbps", "pps", "avg_pkt", "std_pkt",
                "tcp_ratio", "udp_ratio", "flow_count", "active_hosts"]

    def __init__(self, output_dir: Path, run_id: str):
        self.output_dir = output_dir
        self.run_id      = run_id
        output_dir.mkdir(parents=True, exist_ok=True)

    def save_series_json(self, series: dict):
        """Salva a série temporal completa por fluxo em JSON (ajuste #8)."""
        path = self.output_dir / f"series_{self.run_id}.json"
        payload = {
            "run_id": self.run_id,
            "window_seconds": WINDOW,
            "flows": {k: v for k, v in series.items()},
        }
        with open(path, "w") as f:
            json.dump(payload, f, indent=2)
        print(f"  ✓ Série completa salva: {path}")
        return path

    def save_long_csv(self, series: dict):
        """
        Formato long: uma linha por (timestamp, flow).
        timestamp,flow,mbps,pps,avg_pkt,std_pkt,tcp_ratio,udp_ratio,flow_count,active_hosts
        """
        path = self.output_dir / f"flows_long_{self.run_id}.csv"
        header = ["timestamp", "flow"] + self.FEATURES

        with open(path, "w") as f:
            f.write(",".join(header) + "\n")
            for flow_key, points in series.items():
                for p in points:
                    row = [str(p["t"]), flow_key] + [str(p[feat]) for feat in self.FEATURES]
                    f.write(",".join(row) + "\n")

        print(f"  ✓ CSV long salvo: {path}")
        return path

    def save_wide_csv(self, series: dict, feature="mbps"):
        """
        Formato wide: uma coluna por fluxo, pronto para LSTM multivariado.
        timestamp,h1-h2,h1-h3,h2-h3,...
        """
        path = self.output_dir / f"flows_wide_{feature}_{self.run_id}.csv"

        # Descobrir todos os timestamps únicos
        all_ts = sorted({p["t"] for points in series.values() for p in points})
        flow_keys = sorted(series.keys())

        # Indexar por (flow, t) -> valor
        lookup = defaultdict(dict)
        for flow_key, points in series.items():
            for p in points:
                lookup[flow_key][p["t"]] = p[feature]

        with open(path, "w") as f:
            f.write("timestamp," + ",".join(flow_keys) + "\n")
            for t in all_ts:
                row = [str(t)]
                for fk in flow_keys:
                    row.append(str(lookup[fk].get(t, 0.0)))
                f.write(",".join(row) + "\n")

        print(f"  ✓ CSV wide ({feature}) salvo: {path}")
        return path

    def export_sliding_windows(self, series: dict, seq_len=SEQ_LEN):
        """
        Exporta janelas deslizantes (X, y) por fluxo, em .npy,
        prontas para alimentar um LSTM (ajuste #9).

        X.shape = (N, seq_len, n_features)
        y.shape = (N,)   -> mbps do próximo instante (t + seq_len)
        """
        windows_dir = self.output_dir / "lstm_windows"
        windows_dir.mkdir(exist_ok=True)

        summary = {}

        for flow_key, points in series.items():
            if len(points) < seq_len + 1:
                continue

            points_sorted = sorted(points, key=lambda p: p["t"])
            matrix = np.array(
                [[p[feat] for feat in self.FEATURES] for p in points_sorted],
                dtype=np.float32
            )

            X, y = [], []
            mbps_idx = self.FEATURES.index("mbps")

            for i in range(len(matrix) - seq_len):
                X.append(matrix[i:i + seq_len])
                y.append(matrix[i + seq_len, mbps_idx])

            X = np.array(X, dtype=np.float32)
            y = np.array(y, dtype=np.float32)

            safe_name = flow_key.replace(".", "_").replace("-", "_to_")
            np.save(windows_dir / f"{safe_name}_X.npy", X)
            np.save(windows_dir / f"{safe_name}_y.npy", y)

            summary[flow_key] = {"shape_X": list(X.shape), "shape_y": list(y.shape)}
            print(f"  ✓ {flow_key}: X={X.shape} y={y.shape}")

        meta_path = windows_dir / f"metadata_{self.run_id}.json"
        with open(meta_path, "w") as f:
            json.dump({
                "run_id": self.run_id,
                "seq_len": seq_len,
                "features": self.FEATURES,
                "flows": summary,
            }, f, indent=2)

        return summary


# ─── Programa Principal ────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="SDN Live Traffic Collector (tshark)")
    parser.add_argument("--duration",   default=60,      type=int,
                        help="Duração da captura em segundos")
    parser.add_argument("--output-dir", default="./pcap", type=str)
    parser.add_argument("--window",     default=WINDOW,  type=float,
                        help="Tamanho da janela temporal (segundos)")
    parser.add_argument("--seq-len",    default=SEQ_LEN, type=int,
                        help="Tamanho da janela deslizante para LSTM")
    args = parser.parse_args()

    output_dir = Path(args.output_dir)
    run_id     = datetime.now().strftime("%Y%m%d_%H%M%S")

    print(f"{'='*70}")
    print(f"  SDN Live Traffic Collector (tshark)")
    print(f"  Run ID  : {run_id}")
    print(f"  Janela  : {args.window}s")
    print(f"  Seq Len : {args.seq_len}")
    print(f"  Duração : {args.duration}s")
    print(f"{'='*70}")

    if subprocess.run(["which", "tshark"], capture_output=True).returncode != 0:
        print("ERRO: tshark não encontrado. Instale: sudo apt install tshark")
        return

    capture = LiveCapture(window=args.window)
    capture.start_all()

    if not capture.readers:
        print("Nenhuma captura iniciada. O Mininet está rodando?")
        return

    print(f"Capturando por {args.duration}s (processamento em tempo real)...")
    start = time.time()
    try:
        for i in range(args.duration):
            time.sleep(1)
            now = time.time()
            capture.aggregator.flush_closed_windows(now - start)
            if (i + 1) % 10 == 0:
                n_flows = len(capture.aggregator.series)
                print(f"  {i+1}/{args.duration}s | fluxos ativos: {n_flows}")
    except KeyboardInterrupt:
        print("\nInterrompido pelo usuário")

    end = time.time()
    capture.aggregator.finalize(end - start)
    capture.stop_all()

    series = capture.aggregator.series
    if not series:
        print("\nNenhum fluxo capturado. Verifique se há tráfego na rede.")
        return

    print(f"\n=== Exportando dataset ({len(series)} fluxos) ===")
    exporter = DatasetExporter(output_dir, run_id)
    exporter.save_series_json(series)
    exporter.save_long_csv(series)
    exporter.save_wide_csv(series, feature="mbps")
    exporter.save_wide_csv(series, feature="pps")
    exporter.export_sliding_windows(series, seq_len=args.seq_len)

    # Resumo final
    print(f"\n{'='*70}")
    print(f"  RESUMO DA COLETA")
    print(f"{'='*70}")
    total_points = sum(len(v) for v in series.values())
    print(f"  Fluxos capturados   : {len(series)}")
    print(f"  Total de janelas    : {total_points}")
    for flow_key, points in sorted(series.items(),
                                    key=lambda x: sum(p["mbps"] for p in x[1]),
                                    reverse=True)[:10]:
        avg_mbps = np.mean([p["mbps"] for p in points])
        max_mbps = np.max([p["mbps"] for p in points])
        print(f"    {flow_key:<25} janelas={len(points):>4}  "
              f"mbps_medio={avg_mbps:>7.2f}  mbps_max={max_mbps:>7.2f}")
    print(f"{'='*70}\n")


if __name__ == "__main__":
    main()

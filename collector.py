#!/usr/bin/env python3
"""
collector.py - Structured SDN Dataset Collector
================================================
Consumes real-time metrics from the SDN controller (via IPC socket),
enriches them with latency / packet-loss probes, and writes a
structured multi-link time-series dataset to disk.

Output schema (CSV):
    timestamp | link_id | utilization | throughput_mbps |
    rtt_ms | loss_pct | growth_rate | tx_mbps | rx_mbps |
    tx_pkts | rx_pkts | tx_errors | rx_errors | dpid | port_no

Features:
  - Synchronized 1-second collection windows
  - Per-link independent time series
  - Growth rate calculation (utilization derivative)
  - Automatic dataset partitioning by experiment run
  - JSONL + CSV dual output
  - Sliding-window export helper for LSTM/GRU training

Usage:
    python3 collector.py [--output-dir ./dataset] [--duration 300]
                         [--controller-host 127.0.0.1]
                         [--topology topology.json]
"""

import argparse
import csv
import json
import logging
import os
import socket
import subprocess
import threading
import time
from collections import defaultdict, deque
from datetime import datetime
from pathlib import Path
import struct

import numpy as np

# ─── Configuration ─────────────────────────────────────────────────────────────

CONTROLLER_HOST   = "127.0.0.1"
CONTROLLER_IPC    = 9999          # TCP port of controller IPC publisher
COLLECTION_INTERVAL = 1.0         # seconds
PING_COUNT        = 3             # pings per probe
PING_HOSTS        = [             # host pairs to probe (index-based)
    ("10.0.0.1", "10.0.0.5"),
    ("10.0.0.2", "10.0.0.6"),
    ("10.0.0.3", "10.0.0.7"),
    ("10.0.0.4", "10.0.0.8"),
]
GROWTH_WINDOW     = 5             # samples used for growth rate calculation
SLIDING_WINDOW_SIZE = 10          # default window for LSTM export

LOG = logging.getLogger("sdn.collector")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s %(message)s"
)


# ─── CSV Schema ────────────────────────────────────────────────────────────────

FIELDNAMES = [
    "timestamp",
    "link_id",
    "utilization",       # max(tx, rx) utilization %
    "throughput_mbps",   # tx_mbps (egress)
    "rtt_ms",            # latency probe (ping RTT avg)
    "loss_pct",          # packet loss from ping
    "growth_rate",       # d(utilization)/dt
    "tx_mbps",
    "rx_mbps",
    "tx_pkts",
    "rx_pkts",
    "tx_errors",
    "rx_errors",
    "dpid",
    "port_no",
    "run_id",
]


# ─── Latency Prober ────────────────────────────────────────────────────────────

class LatencyProber:
    """
    Runs ping probes in parallel threads and caches results.
    Each probe runs every COLLECTION_INTERVAL seconds.
    """

    def __init__(self, host_pairs=PING_HOSTS, count=PING_COUNT):
        self.host_pairs = host_pairs
        self.count      = count
        self._results   = {}   # {(src, dst): {"rtt_ms": float, "loss_pct": float, "ts": float}}
        self._lock      = threading.Lock()
        self._threads   = []
        self._stop      = threading.Event()

    def start(self):
        for src, dst in self.host_pairs:
            t = threading.Thread(
                target=self._probe_loop,
                args=(src, dst),
                daemon=True
            )
            t.start()
            self._threads.append(t)
        LOG.info("LatencyProber started for %d host pairs", len(self.host_pairs))

    def stop(self):
        self._stop.set()

    def get(self, src=None, dst=None):
        """
        Return latest probe result for (src, dst).
        If no specific pair given, returns average over all pairs.
        """
        with self._lock:
            if src and dst:
                return self._results.get((src, dst), {"rtt_ms": None, "loss_pct": None})

            results = list(self._results.values())

        if not results:
            return {"rtt_ms": None, "loss_pct": None}

        rtts   = [r["rtt_ms"]   for r in results if r["rtt_ms"]   is not None]
        losses = [r["loss_pct"] for r in results if r["loss_pct"] is not None]

        return {
            "rtt_ms":   float(np.mean(rtts))   if rtts   else None,
            "loss_pct": float(np.mean(losses)) if losses else None,
        }

    def _probe_loop(self, src, dst):
        while not self._stop.is_set():
            result = self._run_ping(src, dst)
            with self._lock:
                self._results[(src, dst)] = result
            self._stop.wait(COLLECTION_INTERVAL)

    def _run_ping(self, src, dst):
        """Execute ping and parse RTT + loss."""
        try:
            cmd = ["ping", "-c", str(self.count), "-W", "1", dst]
            out = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=10
            )
            return self._parse_ping(out.stdout)
        except Exception as e:
            LOG.debug("Ping %s→%s failed: %s", src, dst, e)
            return {"rtt_ms": None, "loss_pct": 100.0, "ts": time.time()}

    @staticmethod
    def _parse_ping(output):
        """Parse ping output for RTT avg and packet loss."""
        rtt_ms   = None
        loss_pct = 100.0
        ts       = time.time()

        for line in output.splitlines():
            # Packet loss: "3 packets transmitted, 3 received, 0% packet loss"
            if "packet loss" in line:
                try:
                    loss_pct = float(line.split("%")[0].split()[-1])
                except (ValueError, IndexError):
                    pass

            # RTT: "rtt min/avg/max/mdev = 0.123/0.456/0.789/0.100 ms"
            if "rtt min/avg/max/mdev" in line or "round-trip" in line:
                try:
                    parts = line.split("=")[1].strip().split("/")
                    rtt_ms = float(parts[1])   # avg
                except (IndexError, ValueError):
                    pass

        return {"rtt_ms": rtt_ms, "loss_pct": loss_pct, "ts": ts}


# ─── Dataset Writer ────────────────────────────────────────────────────────────

class DatasetWriter:
    """
    Manages CSV + JSONL output files, one per experiment run.
    """

    def __init__(self, output_dir: Path, run_id: str):
        self.output_dir = output_dir
        self.run_id     = run_id
        output_dir.mkdir(parents=True, exist_ok=True)

        self.csv_path  = output_dir / f"run_{run_id}.csv"
        self.jsonl_path = output_dir / f"run_{run_id}.jsonl"

        self._csv_fh   = open(self.csv_path,  "w", newline="", buffering=1)
        self._jsonl_fh = open(self.jsonl_path, "w", buffering=1)

        self._writer = csv.DictWriter(self._csv_fh, fieldnames=FIELDNAMES)
        self._writer.writeheader()

        self._row_count = 0
        LOG.info("Dataset writer: CSV=%s JSONL=%s", self.csv_path, self.jsonl_path)

    def write(self, row: dict):
        row["run_id"] = self.run_id
        # Ensure all fields present
        for f in FIELDNAMES:
            row.setdefault(f, None)
        self._writer.writerow(row)
        self._jsonl_fh.write(json.dumps(row) + "\n")
        self._row_count += 1

    def flush(self):
        self._csv_fh.flush()
        self._jsonl_fh.flush()

    def close(self):
        self._csv_fh.close()
        self._jsonl_fh.close()
        LOG.info("Dataset closed: %d rows written to %s", self._row_count, self.csv_path)

    @property
    def row_count(self):
        return self._row_count


# ─── Growth Rate Calculator ────────────────────────────────────────────────────

class GrowthRateTracker:
    """
    Maintains a rolling window of utilization values per link
    and computes the growth rate (1st derivative) via finite difference.
    """

    def __init__(self, window=GROWTH_WINDOW):
        self.window = window
        self._history = defaultdict(lambda: deque(maxlen=window))

    def update(self, link_id: str, utilization: float, ts: float) -> float:
        """Add a new sample and return current growth rate."""
        hist = self._history[link_id]
        hist.append((ts, utilization))

        if len(hist) < 2:
            return 0.0

        # Simple linear regression over window for robust derivative
        ts_arr  = np.array([h[0] for h in hist])
        val_arr = np.array([h[1] for h in hist])

        # Normalize time axis
        dt = ts_arr - ts_arr[0]
        if dt[-1] == 0:
            return 0.0

        # Least-squares slope
        if len(dt) >= 2:
            coeffs = np.polyfit(dt, val_arr, 1)
            return float(coeffs[0])   # % utilization per second
        return 0.0

    def reset(self, link_id: str):
        self._history.pop(link_id, None)


# ─── IPC Receiver ─────────────────────────────────────────────────────────────

class IPCReceiver:
    """
    Connects to controller's IPC TCP stream and yields parsed metric dicts.
    Handles reconnection automatically.
    """

    def __init__(self, host=CONTROLLER_HOST, port=CONTROLLER_IPC):
        self.host = host
        self.port = port
        self._sock = None

    def connect(self):
        while True:
            try:
                self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                self._sock.connect((self.host, self.port))
                self._sock.settimeout(5.0)
                LOG.info("IPC connected to %s:%d", self.host, self.port)
                return
            except ConnectionRefusedError:
                LOG.warning("Controller IPC not available, retrying in 2s...")
                time.sleep(2)

    def receive(self):
        """Yield parsed metric frames. Reconnects on error."""
        buf = b""
        while True:
            try:
                chunk = self._sock.recv(65536)
                if not chunk:
                    raise ConnectionResetError("Controller closed connection")
                buf += chunk

                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    if line:
                        try:
                            yield json.loads(line)
                        except json.JSONDecodeError:
                            pass

            except (socket.timeout, ConnectionResetError, OSError) as e:
                LOG.warning("IPC disconnected: %s. Reconnecting...", e)
                self.connect()
                buf = b""


# ─── Sliding Window Exporter ───────────────────────────────────────────────────

class SlidingWindowExporter:
    """
    Builds numpy arrays of shape (N, window_size, features)
    suitable for LSTM/GRU training.

    Target variables (configurable):
      - utilization (t+1)
      - rtt_ms (t+1)
      - congestion flag (utilization > threshold)
    """

    FEATURES = [
        "utilization", "throughput_mbps", "rtt_ms",
        "loss_pct", "growth_rate", "tx_mbps", "rx_mbps"
    ]

    def __init__(self, window_size=SLIDING_WINDOW_SIZE, pred_horizon=1,
                 congestion_threshold=80.0):
        self.window_size  = window_size
        self.pred_horizon = pred_horizon
        self.threshold    = congestion_threshold
        self._link_buffers = defaultdict(list)

    def add_row(self, row: dict):
        """Add a collected row to the internal buffer."""
        link = row.get("link_id", "unknown")
        self._link_buffers[link].append(row)

    def export(self, output_dir: Path, run_id: str):
        """
        Export sliding windows for each link.
        Returns dict: {link_id: {"X": ndarray, "y_util": ndarray, "y_cong": ndarray}}
        """
        results = {}
        export_dir = output_dir / "windows"
        export_dir.mkdir(exist_ok=True)

        for link_id, rows in self._link_buffers.items():
            if len(rows) < self.window_size + self.pred_horizon:
                continue

            # Build feature matrix
            matrix = []
            for row in rows:
                vec = []
                for f in self.FEATURES:
                    v = row.get(f)
                    vec.append(float(v) if v is not None else 0.0)
                matrix.append(vec)

            matrix = np.array(matrix, dtype=np.float32)

            # Replace NaN
            matrix = np.nan_to_num(matrix, nan=0.0)

            # Build windows
            X, y_util, y_cong = [], [], []
            util_idx = self.FEATURES.index("utilization")

            for i in range(len(matrix) - self.window_size - self.pred_horizon + 1):
                X.append(matrix[i : i + self.window_size])
                future_util = matrix[i + self.window_size + self.pred_horizon - 1, util_idx]
                y_util.append(future_util)
                y_cong.append(1.0 if future_util >= self.threshold else 0.0)

            X      = np.array(X,      dtype=np.float32)
            y_util = np.array(y_util, dtype=np.float32)
            y_cong = np.array(y_cong, dtype=np.float32)

            safe_id = link_id.replace(":", "_")
            np.save(export_dir / f"{safe_id}_X.npy",      X)
            np.save(export_dir / f"{safe_id}_y_util.npy", y_util)
            np.save(export_dir / f"{safe_id}_y_cong.npy", y_cong)

            results[link_id] = {
                "X":      X,
                "y_util": y_util,
                "y_cong": y_cong,
                "shape":  X.shape,
            }
            LOG.info("Link %s: windows=%d shape=%s", link_id, len(X), X.shape)

        # Save metadata
        meta = {
            "run_id":       run_id,
            "window_size":  self.window_size,
            "pred_horizon": self.pred_horizon,
            "features":     self.FEATURES,
            "links":        list(results.keys()),
            "shapes":       {k: list(v["shape"]) for k, v in results.items()},
        }
        with open(export_dir / f"metadata_{run_id}.json", "w") as f:
            json.dump(meta, f, indent=2)

        LOG.info("Sliding windows exported to %s", export_dir)
        return results


# ─── Main Collector ────────────────────────────────────────────────────────────

class SDNCollector:
    """
    Orchestrates metric collection, enrichment, and storage.
    """

    def __init__(self, output_dir="./dataset", duration=300,
                 controller_host=CONTROLLER_HOST,
                 topology_path=None, run_id=None):
        self.output_dir  = Path(output_dir)
        self.duration    = duration
        self.run_id      = run_id or datetime.now().strftime("%Y%m%d_%H%M%S")

        self.ipc         = IPCReceiver(host=controller_host)
        self.prober      = LatencyProber()
        self.writer      = DatasetWriter(self.output_dir, self.run_id)
        self.growth      = GrowthRateTracker()
        self.exporter    = SlidingWindowExporter()

        self._topology   = self._load_topology(topology_path)
        self._stop       = threading.Event()

        LOG.info("Collector run_id=%s duration=%ds output=%s",
                 self.run_id, self.duration, self.output_dir)

    def _load_topology(self, path):
        if path and os.path.exists(path):
            with open(path) as f:
                topo = json.load(f)
            LOG.info("Topology loaded: %d links", len(topo.get("links", [])))
            return topo
        return {}

    def run(self):
        """Main collection loop."""
        self.prober.start()
        self.ipc.connect()

        start_ts = time.time()
        LOG.info("Collection started. Duration: %ds", self.duration)

        try:
            for frame in self.ipc.receive():
                if time.time() - start_ts >= self.duration:
                    break

                self._process_frame(frame)
                self.writer.flush()

        except KeyboardInterrupt:
            LOG.info("Collection interrupted by user")
        finally:
            self._finalize()

    def _process_frame(self, frame: dict):
        """Process a single metric frame from the controller."""
        ts    = frame.get("ts", time.time())
        stats = frame.get("stats", [])

        # Get latest latency probe (shared across links for now)
        probe = self.prober.get()

        for entry in stats:
            link_id = entry.get("link_id", "unknown")
            util    = entry.get("util_pct", 0.0)

            # Growth rate
            growth_rate = self.growth.update(link_id, util, ts)

            row = {
                "timestamp":      ts,
                "link_id":        link_id,
                "utilization":    round(util, 4),
                "throughput_mbps": round(entry.get("tx_mbps", 0.0), 4),
                "rtt_ms":         round(probe["rtt_ms"], 3) if probe["rtt_ms"] else None,
                "loss_pct":       round(probe["loss_pct"], 2) if probe["loss_pct"] is not None else None,
                "growth_rate":    round(growth_rate, 6),
                "tx_mbps":        round(entry.get("tx_mbps", 0.0), 4),
                "rx_mbps":        round(entry.get("rx_mbps", 0.0), 4),
                "tx_pkts":        entry.get("tx_pkts", 0),
                "rx_pkts":        entry.get("rx_pkts", 0),
                "tx_errors":      entry.get("tx_errors", 0),
                "rx_errors":      entry.get("rx_errors", 0),
                "dpid":           entry.get("dpid", 0),
                "port_no":        entry.get("port_no", 0),
            }

            self.writer.write(row)
            self.exporter.add_row(row)

    def _finalize(self):
        LOG.info("Finalizing dataset...")
        self.prober.stop()
        self.writer.close()

        LOG.info("Exporting sliding windows for LSTM/GRU...")
        windows = self.exporter.export(self.output_dir, self.run_id)

        # Write summary
        summary = {
            "run_id":        self.run_id,
            "total_rows":    self.writer.row_count,
            "links_collected": len(windows),
            "output_dir":    str(self.output_dir),
            "csv_path":      str(self.writer.csv_path),
            "jsonl_path":    str(self.writer.jsonl_path),
            "window_size":   SLIDING_WINDOW_SIZE,
            "features":      SlidingWindowExporter.FEATURES,
        }
        summary_path = self.output_dir / f"summary_{self.run_id}.json"
        with open(summary_path, "w") as f:
            json.dump(summary, f, indent=2)

        LOG.info("Collection complete: %d rows, %d links", self.writer.row_count, len(windows))
        LOG.info("Summary: %s", summary_path)
        return summary


# ─── CLI Entry Point ───────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="SDN Dataset Collector")
    parser.add_argument("--output-dir",       default="./dataset")
    parser.add_argument("--duration",         default=300, type=int,
                        help="Collection duration in seconds")
    parser.add_argument("--controller-host",  default=CONTROLLER_HOST)
    parser.add_argument("--topology",         default="topology.json",
                        help="Path to topology.json from topo.py")
    parser.add_argument("--run-id",           default=None,
                        help="Custom run identifier")
    parser.add_argument("--window-size",      default=SLIDING_WINDOW_SIZE, type=int,
                        help="Sliding window size for LSTM export")
    args = parser.parse_args()

    collector = SDNCollector(
        output_dir=args.output_dir,
        duration=args.duration,
        controller_host=args.controller_host,
        topology_path=args.topology,
        run_id=args.run_id,
    )
    collector.run()


if __name__ == "__main__":
    main()
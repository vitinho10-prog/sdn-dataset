#!/usr/bin/env python3
"""
pipeline.py - SDN Dataset Collection Pipeline Orchestrator
===========================================================
Coordinates Mininet + Ryu controller + traffic generation + collection
in a single script. Designed for automated dataset generation.

Architecture:
  1. Start Ryu controller in a subprocess
  2. Start Mininet with Fat-tree topology
  3. Start dataset collector in a background thread
  4. Run traffic generator scenarios
  5. Stop everything and export final dataset

Usage:
    sudo python3 pipeline.py --duration 300 --scenario mixed
    sudo python3 pipeline.py --benchmark --output-dir ./dataset_v1
    sudo python3 pipeline.py --help
"""

import argparse
import json
import logging
import os
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

LOG = logging.getLogger("sdn.pipeline")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s %(message)s"
)

CONTROLLER_STARTUP_WAIT = 5   # seconds to wait for Ryu to start
MININET_STARTUP_WAIT    = 4   # seconds to wait for Mininet + OVS


def check_dependencies():
    """Verify required tools are installed."""
    required = {
        "mn":          "Mininet not found. Install: apt install mininet",
        "ryu-manager": "Ryu not found. Install: pip install ryu",
        "ovs-vsctl":   "Open vSwitch not found. Install: apt install openvswitch-switch",
        "iperf3":      "iperf3 not found. Install: apt install iperf3",
        "ping":        "ping not found",
    }
    missing = []
    for cmd, msg in required.items():
        if subprocess.run(["which", cmd], capture_output=True).returncode != 0:
            missing.append(f"  ✗ {msg}")

    if missing:
        LOG.error("Missing dependencies:\n%s", "\n".join(missing))
        sys.exit(1)
    LOG.info("All dependencies found.")


def start_controller(log_path="ryu.log"):
    """Start Ryu controller as a subprocess."""
    controller_script = Path(__file__).parent / "controller.py"
    log_fh = open(log_path, "w")

    proc = subprocess.Popen(
        ["ryu-manager", str(controller_script), "--observe-links",
         "--ofp-tcp-listen-port", "6633"],
        stdout=log_fh,
        stderr=log_fh,
        preexec_fn=os.setsid
    )
    LOG.info("Ryu controller started (PID=%d). Log: %s", proc.pid, log_path)
    time.sleep(CONTROLLER_STARTUP_WAIT)

    if proc.poll() is not None:
        LOG.error("Controller exited prematurely. Check %s", log_path)
        sys.exit(1)

    return proc, log_fh


def run_pipeline(args):
    """Main pipeline execution."""
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    run_id = args.run_id or datetime.now().strftime("%Y%m%d_%H%M%S")

    LOG.info("="*60)
    LOG.info("  SDN Dataset Pipeline")
    LOG.info("  Run ID  : %s", run_id)
    LOG.info("  Output  : %s", output_dir)
    LOG.info("  Duration: %ds", args.duration)
    LOG.info("="*60)

    check_dependencies()

    # 1. Start Ryu controller
    ctrl_proc, ctrl_log = start_controller(str(output_dir / f"ryu_{run_id}.log"))

    try:
        # 2. Import modules (after controller is up)
        from topo import FatTreeTopo, build_network, print_topology_summary
        from traffic import TrafficGenerator
        from collector import SDNCollector
        from mininet.log import setLogLevel
        from mininet.cli import CLI

        setLogLevel("warning")

        # 3. Build Mininet network
        LOG.info("Starting Mininet Fat-tree topology...")
        net, topo = build_network()
        print_topology_summary(topo)
        topo.export_topology_json(str(output_dir / "topology.json"))
        net.start()
        LOG.info("Mininet started. Waiting %ds for convergence...", MININET_STARTUP_WAIT)
        time.sleep(MININET_STARTUP_WAIT)

        # 4. Start collector in background thread
        collector = SDNCollector(
            output_dir=str(output_dir),
            duration=args.duration + 30,   # a bit longer than traffic
            run_id=run_id,
            topology_path=str(output_dir / "topology.json")
        )
        collector_thread = threading.Thread(target=collector.run, daemon=True)
        collector_thread.start()
        LOG.info("Collector running in background...")
        time.sleep(2)   # let collector connect

        # 5. Traffic generation
        traffic = TrafficGenerator(net=net)

        try:
            if args.benchmark:
                LOG.info("Running full benchmark sequence...")
                traffic.run_benchmark(total_duration=args.duration)
            else:
                scenario = args.scenario
                LOG.info("Running scenario: %s (%ds)", scenario, args.duration)
                if scenario == "random":
                    traffic.run_random(duration=args.duration)
                else:
                    traffic.run_scenario(scenario, duration=args.duration)

        except KeyboardInterrupt:
            LOG.info("Traffic interrupted by user")
        finally:
            traffic.stop()

        # 6. Wait for collector to finish
        LOG.info("Traffic complete. Waiting for collector to finalize...")
        collector_thread.join(timeout=15)

        # 7. Drop into CLI if requested
        if args.cli:
            LOG.info("Dropping into Mininet CLI. Type 'exit' to quit.")
            CLI(net)

    finally:
        # Cleanup
        try:
            net.stop()
        except Exception:
            pass

        import os, signal
        try:
            os.killpg(os.getpgid(ctrl_proc.pid), signal.SIGTERM)
        except Exception:
            ctrl_proc.terminate()
        try:
            ctrl_log.close()
        except Exception:
            pass

        LOG.info("Pipeline complete. Dataset in: %s", output_dir)


def main():
    parser = argparse.ArgumentParser(
        description="SDN Dataset Collection Pipeline",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  sudo python3 pipeline.py --duration 300 --scenario mixed
  sudo python3 pipeline.py --benchmark --duration 600 --output-dir ./data
  sudo python3 pipeline.py --scenario congestion --duration 120 --cli
        """
    )
    parser.add_argument("--output-dir",  default="./dataset",
                        help="Directory for dataset output")
    parser.add_argument("--duration",    default=300, type=int,
                        help="Total collection duration in seconds")
    parser.add_argument("--scenario",    default="mixed",
                        choices=["background","burst","elephant","mice",
                                 "congestion","sweep","mixed","random"],
                        help="Traffic scenario")
    parser.add_argument("--benchmark",   action="store_true",
                        help="Run all scenarios sequentially")
    parser.add_argument("--run-id",      default=None,
                        help="Custom run identifier")
    parser.add_argument("--cli",         action="store_true",
                        help="Drop into Mininet CLI after traffic generation")
    args = parser.parse_args()

    if os.geteuid() != 0:
        LOG.error("This script must be run as root (sudo)")
        sys.exit(1)

    run_pipeline(args)


if __name__ == "__main__":
    main()
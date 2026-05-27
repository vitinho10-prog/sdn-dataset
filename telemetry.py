#!/usr/bin/env python3
"""
telemetry.py - SDN Real-Time Telemetry Dashboard
=================================================
Lê o stream IPC do controlador e exibe métricas ao vivo no terminal.
Atualiza a cada 1 segundo.

Uso:
    python3 telemetry.py
"""

import socket
import json
import time
import os
import sys
from datetime import datetime
from collections import defaultdict

IPC_HOST = "127.0.0.1"
IPC_PORT = 9999

# Cores ANSI
RED    = "\033[91m"
YELLOW = "\033[93m"
GREEN  = "\033[92m"
BLUE   = "\033[94m"
CYAN   = "\033[96m"
BOLD   = "\033[1m"
RESET  = "\033[0m"
CLEAR  = "\033[2J\033[H"


def color_bar(pct):
    """Barra de progresso colorida baseada na utilização."""
    filled = int(pct / 10)
    empty  = 10 - filled
    bar    = "▓" * filled + "░" * empty

    if pct >= 90:
        return f"{RED}{bar}{RESET}"
    elif pct >= 70:
        return f"{YELLOW}{bar}{RESET}"
    else:
        return f"{GREEN}{bar}{RESET}"


def status_label(pct):
    """Rótulo de status baseado na utilização."""
    if pct >= 90:
        return f"{RED}🔴 CONGESTIONADO{RESET}"
    elif pct >= 70:
        return f"{YELLOW}⚠  ALTO        {RESET}"
    elif pct >= 30:
        return f"{BLUE}🔵 MODERADO    {RESET}"
    else:
        return f"{GREEN}✅ NORMAL      {RESET}"


def draw_dashboard(stats, elapsed):
    """Desenha o dashboard no terminal."""
    print(CLEAR, end="")

    now = datetime.now().strftime("%H:%M:%S")
    n_links = len(stats)
    n_congested = sum(1 for s in stats.values() if s["util"] >= 90)
    n_high      = sum(1 for s in stats.values() if 70 <= s["util"] < 90)

    # Cabeçalho
    print(f"{BOLD}{CYAN}{'='*75}{RESET}")
    print(f"{BOLD}{CYAN}  SDN Telemetria em Tempo Real{RESET}  "
          f"  ⏱  {now}  |  Links: {n_links}  |  Tempo: {elapsed:.0f}s")
    print(f"{BOLD}{CYAN}{'='*75}{RESET}")
    print()

    # Alertas
    if n_congested > 0:
        print(f"{RED}{BOLD}  ⚠  ALERTA: {n_congested} link(s) CONGESTIONADO(s)!{RESET}")
        print()
    elif n_high > 0:
        print(f"{YELLOW}{BOLD}  ⚠  ATENÇÃO: {n_high} link(s) com utilização ALTA!{RESET}")
        print()

    # Cabeçalho da tabela
    print(f"{BOLD}  {'Link':<12} {'Utilização':>12}  {'Barra':<14} "
          f"{'TX Mbps':>10} {'RX Mbps':>10}  Status{RESET}")
    print(f"  {'-'*70}")

    # Linhas ordenadas por utilização
    sorted_stats = sorted(stats.items(),
                          key=lambda x: x[1]["util"], reverse=True)

    for link_id, s in sorted_stats:
        util  = s["util"]
        tx    = s["tx"]
        rx    = s["rx"]
        bar   = color_bar(util)
        label = status_label(util)

        # Cor da linha baseada na utilização
        if util >= 90:
            line_color = RED
        elif util >= 70:
            line_color = YELLOW
        else:
            line_color = RESET

        print(f"  {line_color}{link_id:<12}{RESET} "
              f"{line_color}{util:>11.1f}%{RESET}  "
              f"{bar}  "
              f"{line_color}{tx:>10.1f}{RESET} "
              f"{line_color}{rx:>10.1f}{RESET}  "
              f"{label}")

    print(f"\n  {'-'*70}")

    # Resumo
    if stats:
        utils = [s["util"] for s in stats.values()]
        txs   = [s["tx"]   for s in stats.values()]
        print(f"\n  {BOLD}Resumo:{RESET}")
        print(f"    Utilização média : {sum(utils)/len(utils):.1f}%")
        print(f"    Utilização máxima: {max(utils):.1f}%")
        print(f"    Throughput total : {sum(txs):.1f} Mbps")

    print(f"\n  {CYAN}Pressione Ctrl+C para sair{RESET}")


def connect():
    """Conecta ao IPC do controlador."""
    while True:
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.connect((IPC_HOST, IPC_PORT))
            sock.settimeout(5.0)
            return sock
        except ConnectionRefusedError:
            print(f"{YELLOW}Aguardando controlador na porta {IPC_PORT}...{RESET}")
            time.sleep(2)


def main():
    print(f"{CYAN}Conectando ao controlador SDN...{RESET}")
    sock    = connect()
    buf     = b""
    stats   = {}
    start   = time.time()

    # Histórico para detectar tendência
    history = defaultdict(list)

    print(f"{GREEN}Conectado! Iniciando telemetria...{RESET}")
    time.sleep(1)

    try:
        while True:
            try:
                chunk = sock.recv(65536)
                if not chunk:
                    raise ConnectionResetError

                buf += chunk

                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    if not line:
                        continue
                    try:
                        frame = json.loads(line)
                    except json.JSONDecodeError:
                        continue

                    # Processar cada entrada do frame
                    for entry in frame.get("stats", []):
                        link_id = entry.get("link_id", "")
                        util    = entry.get("util_pct", 0.0)
                        tx      = entry.get("tx_mbps", 0.0)
                        rx      = entry.get("rx_mbps", 0.0)

                        if util < 0:
                            util = 0.0
                        if tx < 0:
                            tx = 0.0
                        if rx < 0:
                            rx = 0.0

                        stats[link_id] = {
                            "util": round(util, 1),
                            "tx":   round(tx, 1),
                            "rx":   round(rx, 1),
                        }

                        # Histórico de utilização
                        history[link_id].append(util)
                        if len(history[link_id]) > 10:
                            history[link_id].pop(0)

                    elapsed = time.time() - start
                    draw_dashboard(stats, elapsed)

            except socket.timeout:
                continue
            except (ConnectionResetError, OSError):
                print(f"\n{YELLOW}Reconectando...{RESET}")
                try:
                    sock.close()
                except Exception:
                    pass
                time.sleep(2)
                sock = connect()
                buf  = b""

    except KeyboardInterrupt:
        print(f"\n{CYAN}Telemetria encerrada.{RESET}")
    finally:
        try:
            sock.close()
        except Exception:
            pass


if __name__ == "__main__":
    main()

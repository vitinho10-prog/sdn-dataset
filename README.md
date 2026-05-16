# SDN Dataset Generation System for LSTM/GRU 🧠🌐

Este projeto implementa um sistema de monitoramento e coleta de métricas em tempo real para redes definidas por software (SDN). Ele gera datasets estruturados em séries temporais (e janelas deslizantes) projetados especificamente para o treinamento de modelos de Deep Learning, como **LSTM** e **GRU**, focados na predição proativa de congestionamento.

## 🚀 Como Funciona
O sistema coleta estatísticas de portas e links (via OpenFlow/IPC) e calcula métricas dinâmicas, com destaque para o **Growth Rate** (taxa de aceleração da utilização do link), permitindo identificar anomalias antes que o link atinja a saturação total.

---

## 🛠️ Pré-requisitos e Instalação

O projeto foi desenvolvido para rodar em ambientes Linux (Ubuntu/WSL2) utilizando o Mininet e o controlador Ryu.

### 1. Ambiente Python (Miniconda)
Recomenda-se o uso do Miniconda para isolar as dependências do projeto:

```bash
conda create -n sdn python=3.10 -y
conda activate sdn
```

### 2. Instalação das Dependências
Instale as bibliotecas necessárias para manipulação de dados, geração de gráficos e o ecossistema SDN:

```bash
pip install pandas matplotlib ryu
```
*(Certifique-se de ter o Mininet e o Open vSwitch instalados no sistema).*

---

## 💻 Instruções de Execução

Para rodar o experimento completo, abra **3 terminais** no seu ambiente WSL e execute-os estritamente na ordem descrita abaixo:

### 📡 Terminal 1 — Controlador Ryu
Inicia o controlador OpenFlow que gerencia os switches e expõe os contadores estatísticos:
```bash
conda activate sdn
cd ~/meus-projetos-p4/metricas
sudo ~/miniconda/envs/sdn/bin/ryu-manager controller.py
```
*Aguarde a mensagem indicando a conexão dos switches (ex: Switch conectado: dpid=7). Deixe este terminal rodando.*

### 📊 Terminal 2 — Coletor de Métricas (Collector)
Inicia a captura ativa dos dados por um período determinado (300 segundos). **Inicie este terminal logo após o Ryu estar pronto:**
```bash
conda activate sdn
cd ~/meus-projetos-p4/metricas
~/miniconda/envs/sdn/bin/python collector.py --duration 300 --output-dir ./dataset
```
*Aguarde aparecer a mensagem Collection started. Duration: 300s.*

### 🏎️ Terminal 3 — Topologia Mininet + Geração de Tráfego Dinâmico
Limpa instâncias antigas do Open vSwitch, constrói a topologia Spine-Leaf de switches e inicia o roteiro automatizado de injeção de tráfego (Fases 1 a 6) via iperf3. **Inicie este terminal imediatamente após o início da coleta:**
```bash
conda activate sdn
cd ~/meus-projetos-p4/metricas

# Limpeza de segurança de interfaces antigas
sudo ip link show | grep -oP '\d+: \K(ag|lf|sp|h)[0-9]+-eth[0-9]+(?=@)' | xargs -I{} sudo ip link delete {} 2>/dev/null
sudo ovs-vsctl list-br 2>/dev/null | xargs -I{} sudo ovs-vsctl del-br {} 2>/dev/null

# Execução do script de tráfego automatizado
sudo ~/miniconda/envs/sdn/bin/python - << 'EOF'
import sys
sys.path.insert(0, '/home/beatriz/meus-projetos-p4/metricas')
from mininet.net import Mininet
from mininet.node import RemoteController, OVSSwitch
from mininet.link import Link
from mininet.log import setLogLevel
from mininet.cli import CLI
import time, random

setLogLevel('warning')
net = Mininet(controller=None, switch=OVSSwitch, link=Link, autoSetMacs=True)
net.addController('c0', controller=RemoteController, ip='127.0.0.1', port=6633)

# Criação dos switches (Spine, Aggregation, Leaf)
sp1 = net.addSwitch('sp1', protocols='OpenFlow13', dpid='0000000000000001')
ag1 = net.addSwitch('ag1', protocols='OpenFlow13', dpid='0000000000000002')
ag2 = net.addSwitch('ag2', protocols='OpenFlow13', dpid='0000000000000003')
lf1 = net.addSwitch('lf1', protocols='OpenFlow13', dpid='0000000000000004')
lf2 = net.addSwitch('lf2', protocols='OpenFlow13', dpid='0000000000000005')
lf3 = net.addSwitch('lf3', protocols='OpenFlow13', dpid='0000000000000006')
lf4 = net.addSwitch('lf4', protocols='OpenFlow13', dpid='0000000000000007')

hosts = [net.addHost('h%d' % i, ip='10.0.0.%d/24' % i) for i in range(1,9)]
net.addLink(sp1, ag1); net.addLink(sp1, ag2)
net.addLink(ag1, lf1); net.addLink(ag1, lf2)
net.addLink(ag2, lf3); net.addLink(ag2, lf4)

for i, lf in enumerate([lf1,lf1,lf2,lf2,lf3,lf3,lf4,lf4]):
    net.addLink(lf, hosts[i])

net.start()

for h in net.hosts:
    h.cmd('sysctl -w net.ipv6.conf.all.disable_ipv6=1 2>/dev/null')
time.sleep(5)

for h in net.hosts:
    h.cmd('iperf3 -s -D -p 5201 2>/dev/null')
    h.cmd('iperf3 -s -D -p 5202 2>/dev/null')
    h.cmd('iperf3 -s -D -p 5203 2>/dev/null')
time.sleep(2)

print('=== Fase 1: Tráfego baixo (30s) ===')
net.get('h1').cmd('iperf3 -c 10.0.0.5 -b 10M -t 30 -p 5201 &')
net.get('h3').cmd('iperf3 -c 10.0.0.7 -b 10M -t 30 -p 5201 &')
time.sleep(32)

print('=== Fase 2: Tráfego médio (30s) ===')
net.get('h1').cmd('iperf3 -c 10.0.0.5 -b 50M -t 30 -p 5201 &')
net.get('h2').cmd('iperf3 -c 10.0.0.6 -b 40M -t 30 -p 5202 &')
net.get('h3').cmd('iperf3 -c 10.0.0.7 -b 30M -t 30 -p 5201 &')
time.sleep(32)

print('=== Fase 3: Congestionamento (30s) ===')
net.get('h1').cmd('iperf3 -c 10.0.0.5 -b 200M -t 30 -p 5201 &')
net.get('h2').cmd('iperf3 -c 10.0.0.6 -b 200M -t 30 -p 5202 &')
net.get('h3').cmd('iperf3 -c 10.0.0.7 -b 200M -t 30 -p 5203 &')
net.get('h4').cmd('iperf3 -c 10.0.0.8 -b 200M -t 30 -p 5201 &')
time.sleep(32)

print('=== Fase 4: Idle (20s) ===')
time.sleep(20)

print('=== Fase 5: Burst variado (60s) ===')
for _ in range(6):
    bw = random.randint(20, 150)
    net.get('h1').cmd('iperf3 -c 10.0.0.5 -b %dM -t 8 -p 5201 &' % bw)
    net.get('h3').cmd('iperf3 -c 10.0.0.7 -b %dM -t 8 -p 5201 &' % bw)
    time.sleep(10)

print('=== Fase 6: Congestionamento severo (30s) ===')
for port in [5201, 5202, 5203]:
    net.get('h1').cmd('iperf3 -c 10.0.0.5 -b 300M -t 30 -p %d &' % port)
    net.get('h2').cmd('iperf3 -c 10.0.0.6 -b 300M -t 30 -p %d &' % port)
time.sleep(32)

print('Tráfego completo!')
CLI(net)
net.stop()
EOF
```

---

## 📈 Estrutura Cronológica do Cenário de Teste

O script de simulação induz variações programadas para simular padrões reais de redes de centros de dados:

1. **Fase 1 (0s - 30s):** Carga inicial leve para estabelecer a baseline de tráfego estável.
2. **Fase 2 (30s - 60s):** Incremento linear controlado de carga em múltiplos destinos.
3. **Fase 3 (60s - 90s):** Saturação artificial para testar a sensibilidade da métrica de *Growth Rate*.
4. **Fase 4 (90s - 110s):** Período de silêncio para analisar o comportamento de esvaziamento de filas.
5. **Fase 5 (110s - 170s):** Rajadas (Bursts) randômicas simulando acessos intermitentes a servidores.
6. **Fase 6 (170s - 200s):** Sobrecarga paralela massiva para induzir perda de pacotes e saturação em 100%.

Ao término dos 300 segundos, o Coletor salvará automaticamente o dataset em `./dataset` e fechará as conexões com segurança.
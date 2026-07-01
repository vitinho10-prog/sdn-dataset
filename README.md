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
pip install pandas matplotlib ryu numpy
```
*(Certifique-se de ter o Mininet, o Open vSwitch e o tshark instalados no sistema: `sudo apt install mininet openvswitch-switch tshark`).*

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

## 📊 Análise de Dados e Visualização Científica

Após o encerramento do experimento de 300 segundos, você pode analisar e extrair os resultados utilizando as ferramentas integradas. **O sistema detecta e carrega automaticamente a coleta mais recente da pasta.**

### 1. Visualizar Resumo de Dados Completos no Terminal
Para inspecionar rapidamente os picos máximos por link e a variação cronológica do link principal (`dp2:p1`), execute o script abaixo no terminal:

```bash
cd ~/meus-projetos-p4/metricas
~/miniconda/envs/sdn/bin/python - << 'EOF'
import pandas as pd
import glob
import os

arquivos = glob.glob('dataset/run_*.csv')
if not arquivos:
    print('Erro: Nenhum arquivo de coleta encontrado em dataset/')
    exit()
ultimo_csv = max(arquivos, key=os.path.getctime)
print(f'Carregando a coleta mais recente: {ultimo_csv}')

df = pd.read_csv(ultimo_csv)

print("=== RESUMO ===")
print(f"Total de rows: {len(df)}")
print(f"Links únicos: {df['link_id'].nunique()}")
print(f"Duração: {round(df['timestamp'].max() - df['timestamp'].min())} segundos")

print("\n=== PRIMEIRAS 5 LINHAS ===")
print(df[['timestamp','link_id','utilization','throughput_mbps','growth_rate','tx_mbps','rx_mbps']].head())

print("\n=== MÁXIMOS POR LINK ===")
print(df.groupby('link_id')[['utilization','throughput_mbps']].max().round(2).sort_values('utilization', ascending=False))

print("\n=== VARIAÇÃO TEMPORAL DO LINK dp2:p1 ===")
dp2 = df[df['link_id']=='dp2:p1'][['timestamp','utilization','throughput_mbps','growth_rate']].copy()
dp2['tempo_s'] = (dp2['timestamp'] - dp2['timestamp'].min()).round(0).astype(int)
print(dp2[['tempo_s','utilization','throughput_mbps','growth_rate']].to_string(index=False))
EOF
```

### 2. Gerar Gráficos Científicos de Desempenho
Para plotar os gráficos de análise temporal detalhada e o comparativo multi-link da sua última execução, use o comando abaixo:

```bash
cd ~/meus-projetos-p4/metricas
~/miniconda/envs/sdn/bin/python - << 'EOF'
import pandas as pd
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
import glob
import os

arquivos = glob.glob('dataset/run_*.csv')
if not arquivos:
    print('Erro: Nenhum arquivo de coleta encontrado em dataset/')
    exit()
ultimo_csv = max(arquivos, key=os.path.getctime)
print(f'Gerando gráficos para a coleta: {ultimo_csv}')

df = pd.read_csv(ultimo_csv)

dp2 = df[df['link_id']=='dp2:p1'].copy()
dp2['tempo_s'] = (dp2['timestamp'] - dp2['timestamp'].min()).round(1)
dp2 = dp2[dp2['tempo_s'] >= 0].sort_values('tempo_s')

fig, axes = plt.subplots(3, 1, figsize=(14, 10))
fig.suptitle('SDN Dataset — Link dp2:p1 (ag1)\nVariação Temporal das Métricas', fontsize=14, fontweight='bold')

fases = [
    (0,   35,  '#e8f5e9', 'Fase 1\nBaixo'),
    (35,  70,  '#fff9c4', 'Fase 2\nMédio'),
    (70,  105, '#ffccbc', 'Fase 3\nCongestionamento'),
    (105, 125, '#f3e5f5', 'Fase 4\nIdle'),
    (125, 185, '#e3f2fd', 'Fase 5\nBurst variado'),
    (185, 210, '#ffcdd2', 'Fase 6\nCong. severo'),
]

ax1 = axes[0]
for inicio, fim, cor, label in fases: ax1.axvspan(inicio, fim, alpha=0.3, color=cor)
ax1.plot(dp2['tempo_s'], dp2['utilization'], color='#1565C0', linewidth=2)
ax1.axhline(y=80, color='orange', linestyle='--', alpha=0.7, label='Alerta 80%')
ax1.axhline(y=100, color='red', linestyle='--', alpha=0.7, label='Saturação 100%')
ax1.set_ylabel('Utilização (%)', fontsize=11)
ax1.set_ylim(-5, 110)
ax1.legend(loc='upper left', fontsize=9)
ax1.grid(True, alpha=0.3)
ax1.set_title('Utilização do Link', fontsize=11)

ax2 = axes[1]
for inicio, fim, cor, label in fases: ax2.axvspan(inicio, fim, alpha=0.3, color=cor)
ax2.plot(dp2['tempo_s'], dp2['throughput_mbps'], color='#2E7D32', linewidth=2)
ax2.set_ylabel('Throughput (Mbps)', fontsize=11)
ax2.grid(True, alpha=0.3)
ax2.set_title('Throughput', fontsize=11)

ax3 = axes[2]
for inicio, fim, cor, label in fases: ax3.axvspan(inicio, fim, alpha=0.3, color=cor)
ax3.plot(dp2['tempo_s'], dp2['growth_rate'], color='#6A1B9A', linewidth=2)
ax3.axhline(y=0, color='black', linestyle='-', alpha=0.3)
ax3.axhline(y=3, color='red', linestyle='--', alpha=0.7, label='Alerta congestionamento')
ax3.set_ylabel('Growth Rate (%/s)', fontsize=11)
ax3.set_xlabel('Tempo (segundos)', fontsize=11)
ax3.legend(loc='upper left', fontsize=9)
ax3.grid(True, alpha=0.3)
ax3.set_title('Taxa de Crescimento da Utilização', fontsize=11)

patches = [mpatches.Patch(color=cor, alpha=0.5, label=label.replace('\n', ' ')) for _, _, cor, label in fases]
fig.legend(handles=patches, loc='lower center', ncol=6, fontsize=9, bbox_to_anchor=(0.5, -0.02))
plt.tight_layout(rect=[0, 0.04, 1, 1])
plt.savefig('dataset/grafico_apresentacao.png', dpi=150, bbox_inches='tight')

fig2, ax = plt.subplots(figsize=(12, 6))
links_principais = ['dp2:p1', 'dp6:p2', 'dp5:p1', 'dp7:p2']
cores = ['#1565C0', '#2E7D32', '#E65100', '#6A1B9A']
for link, cor in zip(links_principais, cores):
    d = df[df['link_id']==link].copy()
    d['tempo_s'] = (d['timestamp'] - d['timestamp'].min()).round(1)
    d = d[d['tempo_s'] >= 0].sort_values('tempo_s')
    ax.plot(d['tempo_s'], d['utilization'], color=cor, linewidth=2, label=link)
for inicio, fim, cor, label in fases: ax.axvspan(inicio, fim, alpha=0.15, color=cor)
ax.set_xlabel('Tempo (segundos)', fontsize=11)
ax.set_ylabel('Utilização (%)', fontsize=11)
ax.set_title('SDN Dataset — Utilização por Link ao Longo do Tempo', fontsize=13, fontweight='bold')
ax.legend(fontsize=10)
ax.grid(True, alpha=0.3)
ax.set_ylim(-5, 110)
plt.tight_layout()
plt.savefig('dataset/grafico_multilink.png', dpi=150, bbox_inches='tight')
print("Gráficos gerados com sucesso na pasta dataset/")
EOF
```

### 3. Visualizar as Imagens Geradas no Windows (WSL Interop)
Como o ambiente WSL opera via terminal, você pode invocar o Visualizador de Fotos do Windows diretamente utilizando a interoperabilidade do sistema para abrir as imagens salvas:

```bash
explorer.exe $(wslpath -w ~/meus-projetos-p4/metricas/dataset/grafico_apresentacao.png)
explorer.exe $(wslpath -w ~/meus-projetos-p4/metricas/dataset/grafico_multilink.png)
```

---

## 📡 Telemetria em Tempo Real

Para visualizar as métricas da rede ao vivo enquanto o pipeline está rodando, abra um **Terminal 4**:

```bash
conda activate sdn
cd ~/meus-projetos-p4/metricas
~/miniconda/envs/sdn/bin/python telemetry.py
```

O dashboard atualiza a cada segundo e mostra:
- Utilização de cada link (%)
- Throughput TX e RX (Mbps)
- Status: ✅ NORMAL / ⚠ ALTO / 🔴 CONGESTIONADO
- Resumo geral: utilização média, máxima e throughput total

---

## 📦 Captura de Tráfego em Tempo Real — Janelas Temporais e Features para LSTM

O sistema captura o **tráfego real pacote a pacote** diretamente dos hosts Mininet, usando **tshark em modo live** (sem salvar arquivo `.pcap` intermediário). Os pacotes são processados em **janelas temporais fixas de 1 segundo**, gerando uma série temporal por fluxo já pronta para treinar modelos LSTM/GRU.

### Como funciona

O `pcap_collector.py` entra no namespace de rede de cada host (via `nsenter`) e roda `tshark` extraindo apenas os campos necessários (`frame.time_epoch`, `ip.src`, `ip.dst`, `frame.len`, `ip.proto`) em tempo real. Cada pacote é processado imediatamente, sem etapa intermediária de gravação em disco.

Para cada fluxo (par de IPs) e cada janela de 1 segundo, são calculadas as seguintes **features**:

| Feature | Descrição |
|---------|-----------|
| `mbps` | Vazão (throughput) na janela |
| `pps` | Pacotes por segundo |
| `avg_pkt` | Tamanho médio do pacote (bytes) |
| `std_pkt` | Desvio padrão do tamanho do pacote |
| `tcp_ratio` | Proporção de pacotes TCP na janela |
| `udp_ratio` | Proporção de pacotes UDP na janela |
| `flow_count` | Número de fluxos ativos na janela (global) |
| `active_hosts` | Número de hosts distintos ativos na janela (global) |

### 🖥️ Terminal 5 — Captura em Tempo Real

Com a rede já rodando (Terminal 1 + Terminal 2 ativos), abra um quinto terminal:

```bash
conda activate sdn
cd ~/meus-projetos-p4/metricas
sudo ~/miniconda/envs/sdn/bin/python pcap_collector.py --duration 60 --output-dir ./pcap
```

Parâmetros disponíveis:

```bash
--duration   # duração da captura em segundos (padrão: 60)
--output-dir # pasta de saída (padrão: ./pcap)
--window     # tamanho da janela temporal em segundos (padrão: 1.0)
--seq-len    # tamanho da janela deslizante para LSTM (padrão: 30)
```

### Exemplo de saída no terminal

```
======================================================================
  SDN Live Traffic Collector (tshark)
  Run ID  : 20260630_094520
  Janela  : 1.0s
  Seq Len : 30
  Duração : 60s
======================================================================
=== Iniciando captura em tempo real (tshark) ===
  ✓ h1 (PID=9850) [tshark live]
  ...
  8 capturas ativas
Capturando por 60s (processamento em tempo real)...
  10/60s | fluxos ativos: 0
  ...
  60/60s | fluxos ativos: 4

=== Exportando dataset (4 fluxos) ===
  ✓ Série completa salva: pcap/series_20260630_094520.json
  ✓ CSV long salvo: pcap/flows_long_20260630_094520.csv
  ✓ CSV wide (mbps) salvo: pcap/flows_wide_mbps_20260630_094520.csv
  ✓ CSV wide (pps) salvo: pcap/flows_wide_pps_20260630_094520.csv
  ✓ 10.0.0.3-10.0.0.7: X=(28, 30, 8) y=(28,)
  ✓ 10.0.0.1-10.0.0.5: X=(28, 30, 8) y=(28,)
  ✓ 10.0.0.2-10.0.0.6: X=(16, 30, 8) y=(16,)

======================================================================
  RESUMO DA COLETA
======================================================================
  Fluxos capturados   : 4
  Total de janelas    : 176
    10.0.0.1-10.0.0.5         janelas=  58  mbps_medio= 106.99  mbps_max= 401.43
    10.0.0.2-10.0.0.6         janelas=  46  mbps_medio= 114.03  mbps_max= 397.29
    10.0.0.3-10.0.0.7         janelas=  58  mbps_medio=  83.60  mbps_max= 397.84
    10.0.0.4-10.0.0.8         janelas=  14  mbps_medio= 215.09  mbps_max= 400.95
======================================================================
```

### Arquivos gerados (`./pcap/`)

```
pcap/
├── series_<run_id>.json              ← série temporal completa por fluxo (JSON)
├── flows_long_<run_id>.csv           ← formato long: uma linha por (timestamp, fluxo)
├── flows_wide_mbps_<run_id>.csv      ← formato wide: timestamp + 1 coluna por fluxo (mbps)
├── flows_wide_pps_<run_id>.csv       ← idem, mas com pacotes/segundo
└── lstm_windows/
    ├── <fluxo>_X.npy                 ← janelas deslizantes (seq_len, n_features)
    ├── <fluxo>_y.npy                 ← alvo: mbps do próximo segundo
    └── metadata_<run_id>.json        ← shapes e features de cada fluxo
```

### Formato dos CSVs

**`flows_long_<run_id>.csv`** — uma linha por janela e por fluxo, com todas as features:
```
timestamp,flow,mbps,pps,avg_pkt,std_pkt,tcp_ratio,udp_ratio,flow_count,active_hosts
0.0,10.0.0.3-10.0.0.7,21.0264,104.0,25272.2,31587.5,1.0,0.0,2,4
1.0,10.0.0.3-10.0.0.7,21.0243,100.0,26280.4,31800.1,1.0,0.0,2,4
```

**`flows_wide_mbps_<run_id>.csv`** — uma coluna por fluxo, pronto para entrada multivariada em LSTM:
```
timestamp,10.0.0.1-10.0.0.5,10.0.0.2-10.0.0.6,10.0.0.3-10.0.0.7,10.0.0.4-10.0.0.8
0.0,21.0233,0.0,21.0264,0.0
1.0,21.0233,0.0,21.0243,0.0
```

### Janelas deslizantes para LSTM (`lstm_windows/`)

Cada fluxo com dados suficientes (mais que `seq_len + 1` janelas) gera um par de arrays `.npy`:

```python
import numpy as np

X = np.load("pcap/lstm_windows/10_0_0_1_to_10_0_0_5_X.npy")  # shape: (N, 30, 8)
y = np.load("pcap/lstm_windows/10_0_0_1_to_10_0_0_5_y.npy")  # shape: (N,)

# X[i] = 30 segundos anteriores (8 features cada)
# y[i] = mbps do segundo seguinte (o que o modelo deve prever)
```

---

## 🗂️ Pipeline Completo — Resumo de Todos os Terminais

| Terminal | Função |
|----------|--------|
| **Terminal 1** | Ryu (controlador SDN) |
| **Terminal 3** | Collector (gravação do dataset CSV/JSONL via OpenFlow) |
| **Terminal 2** | Mininet + geração de tráfego (iperf3) |
| **Terminal 4** | Telemetria ao vivo (dashboard em tempo real) — opcional |
| **Terminal 5** | Captura em tempo real (janelas + features + LSTM windows) — opcional |

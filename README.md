# SDN Dataset Generation System for LSTM/GRU 🧠🌐

Este projeto implementa um sistema de monitoramento e coleta de métricas em tempo real para redes definidas por software (SDN). Ele gera datasets estruturados em séries temporais (e janelas deslizantes) projetados especificamente para o treinamento de modelos de Deep Learning, como **LSTM** e **GRU**, focados na predição proativa de congestionamento.

## 🚀 Como Funciona
O sistema coleta estatísticas de portas e links (via OpenFlow/IPC) e calcula métricas dinâmicas, com destaque para o **Growth Rate** (taxa de aceleração da utilização do link), permitindo identificar anomalias antes que o link atinja a saturação total.

O tráfego é gerado por um **Gerador de Cenários** (`scenarios.py` + `run_experiments.py`) que sorteia, a cada experimento, um cenário de um catálogo com 12 padrões diferentes — desde tráfego ocioso até congestionamento severo e falhas de link — garantindo que o dataset final seja rico e variado o suficiente para treinar modelos que generalizem bem.

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

### 📊 Terminal 3 — Coletor de Métricas (Collector)
Inicia a captura ativa dos dados por um período determinado. **Inicie este terminal logo após o Ryu estar pronto, e antes do Terminal 2**, para não perder o início dos experimentos:
```bash
conda activate sdn
cd ~/meus-projetos-p4/metricas
~/miniconda/envs/sdn/bin/python collector.py --duration 900 --output-dir ./dataset
```
*Aguarde aparecer a mensagem Collection started. Duration: 900s.*

> **Dica de dimensionamento:** o `--duration` do collector deve ser **maior ou igual** à soma esperada da duração de todos os experimentos que você vai rodar no Terminal 2. Para ~20-30 experimentos, 900 a 1200 segundos costuma ser suficiente.

### 🏎️ Terminal 2 — Gerador de Cenários (Experimentos Aleatórios)
Limpa instâncias antigas do Open vSwitch, constrói a topologia Fat-tree e executa uma sequência de **N experimentos**, cada um sorteando um cenário aleatório do catálogo (substitui o antigo roteiro fixo de "Fase 1 a 6"):
```bash
conda activate sdn
cd ~/meus-projetos-p4/metricas

# Limpeza de segurança de interfaces antigas
sudo ip link show | grep -oP '\d+: \K(ag|lf|sp|h)[0-9]+-eth[0-9]+(?=@)' | xargs -I{} sudo ip link delete {} 2>/dev/null
sudo ovs-vsctl list-br 2>/dev/null | xargs -I{} sudo ovs-vsctl del-br {} 2>/dev/null

# Executa 20 experimentos aleatórios, reprodutíveis via seed
sudo ~/miniconda/envs/sdn/bin/python run_experiments.py --n-experiments 20 --seed 42
```

Parâmetros disponíveis:
```bash
--n-experiments   # número de experimentos a executar (padrão: 20)
--seed            # seed para reprodutibilidade (padrão: aleatório)
--cooldown        # pausa entre experimentos em segundos (padrão: 3.0)
--cli             # abre o Mininet CLI ao final da execução
--manifest        # caminho do CSV de manifesto (padrão: experiments_manifest.csv)
```

---

## 🎲 Gerador de Cenários — Catálogo e Funcionamento

O `scenarios.py` define um catálogo de **12 cenários** de tráfego, cada um com parâmetros próprios de banda, duração, número de fluxos, mix de protocolos e probabilidade de eventos de rede:

| Cenário | Descrição |
|---------|-----------|
| `LOW_LOAD` | Tráfego leve e constante |
| `MEDIUM_LOAD` | Tráfego moderado, múltiplos fluxos |
| `HIGH_LOAD` | Tráfego pesado, muitos fluxos simultâneos |
| `BURST` | Picos curtos e intensos de banda |
| `IDLE` | Rede quase sem tráfego |
| `MIX_TCP` | Tráfego majoritariamente TCP |
| `MIX_UDP` | Tráfego majoritariamente UDP |
| `ELEPHANT_FLOW` | Poucos fluxos, mas de longa duração e alta banda |
| `MANY_MICE` | Muitos fluxos pequenos e curtos |
| `CONGESTION` | Múltiplos fluxos forçados pelo mesmo caminho, saturando o link |
| `FAILURE` | Inclui eventos de queda de link, delay e packet loss |
| `BACKGROUND_TRAFFIC` | Apenas tráfego de fundo, sem fluxos principais |

### O que muda a cada experimento

Diferente do roteiro fixo anterior (sempre h1→h5, h3→h7, bandas e durações fixas), cada experimento agora sorteia:

- **Pares de hosts aleatórios** (`random.sample`) — nunca repete sempre os mesmos caminhos
- **Largura de banda aleatória**, dentro da faixa do cenário sorteado
- **Duração aleatória** de cada fluxo
- **Número de fluxos aleatório**
- **Mix de protocolos**: 60% TCP / 25% UDP / 10% ICMP / 5% misto (configurável por cenário)
- **Tráfego de fundo contínuo** (2 a 4 fluxos pequenos rodando em paralelo aos fluxos principais)
- **Eventos de rede**, com probabilidade configurável por cenário:
  - Queda e recuperação de um link interno (`net.configLinkStatus`)
  - Host saindo e voltando à rede
  - Variação de delay e packet loss via `tc netem`

Tudo isso é controlado por uma **seed**, então o mesmo `--seed` sempre reproduz exatamente a mesma sequência de experimentos.

### Manifesto de experimentos

Cada execução gera um `experiments_manifest.csv`, que registra qual cenário estava ativo em cada janela de tempo:

```
experiment_id,scenario,start_ts,duration_s,seed
1,HIGH_LOAD,1782951000.123,70.8,7709613
2,ELEPHANT_FLOW,1782951071.456,101.4,2931776
3,BACKGROUND_TRAFFIC,1782951180.789,84.0,6594010
```

Isso permite cruzar, por timestamp, qual cenário gerou cada linha do dataset coletado pelo `collector.py` — útil para analisar como cada tipo de tráfego afeta a utilização e o throughput da rede.

---

## 📊 Análise de Dados e Visualização Científica

Após o encerramento da coleta, você pode analisar e extrair os resultados utilizando as ferramentas integradas. **O sistema detecta e carrega automaticamente a coleta mais recente da pasta.**

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

### 2. Correlacionar Dataset com Cenários (Manifesto)
Para ver a utilização e throughput médio/máximo por tipo de cenário, cruzando o dataset com o `experiments_manifest.csv`:

```bash
cd ~/meus-projetos-p4/metricas
~/miniconda/envs/sdn/bin/python - << 'EOF'
import pandas as pd
import glob
import os

ultimo_csv = max(glob.glob('dataset/run_*.csv'), key=os.path.getctime)
df = pd.read_csv(ultimo_csv)
manifest = pd.read_csv('experiments_manifest.csv')

def find_scenario(ts):
    row = manifest[(manifest['start_ts'] <= ts) &
                   (manifest['start_ts'] + manifest['duration_s'] >= ts)]
    return row['scenario'].values[0] if len(row) > 0 else 'unknown'

df['scenario'] = df['timestamp'].apply(find_scenario)

print(f"Dataset: {ultimo_csv}")
print(f"Rows: {len(df)} | Links: {df['link_id'].nunique()}")
print("\n=== UTILIZAÇÃO E THROUGHPUT POR CENÁRIO ===")
print(df.groupby('scenario')[['utilization','throughput_mbps']].agg(['mean','max','count']).round(2))
EOF
```

### 3. Gerar Gráficos Científicos de Desempenho
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

ax1 = axes[0]
ax1.plot(dp2['tempo_s'], dp2['utilization'], color='#1565C0', linewidth=2)
ax1.axhline(y=80, color='orange', linestyle='--', alpha=0.7, label='Alerta 80%')
ax1.axhline(y=100, color='red', linestyle='--', alpha=0.7, label='Saturação 100%')
ax1.set_ylabel('Utilização (%)', fontsize=11)
ax1.set_ylim(-5, 110)
ax1.legend(loc='upper left', fontsize=9)
ax1.grid(True, alpha=0.3)
ax1.set_title('Utilização do Link', fontsize=11)

ax2 = axes[1]
ax2.plot(dp2['tempo_s'], dp2['throughput_mbps'], color='#2E7D32', linewidth=2)
ax2.set_ylabel('Throughput (Mbps)', fontsize=11)
ax2.grid(True, alpha=0.3)
ax2.set_title('Throughput', fontsize=11)

ax3 = axes[2]
ax3.plot(dp2['tempo_s'], dp2['growth_rate'], color='#6A1B9A', linewidth=2)
ax3.axhline(y=0, color='black', linestyle='-', alpha=0.3)
ax3.axhline(y=3, color='red', linestyle='--', alpha=0.7, label='Alerta congestionamento')
ax3.set_ylabel('Growth Rate (%/s)', fontsize=11)
ax3.set_xlabel('Tempo (segundos)', fontsize=11)
ax3.legend(loc='upper left', fontsize=9)
ax3.grid(True, alpha=0.3)
ax3.set_title('Taxa de Crescimento da Utilização', fontsize=11)

plt.tight_layout()
plt.savefig('dataset/grafico_apresentacao.png', dpi=150, bbox_inches='tight')

fig2, ax = plt.subplots(figsize=(12, 6))
links_principais = ['dp2:p1', 'dp6:p2', 'dp5:p1', 'dp7:p2']
cores = ['#1565C0', '#2E7D32', '#E65100', '#6A1B9A']
for link, cor in zip(links_principais, cores):
    d = df[df['link_id']==link].copy()
    d['tempo_s'] = (d['timestamp'] - d['timestamp'].min()).round(1)
    d = d[d['tempo_s'] >= 0].sort_values('tempo_s')
    ax.plot(d['tempo_s'], d['utilization'], color=cor, linewidth=2, label=link)
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

### 4. Visualizar as Imagens Geradas no Windows (WSL Interop)
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

## 📁 Estrutura dos Arquivos Principais

| Arquivo | Função |
|---------|--------|
| `controller.py` | Controlador Ryu (OpenFlow 1.3): aprende MACs, encaminha pacotes e coleta PortStats a cada 1s |
| `topo.py` | Define a topologia Fat-tree base (spine, aggregation, leaf, hosts) |
| `collector.py` | Recebe métricas do controlador via IPC, calcula utilização/growth rate e grava o dataset (CSV/JSONL + janelas LSTM) |
| `scenarios.py` | Catálogo de 12 cenários de tráfego + executor de cenários com eventos de rede |
| `run_experiments.py` | Orquestra N experimentos aleatórios sobre a topologia, gerando o manifesto de execução |
| `telemetry.py` | Dashboard de telemetria em tempo real no terminal |
| `pcap_collector.py` | Captura de tráfego em tempo real via tshark, com janelas temporais e features para LSTM |

---

## 🗂️ Pipeline Completo — Resumo de Todos os Terminais

| Terminal | Função |
|----------|--------|
| **Terminal 1** | Ryu (controlador SDN) |
| **Terminal 3** | Collector (gravação do dataset CSV/JSONL via OpenFlow) — inicie antes do Terminal 2 |
| **Terminal 2** | Gerador de Cenários — Mininet + N experimentos aleatórios (`run_experiments.py`) |
| **Terminal 4** | Telemetria ao vivo (dashboard em tempo real) — opcional |
| **Terminal 5** | Captura em tempo real (janelas + features + LSTM windows) — opcional |
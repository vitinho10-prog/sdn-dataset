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

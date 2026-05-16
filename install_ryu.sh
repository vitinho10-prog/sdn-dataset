#!/bin/bash
set -euo pipefail

GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; NC='\033[0m'
info()  { echo -e "${GREEN}[INFO]${NC}  $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC}  $*"; }

PYTHON_VERSION="3.10.14"
VENV_PATH="$HOME/sdn-env"

info "Instalando dependências de build..."
sudo apt-get update -qq
sudo apt-get install -y build-essential libssl-dev zlib1g-dev libbz2-dev \
    libreadline-dev libsqlite3-dev curl git libncursesw5-dev xz-utils \
    tk-dev libxml2-dev libxmlsec1-dev libffi-dev liblzma-dev \
    mininet openvswitch-switch iperf3 iputils-ping

if [ ! -d "$HOME/.pyenv" ]; then
    info "Instalando pyenv..."
    curl https://pyenv.run | bash
fi

export PYENV_ROOT="$HOME/.pyenv"
export PATH="$PYENV_ROOT/bin:$PATH"
eval "$(pyenv init -)"

if ! grep -q "PYENV_ROOT" "$HOME/.bashrc" 2>/dev/null; then
    echo 'export PYENV_ROOT="$HOME/.pyenv"' >> "$HOME/.bashrc"
    echo 'export PATH="$PYENV_ROOT/bin:$PATH"' >> "$HOME/.bashrc"
    echo 'eval "$(pyenv init -)"' >> "$HOME/.bashrc"
fi

info "Instalando Python $PYTHON_VERSION (pode demorar ~5 min)..."
pyenv install -s "$PYTHON_VERSION"

info "Criando virtualenv em $VENV_PATH..."
"$HOME/.pyenv/versions/$PYTHON_VERSION/bin/python" -m venv "$VENV_PATH"
source "$VENV_PATH/bin/activate"

info "Instalando Ryu e dependências..."
pip install --upgrade pip "setuptools<65" wheel -q
pip install "eventlet==0.30.2" oslo.config ryu webob routes numpy -q

pip install torch --index-url https://download.pytorch.org/whl/cpu -q || \
    warn "PyTorch não instalado (opcional, só para train_model.py)"

echo ""
info "=== Verificação ==="
ryu-manager --version && info "✓ ryu-manager OK"
mn --version 2>/dev/null && info "✓ mininet OK" || warn "✗ mininet não encontrado"

echo ""
echo -e "${GREEN}Instalação concluída!${NC}"
echo ""
echo "  Ativar ambiente:  source ~/sdn-env/bin/activate"
echo "  Rodar pipeline:   sudo bash -c 'source ~/sdn-env/bin/activate && python3 pipeline.py --scenario mixed --duration 300'"

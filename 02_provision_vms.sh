#!/bin/bash
# Installs Python, build tools, libbpf, bpftool, and kernel headers on the VMs.

set -euo pipefail

GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
log()  { echo -e "${GREEN}[+]${NC} $1"; }
warn() { echo -e "${YELLOW}[!]${NC} $1"; }

SETUP_SCRIPT=$(cat <<'INNER'
#!/bin/bash
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive

echo "[VM] Updating apt..."
sudo apt-get update -q

echo "[VM] Installing Raft/XDP toolchain..."
sudo apt-get install -y \
    clang \
    llvm \
    libelf-dev \
    libbpf-dev \
    linux-headers-$(uname -r) \
    linux-tools-$(uname -r) \
    linux-tools-common \
    iproute2 \
    tcpdump \
    python3 \
    python3-pip \
    make \
    gcc \
    netcat-openbsd \
    iperf3

echo "[VM] Checking libbpf..."
dpkg -l libbpf-dev | grep -q "^ii" && echo "[VM] libbpf-dev: OK"

echo "[VM] Verifying clang BPF target..."
echo 'int x;' | clang -target bpf -c -x c - -o /dev/null && echo "[VM] clang BPF: OK"

IFACE=$(ip -o link show | awk -F': ' '{print $2}' | grep -v lo | head -1)
echo "[VM] Primary interface: $IFACE"
echo "[VM] Setup complete on $(hostname), kernel $(uname -r)"
INNER
)

NODES=("node1" "node2" "node3")
for name in "${NODES[@]}"; do
    log "Provisioning $name ..."
    multipass exec "$name" -- bash -c "$SETUP_SCRIPT"
    log "$name provisioned."
    echo "---"
done

log "All VMs provisioned. Next: ./03_deploy_raft.sh"

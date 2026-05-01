#!/bin/bash
# Creates the three Ubuntu VMs used by the Raft/XDP lab.

set -euo pipefail

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
log()  { echo -e "${GREEN}[+]${NC} $1"; }
warn() { echo -e "${YELLOW}[!]${NC} $1"; }
die()  { echo -e "${RED}[x]${NC} $1"; exit 1; }

if ! command -v multipass >/dev/null 2>&1; then
    if command -v snap >/dev/null 2>&1; then
        log "Installing multipass with snap..."
        sudo snap install multipass
    else
        die "multipass is not installed. Install it first, then rerun this script."
    fi
else
    log "multipass already installed: $(multipass version | head -1)"
fi

NODES=("node1" "node2" "node3")
for name in "${NODES[@]}"; do
    if multipass info "$name" >/dev/null 2>&1; then
        warn "VM '$name' already exists, skipping creation."
    else
        log "Creating VM: $name ..."
        multipass launch --name "$name" --cpus 2 --memory 1G --disk 10G 22.04
        log "$name created."
    fi
done

log "All VMs are present. Current IP addresses:"
multipass list

echo ""
warn "Next step: ./02_provision_vms.sh"

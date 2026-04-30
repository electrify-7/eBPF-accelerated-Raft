#!/bin/bash
# Runs baseline Python Raft, then XDP fast-path Raft, across four VMs.

set -euo pipefail

GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; NC='\033[0m'
log()     { echo -e "${GREEN}[+]${NC} $1"; }
warn()    { echo -e "${YELLOW}[!]${NC} $1"; }
section() { echo -e "\n${CYAN}== $1 ==${NC}"; }

N_REQUESTS=${N_REQUESTS:-2000}
PAYLOAD_BYTES=${PAYLOAD_BYTES:-64}
HEARTBEAT_INTERVAL=${HEARTBEAT_INTERVAL:-0.1}
RESULTS_DIR="./results_$(date +%Y%m%d_%H%M%S)"
mkdir -p "$RESULTS_DIR"

ip_of() {
    multipass info "$1" | awk '/IPv4/ {print $2; exit}'
}

get_iface() {
    local vm=$1
    multipass exec "$vm" -- bash -c "ip -o link show | grep -v lo | awk -F': ' '{print \$2}' | head -1"
}

stop_all() {
    for vm in node1 node2 node3 node4; do
        multipass exec "$vm" -- bash -c "pkill -f 'python3.*raft_node.py' 2>/dev/null; true"
    done
    sleep 1
}

unload_xdp() {
    multipass exec node2 -- bash -c "sudo ip link set dev $IFACE2 xdp off 2>/dev/null; true"
    multipass exec node3 -- bash -c "sudo ip link set dev $IFACE3 xdp off 2>/dev/null; true"
    multipass exec node4 -- bash -c "sudo ip link set dev $IFACE4 xdp off 2>/dev/null; true"
}

cleanup() {
    set +e
    stop_all
    if [ -n "${IFACE2:-}" ] && [ -n "${IFACE3:-}" ] && [ -n "${IFACE4:-}" ]; then
        unload_xdp
    fi
}

start_followers() {
    local suffix=$1
    for vm in node2 node3 node4; do
        log "Starting Raft follower on $vm..."
        multipass exec "$vm" -- bash -c "cd ~/electrode-lab && nohup python3 protocol/raft_node.py --role follower > /tmp/raft_follower_${suffix}.log 2>&1 &"
    done
    sleep 1
}

start_leader() {
    local suffix=$1
    log "Starting Raft leader on node1..."
    multipass exec node1 -- bash -c "cd ~/electrode-lab && nohup python3 protocol/raft_node.py --role leader --heartbeat-interval $HEARTBEAT_INTERVAL $FOLLOWER1_IP $FOLLOWER2_IP $FOLLOWER3_IP > /tmp/raft_leader_${suffix}.log 2>&1 &"
    sleep 2
}

run_client() {
    local name=$1
    log "Running $name benchmark: $N_REQUESTS requests, $PAYLOAD_BYTES bytes/request"
    python3 protocol/raft_client.py "$LEADER_IP" \
        --count "$N_REQUESTS" \
        --payload-bytes "$PAYLOAD_BYTES" \
        --out "$RESULTS_DIR/${name}.csv" | tee "$RESULTS_DIR/${name}_summary.txt"
}

LEADER_IP=$(ip_of node1)
FOLLOWER1_IP=$(ip_of node2)
FOLLOWER2_IP=$(ip_of node3)
FOLLOWER3_IP=$(ip_of node4)
IFACE2=$(get_iface node2)
IFACE3=$(get_iface node3)
IFACE4=$(get_iface node4)

log "IPs: leader=$LEADER_IP followers=$FOLLOWER1_IP,$FOLLOWER2_IP,$FOLLOWER3_IP"
log "Interfaces: node2=$IFACE2 node3=$IFACE3 node4=$IFACE4"
log "Results directory: $RESULTS_DIR"

trap cleanup EXIT

section "Phase 1: Baseline Python Raft"
stop_all
unload_xdp
start_followers baseline
start_leader baseline
run_client baseline
stop_all

section "Phase 2: XDP Fast-Path Raft"
log "Compiling XDP program on followers..."
multipass exec node2 -- bash -c "cd ~/electrode-lab/xdp && make clean && make"
multipass exec node3 -- bash -c "cd ~/electrode-lab/xdp && make clean && make"
multipass exec node4 -- bash -c "cd ~/electrode-lab/xdp && make clean && make"

log "Loading XDP programs..."
multipass exec node2 -- bash -c "cd ~/electrode-lab/xdp && sudo make load IFACE=$IFACE2"
multipass exec node3 -- bash -c "cd ~/electrode-lab/xdp && sudo make load IFACE=$IFACE3"
multipass exec node4 -- bash -c "cd ~/electrode-lab/xdp && sudo make load IFACE=$IFACE4"

start_followers xdp
start_leader xdp
run_client xdp

section "XDP Map Stats"
for vm in node2 node3 node4; do
    log "$vm raft_stats:"
    multipass exec "$vm" -- bash -c "sudo bpftool map dump name raft_stats 2>/dev/null || true" | tee "$RESULTS_DIR/${vm}_xdp_stats.txt"
    log "$vm last leader heartbeat:"
    multipass exec "$vm" -- bash -c "sudo bpftool map dump name raft_last_seen 2>/dev/null || true" | tee "$RESULTS_DIR/${vm}_last_seen.txt"
done

section "Userspace Follower Logs With XDP"
for vm in node2 node3 node4; do
    log "$vm follower log:"
    multipass exec "$vm" -- bash -c "cat /tmp/raft_follower_xdp.log 2>/dev/null || true" | tee "$RESULTS_DIR/${vm}_follower_xdp.log"
done

stop_all
unload_xdp

section "Comparison"
python3 benchmark/analyze_raft.py \
    "$RESULTS_DIR/baseline.csv" \
    "$RESULTS_DIR/xdp.csv" \
    --out "$RESULTS_DIR/comparison.csv"

log "All results in: $RESULTS_DIR"
ls -lh "$RESULTS_DIR"

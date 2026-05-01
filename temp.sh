#!/bin/bash

# 1. Kill all detached Raft Python processes on the VMs
for node in node1 node2 node3; do
    echo "Stopping processes on $node..."
    multipass exec "$node" -- pkill -f 'python3.*raft_node.py' || true
done

# 2. Unload the eBPF/XDP programs from the interfaces
for node in node1 node2 node3; do
    echo "Unloading eBPF from $node..."
    multipass exec "$node" -- bash -c '
        # Dynamically grab the primary interface used by the VM
        IFACE=$(ip -o link show | grep -v lo | awk -F": " "{print \$2}" | head -1)
        cd ~/electrode-lab/xdp
        
        # Execute the unload target to detach the XDP program
        sudo make unload IFACE=$IFACE 2>/dev/null || true
    '
done

echo "Cluster state reset complete. Ready for debugging."
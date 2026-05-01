CLANG ?= clang
IFACE ?=
FOLLOWER_OBJ := raft_xdp_fastpath.o
QUORUM_OBJ := raft_leader_quorum.o
BROADCAST_OBJ := raft_leader_broadcast_tc.o

BPF_CFLAGS := -O2 -g -Wall -target bpf -D__TARGET_ARCH_x86

.PHONY: all follower leader clean load load-follower load-quorum load-broadcast configure-broadcast unload reload stats show

all: follower leader

follower: $(FOLLOWER_OBJ)

leader: $(QUORUM_OBJ) $(BROADCAST_OBJ)

$(FOLLOWER_OBJ): raft_xdp_fastpath.c
	$(CLANG) $(BPF_CFLAGS) -c $< -o $@

$(QUORUM_OBJ): raft_leader_quorum.c
	$(CLANG) $(BPF_CFLAGS) -c $< -o $@

$(BROADCAST_OBJ): raft_leader_broadcast_tc.c
	$(CLANG) $(BPF_CFLAGS) -c $< -o $@

load: load-follower

load-follower: $(FOLLOWER_OBJ)
	@test -n "$(IFACE)" || (echo "Usage: make load IFACE=<interface>"; exit 1)
	sudo ip link set dev $(IFACE) xdp off 2>/dev/null || true
	sudo ip link set dev $(IFACE) xdp obj $(FOLLOWER_OBJ) sec xdp
	ip link show $(IFACE)

load-quorum: $(QUORUM_OBJ)
	@test -n "$(IFACE)" || (echo "Usage: make load-quorum IFACE=<interface>"; exit 1)
	sudo ip link set dev $(IFACE) xdp off 2>/dev/null || true
	sudo ip link set dev $(IFACE) xdp obj $(QUORUM_OBJ) sec xdp
	ip link show $(IFACE)

load-broadcast: $(BROADCAST_OBJ)
	@test -n "$(IFACE)" || (echo "Usage: make load-broadcast IFACE=<interface>"; exit 1)
	sudo tc qdisc del dev $(IFACE) clsact 2>/dev/null || true
	sudo tc qdisc add dev $(IFACE) clsact
	sudo tc filter add dev $(IFACE) egress protocol ip bpf da obj $(BROADCAST_OBJ) sec tc

configure-broadcast:
	@test -n "$(IFACE)" || (echo "Usage: make configure-broadcast IFACE=<interface> FOLLOWER1=<ip> FOLLOWER2=<ip>"; exit 1)
	@test -n "$(FOLLOWER1)" || (echo "Usage: make configure-broadcast IFACE=<interface> FOLLOWER1=<ip> FOLLOWER2=<ip>"; exit 1)
	@test -n "$(FOLLOWER2)" || (echo "Usage: make configure-broadcast IFACE=<interface> FOLLOWER1=<ip> FOLLOWER2=<ip>"; exit 1)
	sudo python3 configure_broadcast.py $(IFACE) $(FOLLOWER1) $(FOLLOWER2)

unload:
	@test -n "$(IFACE)" || (echo "Usage: make unload IFACE=<interface>"; exit 1)
	sudo ip link set dev $(IFACE) xdp off 2>/dev/null || true
	sudo tc qdisc del dev $(IFACE) clsact 2>/dev/null || true

reload: unload load

stats:
	@if [ -n "$(IFACE)" ]; then sudo tc filter show dev $(IFACE) egress 2>/dev/null || true; fi
	sudo bpftool map dump name raft_stats 2>/dev/null || true
	sudo bpftool map dump name raft_fast_log 2>/dev/null | head -40 || true
	sudo bpftool map dump name raft_last_seen 2>/dev/null || true
	sudo bpftool map dump name raft_qstats 2>/dev/null || true
	sudo bpftool map dump name raft_qslots 2>/dev/null | head -40 || true
	sudo bpftool map dump name raft_fanout 2>/dev/null || true
	sudo bpftool map dump name raft_tc_stats 2>/dev/null || true
	sudo bpftool map show | grep -E 'raft_|ringbuf' || true

show:
	sudo bpftool prog show | grep -A2 'raft_' || true

clean:
	rm -f $(FOLLOWER_OBJ) $(QUORUM_OBJ) $(BROADCAST_OBJ)

CLANG ?= clang
IFACE ?=
BPF_OBJ := raft_xdp_fastpath.o

BPF_CFLAGS := -O2 -g -Wall -target bpf -D__TARGET_ARCH_x86

.PHONY: all clean load unload reload stats show

all: $(BPF_OBJ)

$(BPF_OBJ): raft_xdp_fastpath.c
	$(CLANG) $(BPF_CFLAGS) -c $< -o $@

load: $(BPF_OBJ)
	@test -n "$(IFACE)" || (echo "Usage: make load IFACE=<interface>"; exit 1)
	sudo ip link set dev $(IFACE) xdp off 2>/dev/null || true
	sudo ip link set dev $(IFACE) xdp obj $(BPF_OBJ) sec xdp
	ip link show $(IFACE)

unload:
	@test -n "$(IFACE)" || (echo "Usage: make unload IFACE=<interface>"; exit 1)
	sudo ip link set dev $(IFACE) xdp off 2>/dev/null || true

reload: unload load

stats:
	sudo bpftool map dump name raft_stats 2>/dev/null || true
	sudo bpftool map dump name raft_fast_log 2>/dev/null | head -40 || true
	sudo bpftool map dump name raft_last_seen 2>/dev/null || true
	sudo bpftool map show | grep -E 'raft_|ringbuf' || true

show:
	sudo bpftool prog show | grep -A2 raft_fastpath || true

clean:
	rm -f $(BPF_OBJ)

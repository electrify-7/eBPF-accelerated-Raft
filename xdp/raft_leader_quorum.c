// Leader-side XDP quorum filter for Raft AppendResponse packets.
//
// Runs on node1. It tracks which follower node_ids have ACKed each log index.
// ACK packets are dropped until the remote majority is reached. The ACK that
// reaches quorum is marked with FLAG_QUORUM_REACHED and passed to userspace,
// which lets the leader wake only once the quorum condition is true.

#include <linux/bpf.h>
#include <linux/if_ether.h>
#include <linux/ip.h>
#include <linux/udp.h>
#include <linux/in.h>
#include <bpf/bpf_helpers.h>

#define RAFT_PORT 9000
#define MSG_APPEND_RESPONSE 0x21
#define FLAG_SUCCESS 0x0001
#define FLAG_QUORUM_REACHED 0x0002
#define QUORUM_REMOTE_ACKS 2
#define QUORUM_SLOTS 4096

struct quorum_slot {
    __u32 seq;
    __u32 bitset;
    __u32 count;
};

struct {
    __uint(type, BPF_MAP_TYPE_ARRAY);
    __uint(max_entries, QUORUM_SLOTS);
    __type(key, __u32);
    __type(value, struct quorum_slot);
} raft_qslots SEC(".maps");

// raft_qstats[0] = ACKs dropped before quorum
// raft_qstats[1] = ACKs passed with quorum mark
// raft_qstats[2] = non-Raft/non-response packets passed
struct {
    __uint(type, BPF_MAP_TYPE_ARRAY);
    __uint(max_entries, 3);
    __type(key, __u32);
    __type(value, __u64);
} raft_qstats SEC(".maps");

static __always_inline void bump_stat(__u32 key)
{
    __u64 *value = bpf_map_lookup_elem(&raft_qstats, &key);
    if (value)
        __sync_fetch_and_add(value, 1);
}

static __always_inline __u32 read_be32(__u8 *p)
{
    return ((__u32)p[0] << 24) | ((__u32)p[1] << 16) |
           ((__u32)p[2] << 8) | (__u32)p[3];
}

static __always_inline __u16 read_be16(__u8 *p)
{
    return ((__u16)p[0] << 8) | (__u16)p[1];
}

static __always_inline void write_be16(__u8 *p, __u16 value)
{
    p[0] = value >> 8;
    p[1] = value;
}

SEC("xdp")
int raft_quorum(struct xdp_md *ctx)
{
    void *data = (void *)(long)ctx->data;
    void *data_end = (void *)(long)ctx->data_end;

    struct ethhdr *eth = data;
    if ((void *)(eth + 1) > data_end)
        return XDP_PASS;
    if (eth->h_proto != __builtin_bswap16(ETH_P_IP))
        return XDP_PASS;

    struct iphdr *ip = (void *)(eth + 1);
    if ((void *)(ip + 1) > data_end)
        return XDP_PASS;
    if (ip->protocol != IPPROTO_UDP || ip->ihl != 5)
        return XDP_PASS;

    struct udphdr *udp = (void *)(ip + 1);
    if ((void *)(udp + 1) > data_end)
        return XDP_PASS;
    if (udp->dest != __builtin_bswap16(RAFT_PORT))
        return XDP_PASS;

    __u8 *payload = (void *)(udp + 1);
    if ((void *)(payload + 35) > data_end)
        return XDP_PASS;

    if (payload[0] != MSG_APPEND_RESPONSE) {
        bump_stat(2);
        return XDP_PASS;
    }

    __u16 flags = read_be16(payload + 23);
    if (!(flags & FLAG_SUCCESS)) {
        bump_stat(2);
        return XDP_PASS;
    }

    __u32 seq = read_be32(payload + 5);
    __u16 node_id = read_be16(payload + 21);
    if (seq == 0 || node_id == 0 || node_id > 31) {
        bump_stat(2);
        return XDP_PASS;
    }

    __u32 idx = seq & (QUORUM_SLOTS - 1);
    struct quorum_slot *slot = bpf_map_lookup_elem(&raft_qslots, &idx);
    if (!slot) {
        bump_stat(2);
        return XDP_PASS;
    }

    if (slot->seq != seq) {
        slot->seq = seq;
        slot->bitset = 0;
        slot->count = 0;
    }

    __u32 bit = 1U << node_id;
    if (!(slot->bitset & bit)) {
        slot->bitset |= bit;
        slot->count += 1;
    }

    if (slot->count >= QUORUM_REMOTE_ACKS) {
        write_be16(payload + 23, flags | FLAG_QUORUM_REACHED);
        udp->check = 0;
        bump_stat(1);
        return XDP_PASS;
    }

    bump_stat(0);
    return XDP_DROP;
}

char LICENSE[] SEC("license") = "GPL";

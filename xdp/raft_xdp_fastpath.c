// Follower-side XDP fast path for Raft AppendEntries.
//
// Runs on follower nodes. It keeps volatile metadata for the last log entry in
// BPF maps. If an AppendEntries packet's prevLogIndex/prevLogTerm matches that
// metadata, the program records the append in raft_fast_log / raft_events and
// immediately rewrites the packet into a successful AppendResponse via XDP_TX.
//
// If the prevLog fields do not match, the packet is passed to userspace. The
// Python follower then drains any XDP metadata it can see and returns the
// optimized Raft conflict hint: conflictTerm/conflictIndex.

#include <linux/bpf.h>
#include <linux/if_ether.h>
#include <linux/ip.h>
#include <linux/udp.h>
#include <linux/in.h>
#include <bpf/bpf_helpers.h>
#include <bpf/bpf_endian.h>

#define RAFT_PORT 9000
#define MSG_APPEND_ENTRIES 0x20
#define MSG_APPEND_RESPONSE 0x21

#define FLAG_SUCCESS 0x0001
#define FLAG_EBPF_FAST 0x0004

struct raft_state_value {
    __u32 current_term;
    __u32 last_index;
    __u32 last_term;
};

struct fast_log_entry {
    __u32 term;
    __u16 payload_len;
    __u16 _pad;
    __u64 timestamp_ns;
};

struct raft_event {
    __u32 term;
    __u32 index;
    __u32 prev_index;
    __u32 prev_term;
    __u16 payload_len;
    __u16 node_id;
    __u64 timestamp_ns;
};

// raft_stats[0] = log appends fast-acked by XDP
// raft_stats[1] = heartbeats fast-acked by XDP
// raft_stats[2] = non-AppendEntries Raft packets passed to userspace
// raft_stats[3] = stale term AppendEntries packets passed to userspace
// raft_stats[4] = packets from a non-leader source passed to userspace
// raft_stats[5] = prevLog mismatches passed to userspace
// raft_stats[6] = duplicate appends fast-acked
struct {
    __uint(type, BPF_MAP_TYPE_ARRAY);
    __uint(max_entries, 7);
    __type(key, __u32);
    __type(value, __u64);
} raft_stats SEC(".maps");

struct {
    __uint(type, BPF_MAP_TYPE_ARRAY);
    __uint(max_entries, 1);
    __type(key, __u32);
    __type(value, __be32);
} raft_leader_ip SEC(".maps");

struct {
    __uint(type, BPF_MAP_TYPE_ARRAY);
    __uint(max_entries, 1);
    __type(key, __u32);
    __type(value, struct raft_state_value);
} raft_state SEC(".maps");

struct {
    __uint(type, BPF_MAP_TYPE_ARRAY);
    __uint(max_entries, 1);
    __type(key, __u32);
    __type(value, __u64);
} raft_last_seen SEC(".maps");

struct {
    __uint(type, BPF_MAP_TYPE_HASH);
    __uint(max_entries, 65536);
    __type(key, __u32);
    __type(value, struct fast_log_entry);
} raft_fast_log SEC(".maps");

struct {
    __uint(type, BPF_MAP_TYPE_RINGBUF);
    __uint(max_entries, 1 << 20);
} raft_events SEC(".maps");

static __always_inline void bump_stat(__u32 key)
{
    __u64 *value = bpf_map_lookup_elem(&raft_stats, &key);
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

static __always_inline void write_be32(__u8 *p, __u32 value)
{
    p[0] = value >> 24;
    p[1] = value >> 16;
    p[2] = value >> 8;
    p[3] = value;
}

static __always_inline void write_be16(__u8 *p, __u16 value)
{
    p[0] = value >> 8;
    p[1] = value;
}

static __always_inline void swap_mac(struct ethhdr *eth)
{
    __u8 tmp[ETH_ALEN];
    __builtin_memcpy(tmp, eth->h_source, ETH_ALEN);
    __builtin_memcpy(eth->h_source, eth->h_dest, ETH_ALEN);
    __builtin_memcpy(eth->h_dest, tmp, ETH_ALEN);
}

static __always_inline __u16 ipv4_csum(struct iphdr *ip)
{
    __u32 sum = 0;
    __u16 *ptr = (__u16 *)ip;

    sum += ptr[0]; sum += ptr[1]; sum += ptr[2]; sum += ptr[3]; sum += ptr[4];
    sum += ptr[5]; sum += ptr[6]; sum += ptr[7]; sum += ptr[8]; sum += ptr[9];
    sum = (sum & 0xffff) + (sum >> 16);
    sum += (sum >> 16);
    return (__u16)(~sum);
}

static __always_inline int prev_matches(struct raft_state_value *state,
                                        __u32 prev_index,
                                        __u32 prev_term)
{
    if (prev_index == 0 && prev_term == 0)
        return 1;
    if (prev_index > state->last_index)
        return 0;
    if (prev_index == state->last_index)
        return prev_term == state->last_term;

    struct fast_log_entry *entry = bpf_map_lookup_elem(&raft_fast_log, &prev_index);
    return entry && prev_term == entry->term;
}

SEC("xdp")
int raft_fastpath(struct xdp_md *ctx)
{
    void *data = (void *)(long)ctx->data;
    void *data_end = (void *)(long)ctx->data_end;

    struct ethhdr *eth = data;
    if ((void *)(eth + 1) > data_end)
        return XDP_PASS;
    if (eth->h_proto != bpf_htons(ETH_P_IP))
        return XDP_PASS;

    struct iphdr *ip = (void *)(eth + 1);
    if ((void *)(ip + 1) > data_end)
        return XDP_PASS;
    if (ip->protocol != IPPROTO_UDP || ip->ihl != 5)
        return XDP_PASS;

    struct udphdr *udp = (void *)(ip + 1);
    if ((void *)(udp + 1) > data_end)
        return XDP_PASS;
    if (udp->dest != bpf_htons(RAFT_PORT))
        return XDP_PASS;

    __u8 *payload = (void *)(udp + 1);
    if ((void *)(payload + 35) > data_end)
        return XDP_PASS;

    if (payload[0] != MSG_APPEND_ENTRIES) {
        bump_stat(2);
        return XDP_PASS;
    }

    __u32 zero = 0;
    __be32 *leader_ip = bpf_map_lookup_elem(&raft_leader_ip, &zero);
    if (leader_ip) {
        if (*leader_ip == 0)
            *leader_ip = ip->saddr;
        else if (*leader_ip != ip->saddr) {
            bump_stat(4);
            return XDP_PASS;
        }
    }

    __u32 term = read_be32(payload + 1);
    __u32 index = read_be32(payload + 5);
    __u32 prev_index = read_be32(payload + 9);
    __u32 prev_term = read_be32(payload + 13);
    __u16 node_id = read_be16(payload + 21);
    __u16 payload_len = read_be16(payload + 33);
    if ((void *)(payload + 35 + payload_len) > data_end)
        return XDP_PASS;

    struct raft_state_value *state = bpf_map_lookup_elem(&raft_state, &zero);
    if (!state)
        return XDP_PASS;

    if (term < state->current_term) {
        bump_stat(3);
        return XDP_PASS;
    }
    if (term > state->current_term)
        state->current_term = term;

    if (!prev_matches(state, prev_index, prev_term)) {
        bump_stat(5);
        return XDP_PASS;
    }

    __u64 now = bpf_ktime_get_ns();
    __u64 *last_seen = bpf_map_lookup_elem(&raft_last_seen, &zero);
    if (last_seen)
        *last_seen = now;

    if (index == 0 && payload_len == 0) {
        bump_stat(1);
    } else if (index == state->last_index + 1 && payload_len > 0) {
        struct fast_log_entry entry = {
            .term = term,
            .payload_len = payload_len,
            .timestamp_ns = now,
        };
        bpf_map_update_elem(&raft_fast_log, &index, &entry, BPF_ANY);
        state->last_index = index;
        state->last_term = term;

        struct raft_event *event = bpf_ringbuf_reserve(&raft_events, sizeof(*event), 0);
        if (event) {
            event->term = term;
            event->index = index;
            event->prev_index = prev_index;
            event->prev_term = prev_term;
            event->payload_len = payload_len;
            event->node_id = node_id;
            event->timestamp_ns = now;
            bpf_ringbuf_submit(event, 0);
        }
        bump_stat(0);
    } else if (index <= state->last_index) {
        bump_stat(6);
    } else {
        bump_stat(5);
        return XDP_PASS;
    }

    payload[0] = MSG_APPEND_RESPONSE;
    if (index == 0)
        write_be32(payload + 5, state->last_index);
    write_be16(payload + 23, FLAG_SUCCESS | FLAG_EBPF_FAST);
    write_be32(payload + 25, 0);
    write_be32(payload + 29, 0);

    __be32 old_saddr = ip->saddr;
    ip->saddr = ip->daddr;
    ip->daddr = old_saddr;

    __be16 old_source = udp->source;
    udp->source = udp->dest;
    udp->dest = old_source;

    ip->check = 0;
    ip->check = ipv4_csum(ip);
    udp->check = 0;

    swap_mac(eth);
    return XDP_TX;
}

char LICENSE[] SEC("license") = "GPL";

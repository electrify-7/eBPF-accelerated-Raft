// Leader-side TC egress fan-out for Raft.
//
// Userspace sends one UDP template packet marked with FLAG_BROADCAST_REQUEST.
// This TC hook clones it once per configured follower by rewriting:
//   - Ethernet destination MAC
//   - IPv4 destination address
//   - IPv4 header checksum
// then calling bpf_clone_redirect().
//
// The original template skb is dropped after all clones are emitted, so the
// leader avoids one sendto() per follower in the common broadcast path.

#include <linux/bpf.h>
#include <linux/if_ether.h>
#include <linux/ip.h>
#include <linux/udp.h>
#include <linux/in.h>
#include <linux/pkt_cls.h>
#include <bpf/bpf_helpers.h>
#include <bpf/bpf_endian.h>

#define RAFT_PORT 9000
#define MSG_APPEND_ENTRIES 0x20
#define MSG_COMMIT_NOTICE 0x30
#define FLAG_BROADCAST_REQUEST 0x0008
#define FANOUT_MAX 2

struct fanout_dst {
    __u32 ifindex;
    __be32 dst_ip;
    __u8 dst_mac[ETH_ALEN];
    __u16 node_id;
    __u16 pad;
};

struct {
    __uint(type, BPF_MAP_TYPE_ARRAY);
    __uint(max_entries, FANOUT_MAX);
    __type(key, __u32);
    __type(value, struct fanout_dst);
} raft_fanout SEC(".maps");

// raft_tc_stats[0] = cloned packets
// raft_tc_stats[1] = packets ignored
// raft_tc_stats[2] = broadcast templates dropped
// raft_tc_stats[3] = configured fanout slots missing
struct {
    __uint(type, BPF_MAP_TYPE_ARRAY);
    __uint(max_entries, 4);
    __type(key, __u32);
    __type(value, __u64);
} raft_tc_stats SEC(".maps");

static __always_inline void bump_stat(__u32 key)
{
    __u64 *value = bpf_map_lookup_elem(&raft_tc_stats, &key);
    if (value)
        __sync_fetch_and_add(value, 1);
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

static __always_inline int clone_to_dst(struct __sk_buff *skb,
                                        struct fanout_dst *dst,
                                        __be32 from_daddr,
                                        __u16 flags)
{
    bpf_skb_store_bytes(skb, 0, dst->dst_mac, ETH_ALEN, 0);
    bpf_l3_csum_replace(skb, ETH_HLEN + 10, from_daddr, dst->dst_ip, sizeof(dst->dst_ip));
    bpf_skb_store_bytes(skb, ETH_HLEN + 16, &dst->dst_ip, sizeof(dst->dst_ip), 0);

    __u16 node_id = bpf_htons(dst->node_id);
    bpf_skb_store_bytes(
        skb,
        ETH_HLEN + sizeof(struct iphdr) + sizeof(struct udphdr) + 21,
        &node_id,
        sizeof(node_id),
        0
    );

    __u16 clone_flags = flags & ~FLAG_BROADCAST_REQUEST;
    __u8 encoded_flags[2];
    write_be16(encoded_flags, clone_flags);
    bpf_skb_store_bytes(
        skb,
        ETH_HLEN + sizeof(struct iphdr) + sizeof(struct udphdr) + 23,
        encoded_flags,
        sizeof(encoded_flags),
        0
    );

    __u16 zero = 0;
    bpf_skb_store_bytes(skb, ETH_HLEN + sizeof(struct iphdr) + 6, &zero, sizeof(zero), 0);

    bpf_clone_redirect(skb, dst->ifindex, 0);
    bump_stat(0);
    return 1;
}

SEC("tc")
int raft_broadcast(struct __sk_buff *skb)
{
    void *data = (void *)(long)skb->data;
    void *data_end = (void *)(long)skb->data_end;

    struct ethhdr *eth = data;
    if ((void *)(eth + 1) > data_end)
        return TC_ACT_OK;
    if (eth->h_proto != bpf_htons(ETH_P_IP))
        return TC_ACT_OK;

    struct iphdr *ip = (void *)(eth + 1);
    if ((void *)(ip + 1) > data_end)
        return TC_ACT_OK;
    if (ip->protocol != IPPROTO_UDP || ip->ihl != 5)
        return TC_ACT_OK;

    struct udphdr *udp = (void *)(ip + 1);
    if ((void *)(udp + 1) > data_end)
        return TC_ACT_OK;
    if (udp->dest != bpf_htons(RAFT_PORT))
        return TC_ACT_OK;

    __u8 *payload = (void *)(udp + 1);
    if ((void *)(payload + 35) > data_end)
        return TC_ACT_OK;
    if (payload[0] != MSG_APPEND_ENTRIES && payload[0] != MSG_COMMIT_NOTICE)
        return TC_ACT_OK;

    __u16 flags = read_be16(payload + 23);
    if (!(flags & FLAG_BROADCAST_REQUEST)) {
        bump_stat(1);
        return TC_ACT_OK;
    }

    __be32 current_daddr = ip->daddr;
    int cloned = 0;

    __u32 k0 = 0;
    struct fanout_dst *dst0 = bpf_map_lookup_elem(&raft_fanout, &k0);
    if (dst0 && dst0->ifindex && dst0->dst_ip) {
        clone_to_dst(skb, dst0, current_daddr, flags);
        current_daddr = dst0->dst_ip;
        cloned += 1;
    } else {
        bump_stat(3);
    }

    __u32 k1 = 1;
    struct fanout_dst *dst1 = bpf_map_lookup_elem(&raft_fanout, &k1);
    if (dst1 && dst1->ifindex && dst1->dst_ip) {
        clone_to_dst(skb, dst1, current_daddr, flags);
        cloned += 1;
    } else {
        bump_stat(3);
    }

    if (cloned > 0) {
        bump_stat(2);
        return TC_ACT_SHOT;
    }

    return TC_ACT_OK;
}

char LICENSE[] SEC("license") = "GPL";

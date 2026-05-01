// Experimental leader-side TC hook for kernel-assisted message fan-out.
//
// The core helper shown in the Electrode-style design is bpf_clone_redirect().
// This program demonstrates that hook: when it sees a Raft AppendEntries packet
// marked with FLAG_BROADCAST_REQUEST, it clones the skb to ifindexes stored in
// raft_clone_if.
//
// Production use needs a companion map containing per-follower IP/MAC rewrite
// data. Without that neighbor rewrite setup, this program should be treated as
// a scaffold for the broadcast path, not as the default benchmark path.

#include <linux/bpf.h>
#include <linux/if_ether.h>
#include <linux/ip.h>
#include <linux/udp.h>
#include <linux/in.h>
#include <linux/pkt_cls.h>
#include <bpf/bpf_helpers.h>

#define RAFT_PORT 9000
#define MSG_APPEND_ENTRIES 0x20
#define FLAG_BROADCAST_REQUEST 0x0008

struct {
    __uint(type, BPF_MAP_TYPE_ARRAY);
    __uint(max_entries, 3);
    __type(key, __u32);
    __type(value, __u32);
} raft_clone_if SEC(".maps");

// raft_tc_stats[0] = cloned packets
// raft_tc_stats[1] = packets ignored
struct {
    __uint(type, BPF_MAP_TYPE_ARRAY);
    __uint(max_entries, 2);
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

SEC("tc")
int raft_broadcast(struct __sk_buff *skb)
{
    void *data = (void *)(long)skb->data;
    void *data_end = (void *)(long)skb->data_end;

    struct ethhdr *eth = data;
    if ((void *)(eth + 1) > data_end)
        return TC_ACT_OK;
    if (eth->h_proto != __builtin_bswap16(ETH_P_IP))
        return TC_ACT_OK;

    struct iphdr *ip = (void *)(eth + 1);
    if ((void *)(ip + 1) > data_end)
        return TC_ACT_OK;
    if (ip->protocol != IPPROTO_UDP || ip->ihl != 5)
        return TC_ACT_OK;

    struct udphdr *udp = (void *)(ip + 1);
    if ((void *)(udp + 1) > data_end)
        return TC_ACT_OK;
    if (udp->dest != __builtin_bswap16(RAFT_PORT))
        return TC_ACT_OK;

    __u8 *payload = (void *)(udp + 1);
    if ((void *)(payload + 35) > data_end)
        return TC_ACT_OK;
    if (payload[0] != MSG_APPEND_ENTRIES)
        return TC_ACT_OK;

    __u16 flags = read_be16(payload + 23);
    if (!(flags & FLAG_BROADCAST_REQUEST)) {
        bump_stat(1);
        return TC_ACT_OK;
    }

    #pragma unroll
    for (__u32 i = 0; i < 3; i++) {
        __u32 *ifindex = bpf_map_lookup_elem(&raft_clone_if, &i);
        if (ifindex && *ifindex) {
            bpf_clone_redirect(skb, *ifindex, 0);
            bump_stat(0);
        }
    }

    return TC_ACT_OK;
}

char LICENSE[] SEC("license") = "GPL";

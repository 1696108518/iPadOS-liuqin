/* SPDX-License-Identifier: MIT */
/* liuqin-gpu-test: userspace smoke test for the NativeAdreno user-client ABI.
 *
 * Opens NativeAdrenoGpuService, asks for the device facts, allocates two BOs,
 * maps them into the GPU aperture and into this process, writes one CP_MEM_WRITE
 * indirect buffer, submits it, waits on the returned timestamp fence and reads
 * the magic word the GPU was told to write.
 *
 * This is the same check the kernel runs at bring-up (the CP self test), but
 * through the wire ABI the real userspace driver will use, so a broken ABI is
 * caught before Mesa/Turnip exist.  Everything is printed; run it from a
 * launchd job with StandardOutPath=/dev/console to get the lines into the
 * persistent kernel log.
 *
 * Cache note: the target BO is not touched before the submission.  The first
 * CPU access to those pages therefore happens after the GPU wrote them, so a
 * cacheable user mapping cannot serve a stale line.
 */
#include <stdio.h>
#include <string.h>
#include <stdint.h>
#include <stdarg.h>
#include <unistd.h>
#include <sys/sysctl.h>
#include <mach/mach.h>
#include <IOKit/IOKitLib.h>
#include "NativeAdrenoABI.h"


static io_connect_t gConnect = 0;

static void say(const char *fmt, ...)
{
    va_list args;
    va_start(args, fmt);
    vprintf(fmt, args);
    va_end(args);
    printf("\n");
    fflush(stdout);
}

static kern_return_t rpc(uint32_t selector, const void *in, size_t in_len,
    void *out, size_t out_len)
{
    size_t size = out_len;
    return IOConnectCallStructMethod(gConnect, selector, in, in_len, out, &size);
}

static io_connect_t open_gpu(void)
{
    /* At boot this job starts long before the GPU has finished publishing: the
     * board needs roughly five minutes (GMU cold start, GX power, contexts).
     * Wait for the service instead of failing. */
    io_service_t service = 0;
    for (unsigned attempt = 0; attempt < 600 && !service; ++attempt) {
        CFMutableDictionaryRef match = IOServiceMatching("NativeAdrenoGpuService");
        if (!match) {
            say("gputest: IOServiceMatching failed");
            return 0;
        }
        service = IOServiceGetMatchingService(kIOMainPortDefault, match);
        if (!service) {
            if (attempt == 0)
                say("gputest: waiting for NativeAdrenoGpuService");
            sleep(1);
        }
    }
    if (!service) {
        say("gputest: NativeAdrenoGpuService not found");
        return 0;
    }
    io_connect_t connect = 0;
    const kern_return_t kr = IOServiceOpen(service, mach_task_self(), 0, &connect);
    IOObjectRelease(service);
    if (kr != KERN_SUCCESS) {
        say("gputest: IOServiceOpen 0x%x", kr);
        return 0;
    }
    return connect;
}

/* Allocates one BO and returns its handle. */
static kern_return_t alloc_bo(uint64_t bytes, uint64_t *handle)
{
    NativeAdrenoAbiBoAlloc request = {bytes, 12, 0};
    NativeAdrenoAbiBoAllocReply reply = {0};
    const kern_return_t kr = rpc(NativeAdrenoMethodBoAlloc, &request, sizeof(request),
        &reply, sizeof(reply));
    *handle = reply.handle;
    return kr;
}

static kern_return_t map_bo(uint64_t handle, uint64_t iova, uint64_t *mapped)
{
    NativeAdrenoAbiBoMap request = {handle, iova, 3 /* read|write */, 0 /* write back */, 3 /* inner */, 0};
    NativeAdrenoAbiBoMapReply reply = {0};
    const kern_return_t kr = rpc(NativeAdrenoMethodBoMap, &request, sizeof(request),
        &reply, sizeof(reply));
    *mapped = reply.iova;
    return kr;
}

static void *cpu_map_bo(uint64_t handle)
{
    NativeAdrenoAbiBoCpuMap request = {handle};
    NativeAdrenoAbiBoCpuMapReply reply = {0};
    if (rpc(NativeAdrenoMethodBoCpuMap, &request, sizeof(request), &reply, sizeof(reply)))
        return NULL;
    mach_vm_address_t address = 0;
    mach_vm_size_t size = 0;
    kern_return_t kr = IOConnectMapMemory64(gConnect, reply.token, mach_task_self(),
        &address, &size, kIOMapAnywhere | kIOMapInhibitCache);
    if (kr != KERN_SUCCESS)
        kr = IOConnectMapMemory64(gConnect, reply.token, mach_task_self(),
            &address, &size, kIOMapAnywhere);
    if (kr != KERN_SUCCESS) {
        say("gputest: IOConnectMapMemory64 token %u 0x%x", reply.token, kr);
        return NULL;
    }
    return (void *)(uintptr_t)address;
}

static void free_bo(uint64_t handle)
{
    if (!handle)
        return;
    NativeAdrenoAbiBoFree request = {handle};
    (void)rpc(NativeAdrenoMethodBoFree, &request, sizeof(request), NULL, 0);
}

static kern_return_t submit_one(uint64_t handle, uint64_t iova, uint32_t bytes,
    uint64_t *fence)
{
    NativeAdrenoAbiSubmit request;
    NativeAdrenoAbiSubmitReply reply = {0};
    memset(&request, 0, sizeof(request));
    request.buffer_count = 1;
    request.queue = 0;               /* the queue open() created */
    request.buffers[0].handle = handle;
    request.buffers[0].iova = iova;
    request.buffers[0].bytes = bytes;
    const kern_return_t kr = rpc(NativeAdrenoMethodSubmit, &request, sizeof(request),
        &reply, sizeof(reply));
    *fence = reply.fence;
    return kr;
}

/* The job is installed on every root image, but it must only touch the GPU when
 * asked: on this board the GPU is published early in the boot and a submission
 * at that moment can take the display down with it.  The switch is a boot
 * argument, so a normal boot never runs the test. */
static int enabled(void)
{
    char args[1024] = {0};
    size_t size = sizeof(args) - 1;
    if (sysctlbyname("kern.bootargs", args, &size, NULL, 0) != 0 || !size) {
        say("gputest: skipped (kern.bootargs unreadable)");
        return 0;
    }
    args[sizeof(args) - 1] = 0;
    if (!strstr(args, "sheng-gpu-gputest=1")) {
        say("gputest: skipped (boot with sheng-gpu-gputest=1 to run)");
        return 0;
    }
    return 1;
}

int main(void)
{
    if (!enabled())
        return 0;
    say("gputest: start");
    gConnect = open_gpu();
    if (!gConnect)
        return 1;

    NativeAdrenoAbiParams params = {0};
    kern_return_t kr = rpc(NativeAdrenoMethodQuery, NULL, 0, &params, sizeof(params));
    if (kr) {
        say("gputest: Query 0x%x", kr);
        return 1;
    }
    say("gputest: abi=%u in=%u out=%u flags=0x%x max_bo=%llu",
        params.abi_version, params.input_bits, params.output_bits, params.flags,
        (unsigned long long)params.max_bo_bytes);
    if (params.abi_version != NativeAdrenoAbiVersion) {
        say("gputest: unexpected ABI version");
        return 1;
    }

    NativeAdrenoAbiDeviceInfo info = {0};
    kr = rpc(NativeAdrenoMethodGetDeviceInfo, NULL, 0, &info, sizeof(info));
    if (kr) {
        say("gputest: GetDeviceInfo 0x%x", kr);
        return 1;
    }
    say("gputest: chip=0x%x gmem=0x%llx+0x%x va=0x%llx+0x%llx uche_trap=0x%llx hbb=%u ubwc=%u mt=%u flags=0x%x",
        info.chip_id, (unsigned long long)info.gmem_base, info.gmem_bytes,
        (unsigned long long)info.va_start, (unsigned long long)info.va_size,
        (unsigned long long)info.uche_trap_base, info.highest_bank_bit,
        info.ubwc_version, info.macrotile_mode, info.flags);

    uint64_t cmd_handle = 0, target_handle = 0;
    if ((kr = alloc_bo(0x1000, &cmd_handle)) || !cmd_handle) {
        say("gputest: BoAlloc(cmd) 0x%x", kr);
        return 1;
    }
    if ((kr = alloc_bo(0x1000, &target_handle)) || !target_handle) {
        say("gputest: BoAlloc(target) 0x%x", kr);
        return 1;
    }
    say("gputest: BOs cmd=%llu target=%llu", (unsigned long long)cmd_handle,
        (unsigned long long)target_handle);

    /* Choose GPU addresses inside whatever aperture the client got.  The
     * kernel's own buffers live at 0x100000..0x302fff, so 4 MiB/5 MiB are free
     * in the default 2 GiB window; if the window is smaller, drop to 64K/128K. */
    const uint64_t base = info.va_start;
    const uint64_t room = info.va_size ? info.va_size : 0x80000000ull;
    uint64_t cmd_iova = base + 0x400000;
    uint64_t target_iova = base + 0x500000;
    if (cmd_iova + 0x1000 > base + room || target_iova + 0x1000 > base + room) {
        cmd_iova = base + 0x10000;
        target_iova = base + 0x20000;
    }
    uint64_t mapped = 0;
    if ((kr = map_bo(cmd_handle, cmd_iova, &mapped)) || mapped != cmd_iova) {
        say("gputest: BoMap(cmd) 0x%x iova=0x%llx", kr, (unsigned long long)mapped);
        return 1;
    }
    if ((kr = map_bo(target_handle, target_iova, &mapped)) || mapped != target_iova) {
        say("gputest: BoMap(target) 0x%x iova=0x%llx", kr, (unsigned long long)mapped);
        return 1;
    }
    say("gputest: mapped cmd=0x%llx target=0x%llx",
        (unsigned long long)cmd_iova, (unsigned long long)target_iova);

    volatile uint32_t *cmd = (volatile uint32_t *)cpu_map_bo(cmd_handle);
    if (!cmd) {
        say("gputest: cpu map(cmd) failed");
        return 1;
    }
    /* One CP_MEM_WRITE: header (opcode 0x3d, three payload words), address lo/hi,
     * then the magic value.  The submit path appends the timestamp fence. */
    cmd[0] = (7u << 28) | 3u | (0x3d << 16);
    cmd[1] = (uint32_t)(target_iova + 0x40);
    cmd[2] = (uint32_t)((target_iova + 0x40) >> 32);
    cmd[3] = 0x600dc0deu;
    __asm__ volatile("dsb sy" ::: "memory");

    /* Let the boot settle: the GPU service publishes while launchd is still
     * bringing the system up, and this submission must not race that. */
    sleep(20);

    uint64_t fence = 0;
    kr = submit_one(cmd_handle, cmd_iova, 16, &fence);
    if (kr || !fence) {
        say("gputest: Submit 0x%x fence=%llu", kr, (unsigned long long)fence);
        return 1;
    }
    say("gputest: submitted fence=%llu", (unsigned long long)fence);

    NativeAdrenoAbiWaitFence wait = {fence, 2000000000ull /* 2 s */};
    kr = rpc(NativeAdrenoMethodWaitFence, &wait, sizeof(wait), NULL, 0);
    say("gputest: WaitFence 0x%x", kr);

    volatile uint32_t *target = (volatile uint32_t *)cpu_map_bo(target_handle);
    if (!target) {
        say("gputest: cpu map(target) failed");
        return 1;
    }
    const uint32_t got = target[0x40 / 4];
    say("gputest: target[0x40]=0x%08x want 0x600dc0de -> %s", got,
        got == 0x600dc0deu ? "PASS" : "FAIL");
    free_bo(cmd_handle);
    free_bo(target_handle);
    return got == 0x600dc0deu ? 0 : 2;
}

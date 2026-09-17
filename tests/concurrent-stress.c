/* SPDX-License-Identifier: GPL-3.0-or-later */
/* Concurrent public API stress against an in-memory model V4L2 decoder.
 *
 * Schedules (all offline, deterministic seeds, no real device, no real
 * sleeping while waiting for the decoder: the model device completes
 * queued work after a seeded number of model ticks; the harness does
 * not measure in-driver overlap):
 *
 *   threads N FRAMES REPS SEED   N contexts decode mixed-codec frames
 *                                and read them back through GetImage,
 *                                exported dma-bufs and derived images;
 *                                each context is destroyed while the
 *                                later streams keep decoding. Every
 *                                stream must verify FRAMES frames with
 *                                exact per-stream MD5s.
 *   teardown N FRAMES REPS SEED  One victim context is destroyed
 *                                mid-decode, deterministically while it
 *                                holds a staged picture open at its
 *                                midpoint. Every frame it published must
 *                                complete and verify byte-exact (the
 *                                verified count is exactly FRAMES/2);
 *                                all other streams must still verify
 *                                every frame.
 *   failure N FRAMES REPS SEED   A separate misbehaving client keeps
 *                                issuing invalid calls (bogus ids,
 *                                foreign surfaces, object churn)
 *                                against a valid stream while every
 *                                stream completes all frames exactly.
 *   worker ID FRAMES SEED        Single-stream process mode for the
 *                                multi-process schedule driven by
 *                                tests/concurrent-process.py.
 *   overlap N FRAMES REPS SEED    The same flow, plus a deterministic
 *                                in-driver overlap proof: stream 0's
 *                                frame 0 is held by the model gate so
 *                                its reader's vaSyncSurface spins
 *                                inside the driver holding api_mutex
 *                                (a real locked section), while the
 *                                decoder fires unlocked entrypoints
 *                                into it until the driver-side
 *                                instrumentation
 *                                (v4l2r_overlap_snapshot()) has
 *                                recorded the required overlap events.
 *   overlap-actor N FRAMES REPS  The same gate, but the failure
 *                                actor's own unlocked operations are
 *                                the ones that must overlap the
 *                                locked section in the driver.
 *
 * The model device is the decode oracle: the slice bytes a stream
 * renders into a picture are hashed by the model when the request is
 * queued, and the completion writes a byte pattern derived from that
 * hash into the target CAPTURE plane. Readers recompute the same
 * pattern from the frame bytes, so lost frames, cross-stream pixels
 * and stale buffer reuse all fail an exact byte comparison.
 *
 * The concurrency surface under test is the real public API: the locked
 * entrypoints installed by v4l2r_lock_surface_api() (CreateContext,
 * Begin/Render/EndPicture, SyncSurface, QuerySurfaceStatus,
 * ExportSurfaceHandle, DeriveImage, GetImage, DestroyContext,
 * DestroySurfaces) are called from multiple threads at once,
 * deliberately interleaved with the unlocked ones (CreateSurfaces2,
 * Create/Map/Unmap/DestroyBuffer, Create/DestroyImage), matching how a
 * client like FFmpeg splits decode and filter threads over one VA
 * display while another thread tears a context down.
 */
#define _GNU_SOURCE
#include <assert.h>
#include <errno.h>
#include <fcntl.h>
#include <limits.h>
#include <poll.h>
#include <pthread.h>
#include <stdarg.h>
#include <stdatomic.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/resource.h>
#include <time.h>
#include <unistd.h>
#include <linux/media.h>
#include <va/va.h>
#include <va/va_drmcommon.h>
#include "v4l2_request.h"

/* ------------------------------------------------------------------ */
/* Compact MD5 (RFC 1321) used for stable result hashes; verified     */
/* against the standard vectors at startup.                           */
/* ------------------------------------------------------------------ */

static const uint32_t md5_k[64] = {
    0xd76aa478, 0xe8c7b756, 0x242070db, 0xc1bdceee, 0xf57c0faf, 0x4787c62a,
    0xa8304613, 0xfd469501, 0x698098d8, 0x8b44f7af, 0xffff5bb1, 0x895cd7be,
    0x6b901122, 0xfd987193, 0xa679438e, 0x49b40821, 0xf61e2562, 0xc040b340,
    0x265e5a51, 0xe9b6c7aa, 0xd62f105d, 0x02441453, 0xd8a1e681, 0xe7d3fbc8,
    0x21e1cde6, 0xc33707d6, 0xf4d50d87, 0x455a14ed, 0xa9e3e905, 0xfcefa3f8,
    0x676f02d9, 0x8d2a4c8a, 0xfffa3942, 0x8771f681, 0x6d9d6122, 0xfde5380c,
    0xa4beea44, 0x4bdecfa9, 0xf6bb4b60, 0xbebfbc70, 0x289b7ec6, 0xeaa127fa,
    0xd4ef3085, 0x04881d05, 0xd9d4d039, 0xe6db99e5, 0x1fa27cf8, 0xc4ac5665,
    0xf4292244, 0x432aff97, 0xab9423a7, 0xfc93a039, 0x655b59c3, 0x8f0ccc92,
    0xffeff47d, 0x85845dd1, 0x6fa87e4f, 0xfe2ce6e0, 0xa3014314, 0x4e0811a1,
    0xf7537e82, 0xbd3af235, 0x2ad7d2bb, 0xeb86d391,
};
static const unsigned char md5_r[64] = {
    7, 12, 17, 22, 7, 12, 17, 22, 7, 12, 17, 22, 7, 12, 17, 22,
    5, 9, 14, 20, 5, 9, 14, 20, 5, 9, 14, 20, 5, 9, 14, 20,
    4, 11, 16, 23, 4, 11, 16, 23, 4, 11, 16, 23, 4, 11, 16, 23,
    6, 10, 15, 21, 6, 10, 15, 21, 6, 10, 15, 21, 6, 10, 15, 21,
};
#define MD5_ROL(x, c) (((x) << (c)) | ((x) >> (32 - (c))))

static void md5(const unsigned char *data, size_t len, unsigned char out[16])
{
    uint32_t h[4] = { 0x67452301, 0xefcdab89, 0x98badcfe, 0x10325476 };
    size_t padded = ((len + 8) / 64 + 1) * 64;
    unsigned char *block = calloc(1, padded);
    uint64_t bits;
    assert(block);
    memcpy(block, data, len);
    block[len] = 0x80;
    bits = (uint64_t)len * 8;
    for (int i = 0; i < 8; i++)
        block[padded - 8 + i] = (unsigned char)(bits >> (8 * i));
    for (size_t off = 0; off < padded; off += 64) {
        uint32_t m[16], a = h[0], b = h[1], c = h[2], d = h[3];
        for (int i = 0; i < 16; i++)
            m[i] = block[off + 4 * i] |
                   (uint32_t)block[off + 4 * i + 1] << 8 |
                   (uint32_t)block[off + 4 * i + 2] << 16 |
                   (uint32_t)block[off + 4 * i + 3] << 24;
        for (int i = 0; i < 64; i++) {
            uint32_t f;
            int g = i;
            if (i < 16) {
                f = (b & c) | (~b & d);
            } else if (i < 32) {
                f = (d & b) | (~d & c);
                g = (5 * i + 1) % 16;
            } else if (i < 48) {
                f = b ^ c ^ d;
                g = (3 * i + 5) % 16;
            } else {
                f = c ^ (b | ~d);
                g = (7 * i) % 16;
            }
            f += a + md5_k[i] + m[g];
            a = d;
            d = c;
            c = b;
            b += MD5_ROL(f, md5_r[i]);
        }
        h[0] += a; h[1] += b; h[2] += c; h[3] += d;
    }
    free(block);
    for (int i = 0; i < 4; i++) {
        out[4 * i] = (unsigned char)(h[i]);
        out[4 * i + 1] = (unsigned char)(h[i] >> 8);
        out[4 * i + 2] = (unsigned char)(h[i] >> 16);
        out[4 * i + 3] = (unsigned char)(h[i] >> 24);
    }
}

static void md5_hex(const unsigned char digest[16], char out[33])
{
    static const char hex[] = "0123456789abcdef";
    for (int i = 0; i < 16; i++) {
        out[2 * i] = hex[digest[i] >> 4];
        out[2 * i + 1] = hex[digest[i] & 15];
    }
    out[32] = 0;
}

static void md5_self_test(void)
{
    /* Single-block RFC 1321 vectors plus multi-block and padding-
     * boundary vectors: the per-stream digests hash 8 bytes per frame,
     * which crosses the 55/56 and 64-byte padding boundaries. The
     * reference values come from an independent implementation. */
    static const struct { const char *hex; size_t len; int fill; } vectors[] = {
        { "900150983cd24fb0d6963f7d28e17f72", 3, -1 },   /* "abc" */
        { "d41d8cd98f00b204e9800998ecf8427e", 0, 0 },
        { "c9ea3314b91c9fd4e38f9432064fd1f2", 55, 0 },
        { "e3c4dd21a9171fd39d208efa09bf7883", 56, 0 },
        { "3b5d3c7d207e37dceeedd301e35e2e58", 64, 0 },
        { "aceb486e7e4b2d2f1c5f2328b503502b", 96, 0 },
        { "da0e48224106c7535a4cd8db2ac7b8e3", 96, -2 },  /* 0,1,2,... */
        { "887f30b43b2867f4a9accceee7d16e6c", 200, 'a' },
    };
    unsigned char digest[16];
    char hex[33];

    for (size_t v = 0; v < sizeof(vectors) / sizeof(vectors[0]); v++) {
        unsigned char *data = malloc(vectors[v].len ? vectors[v].len : 1);

        assert(data);
        for (size_t i = 0; i < vectors[v].len; i++)
            data[i] = vectors[v].fill == -1 ? (unsigned char)"abc"[i] :
                      vectors[v].fill == -2 ? (unsigned char)i :
                      (unsigned char)vectors[v].fill;
        md5(data, vectors[v].len, digest);
        md5_hex(digest, hex);
        assert(!strcmp(hex, vectors[v].hex));
        free(data);
    }
}

/* ------------------------------------------------------------------ */
/* Deterministic frame content: pure functions of (seed, stream, frame) */
/* and the frame's byte hash.                                          */
/* ------------------------------------------------------------------ */

#define FRAME_BYTES 96

static uint64_t splitmix64(uint64_t *state)
{
    uint64_t z = (*state += 0x9e3779b97f4a7c15ull);
    z = (z ^ (z >> 30)) * 0xbf58476d1ce4e5b9ull;
    z = (z ^ (z >> 27)) * 0x94d049bb133111ebull;
    return z ^ (z >> 31);
}

static uint64_t fnv1a64(const unsigned char *data, size_t len)
{
    uint64_t hash = 0xcbf29ce484222325ull;
    for (size_t i = 0; i < len; i++) {
        hash ^= data[i];
        hash *= 0x100000001b3ull;
    }
    return hash;
}

/* The pixel pattern the model decoder writes into a CAPTURE plane for a
 * frame whose slice bytes hash to hash. plane_bytes must match the
 * surface's NV12 plane size (width * height * 3 / 2 here). */
static void frame_pattern(uint64_t hash, unsigned char *dst, size_t n)
{
    for (size_t i = 0; i < n; i++)
        dst[i] = (unsigned char)((hash >> ((i & 7) * 8)) +
                                         (unsigned)hash + i * 131u);
}

/* Identifiable slice bytes for (seed, stream, frame). */
static void frame_bytes(uint64_t seed, unsigned stream, unsigned frame,
                        unsigned char out[FRAME_BYTES])
{
    uint64_t state = seed ^ 0x100000001ull * (stream + 1) ^
                     (uint64_t)frame * 0xace5ace5ull;
    for (unsigned i = 0; i < FRAME_BYTES; i += 8) {
        uint64_t value = splitmix64(&state);
        memcpy(out + i, &value, sizeof(value));
    }
    out[0] = 0xc7;
    out[1] = (unsigned char)stream;
    out[2] = (unsigned char)frame;
}

/* Model completion delay in model ticks: pure in (seed, content hash). */
static unsigned model_delay(uint64_t seed, uint64_t hash)
{
    return 1u + (unsigned)((hash ^ seed) % 4u);
}

/* ------------------------------------------------------------------ */
/* Model V4L2 device. One pthread mutex guards all mutable model state; */
/* the model clock is atomic so the wrapped clock never blocks.        */
/*                                                                     */
/* Every open("model-video") is an independent M2M instance (its own   */
/* OUTPUT/CAPTURE queues), exactly like a real per-fd m2m context.     */
/* Plane storage is keyed by (fd generation, fd slot, buffer offset)   */
/* and served to every mmap of that plane, so dm-bufs exported by      */
/* VIDIOC_EXPBUF alias the same bytes the MMAP path maps. Requests     */
/* complete after a seeded number of model ticks; time only moves      */
/* when the driver polls (or the wrapped clock ticks), so waits are    */
/* instantaneous but genuinely interleaved, and a request can never   */
/* complete later than a caller's own deadline.                        */
/* ------------------------------------------------------------------ */

#define MODEL_FDS 512
#define MODEL_PLANES 1024
#define FD_BASE 1000
/* Buffer offsets within one video fd: OUTPUT and CAPTURE index spaces
 * overlap, so give the CAPTURE queue its own high offset base. */
#define OUTPUT_OFFSET(i)  (0x1000u * ((i) + 1u))
#define CAPTURE_OFFSET(i) (0x40000000u + 0x1000u * ((i) + 1u))
/* One model tick: the granularity of seeded decode delays. */
#define TICK_NS 1000000ull

enum fd_kind { FD_NONE, FD_VIDEO, FD_MEDIA, FD_REQUEST, FD_DMABUF };

struct plane {
    void *mem;
    size_t size;
    unsigned gen, slot;
    uint64_t offset;
    bool live;
};

struct inflight {
    bool used;
    /* Held by the overlap gate: a request the harness refuses to let
     * complete, so a vaSyncSurface for it spins inside the driver while
     * holding api_mutex (the deterministic in-driver overlap window). */
    bool held;
    unsigned video_slot;
    int output_index;
    int capture_index;
    uint64_t hash;
    uint64_t complete_ns;
};

struct model_fd {
    enum fd_kind kind;
    bool live;
    unsigned generation;
    struct v4l2_format formats[2]; /* [0] OUTPUT side, [1] CAPTURE side */
    unsigned outputs, captures;
    uint32_t output_queued, output_ready;
    uint64_t capture_queued, capture_ready;
    struct plane *output_mem[32];
    struct plane *capture_mem[V4L2R_MAX_CAPTURE_BUFFERS];
    /* Request fds: association recorded at QBUF(OUTPUT) time. */
    int request_video;      /* video fd slot, -1 until queued */
    int request_output;     /* OUTPUT buffer index */
    unsigned request_target; /* CAPTURE index + 1 from the timestamp */
    size_t request_bytesused;
    /* Dmabuf fds: which plane this handle aliases. */
    struct plane *alias;
};

static struct {
    struct model_fd fds[MODEL_FDS];
    struct plane planes[MODEL_PLANES];
    struct inflight queue[MODEL_FDS]; /* one slot per request fd */
    unsigned fd_count, plane_count;
    uint64_t seed;
    unsigned ioctls, polls, completions;
    pthread_mutex_t mutex;
} model;

static _Atomic uint64_t model_clock;

/* Provided by the linker for --wrap=clock_gettime; real wall time for
 * the watchdog (the driver's CLOCK_MONOTONIC reads the model clock). */
extern int __real_clock_gettime(clockid_t, struct timespec *);
extern void *__real_mmap(void *, size_t, int, int, int, off_t);
extern int __real_munmap(void *, size_t);

static uint64_t model_now(void)
{
    return atomic_load_explicit(&model_clock, memory_order_relaxed);
}

/* Overlap gate: while closed, requests queued on the gate's video fd are
 * held (never retired), and polling that fd stays "ready" so the driver's
 * dequeue loop spins inside vaSyncSurface — a real in-driver section that
 * holds api_mutex for a controlled window. Opened by the harness after
 * the in-driver overlap events are recorded. */
static _Atomic bool model_gate_closed;
/* Latched release: a delayed decoder must not have to observe a short
 * closed->open pulse. The poll handshake identifies the actual held sync. */
static _Atomic bool model_gate_opened;
static _Atomic bool model_gate_sync_waiting;
static _Thread_local bool model_gate_sync_reader;
static _Atomic unsigned long model_gate_proven_events;

static void model_retire(uint64_t now)
{
    for (unsigned s = 0; s < model.fd_count; s++) {
        struct inflight *f = &model.queue[s];
        struct plane *plane;

        if (!f->used || f->complete_ns > now)
            continue;
        if (f->held && atomic_load(&model_gate_closed))
            continue;
        struct model_fd *video = &model.fds[f->video_slot];
        video->output_ready |= 1u << f->output_index;
        video->capture_ready |= UINT64_C(1) << f->capture_index;
        plane = video->capture_mem[f->capture_index];
        /* The target plane always exists by QBUF time; fill it with the
         * oracle pattern so every readback path (mmap, dm-buf alias,
         * preserved backing) sees the same bytes. */
        assert(plane && plane->size);
        frame_pattern(f->hash, plane->mem, plane->size);
        f->used = false;
        model.completions++;
    }
}

/* Any request held by the gate on this video fd? */
static bool model_gate_holds(unsigned video_slot)
{
    if (!atomic_load(&model_gate_closed))
        return false;
    for (unsigned s = 0; s < model.fd_count; s++)
        if (model.queue[s].used && model.queue[s].held &&
            model.queue[s].video_slot == video_slot)
            return true;
    return false;
}

static uint64_t model_next_deadline(void)
{
    uint64_t next = 0;
    for (unsigned s = 0; s < model.fd_count; s++)
        if (model.queue[s].used &&
            !(model.queue[s].held && atomic_load(&model_gate_closed)) &&
            (!next || model.queue[s].complete_ns < next))
            next = model.queue[s].complete_ns;
    return next;
}

static unsigned fd_slot(int fd)
{
    assert(fd >= FD_BASE);
    unsigned value = (unsigned)(fd - FD_BASE);
    unsigned n = value % MODEL_FDS;
    assert(model.fds[n].live);
    assert(model.fds[n].generation == value / MODEL_FDS);
    return n;
}

static int new_fd_locked(enum fd_kind kind)
{
    unsigned n, gen;
    struct model_fd *f;

    for (n = 0; n < MODEL_FDS && model.fds[n].live; n++);
    assert(n < MODEL_FDS);
    f = &model.fds[n];
    gen = f->generation + 1;
    assert(gen < UINT_MAX / MODEL_FDS);
    memset(f, 0, sizeof(*f));
    f->kind = kind;
    f->live = true;
    f->generation = gen;
    f->request_video = -1;
    if (n >= model.fd_count)
        model.fd_count = n + 1;
    return FD_BASE + (int)(gen * MODEL_FDS + n);
}

static struct plane *plane_lookup_locked(unsigned slot, unsigned gen,
                                         uint64_t offset, size_t size)
{
    struct plane *free_slot = NULL;
    for (unsigned i = 0; i < model.plane_count; i++) {
        struct plane *p = &model.planes[i];
        if (p->live && p->slot == slot && p->gen == gen && p->offset == offset) {
            assert(size <= p->size);
            return p;
        }
        if (!p->live && !free_slot)
            free_slot = p;
    }
    assert(model.plane_count < MODEL_PLANES || free_slot);
    struct plane *p = free_slot ? free_slot : &model.planes[model.plane_count++];
    p->mem = __real_mmap(NULL, size, PROT_READ | PROT_WRITE,
                         MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
    assert(p->mem != MAP_FAILED);
    p->size = size;
    p->slot = slot;
    p->gen = gen;
    p->offset = offset;
    p->live = true;
    return p;
}

/* ------------------------------------------------------------------ */
/* Wrapped syscalls: the driver sees an in-memory decoder.            */
/* ------------------------------------------------------------------ */

int __wrap_open(const char *path, int flags, ...)
{
    (void)flags;
    int fd;
    pthread_mutex_lock(&model.mutex);
    if (!strcmp(path, "model-video"))
        fd = new_fd_locked(FD_VIDEO);
    else if (!strcmp(path, "model-media"))
        fd = new_fd_locked(FD_MEDIA);
    else
        assert(!"unexpected open() path");
    pthread_mutex_unlock(&model.mutex);
    return fd;
}

int __wrap_close(int fd)
{
    pthread_mutex_lock(&model.mutex);
    unsigned slot = fd_slot(fd);
    model.fds[slot].live = false;
    if (model.fds[slot].kind == FD_REQUEST)
        model.queue[slot].used = false;
    else {
        /* Requests queued to this video fd can never complete. */
        for (unsigned s = 0; s < model.fd_count; s++)
            if (model.queue[s].used && model.queue[s].video_slot == slot)
                model.queue[s].used = false;
    }
    pthread_mutex_unlock(&model.mutex);
    return 0;
}

int __wrap_dup(int fd)
{
    pthread_mutex_lock(&model.mutex);
    unsigned slot = fd_slot(fd);
    int nfd = new_fd_locked(model.fds[slot].kind);
    struct model_fd *copy = &model.fds[fd_slot(nfd)];
    struct model_fd *original = &model.fds[slot];
    unsigned generation = copy->generation;

    *copy = *original;
    /* The new handle keeps its own generation; only the queue state and
     * request/dmabuf associations are inherited. */
    copy->generation = generation;
    copy->live = true;
    pthread_mutex_unlock(&model.mutex);
    return nfd;
}

int __wrap_fcntl(int fd, int cmd, ...)
{
    assert(cmd == F_DUPFD_CLOEXEC);
    return __wrap_dup(fd);
}

int __wrap_open64(const char *path, int flags, ...)
{
    return __wrap_open(path, flags);
}

int __wrap___open_2(const char *path, int flags)
{
    return __wrap_open(path, flags);
}

int __wrap___open64_2(const char *path, int flags)
{
    return __wrap_open(path, flags);
}

int __wrap_fcntl64(int fd, int cmd, ...)
{
    return __wrap_fcntl(fd, cmd);
}

void *__wrap_mmap(void *addr, size_t len, int prot, int flags, int fd, off_t offset)
{
    struct plane *p;

    (void)addr; (void)prot; (void)flags;
    pthread_mutex_lock(&model.mutex);
    unsigned slot = fd_slot(fd);
    if (model.fds[slot].kind == FD_DMABUF) {
        p = model.fds[slot].alias;
        assert(p && p->live && len <= p->size);
    } else {
        assert(model.fds[slot].kind == FD_VIDEO);
        p = plane_lookup_locked(slot, model.fds[slot].generation,
                                (uint64_t)offset, len);
    }
    pthread_mutex_unlock(&model.mutex);
    return p->mem;
}

void *__wrap_mmap64(void *addr, size_t len, int prot, int flags, int fd, off_t offset)
{
    return __wrap_mmap(addr, len, prot, flags, fd, offset);
}

int __wrap_munmap(void *addr, size_t len)
{
    (void)len;
    pthread_mutex_lock(&model.mutex);
    for (unsigned i = 0; i < model.plane_count; i++)
        if (model.planes[i].live && model.planes[i].mem == addr) {
            /* Model storage stays alive until the model reset; the
             * driver's own bookkeeping of the mapping is unaffected. */
            pthread_mutex_unlock(&model.mutex);
            return 0;
        }
    pthread_mutex_unlock(&model.mutex);
    return __real_munmap(addr, len);
}

static void format_fill(struct v4l2_format *f)
{
    struct v4l2_pix_format_mplane *pix = &f->fmt.pix_mp;

    if (V4L2_TYPE_IS_MULTIPLANAR(f->type)) {
        if (!pix->width)
            pix->width = 64;
        if (!pix->height)
            pix->height = 64;
        if (!pix->pixelformat)
            pix->pixelformat = V4L2_TYPE_IS_OUTPUT(f->type) ?
                               V4L2_PIX_FMT_H264 : V4L2_PIX_FMT_NV12;
        pix->num_planes = 1;
        if (!pix->plane_fmt[0].bytesperline)
            pix->plane_fmt[0].bytesperline = (pix->width + 1) & ~1u;
        if (!pix->plane_fmt[0].sizeimage)
            pix->plane_fmt[0].sizeimage = pix->plane_fmt[0].bytesperline *
                                          pix->height * 3 / 2;
    } else {
        struct v4l2_pix_format *sp = &f->fmt.pix;
        if (!sp->width)
            sp->width = 64;
        if (!sp->height)
            sp->height = 64;
        if (!sp->pixelformat)
            sp->pixelformat = V4L2_TYPE_IS_OUTPUT(f->type) ?
                              V4L2_PIX_FMT_H264 : V4L2_PIX_FMT_NV12;
        if (!sp->bytesperline)
            sp->bytesperline = (sp->width + 1) & ~1u;
        if (!sp->sizeimage)
            sp->sizeimage = sp->bytesperline * sp->height * 3 / 2;
    }
}

int __wrap_ioctl(int fd, unsigned long request, ...)
{
    va_list ap;
    void *arg = NULL;

    pthread_mutex_lock(&model.mutex);
    model.ioctls++;
    unsigned slot = fd_slot(fd);
    struct model_fd *f = &model.fds[slot];
    /* MEDIA_REQUEST_IOC_QUEUE and REINIT take no argument. */
    if (request != MEDIA_REQUEST_IOC_REINIT && request != MEDIA_REQUEST_IOC_QUEUE) {
        va_start(ap, request);
        arg = va_arg(ap, void *);
        va_end(ap);
    }

    if (request == VIDIOC_QUERYCAP) {
        struct v4l2_capability *c = arg;
        memset(c, 0, sizeof(*c));
        c->capabilities = V4L2_CAP_VIDEO_M2M_MPLANE | V4L2_CAP_STREAMING;
        c->device_caps = c->capabilities;
        pthread_mutex_unlock(&model.mutex);
        return 0;
    }
    if (request == VIDIOC_ENUM_FMT) {
        struct v4l2_fmtdesc *d = arg;
        static const uint32_t output_formats[3] = {
            V4L2_PIX_FMT_H264, V4L2_PIX_FMT_HEVC, V4L2_PIX_FMT_VP9,
        };
        if (V4L2_TYPE_IS_OUTPUT(d->type)) {
            if (d->index >= 3) { errno = EINVAL; goto einval; }
            d->pixelformat = output_formats[d->index];
        } else {
            if (d->index >= 1) { errno = EINVAL; goto einval; }
            d->pixelformat = V4L2_PIX_FMT_NV12;
        }
        pthread_mutex_unlock(&model.mutex);
        return 0;
    }
    if (request == VIDIOC_ENUM_FRAMESIZES) {
        struct v4l2_frmsizeenum *d = arg;
        d->type = V4L2_FRMSIZE_TYPE_STEPWISE;
        d->stepwise.min_width = 16;
        d->stepwise.max_width = 4096;
        d->stepwise.step_width = 2;
        d->stepwise.min_height = 16;
        d->stepwise.max_height = 4096;
        d->stepwise.step_height = 2;
        pthread_mutex_unlock(&model.mutex);
        return 0;
    }
    if (request == VIDIOC_S_FMT || request == VIDIOC_G_FMT) {
        struct v4l2_format *fmt = arg;
        unsigned side = V4L2_TYPE_IS_OUTPUT(fmt->type);
        if (request == VIDIOC_G_FMT && f->formats[side].type)
            *fmt = f->formats[side];
        format_fill(fmt);
        f->formats[side] = *fmt;
        pthread_mutex_unlock(&model.mutex);
        return 0;
    }
    if (request == VIDIOC_CREATE_BUFS) {
        struct v4l2_create_buffers *b = arg;
        unsigned side = V4L2_TYPE_IS_OUTPUT(b->format.type);
        unsigned *count = side ? &f->outputs : &f->captures;

        b->capabilities = V4L2_BUF_CAP_SUPPORTS_REQUESTS;
        b->index = *count;
        assert(*count + b->count < (side ? 32u : V4L2R_MAX_CAPTURE_BUFFERS));
        if (b->count)
            f->formats[side] = b->format;
        *count += b->count;
        pthread_mutex_unlock(&model.mutex);
        return 0;
    }
    if (request == VIDIOC_QUERYBUF) {
        struct v4l2_buffer *b = arg;
        unsigned side = V4L2_TYPE_IS_OUTPUT(b->type);
        struct v4l2_format *fmt = &f->formats[side];
        uint32_t offset = side ? OUTPUT_OFFSET(b->index) : CAPTURE_OFFSET(b->index);
        size_t size = V4L2_TYPE_IS_MULTIPLANAR(b->type) ?
                      fmt->fmt.pix_mp.plane_fmt[0].sizeimage :
                      fmt->fmt.pix.sizeimage;
        struct plane *p;

        assert(size);
        p = plane_lookup_locked(slot, f->generation, offset, size);
        if (V4L2_TYPE_IS_MULTIPLANAR(b->type)) {
            b->length = 1;
            b->m.planes[0].length = (unsigned)size;
            b->m.planes[0].m.mem_offset = offset;
        } else {
            b->length = (unsigned)size;
            b->m.offset = offset;
        }
        if (side)
            f->output_mem[b->index] = p;
        else
            f->capture_mem[b->index] = p;
        pthread_mutex_unlock(&model.mutex);
        return 0;
    }
    if (request == MEDIA_IOC_REQUEST_ALLOC) {
        int nfd = new_fd_locked(FD_REQUEST);
        *(int *)arg = nfd;
        pthread_mutex_unlock(&model.mutex);
        return 0;
    }
    if (request == VIDIOC_EXPBUF) {
        struct v4l2_exportbuffer *e = arg;
        unsigned side = V4L2_TYPE_IS_OUTPUT(e->type);
        struct plane *p = side ? f->output_mem[e->index] : f->capture_mem[e->index];

        assert(p && p->live);
        int nfd = new_fd_locked(FD_DMABUF);
        model.fds[fd_slot(nfd)].alias = p;
        e->fd = nfd;
        pthread_mutex_unlock(&model.mutex);
        return 0;
    }
    if (request == VIDIOC_QBUF) {
        struct v4l2_buffer *b = arg;
        bool output = V4L2_TYPE_IS_OUTPUT(b->type);

        if (output) {
            unsigned bytesused = V4L2_TYPE_IS_MULTIPLANAR(b->type) ?
                                 b->m.planes[0].bytesused : b->bytesused;
            assert(b->index < 32);
            assert(!(f->output_queued & (1u << b->index)));
            assert(b->request_fd >= FD_BASE);
            unsigned rs = fd_slot(b->request_fd);
            assert(model.fds[rs].kind == FD_REQUEST);
            /* The driver encodes the target CAPTURE buffer in the
             * OUTPUT timestamp (v4l2r_surface_timestamp). */
            assert(b->timestamp.tv_usec >= 1 &&
                   b->timestamp.tv_usec <= V4L2R_MAX_CAPTURE_BUFFERS);
            model.fds[rs].request_video = (int)slot;
            model.fds[rs].request_output = (int)b->index;
            model.fds[rs].request_target = (unsigned)b->timestamp.tv_usec;
            model.fds[rs].request_bytesused = bytesused;
            f->output_queued |= 1u << b->index;
        } else {
            assert(!(f->capture_queued & (UINT64_C(1) << b->index)));
            f->capture_queued |= UINT64_C(1) << b->index;
            /* DMABUF capture queues name their memory; remember which
             * plane the decode will land in. */
            if (b->memory == V4L2_MEMORY_DMABUF) {
                int dfd = V4L2_TYPE_IS_MULTIPLANAR(b->type) ?
                         b->m.planes[0].m.fd : b->m.fd;
                assert(dfd >= FD_BASE);
                struct model_fd *d = &model.fds[fd_slot(dfd)];
                assert(d->kind == FD_DMABUF && d->alias && d->alias->live);
                f->capture_mem[b->index] = d->alias;
            }
        }
        pthread_mutex_unlock(&model.mutex);
        return 0;
    }
    if (request == MEDIA_REQUEST_IOC_QUEUE) {
        struct model_fd *r = f;
        struct plane *bitstream;
        uint64_t hash;

        assert(r->kind == FD_REQUEST);
        assert(r->request_video >= 0 && r->request_target >= 1);
        assert(!model.queue[slot].used);
        bitstream = model.fds[r->request_video].output_mem[r->request_output];
        assert(bitstream && bitstream->live && r->request_bytesused);
        hash = fnv1a64(bitstream->mem, r->request_bytesused);
        model.queue[slot] = (struct inflight) {
            .used = true,
            .held = atomic_load(&model_gate_closed),
            .video_slot = (unsigned)r->request_video,
            .output_index = r->request_output,
            .capture_index = (int)r->request_target - 1,
            .hash = hash,
            .complete_ns = model_now() +
                (uint64_t)model_delay(model.seed, hash) * TICK_NS,
        };
        pthread_mutex_unlock(&model.mutex);
        return 0;
    }
    if (request == MEDIA_REQUEST_IOC_REINIT) {
        if (f->kind == FD_REQUEST && f->request_video >= 0) {
            struct model_fd *video = &model.fds[f->request_video];

            video->output_queued &= ~(1u << f->request_output);
            video->output_ready &= ~(1u << f->request_output);
            model.queue[slot].used = false;
        }
        pthread_mutex_unlock(&model.mutex);
        return 0;
    }
    if (request == VIDIOC_DQBUF) {
        struct v4l2_buffer *b = arg;
        bool output = V4L2_TYPE_IS_OUTPUT(b->type);
        uint64_t ready;
        unsigned n;

        model_retire(model_now());
        ready = output ? f->output_ready : f->capture_ready;
        if (!ready) { errno = EAGAIN; goto einval; }
        n = (unsigned)__builtin_ctzll(ready);
        if (output) {
            f->output_ready &= ~(1u << n);
            f->output_queued &= ~(1u << n);
            b->index = n;
        } else {
            f->capture_ready &= ~(UINT64_C(1) << n);
            f->capture_queued &= ~(UINT64_C(1) << n);
            b->index = n;
            b->flags = 0;
        }
        pthread_mutex_unlock(&model.mutex);
        return 0;
    }
    if (request == VIDIOC_STREAMOFF) {
        enum v4l2_buf_type type = *(enum v4l2_buf_type *)arg;

        if (V4L2_TYPE_IS_OUTPUT(type)) {
            f->output_queued = 0;
            f->output_ready = 0;
            for (unsigned s = 0; s < model.fd_count; s++)
                if (model.queue[s].used && model.queue[s].video_slot == slot &&
                    (model.queue[s].output_index >= 0))
                    model.queue[s].used = false;
        } else {
            f->capture_queued = 0;
            f->capture_ready = 0;
            for (unsigned s = 0; s < model.fd_count; s++)
                if (model.queue[s].used && model.queue[s].video_slot == slot)
                    model.queue[s].used = false;
        }
        pthread_mutex_unlock(&model.mutex);
        return 0;
    }
    if (request == VIDIOC_REQBUFS || request == VIDIOC_STREAMON ||
        request == VIDIOC_S_EXT_CTRLS || request == VIDIOC_S_CTRL ||
        request == VIDIOC_S_SELECTION) {
        pthread_mutex_unlock(&model.mutex);
        return 0;
    }
    if (request == VIDIOC_QUERY_EXT_CTRL) {
        ((struct v4l2_query_ext_ctrl *)arg)->default_value = 1;
        pthread_mutex_unlock(&model.mutex);
        return 0;
    }
    fprintf(stderr, "concurrent-stress: unmodelled ioctl %#lx\n", request);
    abort();

einval:
    pthread_mutex_unlock(&model.mutex);
    return -1;
}

int __wrap_poll(struct pollfd *fds, nfds_t count, int timeout)
{
    uint64_t deadline;

    assert(count == 1);
    pthread_mutex_lock(&model.mutex);
    model.polls++;
    unsigned slot = fd_slot(fds[0].fd);
    struct model_fd *f = &model.fds[slot];
    deadline = timeout >= 0 ?
               model_now() + (uint64_t)timeout * TICK_NS : UINT64_MAX;
    for (;;) {
        uint64_t now, next;
        bool ready = false;

        model_retire(model_now());
        if (f->kind == FD_DMABUF) {
            /* Exported memory has no fences in the model. */
            ready = (fds[0].events & POLLOUT) != 0;
        } else if (f->kind == FD_VIDEO) {
            if ((fds[0].events & POLLIN) && f->capture_ready)
                ready = true;
            if ((fds[0].events & POLLOUT) && f->output_ready)
                ready = true;
            if (!ready && model_gate_holds(slot)) {
                /* Hold the caller inside the driver with bounded real
                 * sleeping instead of a busy spin, so the gate window
                 * cannot starve the threads that must fire the overlap
                 * events. The frozen model clock keeps the caller's own
                 * poll deadline from expiring meanwhile, and the fd is
                 * reported ready with nothing to dequeue: the driver's
                 * wait loop stays inside the locked entrypoint, which
                 * is exactly the controlled in-driver window. */
                pthread_mutex_unlock(&model.mutex);
                if (model_gate_sync_reader)
                    atomic_store(&model_gate_sync_waiting, true);
                usleep(500);
                fds[0].revents = fds[0].events;
                return 1;
            }
        } else {
            ready = true;
        }
        if (ready) {
            fds[0].revents = fds[0].events;
            pthread_mutex_unlock(&model.mutex);
            return 1;
        }
        now = model_now();
        next = model_next_deadline();
        if (next && next <= now)
            continue;
        if (next && (deadline == UINT64_MAX || next < deadline)) {
            atomic_store_explicit(&model_clock, next, memory_order_relaxed);
            continue;
        }
        if (deadline != UINT64_MAX && now < deadline) {
            atomic_store_explicit(&model_clock, deadline, memory_order_relaxed);
            continue;
        }
        assert(timeout >= 0); /* an endless wait with nothing pending is a hang */
        fds[0].revents = 0;
        pthread_mutex_unlock(&model.mutex);
        return 0;
    }
}

int __wrap___poll_chk(struct pollfd *fds, nfds_t count, int timeout, size_t size)
{
    assert(count <= size / sizeof(*fds));
    return __wrap_poll(fds, count, timeout);
}

int __wrap_clock_gettime(clockid_t clock, struct timespec *ts)
{
    if (clock == CLOCK_MONOTONIC) {
        /* The driver measures waits and deadlines on this clock; tick
         * the model forward a microsecond per read so duration
         * bookkeeping stays nonzero without real sleeping. While the
         * overlap gate is closed the clock is frozen: the gated
         * vaSyncSurface spins in a poll loop whose every iteration
         * reads the clock, and its own 2000 ms model deadline must not
         * run out while the harness collects the in-driver overlap
         * events. Nothing else needs model time with the gate closed
         * (held requests cannot complete, and other callers block on
         * api_mutex); the real-time watchdog still bounds the window. */
        uint64_t ns;

        if (atomic_load(&model_gate_closed))
            ns = atomic_load_explicit(&model_clock,
                                      memory_order_relaxed);
        else
            ns = atomic_fetch_add_explicit(&model_clock, 1000,
                                           memory_order_relaxed) + 1000;
        ts->tv_sec = (time_t)(ns / 1000000000ull);
        ts->tv_nsec = (long)(ns % 1000000000ull);
        return 0;
    }
    return __real_clock_gettime(clock, ts);
}

static void model_reset(uint64_t seed)
{
    pthread_mutex_lock(&model.mutex);
    for (unsigned i = 0; i < model.plane_count; i++)
        if (model.planes[i].live) {
            __real_munmap(model.planes[i].mem, model.planes[i].size);
            model.planes[i].live = false;
        }
    memset(model.fds, 0, sizeof(model.fds));
    memset(model.queue, 0, sizeof(model.queue));
    memset(model.planes, 0, sizeof(model.planes));
    model.fd_count = 0;
    model.plane_count = 0;
    model.seed = seed;
    model.ioctls = model.polls = model.completions = 0;
    atomic_store_explicit(&model_clock, 0, memory_order_relaxed);
    pthread_mutex_unlock(&model.mutex);
}

static void model_assert_idle(void)
{
    pthread_mutex_lock(&model.mutex);
    for (unsigned i = 0; i < model.fd_count; i++)
        assert(!model.fds[i].live && "model fd leaked");
    for (unsigned s = 0; s < model.fd_count; s++)
        assert(!model.queue[s].used && "request left in flight");
    pthread_mutex_unlock(&model.mutex);
}

/* ------------------------------------------------------------------ */
/* Model codecs and driver instance (real driver code, fake device).  */
/* ------------------------------------------------------------------ */

static VAStatus model_codec_init(struct v4l2r_context *ctx)
{
    int64_t value;

    return v4l2r_query_control_default(ctx, 1, &value) < 0 ?
           VA_STATUS_ERROR_OPERATION_FAILED : VA_STATUS_SUCCESS;
}

static VAStatus model_codec_render(struct v4l2r_context *ctx,
                                   struct v4l2r_buffer *buf)
{
    /* The slice bytes the client rendered become the bitstream the
     * model device hashes when the request is queued. */
    return v4l2r_append_output(ctx, buf->data, v4l2r_buffer_bytes(buf));
}

static VAStatus model_codec_end(struct v4l2r_context *ctx)
{
    struct v4l2_ext_control control = { .id = 1, .value = 1 };

    return v4l2r_decode(ctx, &control, 1, true, true);
}

static const struct v4l2r_codec model_codecs[3] = {
    { .name = "model-h264", .pixelformat = V4L2_PIX_FMT_H264,
      .init = model_codec_init, .render_buffer = model_codec_render,
      .end_picture = model_codec_end },
    { .name = "model-hevc", .pixelformat = V4L2_PIX_FMT_HEVC,
      .init = model_codec_init, .render_buffer = model_codec_render,
      .end_picture = model_codec_end },
    { .name = "model-vp9", .pixelformat = V4L2_PIX_FMT_VP9,
      .init = model_codec_init, .render_buffer = model_codec_render,
      .end_picture = model_codec_end },
};
static const VAProfile model_profiles[3] = {
    VAProfileH264Main, VAProfileHEVCMain, VAProfileVP9Profile0,
};
struct model_dim { unsigned width, height; };
static const struct model_dim model_dims[4] = {
    { 64, 48 }, { 64, 64 }, { 128, 96 }, { 96, 64 },
};

static struct v4l2r_driver drv;
static struct VADriverContext va_ctx;
static struct VADriverVTable table;

static void driver_setup(void)
{
    memset(&drv, 0, sizeof(drv));
    memset(&va_ctx, 0, sizeof(va_ctx));
    memset(&table, 0, sizeof(table));
    assert(!pthread_mutex_init(&drv.mutex, NULL));
    assert(!pthread_mutex_init(&drv.api_mutex, NULL));
    assert(!v4l2r_handles_init(&drv.configs, V4L2R_ID_OFFSET_CONFIG));
    assert(!v4l2r_handles_init(&drv.contexts, V4L2R_ID_OFFSET_CONTEXT));
    assert(!v4l2r_handles_init(&drv.surfaces, V4L2R_ID_OFFSET_SURFACE));
    assert(!v4l2r_handles_init(&drv.buffers, V4L2R_ID_OFFSET_BUFFER));
    assert(!v4l2r_handles_init(&drv.images, V4L2R_ID_OFFSET_IMAGE));
    drv.nb_decoders = 1;
    strcpy(drv.decoders[0].video_path, "model-video");
    strcpy(drv.decoders[0].media_path, "model-media");
    drv.decoders[0].nb_pixelformats = 3;
    drv.decoders[0].pixelformats[0] = V4L2_PIX_FMT_H264;
    drv.decoders[0].pixelformats[1] = V4L2_PIX_FMT_HEVC;
    drv.decoders[0].pixelformats[2] = V4L2_PIX_FMT_VP9;
    /* No format converter: keeps every surface on the decode path. */
    drv.converter_probed = true;
    drv.has_converter = false;
    va_ctx.pDriverData = &drv;
    v4l2r_lock_surface_api(&table);
}

static void driver_teardown(void)
{
    v4l2r_handles_destroy(&drv.configs);
    v4l2r_handles_destroy(&drv.contexts);
    v4l2r_handles_destroy(&drv.surfaces);
    v4l2r_handles_destroy(&drv.buffers);
    v4l2r_handles_destroy(&drv.images);
    pthread_mutex_destroy(&drv.mutex);
    pthread_mutex_destroy(&drv.api_mutex);
}

/* ------------------------------------------------------------------ */
/* Streams: one decoder thread and one reader thread per VA context.  */
/* ------------------------------------------------------------------ */

#define STREAM_SURFACES 4
#define MAX_STREAMS 4
#define MAX_FRAMES 256
/* Real-time watchdog budget per repetition. The model device never
 * sleeps, so this only fires on a genuine deadlock or livelock. */
#define REP_DEADLINE_SECONDS 60

enum schedule_id { SCHED_THREADS, SCHED_TEARDOWN, SCHED_FAILURE, SCHED_WORKER,
                   SCHED_OVERLAP, SCHED_OVERLAP_ACTOR };

struct stream {
    unsigned index;
    VAConfigID config;
    VAContextID context;
    unsigned width, height, rotate;
    VASurfaceID surfaces[STREAM_SURFACES];
    /* decoder -> reader publication */
    pthread_mutex_t lock;
    pthread_cond_t published_cv;
    unsigned published;
    bool decoder_done;
    /* reader -> destroyer survivor handshake. teardown_requested is set
     * just before vaDestroyContext (a decoder whose unlocked call then
     * names a freed context id attributes the rejection to teardown);
     * teardown_completed is set after it returns, and survivor reads
     * wait for it so they never race an in-flight destroy. at_midpoint
     * marks the teardown victim holding a staged picture open. */
    bool derive_ready;
    atomic_bool teardown_requested;
    atomic_bool teardown_completed;
    bool at_midpoint; /* under s->lock */
    pthread_cond_t destroyed_cv;
    bool reader_done;
    /* results */
    unsigned verified;
    unsigned char frame_hashes[MAX_FRAMES][8];
    /* Highest frame index the reader fully verified per surface; the
     * decoder waits for the previous use of a surface to be read back
     * before decoding into it again (valid client behavior, like an
     * FFmpeg surface pool; the kernel contract only covers decode
     * completion, not client readback). */
    int surface_last[STREAM_SURFACES];
};

struct rep_state {
    struct stream streams[MAX_STREAMS];
    unsigned n_streams, frames;
    /* Worker mode decodes one stream whose recipe index is the worker
     * id, so every process schedule worker has identifiable content. */
    unsigned base_index;
    uint64_t seed;
    enum schedule_id schedule;
    struct stream *victim; /* SCHED_TEARDOWN: stream destroyed mid-decode */
    /* SCHED_OVERLAP*: this stream's frame 0 is held by the model gate
     * while unlocked operations are proven to execute inside the
     * driver during its reader's spinning vaSyncSurface. */
    struct stream *gate_stream;
    /* In-driver overlap events the gate round must record before the
     * gate opens. */
    unsigned long overlap_target;
    bool late_waiter;
};

static struct rep_state rep;

/* Failure-actor coordination: stream 0's context must stay alive until
 * the actor's one-shot cross-context busy check completed. */
static _Atomic bool actor_one_shot_done;

static void stream_setup(struct stream *s, unsigned index, unsigned frames,
                         uint64_t seed)
{
    const struct model_dim *dim = &model_dims[index % 4];
    uint64_t rot = seed ^ (0x51edull ^ (uint64_t)index * 0x10001ull);

    memset(s, 0, sizeof(*s));
    atomic_init(&s->teardown_requested, false);
    atomic_init(&s->teardown_completed, false);
    for (unsigned k = 0; k < STREAM_SURFACES; k++)
        s->surface_last[k] = -1;
    s->index = index;
    s->width = dim->width;
    s->height = dim->height;
    s->rotate = (unsigned)(splitmix64(&rot) % STREAM_SURFACES);
    s->config = v4l2r_handles_alloc(&drv.configs, sizeof(struct v4l2r_config));
    assert(s->config != VA_INVALID_ID);
    struct v4l2r_config *config = V4L2R_CONFIG(&drv, s->config);
    config->codec = &model_codecs[index % 3];
    config->profile = model_profiles[index % 3];
    config->entrypoint = VAEntrypointVLD;
    assert(v4l2r_CreateSurfaces2(&va_ctx, VA_RT_FORMAT_YUV420, dim->width,
                                 dim->height, s->surfaces, STREAM_SURFACES,
                                 NULL, 0) == VA_STATUS_SUCCESS);
    assert(table.vaCreateContext(&va_ctx, s->config, (int)dim->width,
                                 (int)dim->height, 0, s->surfaces,
                                 STREAM_SURFACES, &s->context) ==
           VA_STATUS_SUCCESS);
    assert(pthread_mutex_init(&s->lock, NULL) == 0);
    assert(pthread_cond_init(&s->published_cv, NULL) == 0);
    assert(pthread_cond_init(&s->destroyed_cv, NULL) == 0);
    (void)frames;
}

/* Called after every thread of the stream has been joined. */
static void stream_teardown(struct stream *s)
{
    pthread_cond_destroy(&s->published_cv);
    pthread_cond_destroy(&s->destroyed_cv);
    pthread_mutex_destroy(&s->lock);
}

static void publish_frame(struct stream *s, unsigned frame)
{
    pthread_mutex_lock(&s->lock);
    if (frame + 1 > s->published)
        s->published = frame + 1;
    pthread_cond_broadcast(&s->published_cv);
    pthread_mutex_unlock(&s->lock);
}

static unsigned wait_published(struct stream *s, unsigned frame)
{
    unsigned published;

    pthread_mutex_lock(&s->lock);
    while (s->published <= frame && !s->decoder_done)
        pthread_cond_wait(&s->published_cv, &s->lock);
    published = s->published;
    pthread_mutex_unlock(&s->lock);
    return published;
}

static void stream_destroy_now(struct stream *s)
{
    /* teardown_requested goes up before the call so a decoder whose
     * call then names a freed context id attributes the rejection to
     * this teardown; teardown_completed goes up after the call returns
     * and is what survivor reads wait for. */
    atomic_store(&s->teardown_requested, true);
    assert(table.vaDestroyContext(&va_ctx, s->context) == VA_STATUS_SUCCESS);
    atomic_store(&s->teardown_completed, true);
    pthread_mutex_lock(&s->lock);
    pthread_cond_broadcast(&s->destroyed_cv);
    pthread_mutex_unlock(&s->lock);
}

/* Coordinate caller start times only. This is outside the driver and does
 * not measure overlapping execution inside API entrypoints. */
static _Atomic unsigned api_rendezvous_arrived;

/* Failure-actor setup barrier for the overlap-actor round: the gate
 * decoder waits for this before closing the model gate, so the actor's
 * context setup can never block on the gated in-driver section. */
static _Atomic bool actor_setup_done;

static void api_rendezvous_decoders(void)
{
    atomic_fetch_add(&api_rendezvous_arrived, 1);
    while (atomic_load(&api_rendezvous_arrived) < rep.n_streams)
        usleep(50);
}

/* Fire unlocked entrypoints into the gated in-driver section until the
 * driver has recorded the required number of in-driver overlap events.
 * Only unlocked calls belong here: every locked entrypoint would block
 * on the api_mutex the gated section holds (that blocking is itself the
 * serialization the round observes). */
static void fire_overlap_unlocked_ops(struct stream *s)
{
    VAImageFormat format = { .fourcc = VA_FOURCC_NV12 };
    unsigned char bytes[FRAME_BYTES] = { 0 };

    unsigned long before = v4l2r_overlap_thread_events();

    while (v4l2r_overlap_thread_events() - before < rep.overlap_target) {
        VABufferID id;
        VAImage image;
        VASurfaceID surfaces[2];
        void *data;

        assert(v4l2r_CreateBuffer(&va_ctx, s->context,
                                 VASliceDataBufferType, FRAME_BYTES, 1,
                                 bytes, &id) == VA_STATUS_SUCCESS);
        assert(v4l2r_MapBuffer(&va_ctx, id, &data) == VA_STATUS_SUCCESS);
        assert(v4l2r_UnmapBuffer(&va_ctx, id) == VA_STATUS_SUCCESS);
        assert(v4l2r_DestroyBuffer(&va_ctx, id) == VA_STATUS_SUCCESS);
        assert(v4l2r_CreateImage(&va_ctx, &format, 64, 48, &image) ==
               VA_STATUS_SUCCESS);
        assert(v4l2r_DestroyImage(&va_ctx, image.image_id) ==
               VA_STATUS_SUCCESS);
        assert(v4l2r_CreateSurfaces2(&va_ctx, VA_RT_FORMAT_YUV420, 64, 48,
                                    surfaces, 2, NULL, 0) ==
               VA_STATUS_SUCCESS);
    }
    atomic_store(&model_gate_proven_events,
                 v4l2r_overlap_thread_events() - before);
}

static void *decoder_thread(void *arg)
{
    struct stream *s = arg;
    bool expect_abort = rep.victim == s;
    unsigned frame;

    for (frame = 0; frame < rep.frames; frame++) {
        unsigned char bytes[FRAME_BYTES];
        unsigned surface = (frame + s->rotate) % STREAM_SURFACES;
        VASurfaceID sid = s->surfaces[surface];
        VABufferID slice;
        VAStatus st;

        /* Valid client reuse discipline: wait until the reader has read
         * back this surface's previous frame before decoding into it
         * again (a real decoder pool does the same). Teardown of this
         * context releases the wait. */
        if (frame >= STREAM_SURFACES) {
            pthread_mutex_lock(&s->lock);
            while (s->surface_last[surface] <
                           (int)frame - (int)STREAM_SURFACES &&
                   !atomic_load(&s->teardown_requested))
                pthread_cond_wait(&s->published_cv, &s->lock);
            pthread_mutex_unlock(&s->lock);
            if (atomic_load(&s->teardown_requested))
                break;
        }
        if (frame == 0) {
            api_rendezvous_decoders();
            if (rep.gate_stream == s) {
                /* The overlap gate closes after the rendezvous so every
                 * request queued from here on is held: this stream's
                 * frame 0 cannot complete, and its reader's
                 * vaSyncSurface will spin inside the driver. */
                if (rep.schedule == SCHED_OVERLAP_ACTOR) {
                    while (!atomic_load(&actor_setup_done))
                        usleep(50);
                }
                atomic_store(&model_gate_closed, true);
            } else if (rep.gate_stream) {
                /* Only the gate stream may have a request held. Any
                 * other reader that synced a held request would spin
                 * inside the driver holding api_mutex while the gate
                 * decoder still needs that mutex to finish its own
                 * gated picture — a circular deadlock. Wait for the
                 * latched release: a transient close/open pulse can be
                 * missed entirely by a descheduled worker. */
                /* Regression schedule: deliberately arrive after the
                 * entire close/open pulse, as a descheduled caller can. */
                if (rep.late_waiter)
                    while (!atomic_load(&model_gate_opened))
                        usleep(50);
                while (!atomic_load(&model_gate_opened))
                    usleep(50);
            }
        }
        st = table.vaBeginPicture(&va_ctx, s->context, sid);
        if (st != VA_STATUS_SUCCESS) {
            /* Only a concurrent teardown of this context may abort a
             * valid sequence; anything else is a driver defect. */
            assert(expect_abort);
            assert(st == VA_STATUS_ERROR_INVALID_CONTEXT);
            assert(atomic_load(&s->teardown_requested));
            break;
        }
        if (expect_abort && frame == rep.frames / 2) {
            /* Deterministic mid-picture teardown window: hold the staged
             * picture open until the destroyer has torn the context
             * down, so the destroy provably lands between BeginPicture
             * and the buffer creation (the reviewer-reproduced race),
             * every repetition. */
            pthread_mutex_lock(&s->lock);
            s->at_midpoint = true;
            pthread_cond_broadcast(&s->published_cv);
            while (!atomic_load(&s->teardown_completed))
                pthread_cond_wait(&s->destroyed_cv, &s->lock);
            pthread_mutex_unlock(&s->lock);
        }
        frame_bytes(rep.seed, s->index, frame, bytes);
        st = v4l2r_CreateBuffer(&va_ctx, s->context, VASliceDataBufferType,
                                FRAME_BYTES, 1, bytes, &slice);
        if (st != VA_STATUS_SUCCESS) {
            /* The unlocked CreateBuffer names the context by id; a
             * concurrent teardown of this context can free it between
             * BeginPicture and here (deterministically at the midpoint
             * above). No buffer was created, so there is nothing to
             * clean up. Any other failure is a driver defect. */
            assert(expect_abort);
            assert(st == VA_STATUS_ERROR_INVALID_CONTEXT);
            assert(atomic_load(&s->teardown_requested));
            break;
        }
        st = table.vaRenderPicture(&va_ctx, s->context, &slice, 1);
        if (st == VA_STATUS_SUCCESS)
            st = table.vaEndPicture(&va_ctx, s->context);
        if (st != VA_STATUS_SUCCESS) {
            assert(expect_abort);
            assert(st == VA_STATUS_ERROR_INVALID_CONTEXT);
            assert(atomic_load(&s->teardown_requested));
            /* Client-owned buffer: stays destroyable across teardown. */
            assert(v4l2r_DestroyBuffer(&va_ctx, slice) == VA_STATUS_SUCCESS);
            break;
        }
        assert(v4l2r_DestroyBuffer(&va_ctx, slice) == VA_STATUS_SUCCESS);
        publish_frame(s, frame);
        if (rep.gate_stream == s && frame == 0) {
            /* The reader's vaSyncSurface for the held frame is now
             * spinning inside the driver, holding api_mutex: a real
             * in-driver locked section, not a caller-side window.
             * Record the required in-driver overlap events against it
             * (this decoder fires them in the overlap round; the
             * failure actor does in the overlap-actor round), then open
             * the gate so the held request completes. */
            while (!atomic_load(&model_gate_sync_waiting))
                usleep(50);
            if (rep.schedule == SCHED_OVERLAP)
                fire_overlap_unlocked_ops(s);
            else
                while (atomic_load(&model_gate_proven_events) < rep.overlap_target)
                    usleep(50);
            atomic_store(&model_gate_closed, false);
            atomic_store(&model_gate_opened, true);
        }
    }
    pthread_mutex_lock(&s->lock);
    s->decoder_done = true;
    pthread_cond_broadcast(&s->published_cv);
    pthread_mutex_unlock(&s->lock);
    return NULL;
}

/* Verify the pattern bytes for one frame and record its hash. */
static void verify_frame(struct stream *s, unsigned frame,
                         const unsigned char *data)
{
    unsigned char bytes[FRAME_BYTES];
    size_t plane = (size_t)s->width * s->height * 3 / 2;
    unsigned char *expected = malloc(plane);
    uint64_t hash;

    assert(expected);
    frame_bytes(rep.seed, s->index, frame, bytes);
    hash = fnv1a64(bytes, FRAME_BYTES);
    frame_pattern(hash, expected, plane);
    assert(!memcmp(data, expected, plane) && "wrong pixels: lost frame, "
           "cross-stream content or stale buffer reuse");
    memcpy(s->frame_hashes[frame], &hash, 8);
    free(expected);
}

/* Same verification for a client image whose rows are strided by pitch
 * (vaCreateImage aligns the pitch to 64 bytes, so pitch may exceed the
 * surface width). The source rows keep the surface's own line length. */
static void verify_frame_strided(struct stream *s, unsigned frame,
                                 const unsigned char *data, unsigned pitch)
{
    unsigned char bytes[FRAME_BYTES];
    size_t plane = (size_t)s->width * s->height * 3 / 2;
    unsigned char *expected = malloc(plane);
    uint64_t hash;

    assert(expected && pitch >= s->width);
    frame_bytes(rep.seed, s->index, frame, bytes);
    hash = fnv1a64(bytes, FRAME_BYTES);
    frame_pattern(hash, expected, plane);
    for (unsigned rows = 0; rows < s->height * 3 / 2; rows++) {
        const unsigned char *row = data + (size_t)rows * pitch;
        const unsigned char *want = expected + (size_t)rows * s->width;

        assert(!memcmp(row, want, s->width) && "wrong pixels: lost frame, "
               "cross-stream content or stale buffer reuse");
    }
    memcpy(s->frame_hashes[frame], &hash, 8);
    free(expected);
}

static void readback_export(struct stream *s, unsigned frame)
{
    VADRMPRIMESurfaceDescriptor desc;
    void *map;

    VAStatus st = table.vaExportSurfaceHandle(&va_ctx,
            s->surfaces[(frame + s->rotate) % STREAM_SURFACES],
            VA_SURFACE_ATTRIB_MEM_TYPE_DRM_PRIME_2, 0, &desc);
    assert(st == VA_STATUS_SUCCESS);
    assert(desc.num_objects >= 1);
    map = mmap(NULL, (size_t)s->width * s->height * 3 / 2, PROT_READ,
               MAP_SHARED, desc.objects[0].fd, 0);
    assert(map != MAP_FAILED);
    verify_frame(s, frame, map);
    assert(munmap(map, (size_t)s->width * s->height * 3 / 2) == 0);
    assert(close(desc.objects[0].fd) == 0);
}

static void readback_image(struct stream *s, unsigned frame, VAImage *image)
{
    void *data;

    VAStatus st = table.vaGetImage(&va_ctx,
            s->surfaces[(frame + s->rotate) % STREAM_SURFACES], 0, 0,
            (int)s->width, (int)s->height, image->image_id);
    assert(st == VA_STATUS_SUCCESS);
    assert(v4l2r_MapBuffer(&va_ctx, image->buf, &data) == VA_STATUS_SUCCESS);
    verify_frame_strided(s, frame, data, image->pitches[0]);
    assert(v4l2r_UnmapBuffer(&va_ctx, image->buf) == VA_STATUS_SUCCESS);
}

static void *reader_thread(void *arg)
{
    struct stream *s = arg;
    VAImageFormat format = { .fourcc = VA_FOURCC_NV12 };
    VAImage image = { 0 };
    unsigned frame;

    /* Unlocked entrypoint exercised concurrently with other threads'
     * locked decode calls. vaCreateImage aligns the pitch up to 64
     * bytes, so it can exceed the surface width. */
    assert(v4l2r_CreateImage(&va_ctx, &format, (int)s->width,
                             (int)s->height, &image) == VA_STATUS_SUCCESS);
    assert(image.pitches[0] >= s->width);

    for (frame = 0; frame < rep.frames; frame++) {
        VASurfaceID sid;
        VAStatus st;
        uint64_t choose = rep.seed ^ (uint64_t)s->index * 0x9e3779b9ull ^
                          (uint64_t)frame * 0x85ebca6bull;

        if (wait_published(s, frame) <= frame)
            break; /* decoder stopped early (teardown victim) */
        sid = s->surfaces[(frame + s->rotate) % STREAM_SURFACES];
        model_gate_sync_reader = rep.gate_stream == s && frame == 0;
        st = table.vaSyncSurface(&va_ctx, sid);
        model_gate_sync_reader = false;
        if (st != VA_STATUS_SUCCESS) {
            /* Only the mid-decode teardown victim may lose a frame;
             * everything already published must complete. */
            assert(rep.victim == s);
            assert(atomic_load(&s->teardown_requested));
            assert(frame >= rep.frames / 2);
            break;
        }
        if (splitmix64(&choose) & 1)
            readback_export(s, frame);
        else
            readback_image(s, frame, &image);
        pthread_mutex_lock(&s->lock);
        s->verified = frame + 1;
        s->surface_last[(frame + s->rotate) % STREAM_SURFACES] = (int)frame;
        /* published_cv doubles as the stream progress cond: the failure
         * actor waits for the first verified frame of stream 0, and the
         * decoder waits for a surface's previous frame to be read. */
        pthread_cond_broadcast(&s->published_cv);
        pthread_mutex_unlock(&s->lock);
    }

    if (rep.victim != s) {
        /* Survivor checks: derive an image on the last decoded surface
         * while the context is alive, let the destroyer tear the
         * context down, then read everything back through the
         * preserved state. */
        VAImage derived = { 0 };
        VASurfaceID sid = s->surfaces[(rep.frames - 1 + s->rotate) %
                                      STREAM_SURFACES];
        void *data;
        VAStatus st;

        st = table.vaDeriveImage(&va_ctx, sid, &derived);
        assert(st == VA_STATUS_SUCCESS);
        assert(v4l2r_MapBuffer(&va_ctx, derived.buf, &data) ==
               VA_STATUS_SUCCESS);
        verify_frame(s, rep.frames - 1, data);
        assert(v4l2r_UnmapBuffer(&va_ctx, derived.buf) == VA_STATUS_SUCCESS);
        pthread_mutex_lock(&s->lock);
        s->derive_ready = true;
        pthread_cond_broadcast(&s->destroyed_cv);
        /* Survivor reads wait for the completed teardown, never for a
         * merely requested one. */
        while (!atomic_load(&s->teardown_completed))
            pthread_cond_wait(&s->destroyed_cv, &s->lock);
        pthread_mutex_unlock(&s->lock);
        /* Post-teardown survivors (regression.c context-lifetime). */
        st = table.vaSyncSurface(&va_ctx, sid);
        assert(st == VA_STATUS_SUCCESS);
        readback_image(s, rep.frames - 1, &image);
        assert(v4l2r_MapBuffer(&va_ctx, derived.buf, &data) ==
               VA_STATUS_SUCCESS);
        verify_frame(s, rep.frames - 1, data);
        assert(v4l2r_UnmapBuffer(&va_ctx, derived.buf) == VA_STATUS_SUCCESS);
        assert(v4l2r_DestroyImage(&va_ctx, derived.image_id) ==
               VA_STATUS_SUCCESS);
    } else {
        /* Teardown victim: everything verified before the destroy must
         * still be readable afterwards. With the forced midpoint the
         * verified count is exactly half the frames. */
        pthread_mutex_lock(&s->lock);
        unsigned verified = s->verified;
        pthread_mutex_unlock(&s->lock);
        assert(verified >= 1);
        readback_image(s, verified - 1, &image);
    }
    assert(v4l2r_DestroyImage(&va_ctx, image.image_id) == VA_STATUS_SUCCESS);
    pthread_mutex_lock(&s->lock);
    s->reader_done = true;
    pthread_cond_broadcast(&s->destroyed_cv);
    pthread_mutex_unlock(&s->lock);
    return NULL;
}

static void *destroyer_thread(void *arg)
{
    unsigned i;

    (void)arg;
    /* Stream 0's context must outlive the failure actor's one-shot
     * cross-context busy check (the actor sets the flag when there is
     * no actor). */
    while (!atomic_load(&actor_one_shot_done)) {
        struct timespec pause = { 0, 100000 };

        nanosleep(&pause, NULL);
    }
    /* SCHED_TEARDOWN: destroy the victim while it provably holds a
     * staged picture open at its midpoint (deterministic mid-decode
     * teardown; the victim signals at_midpoint after BeginPicture of
     * frame frames/2 and waits for the completed destroy). */
    if (rep.victim) {
        struct stream *victim = rep.victim;

        pthread_mutex_lock(&victim->lock);
        while (!victim->at_midpoint && !victim->decoder_done)
            pthread_cond_wait(&victim->published_cv, &victim->lock);
        pthread_mutex_unlock(&victim->lock);
        assert(victim->at_midpoint);
        stream_destroy_now(victim);
    }
    /* Every other context is destroyed in stream order while later
     * streams are still decoding (the concurrent version of the
     * shared-contexts.sh early teardown). */
    for (i = 0; i < rep.n_streams; i++) {
        struct stream *s = &rep.streams[i];

        if (s == rep.victim)
            continue;
        pthread_mutex_lock(&s->lock);
        while (!s->derive_ready && !s->reader_done)
            pthread_cond_wait(&s->destroyed_cv, &s->lock);
        pthread_mutex_unlock(&s->lock);
        if (!atomic_load(&s->teardown_completed))
            stream_destroy_now(s);
    }
    return NULL;
}

/* The failure actor's own valid objects. Per-context picture sequencing
 * (Begin/Render/EndPicture) belongs to one client thread per context in
 * VA-API, so the misbehaving client uses its OWN context for sequencing
 * faults and never injects them into a stream's live context; it only
 * shares the driver-wide tables, exactly like a second client process
 * would. */
struct actor_objects {
    VAConfigID config;
    VAContextID context;
    VASurfaceID surface;
};

/* One injected client-level failure. Every fault must be rejected
 * without disturbing any valid stream; none of them stage a picture on
 * a context that a valid thread is using. The cross-context busy check
 * is a one-shot in actor_thread, not part of this rotation. */
static void actor_fault(const struct actor_objects *own, uint64_t *rng)
{
    switch ((unsigned)(splitmix64(rng) % 9)) {
    case 0: {
        VAContextID ctx;

        assert(table.vaCreateContext(&va_ctx, (VAConfigID)0xdead0001,
                                     64, 64, 0, NULL, 0, &ctx) !=
               VA_STATUS_SUCCESS);
        break;
    }
    case 1: {
        VABufferID bogus = (VABufferID)0x8000beef;

        assert(table.vaRenderPicture(&va_ctx, own->context, &bogus, 1) !=
               VA_STATUS_SUCCESS);
        break;
    }
    case 2:
        assert(table.vaEndPicture(&va_ctx, own->context) !=
               VA_STATUS_SUCCESS);
        break;
    case 3: {
        VASurfaceStatus status;

        assert(table.vaQuerySurfaceStatus(&va_ctx,
               (VASurfaceID)0x4000beef, &status) != VA_STATUS_SUCCESS);
        break;
    }
    case 4: {
        void *data;

        assert(v4l2r_MapBuffer(&va_ctx, (VABufferID)0x8000beef, &data) !=
               VA_STATUS_SUCCESS);
        break;
    }
    case 5: {
        /* Unlocked surface-table churn against the shared driver.
         * Double destroys are deliberately NOT part of the concurrent
         * rotation: the handle namespace is shared, so a freed id can
         * legitimately be reused by another thread between the two
         * calls (which would make the second destroy succeed and take
         * someone else's object). The deterministic double-destroy
         * semantics are owned by the single-threaded api-lifecycle
         * cases. */
        VASurfaceID surfaces[2];

        assert(v4l2r_CreateSurfaces2(&va_ctx, VA_RT_FORMAT_YUV420, 64, 48,
                                     surfaces, 2, NULL, 0) ==
               VA_STATUS_SUCCESS);
        assert(table.vaDestroySurfaces(&va_ctx, surfaces, 2) ==
               VA_STATUS_SUCCESS);
        break;
    }
    case 6: {
        VAImageFormat format = { .fourcc = VA_FOURCC_NV12 };
        VAImage image;

        assert(v4l2r_CreateImage(&va_ctx, &format, 64, 48, &image) ==
               VA_STATUS_SUCCESS);
        assert(v4l2r_DestroyImage(&va_ctx, image.image_id) ==
               VA_STATUS_SUCCESS);
        break;
    }
    case 7: {
        /* Context-table churn concurrent with live decode contexts. */
        VAContextID ctx;

        assert(table.vaCreateContext(&va_ctx, own->config, 64, 48, 0,
                                     NULL, 0, &ctx) == VA_STATUS_SUCCESS);
        assert(table.vaDestroyContext(&va_ctx, ctx) == VA_STATUS_SUCCESS);
        break;
    }
    default: {
        /* Unlocked buffer-table operations against a context that stays
         * alive for the whole actor run (a stream context may already
         * have been destroyed by the concurrent destroyer). */
        VABufferID id;
        unsigned char bytes[FRAME_BYTES] = { 0 };

        assert(v4l2r_CreateBuffer(&va_ctx, own->context,
                                 VASliceDataBufferType, FRAME_BYTES, 1,
                                 bytes, &id) == VA_STATUS_SUCCESS);
        assert(v4l2r_DestroyBuffer(&va_ctx, id) == VA_STATUS_SUCCESS);
        break;
    }
    }
}

/* Unlocked-only fault for the gated overlap window: while the gated
 * vaSyncSurface holds api_mutex inside the driver, every locked
 * entrypoint (including the actor's) would block on it — which is the
 * serialization being observed, not a fault the actor can deliver. */
static void actor_unlocked_fault(const struct actor_objects *own, uint64_t *rng)
{
    VAImageFormat format = { .fourcc = VA_FOURCC_NV12 };
    VAImage image;
    VASurfaceID surfaces[2];
    VABufferID id;
    unsigned char bytes[FRAME_BYTES] = { 0 };
    void *data;

    switch ((unsigned)(splitmix64(rng) % 4)) {
    case 0:
        assert(v4l2r_MapBuffer(&va_ctx, (VABufferID)0x8000beef, &data) !=
               VA_STATUS_SUCCESS);
        break;
    case 1:
        assert(v4l2r_CreateImage(&va_ctx, &format, 64, 48, &image) ==
               VA_STATUS_SUCCESS);
        assert(v4l2r_DestroyImage(&va_ctx, image.image_id) ==
               VA_STATUS_SUCCESS);
        break;
    case 2:
        /* No DestroySurfaces here: it is a locked entrypoint. The
         * surfaces free with the handle table at repetition end. */
        assert(v4l2r_CreateSurfaces2(&va_ctx, VA_RT_FORMAT_YUV420, 64, 48,
                                    surfaces, 2, NULL, 0) ==
               VA_STATUS_SUCCESS);
        break;
    default:
        assert(v4l2r_CreateBuffer(&va_ctx, own->context,
                                 VASliceDataBufferType, FRAME_BYTES, 1,
                                 bytes, &id) == VA_STATUS_SUCCESS);
        assert(v4l2r_DestroyBuffer(&va_ctx, id) == VA_STATUS_SUCCESS);
        break;
    }
}

static void *actor_thread(void *arg)
{
    struct actor_objects own;
    uint64_t rng = rep.seed ^ 0x0f001cafeull;
    unsigned faults = 0;
    VAStatus st;

    (void)arg;

    /* Setup before anything else: in the overlap-actor round the gate
     * decoder waits for actor_setup_done before closing the model
     * gate, so none of these calls can block on the gated in-driver
     * section. */
    own.config = v4l2r_handles_alloc(&drv.configs,
                                     sizeof(struct v4l2r_config));
    assert(own.config != VA_INVALID_ID);
    struct v4l2r_config *config = V4L2R_CONFIG(&drv, own.config);
    config->codec = &model_codecs[0];
    config->profile = model_profiles[0];
    config->entrypoint = VAEntrypointVLD;
    assert(v4l2r_CreateSurfaces2(&va_ctx, VA_RT_FORMAT_YUV420, 64, 48,
                                 &own.surface, 1, NULL, 0) == VA_STATUS_SUCCESS);
    assert(table.vaCreateContext(&va_ctx, own.config, 64, 48, 0,
                                 &own.surface, 1, &own.context) ==
           VA_STATUS_SUCCESS);
    atomic_store(&actor_setup_done, true);

    if (rep.schedule == SCHED_OVERLAP_ACTOR) {
        /* Wait until the chosen reader is polling inside SyncSurface
         * with api_mutex held, not merely until the model gate closes. */
        struct timespec pace = { 0, 100000 };

        while (!atomic_load(&model_gate_sync_waiting))
            nanosleep(&pace, NULL);
        unsigned long before = v4l2r_overlap_thread_events();
        void *invalid_data;
        /* Guarantee a failing actor operation in the held sync window;
         * random valid object churn is not itself a failure actor. */
        assert(v4l2r_MapBuffer(&va_ctx, (VABufferID)0x8000beef,
                              &invalid_data) == VA_STATUS_ERROR_INVALID_BUFFER);
        faults++;
        /* While the gate is closed, this actor's unlocked operations
         * are the ones executing inside the driver during the gated
         * locked section — the in-driver failure-actor overlap this
         * round proves. The gate decoder opens the gate once the
         * driver has recorded the required events. */
        while (v4l2r_overlap_thread_events() - before < rep.overlap_target) {
            actor_unlocked_fault(&own, &rng);
            faults++;
            nanosleep(&pace, NULL);
        }
        atomic_store(&model_gate_proven_events,
                     v4l2r_overlap_thread_events() - before);
        while (!atomic_load(&model_gate_opened))
            nanosleep(&pace, NULL);
    }

    /* The foreign-surface busy check needs stream 0's first surface
     * bound to its context (its first frame verified). It is a one-shot
     * right after that point: once stream 0's context is destroyed the
     * surface detaches, and a BeginPicture on a detached surface would
     * legitimately stage a picture on this context instead of failing
     * busy. */
    pthread_mutex_lock(&rep.streams[0].lock);
    while (rep.streams[0].verified < 1 && !rep.streams[0].reader_done)
        pthread_cond_wait(&rep.streams[0].published_cv, &rep.streams[0].lock);
    pthread_mutex_unlock(&rep.streams[0].lock);

    /* Keep stream 0 alive until this check completes. This proves lifetime
     * ordering, not simultaneous progress inside another API call. */
    st = table.vaBeginPicture(&va_ctx, own.context,
           rep.streams[0].surfaces[rep.streams[0].rotate]);
    assert(st == VA_STATUS_ERROR_SURFACE_BUSY);
    faults++;
    /* Release the destroyer (and record it) only after the check. */
    atomic_store(&actor_one_shot_done, true);

    for (;;) {
        bool all_done = true;
        struct timespec pause = { 0, 200000 };

        for (unsigned i = 0; i < rep.n_streams; i++) {
            pthread_mutex_lock(&rep.streams[i].lock);
            if (!rep.streams[i].reader_done)
                all_done = false;
            pthread_mutex_unlock(&rep.streams[i].lock);
        }
        if (all_done)
            break;
        /* A reader has not finished; it may be waiting for teardown. */
        actor_fault(&own, &rng);
        faults++;
        nanosleep(&pause, NULL);
    }
    assert(table.vaDestroyContext(&va_ctx, own.context) == VA_STATUS_SUCCESS);
    assert(table.vaDestroySurfaces(&va_ctx, &own.surface, 1) ==
           VA_STATUS_SUCCESS);
    printf("actor faults=%u (one-shot busy check completed before stream 0 teardown)\n",
           faults);
    return NULL;
}

static _Atomic bool rep_done;
static _Atomic uint64_t rep_deadline;

static void *watchdog_thread(void *arg)
{
    struct timespec now;

    (void)arg;
    for (;;) {
        if (atomic_load_explicit(&rep_done, memory_order_acquire))
            return NULL;
        assert(__real_clock_gettime(CLOCK_MONOTONIC, &now) == 0);
        if ((uint64_t)now.tv_sec >= atomic_load(&rep_deadline)) {
            fprintf(stderr, "concurrent-stress: repetition deadline "
                    "breached (deadlock or livelock)\n");
            abort();
        }
        usleep(200000);
    }
}

static void print_stream_result(struct stream *s)
{
    unsigned char digest[16];
    char hex[33];
    unsigned frames;

    pthread_mutex_lock(&s->lock);
    frames = s->verified;
    pthread_mutex_unlock(&s->lock);
    md5((const unsigned char *)s->frame_hashes, (size_t)frames * 8, digest);
    md5_hex(digest, hex);
    printf("stream %u frames=%u MD5=%s\n", s->index, frames, hex);
}

static void run_rep(void)
{
    pthread_t threads[MAX_STREAMS * 2 + 3];
    unsigned n_threads = 0;
    struct timespec now;
    struct v4l2r_overlap_stats overlap;

    driver_setup();
    model_reset(rep.seed);
    atomic_store(&actor_one_shot_done, rep.schedule != SCHED_FAILURE &&
                 rep.schedule != SCHED_OVERLAP_ACTOR);
    atomic_store(&actor_setup_done, false);
    atomic_store(&model_gate_closed, false);
    atomic_store(&model_gate_opened, false);
    atomic_store(&model_gate_sync_waiting, false);
    atomic_store(&model_gate_proven_events, 0);
    atomic_store(&api_rendezvous_arrived, 0);
    /* In-driver concurrency instrumentation on for the whole
     * repetition: every schedule asserts the serialization invariant,
     * and the overlap rounds assert the in-driver overlap events. */
    v4l2r_overlap_configure(true);
    for (unsigned i = 0; i < rep.n_streams; i++)
        stream_setup(&rep.streams[i], rep.base_index + i, rep.frames,
                     rep.seed);

    atomic_store_explicit(&rep_done, false, memory_order_release);
    assert(__real_clock_gettime(CLOCK_MONOTONIC, &now) == 0);
    atomic_store(&rep_deadline, (uint64_t)now.tv_sec + REP_DEADLINE_SECONDS);

    assert(pthread_create(&threads[n_threads++], NULL,
                          watchdog_thread, NULL) == 0);
    for (unsigned i = 0; i < rep.n_streams; i++)
        assert(pthread_create(&threads[n_threads++], NULL,
                              decoder_thread, &rep.streams[i]) == 0);
    for (unsigned i = 0; i < rep.n_streams; i++)
        assert(pthread_create(&threads[n_threads++], NULL,
                              reader_thread, &rep.streams[i]) == 0);
    assert(pthread_create(&threads[n_threads++], NULL,
                          destroyer_thread, NULL) == 0);
    if (rep.schedule == SCHED_FAILURE || rep.schedule == SCHED_OVERLAP_ACTOR)
        assert(pthread_create(&threads[n_threads++], NULL,
                              actor_thread, NULL) == 0);

    /* The watchdog is joined last, after rep_done is set. */
    for (unsigned i = 1; i < n_threads; i++)
        assert(pthread_join(threads[i], NULL) == 0);
    atomic_store_explicit(&rep_done, true, memory_order_release);
    assert(pthread_join(threads[0], NULL) == 0);

    for (unsigned i = 0; i < rep.n_streams; i++) {
        struct stream *s = &rep.streams[i];

        assert(atomic_load(&s->teardown_completed));
        assert(s->reader_done);
        if (rep.victim == s) {
            /* The forced midpoint makes the victim's verified count
             * exact: every frame published before the staged picture
             * was torn down completes and verifies byte-exact. */
            assert(s->verified == rep.frames / 2);
        } else {
            assert(s->verified == rep.frames);
        }
    }

    /* Driver-side concurrency evidence (AC2): api_mutex must serialize
     * the locked entrypoints (max one active section, every schedule),
     * and the overlap rounds require the gated locked section to have
     * executed in the driver concurrently with the required number of
     * unlocked entrypoint executions. */
    overlap = v4l2r_overlap_snapshot();
    assert(overlap.max_locked_active == 1);
    if (rep.schedule == SCHED_OVERLAP || rep.schedule == SCHED_OVERLAP_ACTOR) {
        assert(atomic_load(&model_gate_sync_waiting));
        assert(atomic_load(&model_gate_proven_events) >= rep.overlap_target);
        assert(overlap.unlocked_over_locked >= rep.overlap_target);
        assert(overlap.max_unlocked_during_locked >= 1);
    }
    v4l2r_overlap_configure(false);

    pthread_mutex_lock(&model.mutex);
    printf("rep ioctls=%u polls=%u completions=%u "
           "driver_overlap locked=%lu unlocked=%lu over=%lu "
           "max_locked=%u max_unl=%u gated_thread_events=%lu\n",
           model.ioctls, model.polls, model.completions,
           overlap.locked_sections, overlap.unlocked_calls,
           overlap.unlocked_over_locked, overlap.max_locked_active,
           overlap.max_unlocked_during_locked,
           atomic_load(&model_gate_proven_events));
    pthread_mutex_unlock(&model.mutex);
    for (unsigned i = 0; i < rep.n_streams; i++)
        print_stream_result(&rep.streams[i]);

    for (unsigned i = 0; i < rep.n_streams; i++)
        assert(table.vaDestroySurfaces(&va_ctx, rep.streams[i].surfaces,
                                       STREAM_SURFACES) == VA_STATUS_SUCCESS);
    for (unsigned i = 0; i < rep.n_streams; i++)
        stream_teardown(&rep.streams[i]);
    model_assert_idle();
    driver_teardown();
}

static void usage(const char *program)
{
    fprintf(stderr,
        "Usage:\n"
        "  %s threads N FRAMES REPS SEED\n"
        "  %s teardown N FRAMES REPS SEED\n"
        "  %s failure N FRAMES REPS SEED\n"
        "  %s overlap N FRAMES REPS SEED\n"
        "  %s overlap-actor N FRAMES REPS SEED\n"
        "  %s overlap-late N FRAMES REPS SEED\n"
        "  %s overlap-counters\n"
        "  %s worker ID FRAMES SEED\n"
        "N in 1..4, FRAMES in 2..%d (the teardown midpoint needs >= 2), REPS >= 1.\n",
        program, program, program, program, program, program, program, program, MAX_FRAMES);
    exit(2);
}

static void *counter_worker(void *unused)
{
    VAImageFormat format = { .fourcc = VA_FOURCC_NV12 };
    VAImage image;
    unsigned long before = v4l2r_overlap_thread_events();

    (void)unused;
    assert(v4l2r_CreateImage(&va_ctx, &format, 64, 48, &image) == VA_STATUS_SUCCESS);
    assert(v4l2r_DestroyImage(&va_ctx, image.image_id) == VA_STATUS_SUCCESS);
    assert(v4l2r_overlap_thread_events() - before == 2);
    return NULL;
}

static void check_overlap_counters(void)
{
    VAImageFormat format = { .fourcc = VA_FOURCC_NV12 };
    VAImage image;
    pthread_t thread;
    struct v4l2r_overlap_stats stats;

    driver_setup();
    model_reset(1);
    v4l2r_overlap_configure(true);
    pthread_mutex_lock(&drv.api_mutex);
    v4l2r_overlap_locked_enter();
    /* Same-thread internal calls cannot establish cross-thread overlap. */
    assert(v4l2r_CreateImage(&va_ctx, &format, 64, 48, &image) == VA_STATUS_SUCCESS);
    assert(v4l2r_DestroyImage(&va_ctx, image.image_id) == VA_STATUS_SUCCESS);
    assert(v4l2r_overlap_snapshot().unlocked_over_locked == 0);
    assert(pthread_create(&thread, NULL, counter_worker, NULL) == 0);
    assert(pthread_join(thread, NULL) == 0);
    v4l2r_overlap_locked_exit();
    pthread_mutex_unlock(&drv.api_mutex);
    stats = v4l2r_overlap_snapshot();
    /* Nested Create/DestroyBuffer are helpers, not additional callers. */
    assert(stats.unlocked_calls == 2 && stats.unlocked_over_locked == 2);
    assert(stats.max_locked_active == 1 && stats.max_unlocked_during_locked == 1);
    v4l2r_overlap_configure(false);
    driver_teardown();
    puts("overlap counter ownership/nesting: PASS");
}

int main(int argc, char **argv)
{
    struct rlimit core = { 0, 0 };
    enum schedule_id schedule;
    unsigned n_streams = 1, frames = 12, reps = 10, worker_id = 0;
    uint64_t seed;

    assert(!setrlimit(RLIMIT_CORE, &core));
    md5_self_test();
    assert(pthread_mutex_init(&model.mutex, NULL) == 0);
    if (argc < 2)
        usage(argv[0]);
    if (!strcmp(argv[1], "overlap-counters") && argc == 2) {
        check_overlap_counters();
        return 0;
    }

    if (!strcmp(argv[1], "threads")) {
        schedule = SCHED_THREADS;
    } else if (!strcmp(argv[1], "teardown")) {
        schedule = SCHED_TEARDOWN;
    } else if (!strcmp(argv[1], "failure")) {
        schedule = SCHED_FAILURE;
    } else if (!strcmp(argv[1], "worker")) {
        schedule = SCHED_WORKER;
    } else if (!strcmp(argv[1], "overlap") || !strcmp(argv[1], "overlap-late")) {
        schedule = SCHED_OVERLAP;
    } else if (!strcmp(argv[1], "overlap-actor")) {
        schedule = SCHED_OVERLAP_ACTOR;
    } else {
        usage(argv[0]);
        return 2;
    }

    if (schedule == SCHED_WORKER) {
        if (argc != 5)
            usage(argv[0]);
        worker_id = (unsigned)strtoul(argv[2], NULL, 0);
        frames = (unsigned)strtoul(argv[3], NULL, 0);
        seed = strtoull(argv[4], NULL, 0);
        reps = 1;
    } else {
        if (argc != 6)
            usage(argv[0]);
        n_streams = (unsigned)strtoul(argv[2], NULL, 0);
        frames = (unsigned)strtoul(argv[3], NULL, 0);
        reps = (unsigned)strtoul(argv[4], NULL, 0);
        seed = strtoull(argv[5], NULL, 0);
    }
    if (!n_streams || n_streams > MAX_STREAMS || !frames ||
        frames > MAX_FRAMES || !reps || frames < 2)
        usage(argv[0]);

    if (schedule == SCHED_WORKER) {
        /* Start barrier: tests/concurrent-process.py spawns every worker
         * first and then releases them together through this byte. A
         * manual run without an orchestrator sees EOF and just starts. */
        char start;
        ssize_t n = read(STDIN_FILENO, &start, 1);

        assert(n == 1 || n == 0);
    }

    memset(&rep, 0, sizeof(rep));
    rep.schedule = schedule;
    rep.late_waiter = !strcmp(argv[1], "overlap-late");
    rep.n_streams = n_streams;
    rep.frames = frames;
    /* Worker mode: the single stream carries the worker id's recipe
     * (codec, dimensions, content), so every process-schedule worker
     * produces an independently derived, distinct digest. */
    rep.base_index = schedule == SCHED_WORKER ? worker_id : 0;
    rep.victim = schedule == SCHED_TEARDOWN ? &rep.streams[0] : NULL;
    /* Overlap rounds: stream 0's frame 0 is held by the model gate
     * while the required number of unlocked entrypoint executions is
     * recorded inside the driver against the spinning locked section
     * (fired by the decoder in the overlap round, by the failure actor
     * in the overlap-actor round). */
    if (schedule == SCHED_OVERLAP || schedule == SCHED_OVERLAP_ACTOR) {
        rep.gate_stream = &rep.streams[0];
        rep.overlap_target = 8;
    }

    printf("schedule %s streams=%u frames=%u reps=%u seed=%#llx\n",
           argv[1], n_streams, frames, reps,
           (unsigned long long)seed);
    for (unsigned r = 0; r < reps; r++) {
        uint64_t rep_seed = seed ^ (0x5bf03635ull ^ (uint64_t)r);

        rep.seed = splitmix64(&rep_seed);
        run_rep();
    }

    if (schedule == SCHED_WORKER) {
        /* The single stream's result line was printed by run_rep; repeat
         * it in the process-mode format for concurrent-process.py. All
         * threads are joined, so the results are read directly. */
        unsigned verified = rep.streams[0].verified;
        unsigned char digest[16];
        char hex[33];

        md5((const unsigned char *)rep.streams[0].frame_hashes,
            (size_t)verified * 8, digest);
        md5_hex(digest, hex);
        printf("worker %u frames=%u MD5=%s\n", worker_id, verified, hex);
    }
    printf("PASS %s\n", argv[1]);
    return 0;
}

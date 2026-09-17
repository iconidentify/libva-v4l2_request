/* SPDX-License-Identifier: GPL-3.0-or-later */
/* A decode context and its surfaces have independent VA lifetimes. FFmpeg
 * destroys a context on its decoder thread while its filter thread downloads
 * the last frames. Keep teardown from invalidating another entrypoint's view.
 * Internal calls use the unlocked implementations, so the lock is not recursive.
 *
 * The LOCKED wrappers below also feed the in-driver concurrency
 * instrumentation (see v4l2_request.h): the interval between
 * v4l2r_overlap_locked_enter() and v4l2r_overlap_locked_exit() is
 * exactly the time this entrypoint spends executing driver code under
 * api_mutex — what an unlocked entrypoint can genuinely overlap with.
 */
#include "v4l2_request.h"

#define LOCKED(name, params, args) \
static VAStatus locked_##name params \
{ \
	struct v4l2r_driver *drv = v4l2r_driver(va); \
	bool overlap = atomic_load_explicit(&v4l2r_overlap_enabled, \
					    memory_order_relaxed); \
	VAStatus result; \
	pthread_mutex_lock(&drv->api_mutex); \
	if (overlap) \
		v4l2r_overlap_locked_enter(); \
	result = v4l2r_##name args; \
	if (overlap) \
		v4l2r_overlap_locked_exit(); \
	pthread_mutex_unlock(&drv->api_mutex); \
	return result; \
}

LOCKED(CreateContext, (VADriverContextP va, VAConfigID config, int w, int h,
    int flags, VASurfaceID *targets, int n, VAContextID *id),
    (va, config, w, h, flags, targets, n, id))
LOCKED(DestroyContext, (VADriverContextP va, VAContextID id), (va, id))
LOCKED(DestroySurfaces, (VADriverContextP va, VASurfaceID *ids, int n), (va, ids, n))
LOCKED(BeginPicture, (VADriverContextP va, VAContextID id, VASurfaceID surface), (va, id, surface))
LOCKED(RenderPicture, (VADriverContextP va, VAContextID id, VABufferID *buffers, int n),
    (va, id, buffers, n))
LOCKED(EndPicture, (VADriverContextP va, VAContextID id), (va, id))
LOCKED(SyncSurface, (VADriverContextP va, VASurfaceID id), (va, id))
LOCKED(QuerySurfaceStatus, (VADriverContextP va, VASurfaceID id, VASurfaceStatus *status),
    (va, id, status))
LOCKED(ExportSurfaceHandle, (VADriverContextP va, VASurfaceID id, uint32_t type,
    uint32_t flags, void *desc), (va, id, type, flags, desc))
LOCKED(DeriveImage, (VADriverContextP va, VASurfaceID id, VAImage *image), (va, id, image))
LOCKED(GetImage, (VADriverContextP va, VASurfaceID id, int x, int y,
    unsigned int w, unsigned int h, VAImageID image), (va, id, x, y, w, h, image))
LOCKED(PutImage, (VADriverContextP va, VASurfaceID id, VAImageID image,
    int sx, int sy, unsigned int sw, unsigned int sh,
    int dx, int dy, unsigned int dw, unsigned int dh),
    (va, id, image, sx, sy, sw, sh, dx, dy, dw, dh))

void v4l2r_lock_surface_api(struct VADriverVTable *vtable)
{
#define WRAP(name) vtable->va##name = locked_##name
    WRAP(CreateContext);
    WRAP(DestroyContext);
    WRAP(DestroySurfaces);
    WRAP(BeginPicture);
    WRAP(RenderPicture);
    WRAP(EndPicture);
    WRAP(SyncSurface);
    WRAP(QuerySurfaceStatus);
    WRAP(ExportSurfaceHandle);
    WRAP(DeriveImage);
    WRAP(GetImage);
    WRAP(PutImage);
#undef WRAP
}

/* --- in-driver concurrency instrumentation (test hook; v4l2_request.h) ---
 *
 * Only the LOCKED wrappers above and the unlocked entrypoints'
 * V4L2R_OVERLAP_UNLOCKED() brackets touch these counters. Every counter
 * is a plain atomic: the locked entrypoints hold api_mutex while
 * counted and the unlocked ones run without it, so the counters
 * themselves must be lock-free. */

_Atomic bool v4l2r_overlap_enabled;

static _Atomic unsigned overlap_locked_active;
static _Atomic unsigned overlap_max_locked;
static _Atomic unsigned overlap_unlocked_active;
static _Atomic unsigned overlap_max_unlocked_during_locked;
static _Atomic unsigned long overlap_locked_sections;
static _Atomic unsigned long overlap_unlocked_calls;
static _Atomic unsigned long overlap_unlocked_over_locked;
static _Thread_local unsigned overlap_unlocked_depth;
static _Thread_local unsigned overlap_locked_depth;
static _Thread_local unsigned long overlap_thread_events;

static void overlap_max(_Atomic unsigned *counter, unsigned value)
{
	unsigned prev = atomic_load(counter);

	while (prev < value &&
	       !atomic_compare_exchange_weak(counter, &prev, value))
		;
}

void v4l2r_overlap_locked_enter(void)
{
	overlap_locked_depth++;
	unsigned active = atomic_fetch_add(&overlap_locked_active, 1) + 1;

	overlap_max(&overlap_max_locked, active);
	atomic_fetch_add(&overlap_locked_sections, 1);
}

void v4l2r_overlap_locked_exit(void)
{
	atomic_fetch_sub(&overlap_locked_active, 1);
	overlap_locked_depth--;
}

void v4l2r_overlap_unlocked_enter(void)
{
	/* Create/DestroyImage call instrumented buffer helpers internally.
	 * Count only the outer unlocked call, never nesting as another caller. */
	if (overlap_unlocked_depth++ || overlap_locked_depth)
		return;
	unsigned active = atomic_fetch_add(&overlap_unlocked_active, 1) + 1;

	atomic_fetch_add(&overlap_unlocked_calls, 1);
	if (atomic_load(&overlap_locked_active)) {
		overlap_thread_events++;
		atomic_fetch_add(&overlap_unlocked_over_locked, 1);
		overlap_max(&overlap_max_unlocked_during_locked, active);
	}
}

void v4l2r_overlap_unlocked_exit(void)
{
	if (--overlap_unlocked_depth || overlap_locked_depth)
		return;
	atomic_fetch_sub(&overlap_unlocked_active, 1);
}

unsigned long v4l2r_overlap_thread_events(void)
{
	return overlap_thread_events;
}

void v4l2r_overlap_configure(bool enable)
{
	atomic_store(&overlap_locked_active, 0);
	atomic_store(&overlap_max_locked, 0);
	atomic_store(&overlap_unlocked_active, 0);
	atomic_store(&overlap_max_unlocked_during_locked, 0);
	atomic_store(&overlap_locked_sections, 0);
	atomic_store(&overlap_unlocked_calls, 0);
	atomic_store(&overlap_unlocked_over_locked, 0);
	atomic_store(&v4l2r_overlap_enabled, enable);
}

struct v4l2r_overlap_stats v4l2r_overlap_snapshot(void)
{
	return (struct v4l2r_overlap_stats) {
		.locked_sections = atomic_load(&overlap_locked_sections),
		.unlocked_calls = atomic_load(&overlap_unlocked_calls),
		.unlocked_over_locked = atomic_load(&overlap_unlocked_over_locked),
		.max_locked_active = atomic_load(&overlap_max_locked),
		.max_unlocked_during_locked =
			atomic_load(&overlap_max_unlocked_during_locked),
	};
}

unsigned v4l2r_overlap_locked_active(void)
{
	return atomic_load(&overlap_locked_active);
}

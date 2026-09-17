/*
 * Opt-in HEVC reference-control trace (issue #84).
 *
 * One JSON line per request the driver actually queued, describing the
 * reference-control content that was submitted: the DPB as sent in
 * V4L2_CID_STATELESS_HEVC_DECODE_PARAMS, the RPS current lists, and each
 * slice's reference indices. It exists so that two client paths (VA-API and
 * a direct V4L2 client) can be compared command-by-command offline, without
 * changing what is submitted.
 *
 * Off unless LIBVA_V4L2_HEVC_REFTRACE names a sink. Disabled cost: one
 * relaxed atomic load plus ordinal bookkeeping per submitted request. Records carry only small
 * integers derived from the controls (POC values, CAPTURE buffer indices,
 * flags, list indices); never bitstream bytes, timestamps used as addresses,
 * pointers, file paths or the environment value itself.
 *
 * SPDX-License-Identifier: GPL-3.0-or-later
 */
#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <inttypes.h>
#include <pthread.h>
#include <stdarg.h>
#include <stdatomic.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>

#include "v4l2_request.h"

#if HAVE_V4L2_CTRL_HEVC

/* A record never exceeds this; the slice list is capped so it cannot. */
#define TRACE_RECORD_MAX	4096
#define TRACE_MAX_SLICES	16

static struct {
	pthread_mutex_t mutex;
	bool initialized;
	bool enabled;
	FILE *sink;			/* NULL = stderr */
	bool owns_sink;
	uint64_t seq;
	char run[17];
} trace = {
	.mutex = PTHREAD_MUTEX_INITIALIZER,
};

/* Fast path for the disabled case; authoritative state is under the mutex. */
static _Atomic int trace_state = -1;	/* -1 unknown, 0 off, 1 on */

/* Called with trace.mutex held. */
static void trace_init_locked(void)
{
	const char *sink;

	if (trace.initialized)
		return;
	trace.initialized = true;
	trace.seq = 0;
	v4l2r_diag_run_id(trace.run);

	sink = getenv("LIBVA_V4L2_HEVC_REFTRACE");
	if (!sink || !*sink) {
		trace.enabled = false;
	} else if (!strcmp(sink, "stderr")) {
		trace.enabled = true;
		trace.sink = NULL;
	} else {
		/* Opening a FIFO while holding api_mutex can freeze decoding before
		 * the first record. Path sinks must be regular files; never inherit
		 * their descriptor into an unrelated exec'd child. */
		int fd = open(sink, O_WRONLY | O_CREAT | O_APPEND | O_CLOEXEC | O_NONBLOCK, 0600);
		FILE *out = NULL;
		struct stat info;

		if (fd >= 0) {
			int error = 0;
			if (fstat(fd, &info) < 0)
				error = errno;
			else if (!S_ISREG(info.st_mode))
				error = EINVAL;
			else {
				out = fdopen(fd, "a");
				if (!out)
					error = errno;
			}
			if (!out) {
				close(fd);
				errno = error;
			}
		}

		if (!out) {
			/* Do not echo the value: it is arbitrary environment input. */
			v4l2r_diag(NULL, V4L2R_DIAG_LEVEL_WARNING, V4L2R_DIAG_CLIENT,
				   "hevc-reftrace", -errno,
				   "cannot open the LIBVA_V4L2_HEVC_REFTRACE sink; "
				   "reference tracing disabled");
			trace.enabled = false;
		} else {
			trace.enabled = true;
			trace.sink = out;
			trace.owns_sink = true;
		}
	}
	atomic_store(&trace_state, trace.enabled);
}

void v4l2r_hevc_trace_configure(const struct v4l2r_hevc_trace_options *options)
{
	pthread_mutex_lock(&trace.mutex);
	if (trace.owns_sink && trace.sink)
		fclose(trace.sink);
	trace.sink = NULL;
	trace.owns_sink = false;
	trace.initialized = false;
	atomic_store(&trace_state, -1);
	if (options) {
		trace.initialized = true;
		trace.seq = 0;
		v4l2r_diag_run_id(trace.run);
		trace.enabled = options->enable;
		trace.sink = options->sink;
		atomic_store(&trace_state, trace.enabled);
	}
	pthread_mutex_unlock(&trace.mutex);
}

bool v4l2r_hevc_trace_enabled(void)
{
	int state = atomic_load_explicit(&trace_state, memory_order_relaxed);

	if (state >= 0)
		return state;
	pthread_mutex_lock(&trace.mutex);
	trace_init_locked();
	state = trace.enabled;
	pthread_mutex_unlock(&trace.mutex);
	return state;
}

/* Bounded appends; overflow is remembered, never silently truncated. */
struct line {
	char buf[TRACE_RECORD_MAX];
	size_t pos;
	bool overflow;
};

static void append(struct line *l, const char *fmt, ...)
	__attribute__((format(printf, 2, 3)));
static void append(struct line *l, const char *fmt, ...)
{
	va_list args;
	int n;

	if (l->overflow)
		return;
	va_start(args, fmt);
	n = vsnprintf(l->buf + l->pos, sizeof(l->buf) - l->pos, fmt, args);
	va_end(args);
	if (n < 0 || (size_t)n >= sizeof(l->buf) - l->pos) {
		l->overflow = true;
		return;
	}
	l->pos += (size_t)n;
}

static void append_list(struct line *l, const char *key, const uint8_t *idx,
			unsigned int count)
{
	append(l, ",\"%s\":[", key);
	for (unsigned int i = 0; i < count && i < 16; i++)
		append(l, "%s%u", i ? "," : "", idx[i]);
	append(l, "]");
}

static const char *slice_type_name(uint32_t type)
{
	switch (type) {
	case V4L2_HEVC_SLICE_TYPE_B: return "B";
	case V4L2_HEVC_SLICE_TYPE_P: return "P";
	case V4L2_HEVC_SLICE_TYPE_I: return "I";
	default: return "?";
	}
}

static void append_slice(struct line *l, unsigned int i,
			 const struct v4l2_ctrl_hevc_slice_params *s)
{
	uint32_t type = s->slice_type;
	bool tmvp = s->flags & V4L2_HEVC_SLICE_PARAMS_FLAG_SLICE_TEMPORAL_MVP_ENABLED;
	unsigned int n0 = type != V4L2_HEVC_SLICE_TYPE_I ?
			  (unsigned int)s->num_ref_idx_l0_active_minus1 + 1 : 0;
	unsigned int n1 = type == V4L2_HEVC_SLICE_TYPE_B ?
			  (unsigned int)s->num_ref_idx_l1_active_minus1 + 1 : 0;

	append(l, "%s{\"i\":%u,\"type\":\"%s\",\"nal\":%u", i ? "," : "", i,
	       slice_type_name(type), s->nal_unit_type);
	append_list(l, "l0", s->ref_idx_l0, n0 <= 16 ? n0 : 16);
	append_list(l, "l1", s->ref_idx_l1, n1 <= 16 ? n1 : 16);
	append(l, ",\"tmvp\":%u", tmvp ? 1u : 0u);
	if (tmvp)
		append(l, ",\"col_l0\":%u,\"col\":%u",
		       s->flags & V4L2_HEVC_SLICE_PARAMS_FLAG_COLLOCATED_FROM_L0 ? 1u : 0u,
		       s->collocated_ref_idx);
	append(l, "}");
}

void v4l2r_hevc_trace_request(const struct v4l2r_context *ctx,
			      const struct v4l2r_hevc_trace_request *req)
{
	const struct v4l2_ctrl_hevc_decode_params *dp = req->decode_params;
	struct line l = { .pos = 0 };
	unsigned int entries;
	unsigned int slices;

	if (!v4l2r_hevc_trace_enabled())
		return;

	pthread_mutex_lock(&trace.mutex);
	trace_init_locked();
	if (!trace.enabled) {
		pthread_mutex_unlock(&trace.mutex);
		return;
	}

	append(&l, "{\"schema\":\"%s\",\"run\":\"%s\",\"seq\":%" PRIu64,
	       V4L2R_HEVC_TRACE_SCHEMA, trace.run, ++trace.seq);
	append(&l, ",\"ctx\":%u,\"va_context\":\"0x%08x\"", ctx->diag_serial,
	       ctx->id);
	append(&l, ",\"pic\":%u,\"req\":%u,\"first\":%u,\"last\":%u,\"target\":%d",
	       req->picture, req->request, req->first_slice ? 1u : 0u,
	       req->last_slice ? 1u : 0u, req->target_index);
	append(&l, ",\"poc\":%" PRId32 ",\"irap\":%u,\"idr\":%u",
	       dp->pic_order_cnt_val,
	       dp->flags & V4L2_HEVC_DECODE_PARAM_FLAG_IRAP_PIC ? 1u : 0u,
	       dp->flags & V4L2_HEVC_DECODE_PARAM_FLAG_IDR_PIC ? 1u : 0u);
	append(&l, ",\"ltr_sps\":%u,\"reorder\":%u,\"total_curr\":%u",
	       req->ltr_sps ? 1u : 0u, req->reorder ? 1u : 0u,
	       req->num_pic_total_curr);

	/* The DPB exactly as submitted: slot order is what the decoder sees.
	 * "buf" is the CAPTURE buffer index the timestamp names (a context-
	 * local small integer), so a slot can be matched to the earlier record
	 * whose "target" wrote that buffer. */
	entries = dp->num_active_dpb_entries;
	if (entries > V4L2_HEVC_DPB_ENTRIES_NUM_MAX)
		entries = V4L2_HEVC_DPB_ENTRIES_NUM_MAX;
	append(&l, ",\"dpb\":[");
	for (unsigned int i = 0; i < entries; i++) {
		const struct v4l2_hevc_dpb_entry *e = &dp->dpb[i];
		long long buf = e->timestamp ?
			(long long)(e->timestamp / 1000) - 1 : -1;

		append(&l, "%s{\"i\":%u,\"buf\":%lld,\"poc\":%" PRId32
		       ",\"lt\":%u,\"field\":%u}", i ? "," : "", i, buf,
		       e->pic_order_cnt_val,
		       e->flags & V4L2_HEVC_DPB_ENTRY_LONG_TERM_REFERENCE ? 1u : 0u,
		       e->field_pic ? 1u : 0u);
	}
	append(&l, "]");
	append_list(&l, "st_before", dp->poc_st_curr_before,
		    dp->num_poc_st_curr_before);
	append_list(&l, "st_after", dp->poc_st_curr_after,
		    dp->num_poc_st_curr_after);
	append_list(&l, "lt_curr", dp->poc_lt_curr, dp->num_poc_lt_curr);

	slices = req->num_slices;
	append(&l, ",\"slices\":[");
	for (unsigned int i = 0; i < slices && i < TRACE_MAX_SLICES; i++)
		append_slice(&l, i, &req->slices[i]);
	append(&l, "]");
	if (slices > TRACE_MAX_SLICES)
		append(&l, ",\"slices_omitted\":%u", slices - TRACE_MAX_SLICES);
	append(&l, "}\n");

	if (l.overflow) {
		/* Keep the sequence intact and say what happened. */
		l.pos = 0;
		l.overflow = false;
		append(&l, "{\"schema\":\"%s\",\"run\":\"%s\",\"seq\":%" PRIu64
		       ",\"ctx\":%u,\"pic\":%u,\"req\":%u,\"error\":\"record-overflow\"}\n",
		       V4L2R_HEVC_TRACE_SCHEMA, trace.run, trace.seq,
		       ctx->diag_serial, req->picture, req->request);
	}

	/* One write per record keeps lines from concurrent contexts intact. */
	FILE *out = trace.sink ? trace.sink : stderr;
	if (fwrite(l.buf, 1, l.pos, out) != l.pos || fflush(out) != 0 || ferror(out)) {
		int error = errno ? errno : EIO;
		trace.enabled = false;
		atomic_store(&trace_state, 0);
		v4l2r_diag(NULL, V4L2R_DIAG_LEVEL_WARNING, V4L2R_DIAG_CLIENT,
			   "hevc-reftrace", -error,
			   "reference trace write failed; capture incomplete and tracing disabled");
	}
	pthread_mutex_unlock(&trace.mutex);
}

#else /* !HAVE_V4L2_CTRL_HEVC */

void v4l2r_hevc_trace_configure(const struct v4l2r_hevc_trace_options *options)
{
	(void)options;
}

bool v4l2r_hevc_trace_enabled(void)
{
	return false;
}

#endif

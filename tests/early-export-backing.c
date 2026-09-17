/* SPDX-License-Identifier: GPL-3.0-or-later */
/* CREATE_BUFS flag/capability matrix and ENOMEM classification for standalone
 * early-export backing. Fake ioctl only; does not open a decoder. */
#include <assert.h>
#include <errno.h>
#include <fcntl.h>
#include <stdarg.h>
#include <stdio.h>
#include <string.h>
#include <sys/resource.h>
#include <unistd.h>
#include "v4l2_request.h"

enum {
    MODE_HINTS,
    MODE_NO_HINTS,
    MODE_ENOMEM,
    MODE_EINVAL,
};

static int mode;
static unsigned query_count;
static unsigned alloc_count;
static uint32_t last_flags;

int __wrap_ioctl(int fd, unsigned long request, ...)
{
    (void)fd;
    va_list ap;
    va_start(ap, request);
    void *arg = va_arg(ap, void *);
    va_end(ap);

    if (request == VIDIOC_QUERYCAP) {
        ((struct v4l2_capability *)arg)->capabilities = V4L2_CAP_VIDEO_M2M;
        return 0;
    }

    if (request == VIDIOC_S_FMT) {
        struct v4l2_format *format = arg;

        if (V4L2_TYPE_IS_MULTIPLANAR(format->type)) {
            struct v4l2_pix_format_mplane *pix = &format->fmt.pix_mp;

            if (!pix->num_planes)
                pix->num_planes = 1;
            if (!pix->plane_fmt[0].bytesperline)
                pix->plane_fmt[0].bytesperline = pix->width;
            if (!pix->plane_fmt[0].sizeimage)
                pix->plane_fmt[0].sizeimage =
                    pix->plane_fmt[0].bytesperline * pix->height;
            return 0;
        }
        if (!format->fmt.pix.bytesperline)
            format->fmt.pix.bytesperline = format->fmt.pix.width;
        if (!format->fmt.pix.sizeimage)
            format->fmt.pix.sizeimage =
                format->fmt.pix.bytesperline * format->fmt.pix.height;
        return 0;
    }

    if (request == VIDIOC_CREATE_BUFS) {
        struct v4l2_create_buffers *buffers = arg;

        if (buffers->count == 0) {
            query_count++;
#if HAVE_V4L2_MEMORY_FLAG_NON_COHERENT
            buffers->capabilities = (mode == MODE_HINTS) ?
                V4L2_BUF_CAP_SUPPORTS_MMAP_CACHE_HINTS : 0;
#endif
            return 0;
        }

        alloc_count++;
#if HAVE_V4L2_MEMORY_FLAG_NON_COHERENT
        last_flags = buffers->flags;
#endif
        if (mode == MODE_ENOMEM) {
            errno = ENOMEM;
            return -1;
        }
        if (mode == MODE_EINVAL) {
            errno = EINVAL;
            return -1;
        }
        buffers->index = 0;
        return 0;
    }

    if (request == VIDIOC_QUERYBUF) {
        ((struct v4l2_buffer *)arg)->length = 4096;
        return 0;
    }

    if (request == VIDIOC_EXPBUF) {
        struct v4l2_exportbuffer *exportbuffer = arg;
        int expfd = open("/dev/null", O_RDWR | O_CLOEXEC);

        if (expfd < 0)
            return -1;
        exportbuffer->fd = expfd;
        return 0;
    }

    assert(!"unexpected ioctl");
    return -1;
}

static void run(int test_mode, VAStatus expected)
{
    struct v4l2r_driver drv = { .nb_decoders = 1 };
    struct v4l2r_surface surface = { .width = 64, .height = 64 };
    VAStatus status;

    mode = test_mode;
    query_count = 0;
    alloc_count = 0;
    last_flags = 0xdeadbeef;
    snprintf(drv.decoders[0].video_path, sizeof(drv.decoders[0].video_path),
             "/dev/null");
    drv.decoders[0].pixelformats[0] = v4l2_fourcc('T', 'E', 'S', 'T');
    drv.decoders[0].nb_pixelformats = 1;

    status = v4l2r_surface_alloc_backing(&drv, &surface);
    assert(status == expected);
    assert(alloc_count == 1);

    if (expected == VA_STATUS_SUCCESS) {
        assert(surface.backing && surface.backing->dmabuf_fd[0] >= 0);
        v4l2r_surface_free_backing(&surface);
    } else {
        assert(!surface.backing);
    }
}

int main(void)
{
    struct rlimit core = { 0, 0 };

    assert(!setrlimit(RLIMIT_CORE, &core));

#if HAVE_V4L2_MEMORY_FLAG_NON_COHERENT
    run(MODE_HINTS, VA_STATUS_SUCCESS);
    assert(query_count == 1);
    assert(last_flags & V4L2_MEMORY_FLAG_NON_COHERENT);

    run(MODE_NO_HINTS, VA_STATUS_SUCCESS);
    assert(query_count == 1);
    assert(!(last_flags & V4L2_MEMORY_FLAG_NON_COHERENT));
#endif

    run(MODE_ENOMEM, VA_STATUS_ERROR_ALLOCATION_FAILED);
    run(MODE_EINVAL, VA_STATUS_ERROR_OPERATION_FAILED);
    return 0;
}

/* SPDX-License-Identifier: GPL-3.0-or-later */
/* Hash decoded pixels at each frame's native size. The ffmpeg CLI's rawvideo
 * encoder keeps the first output size, which hides resolution-change coverage. */
#include <stdio.h>
#include <string.h>
#include <libavcodec/avcodec.h>
#include <libavformat/avformat.h>
#include <libavutil/error.h>
#include <libavutil/hwcontext.h>
#include <libavutil/imgutils.h>
#include <libavutil/md5.h>
#include <libavutil/pixdesc.h>
#include <libswscale/swscale.h>

struct check {
    int hardware;
    unsigned int frames;
    enum AVPixelFormat format;
    struct AVMD5 *md5;
    struct SwsContext *sws;
};

static enum AVPixelFormat vaapi_format(AVCodecContext *ctx,
                                      const enum AVPixelFormat *formats)
{
    (void)ctx;
    for (; *formats != AV_PIX_FMT_NONE; formats++)
        if (*formats == AV_PIX_FMT_VAAPI)
            return *formats;
    return AV_PIX_FMT_NONE; /* Never count software fallback as a hardware pass. */
}

static int hash_frame(struct check *check, AVFrame *frame)
{
    AVFrame *download = av_frame_alloc(), *planar = av_frame_alloc();
    AVFrame *source = frame;
    uint8_t *pixels = NULL;
    int ret = AVERROR(ENOMEM);
    if (!download || !planar)
        goto done;
    if (frame->flags & AV_FRAME_FLAG_CORRUPT) {
        ret = AVERROR_INVALIDDATA;
        goto done;
    }
    if (check->hardware) {
        if (frame->format != AV_PIX_FMT_VAAPI) {
            fprintf(stderr, "frame-check: software frame rejected in hardware mode\n");
            ret = AVERROR_INVALIDDATA;
            goto done;
        }
        ret = av_hwframe_transfer_data(download, frame, 0);
        if (ret < 0)
            goto done;
        ret = av_frame_copy_props(download, frame);
        if (ret < 0)
            goto done;
        source = download;
    }

    /* The default decoder crop can round the left edge for alignment; hardware
     * frames also retain top/left crop metadata until after download. Apply the
     * complete conformance window here, where unaligned pointers are supported. */
    ret = av_frame_apply_cropping(source, AV_FRAME_CROP_UNALIGNED);
    if (ret < 0)
        goto done;
    frame = source;

    /* Use the visible frame size, including when the decoder storage is padded.
     * swscale only repacks the samples here; it never changes their dimensions. */
    planar->width = frame->width;
    planar->height = frame->height;
    planar->format = check->format;
    ret = av_frame_get_buffer(planar, 32);
    if (ret < 0)
        goto done;
    check->sws = sws_getCachedContext(check->sws, frame->width, frame->height,
        source->format, frame->width, frame->height, check->format,
        SWS_POINT | SWS_BITEXACT, NULL, NULL, NULL);
    if (!check->sws) {
        ret = AVERROR(ENOMEM);
        goto done;
    }
    ret = sws_scale(check->sws, (const uint8_t *const *)source->data,
        source->linesize, 0, frame->height, planar->data, planar->linesize);
    if (ret != frame->height) {
        ret = AVERROR_INVALIDDATA;
        goto done;
    }
    int size = av_image_get_buffer_size(check->format, frame->width, frame->height, 1);
    if (size < 0) {
        ret = size;
        goto done;
    }
    pixels = av_malloc(size);
    if (!pixels) {
        ret = AVERROR(ENOMEM);
        goto done;
    }
    ret = av_image_copy_to_buffer(pixels, size, (const uint8_t *const *)planar->data,
        planar->linesize, check->format, frame->width, frame->height, 1);
    if (ret < 0)
        goto done;
    av_md5_update(check->md5, pixels, size);
    uint8_t digest[16];
    av_md5_sum(digest, pixels, size);
    printf("frame %u %dx%d %s ", check->frames++, frame->width, frame->height,
           av_get_pix_fmt_name(check->format));
    for (int i = 0; i < 16; i++)
        printf("%02x", digest[i]);
    putchar('\n');
    fflush(stdout);
    ret = 0;
done:
    av_free(pixels);
    av_frame_free(&download);
    av_frame_free(&planar);
    return ret;
}

static int decode(struct check *check, AVCodecContext *decoder, AVPacket *packet)
{
    int ret = avcodec_send_packet(decoder, packet);
    if (ret < 0)
        return ret;
    AVFrame *frame = av_frame_alloc();
    if (!frame)
        return AVERROR(ENOMEM);
    while ((ret = avcodec_receive_frame(decoder, frame)) >= 0) {
        ret = hash_frame(check, frame);
        av_frame_unref(frame);
        if (ret < 0)
            break;
    }
    av_frame_free(&frame);
    return ret == AVERROR(EAGAIN) || ret == AVERROR_EOF ? 0 : ret;
}

int main(int argc, char **argv)
{
    if (argc < 4 || argc > 5 ||
        (strcmp(argv[1], "software") && strcmp(argv[1], "vaapi")) ||
        (argc == 5 && strcmp(argv[4], "--allow-profile-mismatch"))) {
        fprintf(stderr, "Usage: %s software|vaapi INPUT PIX_FMT [--allow-profile-mismatch]\n", argv[0]);
        return 2;
    }
    struct check check = {.hardware = !strcmp(argv[1], "vaapi"),
                          .format = av_get_pix_fmt(argv[3])};
    AVFormatContext *input = NULL;
    AVCodecContext *decoder = NULL;
    AVBufferRef *device = NULL;
    AVPacket *packet = NULL;
    /* av_find_best_stream() takes const AVCodec ** from libavcodec 59 (FFmpeg
     * 5.0); older FFmpeg (4.4 on Ubuntu 22.04, 4.2 on 20.04) wants AVCodec **. */
#if LIBAVCODEC_VERSION_INT >= AV_VERSION_INT(59, 0, 100)
    const AVCodec *codec = NULL;
#else
    AVCodec *codec = NULL;
#endif
    int ret = AVERROR(EINVAL);
    av_log_set_level(AV_LOG_WARNING);
    if (check.format == AV_PIX_FMT_NONE)
        goto done;
    check.md5 = av_md5_alloc();
    packet = av_packet_alloc();
    if (!check.md5 || !packet) {
        ret = AVERROR(ENOMEM);
        goto done;
    }
    av_md5_init(check.md5);
    if ((ret = avformat_open_input(&input, argv[2], NULL, NULL)) < 0 ||
        (ret = avformat_find_stream_info(input, NULL)) < 0)
        goto done;
    int stream = av_find_best_stream(input, AVMEDIA_TYPE_VIDEO, -1, -1, &codec, 0);
    if (stream < 0) {
        ret = stream;
        goto done;
    }
    decoder = avcodec_alloc_context3(codec);
    if (!decoder) {
        ret = AVERROR(ENOMEM);
        goto done;
    }
    if ((ret = avcodec_parameters_to_context(decoder, input->streams[stream]->codecpar)) < 0)
        goto done;
    decoder->thread_count = 1;
    decoder->apply_cropping = 0;
    if (argc == 5)
        decoder->hwaccel_flags |= AV_HWACCEL_FLAG_ALLOW_PROFILE_MISMATCH;
    if (check.hardware) {
        if ((ret = av_hwdevice_ctx_create(&device, AV_HWDEVICE_TYPE_VAAPI,
                                         NULL, NULL, 0)) < 0)
            goto done;
        decoder->hw_device_ctx = av_buffer_ref(device);
        if (!decoder->hw_device_ctx) {
            ret = AVERROR(ENOMEM);
            goto done;
        }
        decoder->get_format = vaapi_format;
    }
    if ((ret = avcodec_open2(decoder, codec, NULL)) < 0)
        goto done;
    while ((ret = av_read_frame(input, packet)) >= 0) {
        if (packet->stream_index == stream)
            ret = decode(&check, decoder, packet);
        av_packet_unref(packet);
        if (ret < 0)
            goto done;
    }
    if (ret != AVERROR_EOF || (ret = decode(&check, decoder, NULL)) < 0)
        goto done;
    if (!check.frames) {
        ret = AVERROR_INVALIDDATA;
        goto done;
    }
    uint8_t digest[16];
    av_md5_final(check.md5, digest);
    printf("MD5=");
    for (int i = 0; i < 16; i++)
        printf("%02x", digest[i]);
    putchar('\n');
    ret = 0;
done:
    if (ret < 0)
        fprintf(stderr, "Decode failed: %s\n", av_err2str(ret));
    av_packet_free(&packet);
    avcodec_free_context(&decoder);
    avformat_close_input(&input);
    av_buffer_unref(&device);
    av_free(check.md5);
    sws_freeContext(check.sws);
    return ret < 0;
}

/* SPDX-License-Identifier: GPL-3.0-or-later */
/* Real FFmpeg/libva concurrent worker. Software hashes are the offline oracle.
 * Hardware mode requires AV_PIX_FMT_VAAPI and never counts software fallback.
 * This is not the model concurrent-stress worker. */
#define main single_stream_main
#include "frame-check.c"
#undef main

#include <pthread.h>
#include <unistd.h>

struct job {
    const char *path;
    const char *pix_fmt;
    int id, hardware, teardown_after, invalid;
    AVBufferRef *device;
    unsigned frames;
    uint8_t md5[16];
    int ret;
    AVFrame *kept;
};

static void *run_job(void *arg)
{
    struct job *job = arg;
    AVFormatContext *input = NULL;
    AVCodecContext *decoder = NULL;
    AVPacket *packet = av_packet_alloc();
    struct check check = {0};
#if LIBAVCODEC_VERSION_INT >= AV_VERSION_INT(59, 0, 100)
    const AVCodec *codec = NULL;
#else
    AVCodec *codec = NULL;
#endif
    int stream, ret = AVERROR(ENOMEM);

    check.hardware = job->hardware;
    check.format = av_get_pix_fmt(job->pix_fmt);
    if (check.format == AV_PIX_FMT_NONE) {
        job->ret = AVERROR(EINVAL);
        return NULL;
    }
    check.md5 = av_md5_alloc();
    if (!check.md5 || !packet)
        goto done;
    av_md5_init(check.md5);
    if ((ret = avformat_open_input(&input, job->path, NULL, NULL)) < 0 ||
        (ret = avformat_find_stream_info(input, NULL)) < 0)
        goto done;
    stream = av_find_best_stream(input, AVMEDIA_TYPE_VIDEO, -1, -1, &codec, 0);
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
    if (job->hardware) {
        if (!job->device) {
            ret = AVERROR(EINVAL);
            goto done;
        }
        decoder->hw_device_ctx = av_buffer_ref(job->device);
        decoder->get_format = vaapi_format;
        decoder->extra_hw_frames = 8;
    }
    if ((ret = avcodec_open2(decoder, codec, NULL)) < 0)
        goto done;
    while ((ret = av_read_frame(input, packet)) >= 0) {
        if (packet->stream_index == stream)
            ret = decode(&check, decoder, packet);
        av_packet_unref(packet);
        if (ret < 0)
            goto done;
        if (job->teardown_after && (int)check.frames >= job->teardown_after)
            break;
    }
    if (ret == AVERROR_EOF)
        ret = 0;
    if (!job->teardown_after && (ret = decode(&check, decoder, NULL)) < 0)
        goto done;
    if (job->teardown_after) {
        AVFrame *hold = av_frame_alloc();
        if (!hold) {
            ret = AVERROR(ENOMEM);
            goto done;
        }
        /* Keep a decoded frame across this context's teardown. */
        job->kept = hold;
    }
    if (job->invalid && decoder) {
        /* Client-level invalid: send after the codec was closed. */
        avcodec_free_context(&decoder);
        ret = 0;
    }
    if (check.md5)
        av_md5_final(check.md5, job->md5);
    job->frames = check.frames;
done:
    av_packet_free(&packet);
    av_freep(&check.md5);
    sws_freeContext(check.sws);
    if (!job->invalid)
        avcodec_free_context(&decoder);
    avformat_close_input(&input);
    job->ret = ret;
    return NULL;
}

static int open_device(AVBufferRef **device, int hardware)
{
    if (!hardware) {
        *device = NULL;
        return 0;
    }
    return av_hwdevice_ctx_create(device, AV_HWDEVICE_TYPE_VAAPI, NULL, NULL, 0);
}

int main(int argc, char **argv)
{
    int hardware, i, n = 0, teardown = 0, invalid = 0, threads = 1;
    int arg = 1;
    AVBufferRef *device = NULL;
    pthread_t *tids;
    struct job *jobs;
    int ret;

    av_log_set_level(AV_LOG_ERROR);
    if (argc < 4) {
        fprintf(stderr, "Usage: %s software|vaapi [--threads N] [--teardown] [--invalid] CLIP PIX [...]\n",
                argv[0]);
        return 2;
    }
    if (!strcmp(argv[arg], "software"))
        hardware = 0;
    else if (!strcmp(argv[arg], "vaapi"))
        hardware = 1;
    else
        return 2;
    arg++;
    while (arg < argc && argv[arg][0] == '-') {
        if (!strcmp(argv[arg], "--threads") && arg + 1 < argc) {
            threads = atoi(argv[++arg]);
            arg++;
        } else if (!strcmp(argv[arg], "--teardown")) {
            teardown = 1;
            arg++;
        } else if (!strcmp(argv[arg], "--invalid")) {
            invalid = 1;
            arg++;
        } else {
            return 2;
        }
    }
    if ((argc - arg) < 2 || (argc - arg) % 2)
        return 2;
    n = (argc - arg) / 2;
    if (threads < 1 || threads > 4 || n < 1 || n > 4)
        return 2;
    if ((ret = open_device(&device, hardware)) < 0) {
        fprintf(stderr, "vaapi device unavailable: %s\n", av_err2str(ret));
        return hardware ? 77 : 1;
    }
    jobs = av_calloc(n, sizeof(*jobs));
    tids = av_calloc(n, sizeof(*tids));
    if (!jobs || !tids)
        return 1;
    for (i = 0; i < n; i++) {
        jobs[i].path = argv[arg + i * 2];
        jobs[i].pix_fmt = argv[arg + i * 2 + 1];
        jobs[i].id = i;
        jobs[i].hardware = hardware;
        jobs[i].device = device;
        jobs[i].teardown_after = (teardown && i == 0) ? 2 : 0;
        jobs[i].invalid = invalid && i == 0;
    }
    if (n == 1) {
        run_job(&jobs[0]);
    } else {
        for (i = 0; i < n; i++)
            if (pthread_create(&tids[i], NULL, run_job, &jobs[i]))
                return 1;
        for (i = 0; i < n; i++)
            pthread_join(tids[i], NULL);
    }
    ret = 0;
    for (i = 0; i < n; i++) {
        if (jobs[i].ret && !(invalid && i == 0 && jobs[i].ret == 0)) {
            if (jobs[i].ret)
                ret = 1;
        }
        if (jobs[i].ret && !invalid) {
            fprintf(stderr, "worker %d failed: %s\n", i, av_err2str(jobs[i].ret));
            ret = 1;
        }
        printf("worker %d frames=%u MD5=", i, jobs[i].frames);
        for (int b = 0; b < 16; b++)
            printf("%02x", jobs[i].md5[b]);
        putchar('\n');
        av_frame_free(&jobs[i].kept);
    }
    av_buffer_unref(&device);
    av_free(jobs);
    av_free(tids);
    return ret;
}

/* SPDX-License-Identifier: GPL-3.0-or-later
 * Real FFmpeg client; preserves the separate grudev/model overlap harness.
 * Hardware mode requires the portable guard and actual VAAPI frames.
 * Maintainer remediation of z23's initial client; no hardware acceptance here. */
#define main single_stream_main
#include "frame-check.c"
#undef main
#include <errno.h>
#include <pthread.h>
#include <stdint.h>
#include <stdlib.h>
#include <sys/stat.h>
#include <time.h>
#include <unistd.h>

#define MAX_JOBS 4
struct schedule {
    pthread_mutex_t mutex;
    pthread_cond_t cond;
    struct timespec limit;
    int count, ready, midpoint, abort, victim_done;
    int mode; /* 0 normal, 1 teardown, 2 valid API error then teardown */
    unsigned frames, seed;
};
struct job {
    const char *path, *format;
    int id, hardware, ret, retained, client_error;
    AVBufferRef *device;
    struct schedule *schedule;
    unsigned frames;
    uint8_t digest[16], held_digest[16];
};
static void abort_schedule(struct schedule *s)
{
    pthread_mutex_lock(&s->mutex);
    s->abort=1;
    pthread_cond_broadcast(&s->cond);
    pthread_mutex_unlock(&s->mutex);
}
static int wait_changed(struct schedule *s)
{
    int r=pthread_cond_timedwait(&s->cond,&s->mutex,&s->limit);
    if (r) {
        s->abort=1;
        pthread_cond_broadcast(&s->cond);
    }
    return s->abort ? AVERROR(ECANCELED) : 0;
}
static int rendezvous(struct schedule *s,int midpoint)
{
    int ret=0;
    pthread_mutex_lock(&s->mutex);
    int *arrived=midpoint ? &s->midpoint : &s->ready;
    ++*arrived;
    pthread_cond_broadcast(&s->cond);
    while (*arrived<s->count && !s->abort)
        if ((ret=wait_changed(s))<0) break;
    if (s->abort) ret=AVERROR(ECANCELED);
    pthread_mutex_unlock(&s->mutex);
    return ret;
}
static int wait_victim(struct schedule *s)
{
    int ret=0;
    pthread_mutex_lock(&s->mutex);
    while (!s->victim_done && !s->abort)
        if ((ret=wait_changed(s))<0) break;
    if (s->abort) ret=AVERROR(ECANCELED);
    pthread_mutex_unlock(&s->mutex);
    return ret;
}
static int interrupted(void *opaque)
{
    struct schedule *s=opaque;
    struct timespec now;
    clock_gettime(CLOCK_MONOTONIC,&now);
    return now.tv_sec>s->limit.tv_sec ||
        (now.tv_sec==s->limit.tv_sec && now.tv_nsec>=s->limit.tv_nsec);
}
static int frame_hash(struct job *job,struct check *check,AVFrame *frame)
{
    flockfile(stdout);
    printf("worker-frame %d ",job->id);
    int ret=hash_frame(check,frame);
    funlockfile(stdout);
    return ret;
}
static int digest_one(struct job *job,AVFrame *frame,uint8_t digest[16])
{
    struct check c={.hardware=job->hardware,.format=av_get_pix_fmt(job->format)};
    c.md5=av_md5_alloc();
    if (!c.md5) return AVERROR(ENOMEM);
    av_md5_init(c.md5);
    int ret=frame_hash(job,&c,frame);
    if (!ret) av_md5_final(c.md5,digest);
    av_free(c.md5);sws_freeContext(c.sws);
    return ret;
}
/* Use a LIVE decoder: enter drain, consume buffered output, then require the
 * documented EOF response to another packet. Never call through a freed pointer.
 * These deliberately discarded drain frames are outside the victim's selected
 * midpoint prefix; the expected victim output is exactly frames/2. */
static int reject_after_drain(AVCodecContext *decoder,AVFrame *scratch)
{
    int r;
    unsigned drained=0;
    while ((r=avcodec_receive_frame(decoder,scratch))>=0) {
        av_frame_unref(scratch);
        if (++drained>256) return AVERROR_INVALIDDATA;
    }
    if (r!=AVERROR(EAGAIN)) return r;
    if ((r=avcodec_send_packet(decoder,NULL))<0) return r;
    while ((r=avcodec_receive_frame(decoder,scratch))>=0) {
        av_frame_unref(scratch);
        if (++drained>256) return AVERROR_INVALIDDATA;
    }
    if (r!=AVERROR_EOF) return AVERROR_INVALIDDATA;
    r=avcodec_send_packet(decoder,NULL);
    return r==AVERROR_EOF ? 0 : AVERROR_INVALIDDATA;
}
static int receive(struct job *job,struct check *check,AVCodecContext **decoder,
                   AVFrame *frame,AVFrame **held)
{
    int ret;
    struct schedule *s=job->schedule;
    while ((ret=avcodec_receive_frame(*decoder,frame))>=0) {
        if (check->frames>=s->frames) return AVERROR_INVALIDDATA;
        if ((ret=frame_hash(job,check,frame))<0) return ret;
        if (s->mode && check->frames==s->frames/2) {
            if (!job->id) {
                *held=av_frame_clone(frame);
                if (!*held) return AVERROR(ENOMEM);
                if ((ret=digest_one(job,*held,job->held_digest))<0) return ret;
            }
            av_frame_unref(frame);
            if ((ret=rendezvous(s,1))<0) return ret;
            if (!job->id) {
                if (s->mode==2) {
                    if ((ret=reject_after_drain(*decoder,frame))<0) return ret;
                    job->client_error=1;
                }
                avcodec_free_context(decoder);
                uint8_t after[16];
                if ((ret=digest_one(job,*held,after))<0) return ret;
                if (memcmp(after,job->held_digest,sizeof(after))) return AVERROR_INVALIDDATA;
                job->retained=1;
                pthread_mutex_lock(&s->mutex);
                s->victim_done=1;pthread_cond_broadcast(&s->cond);
                pthread_mutex_unlock(&s->mutex);
                return 1; /* successful intentional midpoint stop */
            }
            if ((ret=wait_victim(s))<0) return ret;
        } else av_frame_unref(frame);
    }
    return ret==AVERROR(EAGAIN) || ret==AVERROR_EOF ? 0 : ret;
}
static void *run_job(void *opaque)
{
    struct job *job=opaque;
    struct schedule *s=job->schedule;
    AVFormatContext *input=avformat_alloc_context();
    AVCodecContext *decoder=NULL;
    AVPacket *packet=av_packet_alloc();
    AVFrame *frame=av_frame_alloc(),*held=NULL;
    struct check check={.hardware=job->hardware,.format=av_get_pix_fmt(job->format)};
#if LIBAVCODEC_VERSION_INT >= AV_VERSION_INT(59, 0, 100)
    const AVCodec *codec=NULL;
#else
    AVCodec *codec=NULL;
#endif
    int ret=AVERROR(ENOMEM),stream=-1,stopped=0;
    if (!input || !packet || !frame || check.format==AV_PIX_FMT_NONE) goto done;
    input->interrupt_callback=(AVIOInterruptCB){.callback=interrupted,.opaque=s};
    check.md5=av_md5_alloc();
    if (!check.md5) goto done;
    av_md5_init(check.md5);
    if ((ret=avformat_open_input(&input,job->path,NULL,NULL))<0 ||
        (ret=avformat_find_stream_info(input,NULL))<0) goto done;
    stream=av_find_best_stream(input,AVMEDIA_TYPE_VIDEO,-1,-1,&codec,0);
    if (stream<0) {ret=stream;goto done;}
    decoder=avcodec_alloc_context3(codec);
    if (!decoder) {ret=AVERROR(ENOMEM);goto done;}
    if ((ret=avcodec_parameters_to_context(decoder,input->streams[stream]->codecpar))<0) goto done;
    decoder->thread_count=1;
    decoder->apply_cropping=0;
    if (job->hardware) {
        decoder->hw_device_ctx=av_buffer_ref(job->device);
        if (!decoder->hw_device_ctx) {ret=AVERROR(ENOMEM);goto done;}
        decoder->get_format=vaapi_format;
        decoder->extra_hw_frames=8;
    }
    if ((ret=avcodec_open2(decoder,codec,NULL))<0) goto done;
    if ((ret=rendezvous(s,0))<0) goto done;
    /* Seed fixes bounded per-worker pacing; it does not claim deterministic OS
     * interleaving or prove real in-driver overlap (the model hook owns that). */
    struct timespec delay={0,(long)((s->seed^(unsigned)job->id)%7)*100000};
    nanosleep(&delay,NULL);
    while ((ret=av_read_frame(input,packet))>=0) {
        if (packet->stream_index==stream) {
            ret=avcodec_send_packet(decoder,packet);
            if (ret>=0) ret=receive(job,&check,&decoder,frame,&held);
        }
        av_packet_unref(packet);
        if (ret==1) {stopped=1;ret=0;break;}
        if (ret<0) goto done;
    }
    if (ret!=AVERROR_EOF && ret<0) goto done;
    if (!stopped) {
        if ((ret=avcodec_send_packet(decoder,NULL))<0) goto done;
        ret=receive(job,&check,&decoder,frame,&held);
        if (ret<0) goto done;
        stopped=ret==1;
    }
    unsigned expected=s->mode && !job->id ? s->frames/2 : s->frames;
    if (check.frames!=expected || (s->mode && !job->id && !stopped)) {
        ret=AVERROR_INVALIDDATA;goto done;
    }
    av_md5_final(check.md5,job->digest);
    job->frames=check.frames;
    ret=0;
done:
    av_packet_free(&packet);av_frame_free(&frame);av_frame_free(&held);
    av_free(check.md5);sws_freeContext(check.sws);
    avcodec_free_context(&decoder);avformat_close_input(&input);
    job->ret=ret;
    if (ret<0) abort_schedule(s);
    return NULL;
}
static int number(const char *s,unsigned min,unsigned max,unsigned *out)
{
    char *end;errno=0;
    if (!*s || *s=='-') return 0;
    unsigned long n=strtoul(s,&end,10);
    if (errno || *end || n<min || n>max) return 0;
    *out=(unsigned)n;return 1;
}
int main(int argc,char **argv)
{
    int arg=2,hardware,n,created=0,ret=1,wait_start=0;
    unsigned threads=0,frames=8,seed=1;
    int mode=0;
    AVBufferRef *device=NULL;
    struct schedule s={0};
    struct job jobs[MAX_JOBS]={0};
    pthread_t tids[MAX_JOBS];
    av_log_set_level(AV_LOG_ERROR);
    if (argc<4) return 2;
    if (!strcmp(argv[1],"software")) hardware=0;
    else if (!strcmp(argv[1],"vaapi")) hardware=1;
    else return 2;
    while (arg<argc && argv[arg][0]=='-') {
        if (!strcmp(argv[arg],"--wait-start")) {wait_start=1;arg++;continue;}
        if (arg+1>=argc) return 2;
        if (!strcmp(argv[arg],"--threads")) {
            if (!number(argv[arg+1],1,4,&threads) || threads==3) return 2;
        } else if (!strcmp(argv[arg],"--frames")) {
            if (!number(argv[arg+1],4,256,&frames) || frames%2) return 2;
        } else if (!strcmp(argv[arg],"--seed")) {
            if (!number(argv[arg+1],0,UINT32_MAX,&seed)) return 2;
        } else if (!strcmp(argv[arg],"--schedule")) {
            if (!strcmp(argv[arg+1],"normal")) mode=0;
            else if (!strcmp(argv[arg+1],"teardown")) mode=1;
            else if (!strcmp(argv[arg+1],"failure")) mode=2;
            else return 2;
        } else return 2;
        arg+=2;
    }
    if ((argc-arg)%2) return 2;
    n=(argc-arg)/2;
    if (n<1 || n>MAX_JOBS || n==3 || (threads && threads!=(unsigned)n)) return 2;
    for (int i=0;i<n;i++) {
        struct stat st;
        if (stat(argv[arg+2*i],&st) || !S_ISREG(st.st_mode) || av_get_pix_fmt(argv[arg+2*i+1])==AV_PIX_FMT_NONE) return 2;
    }
    if (hardware && (!getenv("LIBVA_HW_GUARD_LEASE") || !*getenv("LIBVA_HW_GUARD_LEASE"))) {
        fprintf(stderr,"unguarded hardware run refused; use tests/hwguard.py\n");return 2;
    }
    if (wait_start && getchar()!='S') return 2; /* outer runner's finite launch barrier */
    if (hardware && av_hwdevice_ctx_create(&device,AV_HWDEVICE_TYPE_VAAPI,NULL,NULL,0)<0) return 1;
    s.count=n;s.frames=frames;s.seed=seed;s.mode=mode;
    clock_gettime(CLOCK_MONOTONIC,&s.limit);s.limit.tv_sec+=20;
    pthread_condattr_t attr;
    if (pthread_mutex_init(&s.mutex,NULL)) goto release;
    if (pthread_condattr_init(&attr)) goto mutex;
    if (pthread_condattr_setclock(&attr,CLOCK_MONOTONIC)) goto attr;
    if (pthread_cond_init(&s.cond,&attr)) goto attr;
    for (int i=0;i<n;i++) {
        jobs[i]=(struct job){.path=argv[arg+i*2],.format=argv[arg+i*2+1],.id=i,
            .hardware=hardware,.device=device,.schedule=&s};
        if (pthread_create(&tids[i],NULL,run_job,&jobs[i])) {abort_schedule(&s);break;}
        created++;
    }
    for (int i=0;i<created;i++) pthread_join(tids[i],NULL);
    ret=created!=n;
    for (int i=0;i<created;i++) if (jobs[i].ret<0) {
        fprintf(stderr,"worker %d failed: %s\n",i,av_err2str(jobs[i].ret));ret=1;
    }
    if (!ret) {
        printf("schedule %s seed=%u threads=%d\n",mode==0?"normal":mode==1?"teardown":"failure",seed,n);
        for (int i=0;i<n;i++) {
            printf("worker %d frames=%u MD5=",i,jobs[i].frames);
            for (int b=0;b<16;b++) printf("%02x",jobs[i].digest[b]);
            putchar('\n');
            if (jobs[i].retained) {
                printf("retained %d MD5=",i);
                for(int b=0;b<16;b++) printf("%02x",jobs[i].held_digest[b]);
                printf(" client_error=%d\n",jobs[i].client_error);
            }
        }
    }
    pthread_cond_destroy(&s.cond);
attr:
    pthread_condattr_destroy(&attr);
mutex:
    pthread_mutex_destroy(&s.mutex);
release:
    av_buffer_unref(&device);
    return ret;
}

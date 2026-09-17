#!/usr/bin/env python3
"""Run a race-free startup probe before the real TSan schedules."""
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import tempfile

STARTUP_UNAVAILABLE = re.compile(
    r'^(?:FATAL: ThreadSanitizer: unexpected memory mapping [^\n]+|'
    r'FATAL: ThreadSanitizer: unsupported VMA range[^\n]*|'
    r'FATAL: ThreadSanitizer: memory layout is incompatible[^\n]*)$', re.M)
PROBE = '''#include <pthread.h>
#include <stdatomic.h>
static atomic_int shared;
static void *fn(void *p) { shared++; return p; }
int main(void) { pthread_t t; if (pthread_create(&t, 0, fn, 0)) return 1;
return pthread_join(t, 0); }
'''

def probe_status(code, output):
    if code == 0:
        return 0
    # A diagnostic alongside a startup line must not become a skip.
    if 'WARNING: ThreadSanitizer' in output or 'SUMMARY: ThreadSanitizer' in output:
        return 1
    lines = [line for line in output.splitlines() if line.strip()]
    if lines and all(STARTUP_UNAVAILABLE.fullmatch(line) for line in lines):
        return 77
    return 1


def probe(compiler, work):
    source, binary = work / 'probe.c', work / 'probe'
    source.write_text(PROBE)
    built = subprocess.run(compiler + ['-fsanitize=thread', '-pthread', '-o', str(binary), str(source)],
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=30)
    if built.returncode:
        print(built.stdout)
        # An unavailable sanitizer/toolchain is distinguishable from a source error.
        if re.search(r'cannot find -ltsan|cannot find libtsan_preinit\.o: No such file or directory|unsupported.*fsanitize=thread|libclang_rt.*tsan.*(?:No such file|not found)', built.stdout):
            return 77
        return 1
    run = subprocess.run([str(binary)], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                         text=True, timeout=30)
    print(run.stdout, end='')
    return probe_status(run.returncode, run.stdout)


def self_test():
    assert probe_status(66, 'FATAL: ThreadSanitizer: unexpected memory mapping 0x1-0x2\n') == 77
    assert probe_status(66, 'FATAL: ThreadSanitizer: unknown failure\n') == 1
    assert probe_status(66, 'WARNING: ThreadSanitizer: data race\n') == 1
    assert probe_status(66, 'FATAL: ThreadSanitizer: unexpected memory mapping 0x1-0x2\nSUMMARY: ThreadSanitizer: data race\n') == 1
    with tempfile.TemporaryDirectory() as directory:
        work = Path(directory)
        compiler = work / 'fake compiler.py'
        compiler.write_text("import pathlib,sys\nassert '--extra-flag' in sys.argv\np=pathlib.Path(sys.argv[sys.argv.index('-o')+1]);p.write_text('#!/bin/sh\\nexit 0\\n');p.chmod(0o755)\n")
        argv = shlex.split('%s %s --extra-flag' % (shlex.quote(sys.executable), shlex.quote(str(compiler))))
        assert probe(argv, work) == 0
    print('TSan startup classification and compiler arguments: PASS')
    return 0


def main():
    if sys.argv[1:] == ['--self-test']:
        return self_test()
    compiler = shlex.split(os.environ.get('CC', 'cc'))
    if not compiler or not shutil.which(compiler[0]) or not shutil.which('meson'):
        print('concurrent-tsan: SKIP (compiler or Meson unavailable)')
        return 77
    root = Path(__file__).resolve().parent.parent
    with tempfile.TemporaryDirectory() as directory:
        work = Path(directory)
        status = probe(compiler, work)
        if status:
            print('concurrent-tsan: %s (runtime/toolchain probe)' % ('SKIP' if status == 77 else 'FAIL'))
            return status
        build = work / 'build'
        env = dict(os.environ, CC=' '.join(shlex.quote(arg) for arg in compiler), TSAN_OPTIONS='halt_on_error=1:exitcode=66')
        subprocess.run(['meson', 'setup', str(build), str(root), '-Db_sanitize=thread'], env=env, check=True, timeout=60, stdout=subprocess.DEVNULL)
        subprocess.run(['meson', 'compile', '-C', str(build), 'tests/concurrent-stress'], env=env, check=True, timeout=120, stdout=subprocess.DEVNULL)
        for schedule in ['threads 1 12 3 1', 'threads 2 12 3 2', 'threads 4 12 3 4',
                         'teardown 2 12 3 812734691', 'teardown 4 12 3 812734691', 'failure 4 12 3 3372110043',
                         'overlap 2 6 3 73204115', 'overlap-actor 2 6 3 1946285037',
                         'overlap-late 2 6 3 73204115', 'overlap-counters']:
            print('tsan-schedule: ' + schedule, flush=True)
            subprocess.run([str(build / 'tests/concurrent-stress')] + schedule.split(), env=env,
                           check=True, timeout=90, stdout=subprocess.DEVNULL)
    print('concurrent-tsan: EXECUTED and PASSED (9 schedules x 3 repetitions plus counter oracle)')
    return 0

if __name__ == '__main__':
    try:
        sys.exit(main())
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        print('concurrent-tsan: FAIL: %s' % error, file=sys.stderr)
        sys.exit(1)

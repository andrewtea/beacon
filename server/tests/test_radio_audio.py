"""Radio audio on its way to Ember: what kind of file it is, how long it really
plays, and how ffmpeg is confined while it decodes one."""

import ctypes
import errno
import io
import math
import os
import shutil
import subprocess
import sys
import threading
import time
import wave
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from beacon.radio import (
    AudioError,
    ConversionStopped,
    ConversionUnavailable,
    Ffmpeg,
    plain_wav,
)
from beacon.radio import audio as audio_module
from beacon.radio.audio import (
    MAX_SECONDS,
    SAMPLE_RATE,
    confinement,
    container,
    core_dump_problem,
    forbid_core_dumps,
    pcm_to_wav,
    wav_seconds,
)

_CAN_CONVERT = sys.platform == "linux" and Ffmpeg().problem() is None
if os.environ.get("CI") and not _CAN_CONVERT:
    # CI installs ffmpeg and bubblewrap, so that the conversion and its confinement
    # are not quietly left untested there.
    raise RuntimeError(
        "CI runs the audio conversion tests, which need ffmpeg and a working bwrap on Linux."
    )
needs_ffmpeg = pytest.mark.skipif(
    not _CAN_CONVERT, reason="Converting radio audio needs ffmpeg and bubblewrap, on Linux."
)


def tone(seconds: float, rate: int = SAMPLE_RATE) -> bytes:
    """A generated tone as a WAV file. Nothing in these tests is a recording."""
    samples = b"".join(
        round(8000 * math.sin(2 * math.pi * 440 * n / rate)).to_bytes(2, "little", signed=True)
        for n in range(round(seconds * rate))
    )
    out = io.BytesIO()
    with wave.open(out, "wb") as clip:
        clip.setnchannels(1)
        clip.setsampwidth(2)
        clip.setframerate(rate)
        clip.writeframes(samples)
    return out.getvalue()


def as_m4a(wav: bytes, directory: Path) -> bytes:
    """The clip as ffmpeg writes an M4A file, which is how a recorder that compresses
    its calls keeps one."""
    target = directory / "call.m4a"
    subprocess.run(
        ["ffmpeg", "-loglevel", "quiet", "-y", "-f", "wav", "-i", "pipe:0", "-c:a", "aac", target],
        input=wav,
        check=True,
    )
    return target.read_bytes()


def test_a_file_is_known_by_how_it_begins_and_not_by_its_name() -> None:
    assert container(tone(0.1)) == "wav"
    assert container(b"\x00\x00\x00\x1cftypM4A \x00\x00\x00\x00") == "m4a"
    assert container(b"ID3\x04\x00\x00\x00\x00\x00\x00") == "mp3"
    assert container(b"\xff\xfb\x90\x00") == "mp3"
    for other in (b"", b"RIFF", b"<html>call.m4a</html>", b"#EXTM3U\n", b"OggS\x00"):
        assert container(other) is None


def test_a_wav_file_says_how_long_it_plays() -> None:
    assert wav_seconds(tone(2.5)) == pytest.approx(2.5)
    assert wav_seconds(pcm_to_wav(b"\x00\x00" * SAMPLE_RATE)) == pytest.approx(1.0)
    assert wav_seconds(b"not a wav file") is None
    assert wav_seconds(b"") is None


def as_mp3(wav: bytes, directory: Path) -> bytes:
    target = directory / "call.mp3"
    subprocess.run(
        ["ffmpeg", "-loglevel", "quiet", "-y", "-f", "wav", "-i", "pipe:0", target],
        input=wav,
        check=True,
    )
    return target.read_bytes()


def test_a_wav_file_is_written_out_again_as_its_samples_and_nothing_else() -> None:
    clean = tone(1.5, rate=8_000)
    # A chunk of text after the samples, as a recorder can add.
    note = b"LIST" + (20).to_bytes(4, "little") + b"INFOINAM" + b"Jane Doe 555"
    padded = clean[:4] + (len(clean) - 8 + len(note)).to_bytes(4, "little") + clean[8:] + note
    assert wav_seconds(padded) == pytest.approx(1.5)

    assert plain_wav(padded) == clean
    assert plain_wav(clean) == clean


def test_a_wav_file_plays_for_as_long_as_its_samples_whatever_its_header_says() -> None:
    clean = tone(1.0)
    # The header says ten times the samples that follow it.
    lying = clean[:40] + (10 * (len(clean) - 44)).to_bytes(4, "little") + clean[44:]
    assert wav_seconds(lying) == pytest.approx(10.0)

    assert wav_seconds(plain_wav(lying)) == pytest.approx(1.0)


def test_a_wav_file_longer_than_any_call_is_read_only_far_enough_to_know_it() -> None:
    long = pcm_to_wav(b"\x00\x01" * SAMPLE_RATE * (MAX_SECONDS + 30))

    assert wav_seconds(plain_wav(long)) == MAX_SECONDS + 1


@pytest.mark.parametrize(
    "not_samples",
    [
        b"",
        b"RIFF\x00\x00\x00\x00WAVE",
        b"RIFF\x24\x00\x00\x00WAVEJane Doe, 1458 Hayes Street",
        # A WAV file with no samples in it.
        pcm_to_wav(b""),
        # A compressed WAV file, whose format tag is not plain samples.
        tone(0.1)[:20] + (85).to_bytes(2, "little") + tone(0.1)[22:],
    ],
)
def test_what_is_not_a_wav_file_of_plain_samples_is_refused_with_a_fixed_sentence(
    not_samples: bytes,
) -> None:
    with pytest.raises(AudioError) as failure:
        _while_handling(ValueError("Jane Doe"), lambda: plain_wav(not_samples))

    assert str(failure.value) == "A radio call is not a WAV file of plain samples."
    assert (failure.value.__cause__, failure.value.__context__) == (None, None)


@needs_ffmpeg
def test_an_m4a_call_becomes_a_mono_wav_of_the_same_length(tmp_path: Path) -> None:
    m4a = as_m4a(tone(3.0, rate=8_000), tmp_path)
    assert container(m4a) == "m4a"

    wav = Ffmpeg().to_wav(m4a, "m4a")

    assert container(wav) == "wav"
    with wave.open(io.BytesIO(wav)) as clip:
        assert (clip.getnchannels(), clip.getsampwidth(), clip.getframerate()) == (1, 2, 16_000)
    # AAC pads a clip by a few hundredths of a second.
    assert wav_seconds(wav) == pytest.approx(3.0, abs=0.15)


@needs_ffmpeg
def test_an_mp3_call_is_decoded_too(tmp_path: Path) -> None:
    mp3 = as_mp3(tone(2.0), tmp_path)
    assert container(mp3) == "mp3"

    assert wav_seconds(Ffmpeg().to_wav(mp3, "mp3")) == pytest.approx(2.0, abs=0.15)


@needs_ffmpeg
def test_a_clip_longer_than_any_call_is_decoded_only_far_enough_to_know_it(
    tmp_path: Path,
) -> None:
    long = as_m4a(tone(MAX_SECONDS + 20, rate=8_000), tmp_path)

    # A second past the longest call, so whoever asked can see that it is longer.
    assert wav_seconds(Ffmpeg().to_wav(long, "m4a")) == MAX_SECONDS + 1


@needs_ffmpeg
def test_a_call_is_converted_without_a_file_of_it_being_written(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    m4a = as_m4a(tone(1.0), tmp_path)
    empty = tmp_path / "empty"
    empty.mkdir()
    # Wherever a program would put a file it was not told where to put.
    monkeypatch.chdir(empty)
    for variable in ("TMPDIR", "TEMP", "TMP", "HOME"):
        monkeypatch.setenv(variable, str(empty))
    # And where ffmpeg would write a report about the clip, were it told to.
    report = empty / "report.log"
    monkeypatch.setenv("FFREPORT", f"file={report}:level=48")
    assert _ffmpeg_reports_when_told(m4a, report), "The setting does make ffmpeg write a report."
    report.unlink()

    assert wav_seconds(Ffmpeg().to_wav(m4a, "m4a")) == pytest.approx(1.0, abs=0.15)

    assert list(empty.iterdir()) == []


def _ffmpeg_reports_when_told(m4a: bytes, report: Path) -> bool:
    """Whether ffmpeg, run with this environment as it is, writes its report."""
    subprocess.run(
        ["ffmpeg", "-loglevel", "quiet", "-f", "mov", "-i", "pipe:0", "-f", "null", "-"],
        input=m4a,
        check=False,
    )
    return report.exists()


@needs_ffmpeg
def test_ffmpeg_is_confined_to_the_systems_programs_with_no_network_and_no_room_to_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    secret = tmp_path / "secret.txt"
    secret.write_text("MODEL_API_KEY")
    monkeypatch.setenv("MODEL_API_KEY", "a-key-ffmpeg-must-not-see")
    bwrap = shutil.which("bwrap")
    assert bwrap is not None
    look = (
        f"cat {secret} 2>/dev/null; ls /home /root /tmp 2>/dev/null; env; "
        "echo interfaces=$(ls /sys/class/net 2>/dev/null | tr '\\n' ' '); "
        "tail -n +3 /proc/net/dev | cut -d: -f1 | tr -d ' ' | sed 's/^/interface=/'; "
        "echo core=$(ulimit -c) file=$(ulimit -f) time=$(ulimit -t) memory=$(ulimit -v) "
        "open=$(ulimit -n); "
        "(echo x > /usr/beacon-test-file) 2>/dev/null; echo wrote=$?"
    )

    seen = subprocess.run(
        [*confinement(bwrap), "/bin/sh", "-c", look],
        capture_output=True,
        text=True,
        # As `Ffmpeg` runs it. The environment of this process is not handed on.
        env={},
        cwd="/",
        check=True,
    ).stdout

    assert "MODEL_API_KEY" not in seen
    assert "a-key-ffmpeg-must-not-see" not in seen
    assert str(tmp_path) not in seen
    # No interface but the loopback of a network namespace of its own.
    assert [line for line in seen.splitlines() if line.startswith("interface=")] == ["interface=lo"]
    assert "core=0 file=0 time=30 memory=1048576 open=64" in seen
    assert "wrote=0" not in seen


@needs_ffmpeg
def test_ffmpeg_is_shown_the_program_directories_and_not_the_rest_of_usr() -> None:
    bwrap = shutil.which("bwrap")
    assert bwrap is not None

    seen = subprocess.run(
        [*confinement(bwrap), "/bin/sh", "-c", "ls -d /usr/* /etc/* /opt /var /home /root"],
        capture_output=True,
        text=True,
        env={},
        cwd="/",
        check=False,
    ).stdout.split()

    # Where programs and their libraries are, and what the loader reads. Not
    # /usr/local or /usr/share, where a deployment can keep things of its own.
    assert set(seen) <= {
        "/usr/bin",
        "/usr/lib",
        "/usr/lib32",
        "/usr/lib64",
        "/usr/libx32",
        "/etc/ld.so.cache",
        "/etc/alternatives",
    }
    assert "/usr/bin" in seen


@needs_ffmpeg
def test_a_path_that_is_hidden_is_covered_over_inside_a_directory_ffmpeg_is_shown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A deployment that keeps Beacon's data under a directory the decoder sees.
    data = tmp_path / "lib" / "beacon-data"
    data.mkdir(parents=True)
    (data / "ledger.sqlite3").write_text("private")
    (tmp_path / "lib" / "libshared.so").write_text("a library")
    monkeypatch.setattr(
        "beacon.radio.audio._SYSTEM_DIRS", (*audio_module._SYSTEM_DIRS, str(tmp_path / "lib"))
    )
    bwrap = shutil.which("bwrap")
    assert bwrap is not None

    def look(hidden: list[Path]) -> str:
        return subprocess.run(
            [*confinement(bwrap, hidden), "/bin/sh", "-c", f"ls {tmp_path}/lib {data}"],
            capture_output=True,
            text=True,
            env={},
            cwd="/",
            check=False,
        ).stdout

    assert "ledger.sqlite3" in look([])
    covered = look([data])
    assert "ledger.sqlite3" not in covered
    assert "libshared.so" in covered
    # A path outside what is shown needs no covering, and adds nothing to the command.
    assert confinement(bwrap, [Path("/home/somebody/data")]) == confinement(bwrap)


@pytest.mark.parametrize(
    ("setting", "problem"),
    [
        ("core\n", None),
        ("/var/crash/core.%p\n", None),
        # Piped to a collector, which Linux hands the core whatever the limit is.
        ("|/usr/lib/systemd/systemd-coredump %P %u %g %s %t %c %h\n", "to a collector"),
        ("|/usr/share/apport/apport -p%p -s%s -c%c\n", "to a collector"),
        (" |/bin/false\n", "to a collector"),
        # Sent to a collector's socket, in both forms Linux has, which also get the
        # core whatever the limit is.
        ("@/run/systemd/coredump.socket\n", "to a collector"),
        ("@@/run/systemd/coredump.socket\n", "to a collector"),
    ],
)
def test_a_host_that_hands_core_dumps_to_a_collector_is_a_problem(
    tmp_path: Path, setting: str, problem: str | None
) -> None:
    pattern = tmp_path / "core_pattern"
    pattern.write_text(setting)

    found = core_dump_problem(pattern)

    assert (found is None) if problem is None else (problem in (found or ""))


def test_a_host_whose_core_dump_setting_cannot_be_read_is_a_problem(tmp_path: Path) -> None:
    found = core_dump_problem(tmp_path / "no-such-setting")

    assert found is not None
    assert "cannot be read" in found


# `prctl` operations: whether the kernel makes a core of this process.
_GET_DUMPABLE, _SET_DUMPABLE = 3, 4


def test_this_process_can_be_kept_from_leaving_a_core_in_a_file_or_with_a_collector() -> None:
    resource = pytest.importorskip("resource")
    before = resource.getrlimit(resource.RLIMIT_CORE)
    libc = ctypes.CDLL(None) if sys.platform == "linux" else None
    try:
        if libc is not None:
            libc.prctl(_SET_DUMPABLE, 1, 0, 0, 0)

        assert forbid_core_dumps() is True

        # No core file, and on Linux no core at all, which a collector would get
        # whatever the limit says.
        assert resource.getrlimit(resource.RLIMIT_CORE)[0] == 0
        if libc is not None:
            assert libc.prctl(_GET_DUMPABLE, 0, 0, 0, 0) == 0
    finally:
        resource.setrlimit(resource.RLIMIT_CORE, before)
        if libc is not None:
            libc.prctl(_SET_DUMPABLE, 1, 0, 0, 0)


@pytest.mark.skipif(sys.platform != "linux", reason="The mark is Linux's.")
def test_a_process_that_cannot_be_marked_is_said_not_to_be_kept_from_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resource = pytest.importorskip("resource")
    before = resource.getrlimit(resource.RLIMIT_CORE)

    class Refusing:
        """A C library whose `prctl` changes nothing, as under a filter that forbids it."""

        def prctl(self, operation: int, *arguments: int) -> int:
            return 1 if operation == _GET_DUMPABLE else -1

    try:
        monkeypatch.setattr(ctypes, "CDLL", lambda *args, **kwargs: Refusing())
        assert forbid_core_dumps() is False

        def missing(*args: object, **kwargs: object) -> object:
            raise OSError("no C library")

        monkeypatch.setattr(ctypes, "CDLL", missing)
        assert forbid_core_dumps() is False
    finally:
        resource.setrlimit(resource.RLIMIT_CORE, before)


@needs_ffmpeg
def test_closing_the_converter_calls_off_a_conversion_and_refuses_the_next(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    m4a = as_m4a(tone(1.0), tmp_path)
    converter = Ffmpeg()
    # A conversion that would run for a minute: the confined shell sleeps instead
    # of becoming ffmpeg.
    monkeypatch.setattr("beacon.radio.audio._LIMITS", "sleep 60")
    outcome: list[object] = []

    def convert() -> None:
        try:
            outcome.append(converter.to_wav(m4a, "m4a"))
        except Exception as error:
            outcome.append(error)

    thread = threading.Thread(target=convert)
    thread.start()
    deadline = time.monotonic() + 10
    while not converter._running and time.monotonic() < deadline:
        time.sleep(0.01)
    began = time.monotonic()

    converter.close()
    thread.join(10)

    assert not thread.is_alive()
    assert time.monotonic() - began < 5
    assert isinstance(outcome[0], ConversionStopped)
    with pytest.raises(ConversionStopped):
        converter.to_wav(m4a, "m4a")


def _while_handling(error: Exception, call: Callable[[], object]) -> None:
    try:
        raise error
    except type(error):
        call()


@needs_ffmpeg
@pytest.mark.parametrize(
    "not_a_call",
    [
        b"",
        b"\x00\x00\x00\x1cftypM4A and then nothing that is audio",
        b"Jane Doe, 1458 Hayes Street",
        # A playlist, which ffmpeg left to itself would follow to other files.
        b"#EXTM3U\n#EXTINF:1,\nfile:///etc/hostname\n",
        b"ffconcat version 1.0\nfile /etc/hostname\n",
    ],
)
@pytest.mark.parametrize("kind", ["m4a", "mp3"])
def test_what_is_not_the_clip_it_is_said_to_be_is_refused_with_a_fixed_sentence(
    not_a_call: bytes, kind: str
) -> None:
    with pytest.raises(AudioError) as failure:
        _while_handling(ValueError("Jane Doe"), lambda: Ffmpeg().to_wav(not_a_call, kind))

    assert str(failure.value) == "A radio call could not be converted by ffmpeg."
    assert (failure.value.__cause__, failure.value.__context__) == (None, None)


_UNAVAILABLE = r"^The converter could not be run on this host just now\.$"


@needs_ffmpeg
@pytest.mark.parametrize("failing", ["memory", "process", "watch"])
def test_a_host_that_could_not_run_the_converter_says_so_and_nothing_about_the_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failing: str
) -> None:
    m4a = as_m4a(tone(1.0), tmp_path)
    started: list[subprocess.Popen[bytes]] = []
    real_popen = subprocess.Popen

    def out_of(*args: object, **kwargs: object) -> object:
        raise OSError(errno.EAGAIN, "Resource temporarily unavailable")

    def popen(*args: Any, **kwargs: Any) -> subprocess.Popen[bytes]:
        started.append(real_popen(*args, **kwargs))
        return started[-1]

    def no_thread(self: threading.Thread) -> None:
        raise RuntimeError("can't start new thread")

    if failing == "memory":
        monkeypatch.setattr(os, "memfd_create", out_of)
    elif failing == "process":
        monkeypatch.setattr(subprocess, "Popen", out_of)
    else:
        # The process runs, and would for a minute, and nothing can watch the time.
        monkeypatch.setattr("beacon.radio.audio._LIMITS", "sleep 60")
        monkeypatch.setattr(subprocess, "Popen", popen)
        monkeypatch.setattr(threading.Timer, "start", no_thread)
    converter = Ffmpeg()
    began = time.monotonic()

    # A good clip, which is not called a bad one: it can be handed over again.
    with pytest.raises(ConversionUnavailable, match=_UNAVAILABLE) as failure:
        _while_handling(ValueError("Jane Doe"), lambda: converter.to_wav(m4a, "m4a"))

    assert (failure.value.__cause__, failure.value.__context__) == (None, None)
    with pytest.raises(ConversionUnavailable, match=_UNAVAILABLE):
        converter.to_jpeg(b"\xff\xd8\xff\xd9", max_side=2048, max_bytes=1_000_000)
    # No process is left behind: each one that began was ended and waited for.
    assert time.monotonic() - began < 10
    assert len(started) == (2 if failing == "watch" else 0)
    assert all(process.returncode is not None for process in started)
    assert converter._running == set()


def test_a_missing_ffmpeg_is_the_same_fixed_sentence_and_a_problem_that_is_named() -> None:
    missing = Ffmpeg("/nonexistent/ffmpeg")

    with pytest.raises(AudioError, match=r"^A radio call could not be converted by ffmpeg\.$"):
        missing.to_wav(b"\x00\x00\x00\x1cftypM4A ", "m4a")
    # A photo is another caller's, which is told that the converter cannot be run.
    with pytest.raises(ConversionUnavailable, match=_UNAVAILABLE):
        missing.to_jpeg(b"\xff\xd8\xff\xd9", max_side=2048, max_bytes=1_000_000)
    if hasattr(os, "memfd_create"):
        assert missing.problem() == "Radio audio is converted by ffmpeg, which is not installed."


def test_a_missing_bubblewrap_is_a_problem_that_is_named() -> None:
    if not hasattr(os, "memfd_create") or shutil.which("ffmpeg") is None:
        pytest.skip("Needs ffmpeg, on Linux.")
    unconfined = Ffmpeg(bwrap="/nonexistent/bwrap")

    assert unconfined.problem() == (
        "ffmpeg is run confined by bubblewrap (bwrap), which is not installed."
    )
    # And nothing is converted without it.
    with pytest.raises(AudioError):
        unconfined.to_wav(b"\x00\x00\x00\x1cftypM4A ", "m4a")

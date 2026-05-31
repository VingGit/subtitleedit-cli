#!/usr/bin/env python3
"""
Batch MKV → SRT subtitle converter.

Extracts PGS (BluRay SUP) subtitle tracks from MKV files,
then uses seconv OCR to convert them to SRT format.
Processes files in parallel using ThreadPoolExecutor.

Usage:
    python batch_convert.py [--workers N] [--dry-run] [--keep-sup]
"""

import subprocess
import json
import os
import sys
import argparse
import logging
import time
from pathlib import Path
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
DOCKER_IMAGE = "seconv-batch:1.0"
INPUT_FOLDER = "movies"
OUTPUT_FOLDER = "generatedMovieSubtitles"
DEFAULT_WORKERS = 4
OCR_DB = "Latin"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------
@dataclass
class SubtitleTrack:
    mkv_path: Path
    track_index: int        # absolute stream index in the file
    sub_stream_index: int   # subtitle-stream-only index (0, 1, 2 …)
    language: str
    codec_name: str
    title: str = ""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def docker_path(p: Path) -> str:
    """Resolve a local path and normalise to forward slashes for Docker."""
    return str(p.resolve()).replace("\\", "/")


def _docker_env() -> dict:
    """Return an env dict that prevents MSYS / Git-Bash path mangling."""
    env = os.environ.copy()
    env["MSYS_NO_PATHCONV"] = "1"
    env["MSYS2_ARG_CONV_EXCL"] = "*"
    return env


def safe_filename_part(s: str) -> str:
    """Sanitise a string for use in a filename."""
    return "".join(c if c.isalnum() or c in "-_." else "_" for c in s)


def fmt_duration(seconds: float) -> str:
    """Format seconds as human-readable duration."""
    if seconds < 60:
        return f"{seconds:.0f}s"
    m, s = divmod(int(seconds), 60)
    if m < 60:
        return f"{m}m {s:02d}s"
    h, m = divmod(m, 60)
    return f"{h}h {m:02d}m {s:02d}s"


def fmt_eta(done: int, total: int, elapsed: float) -> str:
    """Estimate remaining time based on progress so far."""
    if done == 0 or elapsed == 0:
        return "estimating..."
    rate = done / elapsed
    remaining = (total - done) / rate
    return f"~{fmt_duration(remaining)} remaining"


# ---------------------------------------------------------------------------
# Phase 1 – Probe
# ---------------------------------------------------------------------------
def find_mkv_files(input_folder: Path) -> list[Path]:
    return sorted(input_folder.rglob("*.mkv"))


def probe_subtitle_tracks(mkv_path: Path) -> list[SubtitleTrack]:
    """Use ffprobe (inside Docker) to list PGS subtitle tracks."""
    mount_dir = mkv_path.parent.resolve()
    filename = mkv_path.name

    cmd = [
        "docker", "run", "--rm",
        "-v", f"{docker_path(mount_dir)}:/probe:ro",
        "--entrypoint", "ffprobe",
        DOCKER_IMAGE,
        "-v", "error",
        "-select_streams", "s",
        "-show_entries", "stream=index,codec_name,codec_type:stream_tags=language,title",
        "-of", "json",
        f"/probe/{filename}",
    ]

    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=120, env=_docker_env()
        )
        if result.returncode != 0:
            log.warning("ffprobe failed for %s: %s", mkv_path.name, result.stderr.strip())
            return []

        data = json.loads(result.stdout)
        tracks: list[SubtitleTrack] = []
        sub_idx = 0
        for stream in data.get("streams", []):
            codec = stream.get("codec_name", "")
            tags = stream.get("tags", {})
            if codec in ("hdmv_pgs_subtitle", "pgssub"):
                tracks.append(SubtitleTrack(
                    mkv_path=mkv_path,
                    track_index=stream["index"],
                    sub_stream_index=sub_idx,
                    language=tags.get("language", "und"),
                    codec_name=codec,
                    title=tags.get("title", ""),
                ))
            if stream.get("codec_type") == "subtitle":
                sub_idx += 1
        return tracks

    except (subprocess.TimeoutExpired, json.JSONDecodeError) as exc:
        log.warning("Error probing %s: %s", mkv_path.name, exc)
        return []


# ---------------------------------------------------------------------------
# Phase 2 – Extract + OCR
# ---------------------------------------------------------------------------
def build_output_names(track: SubtitleTrack, input_base: Path):
    """Return (relative_dir, stem) for the output files."""
    relative = track.mkv_path.parent.relative_to(input_base)
    stem = track.mkv_path.stem

    lang = safe_filename_part(track.language)
    parts = [stem, lang]
    if track.title:
        parts.append(safe_filename_part(track.title))
    parts.append(f"track{track.sub_stream_index}")
    name = ".".join(parts)
    return relative, name


def extract_all_tracks(
    mkv_path: Path,
    tracks: list[SubtitleTrack],
    input_base: Path,
    output_base: Path,
) -> list[tuple[SubtitleTrack, Path, Path]]:
    """Extract all PGS tracks from one MKV in a single ffmpeg call.

    Returns list of (track, sup_path, srt_path) for successfully extracted tracks.
    """
    if not tracks:
        return []

    mount_input = docker_path(mkv_path.parent.resolve())

    # Build output mapping and ffmpeg -map args for all tracks at once
    results: list[tuple[SubtitleTrack, Path, Path, str]] = []  # track, sup, srt, sup_name
    map_args: list[str] = []

    for track in tracks:
        relative_dir, base_name = build_output_names(track, input_base)
        output_dir = output_base / relative_dir
        sup_name = f"{base_name}.sup"
        srt_name = f"{base_name}.srt"
        srt_path = output_dir / srt_name
        sup_path = output_dir / sup_name

        if srt_path.exists():
            log.info("SKIP (exists): %s", srt_path.relative_to(output_base))
            continue

        output_dir.mkdir(parents=True, exist_ok=True)
        map_args.extend(["-map", f"0:{track.track_index}", "-c", "copy", f"/output/{sup_name}"])
        results.append((track, sup_path, srt_path, sup_name))

    if not results:
        return []

    # Single docker mount for output — all tracks from same MKV go to same relative dir
    # We need a common output mount; use the first track's output dir
    # Actually all tracks share the same MKV parent, so they share output dir
    first_output_dir = output_base / tracks[0].mkv_path.parent.relative_to(input_base)
    first_output_dir.mkdir(parents=True, exist_ok=True)
    mount_output = docker_path(first_output_dir.resolve())

    extract_cmd = [
        "docker", "run", "--rm",
        "-v", f"{mount_input}:/input:ro",
        "-v", f"{mount_output}:/output",
        "--entrypoint", "ffmpeg",
        DOCKER_IMAGE,
        "-y", "-v", "error",
        "-i", f"/input/{mkv_path.name}",
    ] + map_args

    track_count = len(results)
    mkv_size_mb = mkv_path.stat().st_size / (1024 * 1024)
    log.info("EXTRACT: %s  (%d tracks, %.0f MB)", mkv_path.name, track_count, mkv_size_mb)

    # Timeout scales with file count — large MKVs with many tracks need more time
    timeout = max(1200, 300 * track_count)
    t0 = time.monotonic()
    try:
        res = subprocess.run(extract_cmd, capture_output=True, text=True, timeout=timeout, env=_docker_env())
        elapsed = time.monotonic() - t0
        if res.returncode != 0:
            log.error("EXTRACT FAILED: %s after %s: %s",
                      mkv_path.name, fmt_duration(elapsed), res.stderr.strip())
            return []
    except subprocess.TimeoutExpired:
        log.error("EXTRACT TIMEOUT (%ds): %s", timeout, mkv_path.name)
        return []

    # Filter to only successfully extracted tracks
    extracted = []
    for track, sup_path, srt_path, sup_name in results:
        if sup_path.exists() and sup_path.stat().st_size > 0:
            extracted.append((track, sup_path, srt_path))
        else:
            log.warning("EXTRACT EMPTY: %s — skipping", sup_name)
            sup_path.unlink(missing_ok=True)

    log.info("EXTRACTED: %s  %d/%d tracks OK in %s",
             mkv_path.name, len(extracted), track_count, fmt_duration(elapsed))
    return extracted


def ocr_track(
    track: SubtitleTrack,
    sup_path: Path,
    srt_path: Path,
    output_base: Path,
    keep_sup: bool = False,
) -> bool:
    """OCR a single .sup file to .srt."""
    mount_output = docker_path(sup_path.parent.resolve())
    sup_name = sup_path.name
    srt_name = srt_path.name

    convert_cmd = [
        "docker", "run", "--rm",
        "-v", f"{mount_output}:/subtitles",
        "--entrypoint", "/secli/seconv",
        DOCKER_IMAGE,
        sup_name, "subrip",
        "/inputfolder:/subtitles",
        "/outputfolder:/subtitles",
        f"/ocrdb:{OCR_DB}",
        "/overwrite",
    ]

    t0 = time.monotonic()
    try:
        res = subprocess.run(convert_cmd, capture_output=True, text=True, timeout=900, env=_docker_env())
    except subprocess.TimeoutExpired:
        log.error("OCR TIMEOUT: %s", sup_name)
        return False

    elapsed = time.monotonic() - t0

    if res.returncode != 0:
        log.error("OCR FAILED: %s after %s: %s",
                  sup_name, fmt_duration(elapsed), (res.stderr or res.stdout).strip())
        return False

    if srt_path.exists() and srt_path.stat().st_size > 0:
        if not keep_sup:
            sup_path.unlink(missing_ok=True)
        log.info("DONE:    %s  (%s)", srt_path.relative_to(output_base), fmt_duration(elapsed))
        return True
    else:
        log.error("OCR produced no output for %s", sup_name)
        return False


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description="Batch MKV → SRT subtitle converter")
    parser.add_argument("--workers", type=int, default=DEFAULT_WORKERS,
                        help=f"Number of parallel workers (default {DEFAULT_WORKERS})")
    parser.add_argument("--dry-run", action="store_true",
                        help="Only probe and list tracks, don't extract or convert")
    parser.add_argument("--keep-sup", action="store_true",
                        help="Keep intermediate .sup files after conversion")
    parser.add_argument("--input", default=INPUT_FOLDER,
                        help=f"Input folder with MKV files (default: {INPUT_FOLDER})")
    parser.add_argument("--output", default=OUTPUT_FOLDER,
                        help=f"Output folder for SRT files (default: {OUTPUT_FOLDER})")
    args = parser.parse_args()

    script_dir = Path(__file__).parent.resolve()
    input_folder = script_dir / args.input
    output_folder = script_dir / args.output

    if not input_folder.exists():
        log.error("Input folder not found: %s", input_folder)
        sys.exit(1)

    output_folder.mkdir(parents=True, exist_ok=True)

    # ---- Find MKVs ----
    mkv_files = find_mkv_files(input_folder)
    log.info("Found %d MKV file(s) in %s", len(mkv_files), input_folder)
    if not mkv_files:
        return

    # ---- Phase 1: Probe for PGS subtitle tracks ----
    log.info("═══ Phase 1: Probing for PGS subtitle tracks (%d workers) ═══", args.workers)
    all_tracks: list[SubtitleTrack] = []

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(probe_subtitle_tracks, mkv): mkv for mkv in mkv_files}
        for future in as_completed(futures):
            mkv = futures[future]
            try:
                tracks = future.result()
                if tracks:
                    log.info("  %-50s  %d PGS track(s)", mkv.name, len(tracks))
                all_tracks.extend(tracks)
            except Exception as exc:
                log.error("  %s: probe error: %s", mkv.name, exc)

    log.info("Total PGS subtitle tracks: %d", len(all_tracks))
    if not all_tracks:
        log.info("Nothing to convert.")
        return

    # Group tracks by MKV file
    tracks_by_mkv: dict[Path, list[SubtitleTrack]] = defaultdict(list)
    for t in all_tracks:
        tracks_by_mkv[t.mkv_path].append(t)

    if args.dry_run:
        for t in all_tracks:
            _, base_name = build_output_names(t, input_folder)
            log.info("DRY-RUN: would process %s → %s.srt", t.mkv_path.name, base_name)
        log.info("═══ Complete: %d tracks across %d MKV files ═══",
                 len(all_tracks), len(tracks_by_mkv))
        return

    # ---- Phase 2: Extract (one ffmpeg call per MKV, sequential to avoid I/O thrashing) ----
    mkv_list = list(tracks_by_mkv.items())
    total_mkvs = len(mkv_list)
    total_extract_tracks = sum(len(t) for t in tracks_by_mkv.values())
    log.info("═══ Phase 2: Extract SUP tracks (%d MKV files, %d tracks total) ═══",
             total_mkvs, total_extract_tracks)
    ocr_queue: list[tuple[SubtitleTrack, Path, Path]] = []
    phase2_start = time.monotonic()

    for i, (mkv_path, tracks) in enumerate(mkv_list, 1):
        eta = fmt_eta(i - 1, total_mkvs, time.monotonic() - phase2_start) if i > 1 else "estimating..."
        log.info("── MKV %d/%d  (%s)", i, total_mkvs, eta)
        extracted = extract_all_tracks(mkv_path, tracks, input_folder, output_folder)
        ocr_queue.extend(extracted)

    phase2_elapsed = time.monotonic() - phase2_start
    log.info("Extracted %d SUP files in %s, starting OCR",
             len(ocr_queue), fmt_duration(phase2_elapsed))
    if not ocr_queue:
        log.info("Nothing to OCR.")
        return

    # ---- Phase 3: OCR (parallel – CPU-bound, no disk thrashing) ----
    total_ocr = len(ocr_queue)
    log.info("═══ Phase 3: OCR %d files (%d workers) ═══", total_ocr, args.workers)
    success = 0
    failed = 0
    phase3_start = time.monotonic()

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {
            pool.submit(ocr_track, track, sup_path, srt_path,
                        output_folder, args.keep_sup): track
            for track, sup_path, srt_path in ocr_queue
        }
        for future in as_completed(futures):
            track = futures[future]
            try:
                if future.result():
                    success += 1
                else:
                    failed += 1
            except Exception as exc:
                log.error("Error: %s track %d: %s",
                          track.mkv_path.name, track.track_index, exc)
                failed += 1

            done = success + failed
            elapsed = time.monotonic() - phase3_start
            eta = fmt_eta(done, total_ocr, elapsed)
            log.info("PROGRESS: %d/%d done (%d ok, %d fail) — %s",
                     done, total_ocr, success, failed, eta)

    total_elapsed = time.monotonic() - phase2_start
    log.info("═══ Complete: %d succeeded, %d failed — total time %s ═══",
             success, failed, fmt_duration(total_elapsed))


if __name__ == "__main__":
    main()

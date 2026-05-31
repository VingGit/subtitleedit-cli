# Subtitle Edit CLI – Batch MKV → SRT Converter

Extracts BluRay PGS (SUP) subtitle tracks from MKV files and converts them to SRT using OCR.

Built on [Subtitle Edit CLI](https://github.com/SubtitleEdit/subtitleedit-cli) (SE 3.6.9), with a Python batch pipeline for multithreaded processing.

## Prerequisites

- Docker Desktop
- Python 3.10+

## Quick Start

### 1. Build Docker Images

```bash
# Base image (seconv CLI)
docker build -t seconv:1.0 -f docker/Dockerfile .

# Batch image (seconv + ffmpeg)
docker build -t seconv-batch:1.0 -f docker/Dockerfile.batch .
```

### 2. Add MKV Files

Place your MKV files in the `movies/` folder. Subdirectory structure is preserved in the output.

```
movies/
  Ad Astra/
    Ad Astra_t00.mkv
  Dr. No (1962) [imdbid-tt0055928]/
    Dr. No (1962) [imdbid-tt0055928].mkv
```

### 3. Run Batch Conversion

```bash
# Dry-run – probe all MKVs and list PGS tracks without converting
python batch_convert.py --dry-run

# Convert with 4 parallel workers (default)
python batch_convert.py

# Convert with 8 parallel workers
python batch_convert.py --workers 8

# Keep intermediate .sup files after conversion
python batch_convert.py --keep-sup
```

Output is written to `generatedMovieSubtitles/`, mirroring the input folder structure:

```
generatedMovieSubtitles/
  Ad Astra/
    Ad Astra_t00.eng.track0.srt
    Ad Astra_t00.fra.track1.srt
  Dr. No (1962) [imdbid-tt0055928]/
    Dr. No (1962) [imdbid-tt0055928].eng.track0.srt
```

Re-running skips already converted files.

## How It Works

1. **Probe** – ffprobe (in Docker) scans each MKV for PGS subtitle streams
2. **Extract** – ffmpeg extracts each PGS track as a standalone `.sup` file
3. **OCR** – seconv reads the `.sup` bitmap images and converts to SRT text using the nOCR engine
4. **Cleanup** – intermediate `.sup` files are deleted (unless `--keep-sup`)

## CLI Options

| Option | Description |
|---|---|
| `--workers N` | Number of parallel Docker containers (default: 4) |
| `--dry-run` | Probe and list tracks only, no extraction or conversion |
| `--keep-sup` | Keep intermediate `.sup` files after OCR |
| `--input DIR` | Input folder (default: `movies`) |
| `--output DIR` | Output folder (default: `generatedMovieSubtitles`) |

## Limitations

- **OCR engine**: nOCR with the Latin character database. Works well for Latin-script languages (English, French, Spanish, Dutch, Italian, German, etc.)
- **Non-Latin scripts** (CJK, Thai, Arabic, Cyrillic): nOCR will produce `*` for unrecognized characters. The Latin.nocr database only covers Latin alphabet characters.
- **No interactive correction**: Unlike the Subtitle Edit GUI, the CLI cannot prompt for uncertain characters.

## Using seconv Directly

```bash
# Show help
docker run --rm seconv:1.0 /help

# List supported formats
docker run --rm seconv:1.0 /formats

# Convert a single file
docker run --rm -v "$(pwd)/subtitles:/subtitles" seconv:1.0 \
  sample.sup subrip /inputfolder:/subtitles /outputfolder:/subtitles /ocrdb:Latin
```

## Project Structure

```
batch_convert.py              # Python batch orchestrator
docker/
  Dockerfile                  # Base seconv image
  Dockerfile.batch            # Extended image with ffmpeg
movies/                       # Input MKV files (git-ignored, structure kept via .gitkeep)
generatedMovieSubtitles/      # Output SRT files (git-ignored)
src/se-cli/                   # Subtitle Edit CLI source (C# / .NET 8)
```

Parameters:
- -v: Mount local subtitles directory.
- sample.srt: input file or pattern. E.g. *.srt.
- pac: output-format. E.g. pac, stl, srt, ass.


### License
`subtitleedit-cli` is licensed under the GNU LESSER GENERAL PUBLIC LICENSE Version 3, 
so it free to use for commercial software, as long as you don't modify the library itself. 
LGPL 3.0 allows linking to the library in a way that doesn't require you to open source your own code. 
This means that if you use libse in your project, you can keep your own code private, 
as long as you don't modify libse itself.Il
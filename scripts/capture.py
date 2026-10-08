"""Single-link capture using yt-dlp and the installed local Chinese ASR helper."""
import argparse
import datetime
import json
from pathlib import Path
import re
import shutil
import subprocess
import uuid

CACHE = Path.home() / '.cache/watch-tools/bin'
ASR_PYTHON = Path.home() / '.cache/knowledge-video-editing/whisper-venv/bin/python'
ASR_SCRIPT = Path.home() / '.codex/skills/douyin-feishu-collector/scripts/transcribe.py'


def extract_url(text):
    urls = list(dict.fromkeys(u.rstrip('.,;:!?，。；：！？、）)]}》>')
                             for u in re.findall(r'https?://[^\s<>"\u201c\u201d]+', text)))
    if len(urls) != 1:
        raise ValueError(f'Expected exactly one URL; found {len(urls)}')
    return urls[0]


def save(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')


def run(command, log, timeout):
    with log.open('w', encoding='utf-8') as handle:
        subprocess.run([str(x) for x in command], stdout=handle,
                       stderr=subprocess.STDOUT, check=True, timeout=timeout)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument('--source-file', type=Path, help='UTF-8 share text with one URL')
    source.add_argument('--media', type=Path, help='Existing local video; read only')
    parser.add_argument('--workspace', type=Path, required=True)
    parser.add_argument('--mode', choices=['text', 'video', 'both'], default='text')
    parser.add_argument('--review', choices=['evidence', 'none'], default='evidence',
                        help='Default: prepare evidence and await agent correction; none: raw ASR only')
    parser.add_argument('--cookies-file', type=Path, help='Requires explicit account access authorization')
    args = parser.parse_args()
    run_id = datetime.datetime.now().strftime('%Y%m%d-%H%M%S-') + uuid.uuid4().hex[:8]
    root = args.workspace.resolve()
    work = root / 'work/单条链接提取' / run_id
    out = root / 'outputs/单条链接提取' / run_id
    work.mkdir(parents=True, exist_ok=False)
    state = {'status': 'running', 'stage': 'preflight', 'mode': args.mode,
             'review': args.review, 'output': str(out)}
    result = work / 'result.json'
    save(result, state)
    try:
        needed = [CACHE/'ffmpeg', CACHE/'ffprobe']
        if not args.media:
            needed.append(CACHE/'yt-dlp')
        if args.mode != 'video':
            needed += [ASR_PYTHON, ASR_SCRIPT]
        for path in needed:
            if not path.is_file():
                raise FileNotFoundError(str(path))
        if args.media:
            media = args.media.resolve(strict=True)
            metadata = {'source_type': 'local', 'original_filename': media.name}
        else:
            share = args.source_file.read_text(encoding='utf-8')
            url = extract_url(share)
            (work/'share.txt').write_text(share, encoding='utf-8')
            state.update(stage='download', url=url)
            save(result, state)
            command = [CACHE/'yt-dlp', '--ignore-config', '--no-playlist', '--playlist-items', '1',
                       '--no-overwrites', '--socket-timeout', '20', '--retries', '1',
                       '--extractor-retries', '1', '--fragment-retries', '1', '--abort-on-unavailable-fragments',
                       '--ffmpeg-location', CACHE, '--write-info-json', '--no-progress',
                       '--print-to-file', 'after_move:filepath', work/'downloaded-path.txt',
                       '-f', 'bestaudio/best' if args.mode == 'text' and args.review == 'none'
                       else 'bestvideo*+bestaudio/best',
                       '-o', str(work/'media.%(ext)s')]
            if args.cookies_file:
                # yt-dlp may update a cookie jar; preserve the user's source file.
                local_cookie = work/'cookies.txt'
                shutil.copyfile(args.cookies_file.resolve(strict=True), local_cookie)
                local_cookie.chmod(0o600)
                command += ['--cookies', local_cookie]
            try:
                run(command + ['--', url], work/'download.log', 1800)
            finally:
                if args.cookies_file:
                    local_cookie.unlink(missing_ok=True)
            paths = (work/'downloaded-path.txt').read_text().splitlines()
            if len(paths) != 1:
                raise RuntimeError('Expected one downloaded media file')
            media = Path(paths[0]).resolve(strict=True)
            info = json.loads((work/'media.info.json').read_text())
            metadata = {key: info.get(key) for key in
                        ['id', 'title', 'uploader', 'webpage_url', 'upload_date', 'duration', 'tags', 'extractor']}
            metadata['input_url'] = url
        state['stage'] = 'media_validation'
        save(result, state)
        probe = subprocess.run([str(CACHE/'ffprobe'), '-v', 'error', '-show_streams',
                                '-show_format', '-of', 'json', str(media)],
                               capture_output=True, text=True, check=True, timeout=60)
        data = json.loads(probe.stdout)
        types = {s.get('codec_type') for s in data.get('streams', [])}
        if args.mode != 'video' and 'audio' not in types:
            raise RuntimeError('NO_AUDIO: cannot extract spoken text')
        if args.mode != 'text' and 'video' not in types:
            raise RuntimeError('NO_VIDEO: source contains no video track')
        run([CACHE/'ffmpeg', '-v', 'error', '-xerror', '-i', media, '-f', 'null', '-'],
            work/'decode.log', 1800)
        state.update(media=str(media), media_validated=True, has_video='video' in types)
        out.mkdir(parents=True, exist_ok=False)
        save(out/'来源.json', metadata)
        if args.mode != 'video':
            state['stage'] = 'transcription'
            save(result, state)
            save(work/'asr-config.json', {'ffmpeg': str(CACHE/'ffmpeg')})
            run([ASR_PYTHON, ASR_SCRIPT, '--config', work/'asr-config.json',
                 '--media', media, '--out', work/'transcript'], work/'transcription.log', 7200)
            transcript = json.loads((work/'transcript/transcription.json').read_text())
            if transcript.get('status') == 'no_speech':
                shutil.copy2(work/'transcript/transcription.json', out/'transcription.json')
                if args.mode == 'both':
                    shutil.copy2(media, out/('视频' + media.suffix))
                state.update(status='needs_review', stage='no_speech', proofread=False,
                             review_required=True, files=[p.name for p in out.iterdir()])
                save(result, state)
                print(json.dumps(state, ensure_ascii=False))
                return 0
            for name in ['口播转写_机器稿.txt', '口播转写_机器稿.srt', 'transcription.json']:
                shutil.copy2(work/'transcript'/name, out/name)
        if args.mode != 'text':
            shutil.copy2(media, out/('视频' + media.suffix))
        state.update(status='complete', stage='complete', proofread=False,
                     files=[p.name for p in out.iterdir()], media_validated=True)
        if args.mode != 'video' and args.review == 'evidence':
            state.update(status='awaiting_review', stage='evidence_preparation')
            save(result, state)
            from review import prepare
            state = prepare(result)
    except Exception as error:
        state.update(status='failed', error=str(error), logs=str(work))
        save(result, state)
        print(json.dumps(state, ensure_ascii=False))
        return 1
    save(result, state)
    print(json.dumps(state, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

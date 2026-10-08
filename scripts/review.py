"""Prepare local evidence and apply explicit, source-bound transcript corrections.

This helper does not decide wording or claim human proofreading. The invoking
agent reads the full transcript, inspects evidence, and supplies a review plan.
"""
import argparse
import copy
import datetime
import hashlib
import json
import math
from pathlib import Path
import shutil
import subprocess
import uuid
import warnings

CACHE = Path.home() / '.cache/watch-tools/bin'


def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def save(path, value):
    path = Path(path)
    temporary = path.with_name(path.name + '.tmp-' + uuid.uuid4().hex)
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    temporary.replace(path)


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def stamp(t):
    n = round(t * 1000)
    return f'{n//3600000:02}:{n//60000%60:02}:{n//1000%60:02},{n%1000:03}'


def load_run(result):
    result = Path(result).resolve(strict=True)
    state = read(result)
    source = Path(state['output']) / 'transcription.json'
    data = read(source)
    if not data.get('segments'):
        raise ValueError('NO_SPEECH: retain media; cannot create a corrected transcript')
    return result, state, source, data


def frames(result, times):
    result, state, source, data = load_run(result)
    if not state.get('has_video'):
        raise ValueError('NO_VIDEO: use audio evidence and mark unresolved terms')
    directory = result.parent / 'review/frames'
    directory.mkdir(parents=True, exist_ok=True)
    manifest = directory.parent / 'frames.json'
    records = read(manifest) if manifest.exists() else []
    for t in sorted(set(times)):
        if not math.isfinite(t) or not 0 <= t < data['duration']:
            raise ValueError(f'Frame timestamp out of range: {t}')
        path = directory / f'{round(t*1000):09d}ms.jpg'
        if not path.exists():
            subprocess.run([str(CACHE/'ffmpeg'), '-v', 'error', '-nostdin', '-n',
                            '-ss', str(t), '-i', state['media'], '-frames:v', '1',
                            '-vf', 'scale=960:-2', str(path)], check=True, timeout=120)
        if not path.is_file() or path.stat().st_size == 0:
            raise ValueError(f'No frame decoded at {t}s')
        if not any(x['path'] == str(path) for x in records):
            records.append({'timestamp': t, 'path': str(path)})
    save(manifest, sorted(records, key=lambda x: x['timestamp']))
    return records


def prepare(result):
    result, state, source, data = load_run(result)
    if state.get('review_output'):
        raise ValueError('Review already completed; use its existing result')
    directory = result.parent / 'review'
    directory.mkdir(exist_ok=True)
    source_hash = digest(source)
    existing = directory / 'input.json'
    if existing.exists() and read(existing)['source_sha256'] != source_hash:
        raise ValueError('Source changed since preparation; stop and inspect')
    save(existing, {'source': str(source), 'source_sha256': source_hash,
                    'media': state['media'], 'has_video': state.get('has_video', False),
                    'duration': data['duration'],
                    'segments': [dict(s, segment=i) for i, s in enumerate(data['segments'], 1)]})
    template = directory / 'plan.template.json'
    if not template.exists():
        save(template, {'source_sha256': source_hash, 'full_text_read': False,
                        'reviewed_segments': [], 'changes': [], 'uncertainties': [],
                        'timing_changes': []})
    # A few orientation frames; the agent extracts exact frames for suspected words.
    if state.get('has_video'):
        frames(result, [round(data['duration'] * fraction, 3) for fraction in (0, .25, .5, .75)])
    state.update(status='awaiting_review', stage='review', proofread=False,
                 review_input=str(existing), review_plan_template=str(template),
                 review_required=True)
    save(result, state)
    return state


def evidence(items, duration):
    if not isinstance(items, list) or not items:
        raise ValueError('Each correction requires evidence')
    for item in items:
        if item.get('kind') not in ('frame', 'audio_review', 'asr_rerun', 'source_metadata', 'context'):
            raise ValueError('Unsupported evidence kind')
        if not isinstance(item.get('note'), str) or not item['note'].strip():
            raise ValueError('Evidence must explain why the correction is supported')
        if item['kind'] != 'context':
            if not Path(item.get('path', '')).is_file():
                raise ValueError('Evidence file is missing')
        if item['kind'] in ('frame', 'audio_review', 'asr_rerun'):
            t = item.get('timestamp')
            if not isinstance(t, (int, float)) or not math.isfinite(t) or not 0 <= t <= duration:
                raise ValueError('Evidence timestamp is missing or out of range')


def annotate_uncertainty(text, issue):
    spans = issue.get('spans') or [{'start': 0, 'end': len(text)}]
    previous = 0
    output = ''
    for span in spans:
        a, b = span['start'], span['end']
        if type(a) is not int or type(b) is not int or not previous <= a < b <= len(text):
            raise ValueError('Invalid uncertainty span')
        output += text[previous:a] + '（待确认：' + text[a:b] + '）'
        previous = b
    return output + text[previous:]


def apply(result, plan_path):
    result, state, source, original = load_run(result)
    if state.get('status') != 'awaiting_review':
        raise ValueError('Run must be awaiting_review; no duplicate application')
    plan = read(plan_path)
    source_hash = digest(source)
    if plan.get('source_sha256') != source_hash:
        raise ValueError('Source hash mismatch; do not apply a stale correction plan')
    segments = copy.deepcopy(original['segments'])
    count = len(segments)
    if (plan.get('full_text_read') is not True or
            sorted(plan.get('reviewed_segments', [])) != list(range(1, count + 1))):
        raise ValueError('Read and review every segment before completing the plan')
    changes = []
    seen = set()
    for change in plan.get('changes', []):
        index = change['segment']
        if type(index) is not int or not 1 <= index <= count or index in seen:
            raise ValueError('Each segment may have only one final text correction')
        seen.add(index)
        segment = segments[index-1]
        if change.get('before') != segment['text']:
            raise ValueError(f'Exact source text mismatch at segment {index}')
        after = change.get('after')
        if (not isinstance(after, str) or not after.strip() or '\n' in after or
                after == segment['text'] or '【待听清】' in after):
            raise ValueError('Correction must be nonempty single-line text; use uncertainties for markers')
        evidence(change.get('evidence'), original['duration'])
        segment['text'] = after
        changes.append(dict(change, start=segment['start'], end=segment['end']))
    timing_seen = set()
    for patch in plan.get('timing_changes', []):
        index = patch['segment']
        if type(index) is not int or not 1 <= index <= count or index in timing_seen:
            raise ValueError('Invalid or duplicate timing patch')
        timing_seen.add(index)
        s = segments[index-1]
        if patch['before'] != {'start': s['start'], 'end': s['end']}:
            raise ValueError('Timing source mismatch')
        evidence(patch.get('evidence'), original['duration'])
        s['start'], s['end'] = patch['after']['start'], patch['after']['end']
    uncertain_seen = set()
    for issue in plan.get('uncertainties', []):
        index = issue['segment']
        if type(index) is not int or not 1 <= index <= count or index in uncertain_seen or not issue.get('reason'):
            raise ValueError('Invalid or duplicate uncertainty')
        uncertain_seen.add(index)
        if plan.get('uncertainty_format') == 'inline_parentheses':
            segments[index-1]['text'] = annotate_uncertainty(segments[index-1]['text'], issue)
        else:
            segments[index-1]['text'] += '【待听清】'
    for i, s in enumerate(segments):
        if (not all(isinstance(s[k], (int, float)) and math.isfinite(s[k]) for k in ('start', 'end')) or
                not 0 <= s['start'] < s['end'] <= original['duration'] + .001):
            raise ValueError(f'Invalid timestamp at segment {i+1}; supply an evidenced timing patch')
        if i and s['start'] < segments[i-1]['end'] - .001:
            raise ValueError(f'Overlapping subtitles at segment {i+1}; inspect original timing')
    txt = '\n'.join(s['text'] for s in segments) + '\n'
    srt = '\n\n'.join(f'{i}\n{stamp(s["start"])} --> {stamp(s["end"])}\n{s["text"]}'
                       for i, s in enumerate(segments, 1)) + '\n'
    if '\n'.join(b.split('\n', 2)[2] for b in srt.strip().split('\n\n')) + '\n' != txt:
        raise ValueError('TXT/SRT consistency failure')
    output = Path(state['output']) / ('校对-' + datetime.datetime.now().strftime('%Y%m%d-%H%M%S-') + uuid.uuid4().hex[:8])
    output.mkdir(exist_ok=False)
    reviewed = dict(original, segments=segments, source_sha256=source_hash, proofread=False,
                    status='证据辅助纠错，待人工听校', changes=changes,
                    uncertainties=plan.get('uncertainties', []), timing_changes=plan.get('timing_changes', []))
    (output/'口播_校正版.txt').write_text(txt, encoding='utf-8')
    (output/'字幕_校正版.srt').write_text(srt, encoding='utf-8')
    save(output/'校正版.json', reviewed)
    save(output/'纠错记录.json', {'source_sha256': source_hash, 'changes': changes,
                               'uncertainties': reviewed['uncertainties'], 'timing_changes': reviewed['timing_changes']})
    save(output/'验证结果.json', {'source_unchanged': digest(source) == source_hash,
                                 'txt_srt_consistent': True, 'segments': count,
                                 'corrections': len(changes), 'uncertainties': len(uncertain_seen),
                                 'proofread': False})
    if digest(source) != source_hash or read(result) != state:
        raise ValueError('Concurrent source/run change; review output retained but run not advanced')
    state.update(status='complete', stage='complete', review_output=str(output), proofread=False,
                 review_required=bool(uncertain_seen), review_status=reviewed['status'],
                 corrected_files=[str(p) for p in sorted(output.iterdir())],
                 correction_count=len(changes), uncertainty_count=len(uncertain_seen))
    save(result, state)
    return state


def pad_recheck_audio(audio, sampling_rate=16000):
    """Pad the waveform, not the Mel array, so padding represents real silence."""
    import numpy as np
    if (audio.ndim != 1 or not audio.size or not np.isfinite(audio).all() or
            float(np.max(np.abs(audio))) <= 1e-6):
        raise ValueError('INVALID_RECHECK_AUDIO: empty, nonfinite, or silent clip')
    window = 30 * sampling_rate
    length = ((audio.size + window - 1) // window) * window
    return np.pad(audio, (0, length - audio.size))


def recheck_issues(segments, duration):
    """Reject suspect output rather than clipping hallucinated timestamps."""
    issues = []
    if not segments or not any(s['text'].strip() for s in segments):
        issues.append('NO_RECHECK_SPEECH')
    previous_end = 0.0
    for i, s in enumerate(segments, 1):
        a, b = s['start'], s['end']
        if not (math.isfinite(a) and math.isfinite(b) and
                0 <= a < b <= duration + .001 and a >= previous_end - .001):
            issues.append(f'INVALID_RECHECK_TIMING: segment {i}, {a}..{b}, clip {duration}')
        previous_end = b
        probability = s.get('no_speech_prob', 0)
        if not math.isfinite(probability) or probability > .6:
            issues.append(f'UNCERTAIN_RECHECK_SPEECH: segment {i}, probability {probability}')
    return issues


def recheck(result, start, end, model, prompt_file=None):
    result, state, source, data = load_run(result)
    if not 0 <= start < end <= data['duration'] or end-start > 60:
        raise ValueError('Choose a source clip of at most 60 seconds')
    # Load only existing local models; never downloads weights or invokes a paid API.
    from faster_whisper import WhisperModel
    from faster_whisper.audio import decode_audio
    asr = WhisperModel(model, device='cpu', compute_type='int8', cpu_threads=6, local_files_only=True)
    out = result.parent / 'review' / ('recheck-' + uuid.uuid4().hex[:8])
    out.mkdir(parents=True)
    wav = out/'clip.wav'
    subprocess.run([str(CACHE/'ffmpeg'), '-v', 'error', '-nostdin', '-n', '-ss', str(start),
                    '-i', state['media'], '-t', str(end-start), '-vn', '-ac', '1', '-ar', '16000', str(wav)],
                   check=True, timeout=180)
    prompt = Path(prompt_file).read_text(encoding='utf-8') if prompt_file else None
    audio = decode_audio(str(wav), sampling_rate=16000)
    duration = len(audio) / 16000
    # Short large-v3 clips can hallucinate when the inference window is padded
    # with zero Mel values. Waveform silence produces the correct log-Mel floor.
    # VAD must stay off here: it would remove the deliberate silence again.
    padded = pad_recheck_audio(audio)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter('always', RuntimeWarning)
        segments, info = asr.transcribe(padded, language='zh', beam_size=5,
                                       vad_filter=False, initial_prompt=prompt)
        raw = [{'start': s.start, 'end': s.end, 'text': s.text.strip(),
                'no_speech_prob': s.no_speech_prob, 'avg_logprob': s.avg_logprob}
               for s in segments]
    issues = recheck_issues(raw, min(duration, end-start))
    if abs(duration - (end-start)) > .02:
        issues.append('RECHECK_CLIP_DURATION_MISMATCH')
    record = {'model': model, 'source_sha256': digest(source), 'start': start, 'end': end,
              'prompt': prompt, 'proofread': False, 'status': 'rejected' if issues else 'candidate',
              'audio_sha256': digest(wav), 'clip_duration': duration,
              'inference_duration': len(padded)/16000, 'vad_filter': False,
              'padding': 'waveform_silence_to_30_second_multiple',
              'warnings': list(dict.fromkeys(str(w.message) for w in caught)),
              'issues': issues, 'raw_segments': raw,
              'segments': [] if issues else [
                  {'start': round(s['start']+start, 3), 'end': round(s['end']+start, 3),
                   'text': s['text']} for s in raw]}
    save(out/'transcription.json', record)
    if issues:
        raise RuntimeError('RECHECK_REJECTED: ' + '; '.join(issues) +
                           '; evidence retained at ' + str(out/'transcription.json'))
    return {'status': 'candidate', 'recheck': str(out/'transcription.json'), 'audio': str(wav)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    for name in ('prepare', 'frames', 'apply', 'recheck'):
        p = sub.add_parser(name)
        p.add_argument('--result', type=Path, required=True)
        if name == 'frames':
            p.add_argument('--times', type=float, nargs='+', required=True)
        if name == 'apply':
            p.add_argument('--plan', type=Path, required=True)
        if name == 'recheck':
            p.add_argument('--start', type=float, required=True)
            p.add_argument('--end', type=float, required=True)
            p.add_argument('--model', choices=['small', 'large-v3'], default='large-v3')
            p.add_argument('--prompt-file', type=Path)
    a = parser.parse_args()
    if a.command == 'prepare': value = prepare(a.result)
    elif a.command == 'frames': value = frames(a.result, a.times)
    elif a.command == 'apply': value = apply(a.result, a.plan)
    else: value = recheck(a.result, a.start, a.end, a.model, a.prompt_file)
    print(json.dumps(value, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()

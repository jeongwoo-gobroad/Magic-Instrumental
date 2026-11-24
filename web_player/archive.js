/* SPDX-License-Identifier: GPL-3.0-only */
'use strict';
const MAGIC_LIMIT = 1024 * 1024 * 1024;
const AUDIO_LIMIT = 512 * 1024 * 1024;
const AUDIO_VARIANT_SPECS = {
  no_vocals: ['without', 'vocals'], vocals: ['stem', 'vocals'],
  no_guitar: ['without', 'guitar'], guitar: ['stem', 'guitar'],
  no_drums: ['without', 'drums'], drums: ['stem', 'drums'],
  no_bass: ['without', 'bass'], bass: ['stem', 'bass'],
  no_piano: ['without', 'piano'], piano: ['stem', 'piano'],
  no_keys: ['without', 'keys'], keys: ['stem', 'keys'],
  no_synth: ['without', 'synth'], synth: ['stem', 'synth'], original: ['original', null]
};
const decoder = new TextDecoder('utf-8', {fatal: true});
function requireMagic(test, message) { if (!test) throw new Error(message); }
const crcTable = Uint32Array.from({length: 256}, (_, n) => {
  for (let k = 0; k < 8; k++) n = n & 1 ? 0xedb88320 ^ (n >>> 1) : n >>> 1;
  return n >>> 0;
});
function validateMp3(data, audio) {
  const bytes = new DataView(data), error = 'MP3 반주 정보가 손상되었거나 지원하지 않는 형식입니다. 데스크톱에서 다시 저장해 주세요.';
  const rates = [0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320, 0];
  let at = 0, count = 0, declared = 0;
  while (at < bytes.byteLength) {
    requireMagic(at + 4 <= bytes.byteLength, error);
    const header = bytes.getUint32(at), bitrate = rates[(header >>> 12) & 15];
    requireMagic((header >>> 21) === 0x7ff && ((header >>> 19) & 3) === 3 && ((header >>> 17) & 3) === 1 && ((header >>> 10) & 3) === 0 && ((header >>> 6) & 3) !== 3 && bitrate === audio.bitrate_kbps, error);
    const size = Math.floor(144000 * bitrate / 44100) + ((header >>> 9) & 1);
    requireMagic(at + size <= bytes.byteLength, error);
    if (!count) {
      // The first frame carries Info/Xing; only subsequent frames contain audio.
      const tag = 4 + ((header & 0x10000) ? 0 : 2) + 32;
      requireMagic(tag + 16 <= size && [0x496e666f, 0x58696e67].includes(bytes.getUint32(tag)), error);
      const flags = bytes.getUint32(tag + 4);
      requireMagic((flags & 3) === 3 && (flags & ~15) === 0, error);
      declared = bytes.getUint32(tag + 8);
      requireMagic(bytes.getUint32(tag + 12) === bytes.byteLength, error);
      const lame = tag + 16 + ((flags & 4) ? 100 : 0) + ((flags & 8) ? 4 : 0);
      requireMagic(lame + 36 <= size && [0x4c414d45, 0x4c617663, 0x4c617666].includes(bytes.getUint32(lame)), error);
      const padding = bytes.getUint8(lame + 21) * 65536 + bytes.getUint16(lame + 22);
      const samples = declared * 1152 - (padding >>> 12) - (padding & 4095);
      requireMagic(declared > 0 && samples === audio.frames && samples * 4 <= AUDIO_LIMIT, error);
    }
    at += size; count++;
    requireMagic(count <= declared + 1 && (count - 1) * 1152 <= AUDIO_LIMIT / 4 + 8190, error);
  }
  requireMagic(count === declared + 1, error);
}
function validateWav(data, audio) {
  const bytes = new DataView(data), error = 'WAV 음원 길이·형식이 올바르지 않습니다. 컴퓨터에서 다시 저장해 주세요.';
  requireMagic(bytes.byteLength >= 44 && bytes.getUint32(0) === 0x52494646 && bytes.getUint32(8) === 0x57415645 && bytes.getUint32(4, true) === bytes.byteLength - 8, error);
  let at = 12, frames = null, format = false, chunks = 0;
  while (at < bytes.byteLength) {
    requireMagic(at + 8 <= bytes.byteLength && ++chunks <= 1024, error);
    const type = bytes.getUint32(at), size = bytes.getUint32(at + 4, true), from = at + 8;
    requireMagic(from + size <= bytes.byteLength, error);
    if (type === 0x666d7420) {
      requireMagic(!format && size >= 16 && bytes.getUint16(from, true) === 1 && bytes.getUint16(from + 2, true) === 2 && bytes.getUint32(from + 4, true) === 44100 && bytes.getUint32(from + 8, true) === 176400 && bytes.getUint16(from + 12, true) === 4 && bytes.getUint16(from + 14, true) === 16, error);
      format = true;
    } else if (type === 0x64617461) {
      requireMagic(frames === null && size > 0 && size % 4 === 0, error); frames = size / 4;
    }
    at = from + size + (size % 2);
  }
  requireMagic(at === bytes.byteLength && format && frames === audio.frames && frames * 4 <= AUDIO_LIMIT, error);
}
function sameAudio(a, b) {
  return a && b && Object.keys(a).length === Object.keys(b).length && Object.keys(a).every(key => a[key] === b[key]);
}
function validateVariantAudio(audio, identifier) {
  requireMagic(audio && ['wav', 'mp3'].includes(audio.codec) && audio.path === `audio/variants/${identifier}.${audio.codec}`, '재생 음원 경로·형식이 올바르지 않습니다.');
  requireMagic(audio.channels === 2 && audio.sample_rate === 44100 && Number.isInteger(audio.pitch_semitones) && Math.abs(audio.pitch_semitones) <= 12 && Number.isSafeInteger(audio.frames) && audio.frames > 0 && audio.frames * 4 <= AUDIO_LIMIT && audio.duration === audio.frames / 44100, '재생 음원의 길이·키·채널 정보가 올바르지 않습니다.');
  requireMagic(typeof audio.source_sha256 === 'string' && /^[a-f0-9]{64}$/.test(audio.source_sha256), '원곡 식별 정보가 올바르지 않습니다.');
  const fields = ['path', 'codec', 'frames', 'sample_rate', 'channels', 'duration', 'pitch_semitones', 'source_sha256'];
  if (audio.codec === 'mp3') {
    requireMagic([128, 192, 256, 320].includes(audio.bitrate_kbps), 'MP3 음질 정보가 올바르지 않습니다.'); fields.push('bitrate_kbps');
  }
  requireMagic(Object.keys(audio).length === fields.length && fields.every(key => Object.hasOwn(audio, key)), '재생 음원 정보가 올바르지 않습니다.');
}
class MagicArchive {
  constructor(file) { this.file = file; this.entries = new Map(); }
  async open() {
    const file = this.file;
    requireMagic(file.size >= 22 && file.size <= MAGIC_LIMIT, '연습 파일은 최대 1 GB까지 열 수 있습니다.');
    const tail = new DataView(await file.slice(Math.max(0, file.size - 65557)).arrayBuffer());
    let end = -1;
    for (let i = tail.byteLength - 22; i >= 0; i--) {
      if (tail.getUint32(i, true) === 0x06054b50 && i + 22 + tail.getUint16(i + 20, true) === tail.byteLength) { end = i; break; }
    }
    requireMagic(end >= 0, '올바른 .magic 파일이 아닙니다. 데스크톱에서 다시 저장해 주세요.');
    const count = tail.getUint16(end + 10, true), size = tail.getUint32(end + 12, true), start = tail.getUint32(end + 16, true);
    const endOffset = file.size - tail.byteLength + end;
    requireMagic(!tail.getUint16(end + 4, true) && !tail.getUint16(end + 6, true) && tail.getUint16(end + 8, true) === count && count >= 5 && count <= 1100 && size <= 300000 && start + size === endOffset, '지원하지 않거나 손상된 연습 파일입니다.');
    const central = new DataView(await file.slice(start, start + size).arrayBuffer());
    let cursor = 0; const spans = [];
    for (let i = 0; i < count; i++) {
      requireMagic(cursor + 46 <= size && central.getUint32(cursor, true) === 0x02014b50, '파일 목록이 손상되었습니다.');
      const flags = central.getUint16(cursor + 8, true), method = central.getUint16(cursor + 10, true);
      const crc = central.getUint32(cursor + 16, true), packed = central.getUint32(cursor + 20, true), bytes = central.getUint32(cursor + 24, true);
      const names = central.getUint16(cursor + 28, true), extra = central.getUint16(cursor + 30, true), comment = central.getUint16(cursor + 32, true), offset = central.getUint32(cursor + 42, true);
      requireMagic((flags & ~0x800) === 0 && method === 0 && packed === bytes && bytes <= AUDIO_LIMIT && offset + 30 < start && cursor + 46 + names + extra + comment <= size && central.getUint16(cursor + 34, true) === 0, '압축·암호화·분할 파일은 지원하지 않습니다. .magic 저장으로 다시 만들어 주세요.');
      const name = decoder.decode(new Uint8Array(central.buffer, cursor + 46, names));
      requireMagic(/^(manifest\.json|timeline\.json|audio\/instrumental\.(wav|mp3)|audio\/variants\/(no_vocals|vocals|no_guitar|guitar|no_drums|drums|no_bass|bass|no_piano|piano|no_keys|keys|no_synth|synth|original)\.(wav|mp3)|score\/source\.(pdf|gp3|gp4|gp5)|pages\/\d{4}\.png|parts\/\d{2}\/(timeline\.json|pages\/\d{4}\.png))$/.test(name) && !this.entries.has(name), '중복되거나 허용되지 않은 파일 이름입니다.');
      const local = new DataView(await file.slice(offset, offset + 30).arrayBuffer());
      requireMagic(local.getUint32(0, true) === 0x04034b50 && local.getUint16(6, true) === flags && local.getUint16(8, true) === method && local.getUint32(14, true) === crc && local.getUint32(18, true) === bytes && local.getUint32(22, true) === bytes && local.getUint16(26, true) === names, '파일 정보가 일치하지 않습니다.');
      const from = offset + 30 + names + local.getUint16(28, true), to = from + bytes;
      requireMagic(to <= start && decoder.decode(await file.slice(offset + 30, offset + 30 + names).arrayBuffer()) === name, '파일 위치가 올바르지 않습니다.');
      spans.push([offset, to]); this.entries.set(name, {from, to, bytes, crc});
      cursor += 46 + names + extra + comment;
    }
    spans.sort((a, b) => a[0] - b[0]);
    requireMagic(cursor === size && spans.every((s, i) => !i || s[0] >= spans[i - 1][1]), '파일 영역이 겹치거나 손상되었습니다.');
    const manifest = await this.json('manifest.json', 1000000);
    requireMagic(manifest.format === 'Magic-Instrumental' && [1, 2, 3, 4].includes(manifest.version), '이 .magic 버전은 지원하지 않습니다. 재생기를 업데이트해 주세요.');
    requireMagic(typeof manifest.title === 'string' && manifest.title.length <= 200 && Array.isArray(manifest.pages) && manifest.pages.length >= 1 && manifest.pages.length <= 200, '곡·악보 정보가 올바르지 않습니다.');
    const audio = manifest.audio, score = manifest.score;
    requireMagic(audio && Number.isFinite(audio.duration) && audio.duration > 0 && audio.duration <= 3600 && audio.channels === 2 && audio.sample_rate === 44100 && Number.isInteger(audio.pitch_semitones) && Math.abs(audio.pitch_semitones) <= 12 && manifest.timeline === 'timeline.json', '반주 정보가 올바르지 않습니다.');
    if (manifest.version === 4) {
      requireMagic(Number.isFinite(manifest.audio_gain) && manifest.audio_gain > 0 && manifest.audio_gain <= 1, '음원 공통 음량 정보가 올바르지 않습니다.');
      requireMagic(Array.isArray(manifest.audio_variants) && manifest.audio_variants.length >= 1 && manifest.audio_variants.length <= Object.keys(AUDIO_VARIANT_SPECS).length && typeof manifest.default_audio_variant === 'string', '저장된 재생 음원 목록이 올바르지 않습니다.');
      const identifiers = new Set(), shared = ['frames', 'sample_rate', 'channels', 'duration', 'pitch_semitones', 'source_sha256'];
      for (const variant of manifest.audio_variants) {
        const spec = variant && Object.hasOwn(AUDIO_VARIANT_SPECS, variant.id) ? AUDIO_VARIANT_SPECS[variant.id] : null;
        requireMagic(spec && typeof variant.id === 'string' && !identifiers.has(variant.id) && typeof variant.label === 'string' && variant.label.trim().length > 0 && [...variant.label].length <= 100 && variant.mode === spec[0] && Object.hasOwn(variant, 'target') && variant.target === spec[1], '재생 음원 이름·악기·분리 방식이 올바르지 않습니다.');
        identifiers.add(variant.id); validateVariantAudio(variant.audio, variant.id);
        requireMagic(shared.every(key => variant.audio[key] === audio[key]), '재생 음원들의 원곡·길이·키가 일치하지 않습니다.');
      }
      this.audioVariants = manifest.audio_variants;
      this.activeAudioVariant = this.audioVariants.find(item => item.id === manifest.default_audio_variant);
      requireMagic(this.activeAudioVariant && sameAudio(audio, this.activeAudioVariant.audio), '처음 재생할 음원이 목록과 일치하지 않습니다.');
    } else if (manifest.version === 3) {
      requireMagic(audio.path === 'audio/instrumental.mp3' && audio.codec === 'mp3' && [128, 192, 256, 320].includes(audio.bitrate_kbps), 'MP3 반주 형식이 올바르지 않습니다.');
      requireMagic(Number.isSafeInteger(audio.frames) && audio.frames > 0 && audio.frames * 4 <= AUDIO_LIMIT && audio.duration === audio.frames / audio.sample_rate, 'MP3 원본 길이가 올바르지 않거나 재생 메모리 한도를 넘습니다.');
      requireMagic(typeof audio.source_sha256 === 'string' && /^[a-f0-9]{64}$/.test(audio.source_sha256), '원곡 식별 정보가 올바르지 않습니다.');
    } else {
      requireMagic(audio.path === 'audio/instrumental.wav' && audio.codec === undefined, '기존 연습 파일에는 WAV 반주가 필요합니다. 재생기를 업데이트하거나 파일을 다시 저장해 주세요.');
    }
    if (manifest.version !== 4) {
      requireMagic(manifest.audio_variants === undefined && manifest.default_audio_variant === undefined, '여러 재생 음원은 새 .magic 형식이 필요합니다. 재생기를 업데이트해 주세요.');
      this.audioVariants = [{id: 'no_vocals', label: '보컬 제거', mode: 'without', target: 'vocals', audio}];
      this.activeAudioVariant = this.audioVariants[0];
    }
    requireMagic(['pdf', 'gp'].includes(score?.kind) && typeof score.part === 'string' && score.part.length <= 500 && (score.kind === 'pdf' ? score.path === 'score/source.pdf' : /^score\/source\.gp[345]$/.test(score.path)), '악보 종류가 올바르지 않습니다.');
    const expected = new Set(['manifest.json', 'timeline.json', score.path, ...this.audioVariants.map(item => item.audio.path)]);
    const addPages = (pages, prefix) => {
      requireMagic(Array.isArray(pages) && pages.length >= 1 && pages.length <= 200, '파트 페이지 수가 올바르지 않습니다.');
      for (const [i, page] of pages.entries()) {
        requireMagic(page.path === `${prefix}/${String(i + 1).padStart(4, '0')}.png` && Number.isInteger(page.width) && Number.isInteger(page.height) && page.width > 0 && page.height > 0 && page.width <= 2201 && page.height <= 2201, '악보 화면 정보가 올바르지 않습니다.');
        expected.add(page.path);
      }
    };
    addPages(manifest.pages, 'pages');
    this.parts = [{track: score.track ?? 1, name: score.part || '악보', pages: manifest.pages, timeline: manifest.timeline}];
    if (manifest.version === 2 || ([3, 4].includes(manifest.version) && score.kind === 'gp')) {
      requireMagic(score.kind === 'gp' && Array.isArray(manifest.parts) && manifest.parts.length >= 1 && manifest.parts.length <= 64 && manifest.timing_basis?.shared === true && manifest.timing_basis.track === score.track, '전체 파트 정보가 올바르지 않습니다.');
      const tracks = new Set(); let total = 0;
      for (const part of manifest.parts) {
        requireMagic(Number.isInteger(part.track) && part.track >= 1 && part.track <= 64 && !tracks.has(part.track) && typeof part.name === 'string' && part.name.length <= 500 && typeof part.percussion === 'boolean', '파트 이름이나 번호가 올바르지 않습니다.');
        tracks.add(part.track);
        const selected = part.track === score.track, prefix = `parts/${String(part.track).padStart(2, '0')}`;
        requireMagic(part.timeline === (selected ? 'timeline.json' : `${prefix}/timeline.json`), '파트 시간표 경로가 올바르지 않습니다.');
        if (selected) requireMagic(JSON.stringify(part.pages) === JSON.stringify(manifest.pages), '기준 파트의 페이지가 다릅니다.');
        addPages(part.pages, selected ? 'pages' : `${prefix}/pages`);
        expected.add(part.timeline); total += part.pages.length;
      }
      requireMagic(tracks.has(score.track) && total <= 1000, '기준 파트가 없거나 전체 페이지가 너무 많습니다.');
      this.parts = manifest.parts;
    } else if ([3, 4].includes(manifest.version)) {
      requireMagic(manifest.parts === undefined && manifest.timing_basis === undefined, 'PDF 파트 정보가 올바르지 않습니다.');
    }
    requireMagic(expected.size === this.entries.size && [...expected].every(n => this.entries.has(n)), '연습 파일의 구성 요소가 빠졌습니다.');
    if (manifest.version === 4) requireMagic(manifest.entries && Object.keys(manifest.entries).length === expected.size - 1, '재생 음원 파일 검증 목록이 올바르지 않습니다.');
    for (const name of expected) if (name !== 'manifest.json') {
      const entry = manifest.entries?.[name];
      requireMagic(entry?.bytes === this.entries.get(name).bytes && /^[a-f0-9]{64}$/.test(entry.sha256), '파일 검증 정보가 올바르지 않습니다.');
    }
    this.manifest = manifest;
    const timing = await this.readTiming(this.parts.find(p => p.track === (score.track ?? 1)));
    this.baseTiming = timing; this.timing = timing;
    this.activePart = this.parts.find(p => p.track === (score.track ?? 1)); return this;
  }
  get pages() { return this.activePart.pages; }
  get activeAudio() { return this.activeAudioVariant.audio; }
  audioVariant(identifier) {
    const variant = this.audioVariants.find(item => item.id === identifier);
    requireMagic(variant, '선택한 재생 음원이 없습니다.'); return variant;
  }
  async readTiming(part) {
    const timing = await this.json(part.timeline, 8000000);
    requireMagic(timing.camera_mode === undefined || timing.camera_mode === 'system', '악보 화면 이동 방식이 올바르지 않습니다.');
    requireMagic(Array.isArray(timing.cues) && timing.cues.length > 0 && timing.cues.length <= 20000 && Number.isFinite(timing.offset) && Math.abs(timing.offset) <= 3600, '마디 시간표가 올바르지 않습니다.');
    let previous = -1;
    for (const cue of timing.cues) {
      requireMagic(Number.isFinite(cue.start) && cue.start >= 0 && cue.start > previous && cue.start < 86400 && Number.isInteger(cue.page) && cue.page >= 0 && cue.page < part.pages.length && String(cue.measure).length <= 32 && Number.isInteger(cue.visit) && cue.visit > 0 && Number.isInteger(cue.source_index) && cue.source_index >= 0, '마디 순서나 페이지가 올바르지 않습니다.');
      const b = cue.box;
      requireMagic(b && ['left', 'right', 'top', 'bottom', 'next_top'].every(k => Number.isFinite(b[k]) && b[k] >= 0 && b[k] <= 1) && b.right > b.left && b.bottom > b.top, '마디 위치가 올바르지 않습니다.');
      previous = cue.start;
    }
    requireMagic(Number.isFinite(timing.end) && timing.end > previous && timing.end <= 86400, '악보 끝 시간이 올바르지 않습니다.');
    if (this.baseTiming) {
      const base = this.baseTiming;
      requireMagic(timing.end === base.end && timing.offset === base.offset && timing.cues.length === base.cues.length && timing.cues.every((c, i) => ['start', 'source_index', 'visit'].every(k => c[k] === base.cues[i][k])), '파트 간 연주 시간이 다릅니다. 컴퓨터에서 다시 저장하세요.');
    }
    return timing;
  }
  async selectPart(track) {
    const part = this.parts.find(p => p.track === track);
    requireMagic(part, '선택한 파트가 없습니다.');
    const timing = await this.readTiming(part);
    this.timing = timing; this.activePart = part;
  }
  async blob(name, limit = AUDIO_LIMIT) {
    const entry = this.entries.get(name);
    requireMagic(entry && entry.bytes <= limit, '필요한 파일이 없거나 너무 큽니다.');
    const blob = this.file.slice(entry.from, entry.to); let crc = 0xffffffff;
    for (let at = 0; at < blob.size; at += 4194304) {
      const bytes = new Uint8Array(await blob.slice(at, at + 4194304).arrayBuffer());
      for (const byte of bytes) crc = crcTable[(crc ^ byte) & 255] ^ (crc >>> 8);
    }
    requireMagic(((crc ^ 0xffffffff) >>> 0) === entry.crc, '파일 내용이 손상되었습니다. 원본에서 다시 가져와 주세요.');
    return blob;
  }
  async json(name, limit) {
    const blob = await this.blob(name, limit);
    const bytes = await blob.arrayBuffer();
    if (this.manifest && crypto.subtle) {
      const digest = [...new Uint8Array(await crypto.subtle.digest('SHA-256', bytes))].map(b => b.toString(16).padStart(2, '0')).join('');
      requireMagic(digest === this.manifest.entries[name].sha256, '마디 시간표 검증에 실패했습니다. 다시 가져와 주세요.');
    }
    return JSON.parse(decoder.decode(bytes));
  }
}

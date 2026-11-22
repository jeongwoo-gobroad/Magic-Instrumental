/* SPDX-License-Identifier: GPL-3.0-only */
'use strict';
const $ = id => document.getElementById(id);
const clamp = (v, lo, hi) => Math.min(hi, Math.max(lo, v));
const number = (id, lo, hi, fallback) => {
  const v = Number($(id).value); return Number.isFinite(v) ? clamp(v, lo, hi) : fallback;
};
const clockText = t => `${String(Math.floor(Math.max(0, t) / 60)).padStart(2, '0')}:${String(Math.floor(Math.max(0, t)) % 60).padStart(2, '0')}`;
let archive = null, ctx = null, stretch = null, musicGain = null, clickGain = null;
let decodedAudio = null, audioTask = null, audioGeneration = 0;
let ready = false, loading = false, playing = false, metro = false, position = 0, duration = 0;
let segment = null, upcoming = null, latency = .1, independentOrigin = 0, clickUntil = 0;
let shownPage = -1, pendingPage = -1, pageGeneration = 0, currentCue = -1, urls = new Map(), sources = new Set();
let token = 0, timer = null, wakeLock = null, seeking = false;
let ensemble = null;
function setStatus(text, error = false) {
  for (const id of ['status', 'sessionFileStatus']) {
    $(id).textContent = text;
    $(id).classList.toggle('file-error', error);
  }
}
const fail = error => { pause(); ensemble?.unready(); setStatus(error?.message || '작업을 마치지 못했습니다. 다시 열어 주세요.', true); };
function phaseAt(input) {
  const step = 60 / number('bpm', 20, 400, 120) * 4 / Number($('unit').value);
  const beat = (input - number('origin', -30, 1800, 0)) / step;
  return {step, beat, whole: Math.floor(beat + 1e-7), phase: ((beat % 1) + 1) % 1};
}
function deadline(promise, milliseconds, message) {
  let timeout;
  return Promise.race([promise, new Promise((_, reject) => {
    timeout = setTimeout(() => reject(new Error(message)), milliseconds);
  })]).finally(() => clearTimeout(timeout));
}
function audioContext() {
  if (!ctx) {
    const Audio = window.AudioContext || window.webkitAudioContext;
    requireMagic(Audio && window.isSecureContext, 'Safari·Chrome의 웹주소에서 열어 주세요. 이 브라우저에서는 소리를 준비할 수 없습니다.');
    ctx = new Audio({sampleRate: 44100, latencyHint: 'interactive'});
    musicGain = ctx.createGain(); musicGain.connect(ctx.destination);
    clickGain = ctx.createGain(); clickGain.connect(ctx.destination);
    ctx.onstatechange = () => {
      if (ctx.state !== 'running' && (playing || metro || ensemble?.prepared)) {
        ensemble?.unready(); pause(); metro = false; updateMetro(); setStatus('소리가 일시정지되었습니다. 재생을 다시 눌러 주세요.');
      }
    };
  }
  musicGain.gain.value = number('volume', 0, 100, 75) / 100;
  clickGain.gain.value = number('metroVolume', 0, 100, 55) / 100;
  return ctx;
}
async function audioReady() {
  if (document.hidden) return false;
  const generation = audioGeneration;
  // Web Audio defaults to ambient sound on iPad, which can obey the silent switch.
  try { if (navigator.audioSession) navigator.audioSession.type = 'playback'; } catch (_) {}
  const context = audioContext();
  const current = () => generation === audioGeneration && !document.hidden;
  try {
    // Even a running context needs the explicit start request after an interruption.
    await deadline(context.resume(), 4000,
      '소리를 시작하지 못했습니다. 화면을 켠 상태에서 재생 또는 합주 준비를 다시 눌러 주세요.');
    if (!current()) return false;
    const start = context.currentTime, until = performance.now() + 1000;
    const advanced = () => Number.isFinite(context.currentTime) && context.currentTime > start;
    while (context.state === 'running' && !advanced() && performance.now() < until) {
      await new Promise(resolve => setTimeout(resolve, 50));
      if (!current()) return false;
    }
    requireMagic(context.state === 'running', '소리가 일시정지되었습니다. 재생 또는 합주 준비를 다시 눌러 주세요.');
    if (!advanced()) {
      const message = '소리 출력이 멈췄습니다. 재생 또는 합주 준비를 다시 눌러 주세요.';
      // Release a stalled output so the next user action can resume it; keep the song.
      await deadline(context.suspend(), 1000, message).catch(() => {});
      if (!current()) return false;
      throw new Error(message);
    }
    return true;
  } catch (error) {
    if (!current()) return false;
    throw error;
  }
}
function discardNode(node) {
  if (!node) return;
  node.disconnect();
  // A suspended worklet may not reply. Cleanup must never hold the file picker closed.
  const timeout = setTimeout(() => node.port.close(), 1000);
  try {
    node.dropBuffers().catch(() => {}).finally(() => { clearTimeout(timeout); node.port.close(); });
  } catch (_) { clearTimeout(timeout); node.port.close(); }
}
async function preparePlayback() {
  const selected = archive, generation = audioGeneration;
  if (!await audioReady() || generation !== audioGeneration) return false;
  if (!ready || loading || archive !== selected) return false;
  if (stretch) return true;
  if (!audioTask) {
    const decoded = decodedAudio, task = {cancelled: false, node: null, promise: null};
    requireMagic(decoded, '반주를 다시 열어 주세요.');
    audioTask = task;
    const prepare = async () => {
      const node = await SignalsmithStretch(ctx, {numberOfInputs: 1, numberOfOutputs: 1, outputChannelCount: [2]});
      if (task.cancelled) { discardNode(node); return false; }
      task.node = node; node.connect(musicGain);
      const nextLatency = await node.latency();
      if (task.cancelled) return false;
      const channels = [decoded.getChannelData(0).slice(), decoded.getChannelData(1).slice()];
      await node.addBuffers(channels, channels.map(c => c.buffer));
      if (task.cancelled) return false;
      requireMagic(ctx.state === 'running', '소리가 일시정지되었습니다. 재생 또는 합주 준비를 다시 눌러 주세요.');
      stretch = node; task.node = null; latency = nextLatency; decodedAudio = null;
      return true;
    };
    task.promise = deadline(prepare(), 15000,
      '반주 재생을 준비하지 못했습니다. 재생 또는 합주 준비를 다시 눌러 주세요.').catch(error => {
      task.cancelled = true; discardNode(task.node); task.node = null;
      if (archive === selected) throw error;
      return false;
    }).finally(() => { if (audioTask === task) audioTask = null; });
  }
  try {
    const prepared = await audioTask.promise;
    return prepared && generation === audioGeneration && !document.hidden;
  } catch (error) {
    if (generation !== audioGeneration || document.hidden) return false;
    throw error;
  }
}
function silenceClicks() {
  for (const source of sources) { try { source.stop(); } catch (_) {} }
  sources.clear(); clickUntil = ctx?.currentTime || 0;
}
function drum(when, strong, count = false) {
  if (!ctx || when < ctx.currentTime - .015 || clickGain.gain.value === 0) return;
  const osc = ctx.createOscillator(), gain = ctx.createGain();
  osc.type = count ? 'triangle' : 'sine';
  const frequency = count ? (strong ? 150 : 340) : (strong ? 1300 : 900);
  osc.frequency.setValueAtTime(frequency, when);
  if (count) osc.frequency.exponentialRampToValueAtTime(strong ? 48 : 110, when + .09);
  gain.gain.setValueAtTime(.0001, when);
  gain.gain.exponentialRampToValueAtTime(count ? .8 : .3, when + .003);
  gain.gain.exponentialRampToValueAtTime(.0001, when + (count ? .14 : .045));
  osc.connect(gain); gain.connect(clickGain);
  sources.add(osc); osc.onended = () => { sources.delete(osc); osc.disconnect(); gain.disconnect(); };
  osc.start(when); osc.stop(when + .16);
}
function scheduleNode(active, output, input, rate = number('speed', 25, 200, 100) / 100) {
  // The bundled scheduling adapter retains earlier segments until their actual output time.
  stretch.schedule({active, output, input, rate, semitones: Number($('key').value), loopStart: 0, loopEnd: 0}).catch(fail);
}
function loopBounds() {
  if (ensemble?.role === 'member' && ensemble.remoteState) return ensemble.remoteState.loop || [0, duration];
  if (!archive || !$('loop').checked) return [0, duration];
  const cues = archive.timing.cues, offset = archive.timing.offset;
  const first = Number($('loopStart').value), last = Number($('loopEnd').value);
  requireMagic(Number.isInteger(first) && Number.isInteger(last) && first <= last && cues[first] && cues[last], '끝 마디는 시작 마디보다 뒤에 골라 주세요.');
  const lo = Math.max(0, cues[first].start + offset);
  const hi = Math.min(duration, (cues[last + 1]?.start ?? archive.timing.end) + offset);
  requireMagic(hi > lo, '선택한 마디가 반주 시간 범위 밖입니다.'); return [lo, hi];
}
function createSegment(input, earliest, count) {
  const [, end] = loopBounds(), rate = number('speed', 25, 200, 100) / 100;
  const beat = phaseAt(input), beatSeconds = beat.step / rate;
  const countBeats = count ? Number($('countBars').value) * number('beats', 1, 16, 4) : 0;
  const firstCountBeat = Math.ceil(beat.beat - 1e-7) - countBeats;
  const countDuration = count ? (beat.beat - firstCountBeat) * beatSeconds : 0;
  const out = earliest + countDuration;
  const s = {input, end, rate, out, until: out + (end - input) / rate,
             countStart: earliest, countBeats, beatSeconds, firstCountBeat};
  scheduleNode(true, out, input, rate);
  scheduleNode(false, s.until, end, rate);
  if (ensemble?.pending) ensemble.pending.armed = false;
  return s;
}
function nowPosition(time = ctx?.currentTime) {
  if (!playing || !segment || !ctx) return position;
  if (upcoming && time >= upcoming.countStart) { segment = upcoming; upcoming = null; }
  return clamp(segment.input + Math.max(0, time - segment.out) * segment.rate, 0, segment.end);
}
function pause() {
  position = nowPosition(); playing = false; segment = upcoming = null; token++;
  if (stretch && ctx) scheduleNode(false, ctx.currentTime, position);
  silenceClicks(); $('play').textContent = '▶ 재생';
  if (wakeLock) { wakeLock.release().catch(() => {}); wakeLock = null; }
}
async function play(count = $('count').checked || ($('loop').checked && $('loopCount').checked)) {
  if (!ready || loading) return;
  const generation = ++token;
  $('play').textContent = '소리 준비 중…';
  try {
    if (!await preparePlayback()) return;
  } catch (error) {
    if (generation === token) throw error;
    return;
  } finally {
    if (generation === token) $('play').textContent = '▶ 재생';
  }
  if (generation !== token) return;
  requireMagic(ctx.state === 'running', '소리가 일시정지되었습니다. 재생을 다시 눌러 주세요.');
  const [lo, hi] = loopBounds();
  if (position < lo || position >= hi - .001) position = lo;
  silenceClicks();
  segment = createSegment(position, ctx.currentTime + Math.max(.12, latency + .03), count);
  upcoming = null; playing = true; $('play').textContent = 'Ⅱ 일시정지';
  setStatus(archive.timing.reviewed ? '재생 중입니다.' : '재생 중 · 자동 추정한 마디입니다. 반주와 맞는지 확인해 주세요.');
  if (navigator.wakeLock) navigator.wakeLock.request('screen').then(lock => {
    if (playing) wakeLock = lock; else lock.release();
  }).catch(() => {});
}
async function seek(target, resume = playing) {
  pause(); const [lo, hi] = loopBounds(); position = clamp(target, lo, Math.max(lo, hi - .001));
  showPosition(); if (resume) await play(false);
}
function cueAt(time) {
  if (!archive) return -1;
  const cues = archive.timing.cues, t = time - archive.timing.offset;
  let low = 0, high = cues.length;
  while (low < high) { const middle = (low + high) >>> 1; if (cues[middle].start <= t) low = middle + 1; else high = middle; }
  return t < archive.timing.end ? low - 1 : cues.length;
}
function beatsBetween(s, from, to) {
  const beats = number('beats', 1, 16, 4);
  if (s.countBeats) for (let i = 0; i < s.countBeats; i++) {
    const when = s.countStart + i * s.beatSeconds;
    if (when >= from && when < to && when < s.out - .001) drum(when, ((s.firstCountBeat + i) % beats + beats) % beats === 0, true);
  }
  if (!metro || !$('linked').checked) return;
  const start = Math.max(from, s.out), finish = Math.min(to, s.until);
  if (finish <= start) return;
  const step = phaseAt(s.input).step, origin = number('origin', -30, 1800, 0);
  let n = Math.max(0, Math.ceil((s.input + (start - s.out) * s.rate - origin) / step - 1e-7));
  for (;; n++) {
    const when = s.out + (origin + n * step - s.input) / s.rate;
    if (when >= finish - 1e-7) break;
    if (when >= start - 1e-7) drum(when, n % beats === 0);
  }
}
function tick() {
  if (!ctx || ctx.state !== 'running') return;
  ensemble?.activate();
  const now = ctx.currentTime;
  if (playing) {
    position = nowPosition();
    if (!upcoming && segment.until - now < .4 && (ensemble?.role === 'member' ? ensemble.remoteState?.loop : $('loop').checked)) {
      const [start] = loopBounds();
      upcoming = createSegment(start, Math.max(segment.until, now + latency + .03), $('loopCount').checked);
    }
    if (!upcoming && now >= segment.until) { pause(); position = segment?.end ?? position; }
  }
  ensemble?.arm();
  const from = Math.max(now + .006, clickUntil), to = now + .18;
  if (playing) {
    const cutoff = ensemble?.pending?.at ?? Infinity;
    beatsBetween(segment, from, Math.min(to, cutoff));
    if (upcoming) beatsBetween(upcoming, from, Math.min(to, cutoff));
  }
  if (ensemble?.pending?.next) beatsBetween(ensemble.pending.next, Math.max(from, ensemble.pending.at), to);
  if (metro && !$('linked').checked) {
    const step = phaseAt(0).step, beats = number('beats', 1, 16, 4);
    let n = Math.max(0, Math.ceil((from - independentOrigin) / step - 1e-7));
    for (; independentOrigin + n * step < to; n++) drum(independentOrigin + n * step, n % beats === 0);
  }
  clickUntil = to;
}
function updateMetro() {
  $('metro').textContent = metro ? '메트로놈 끄기' : '메트로놈 켜기';
  $('metro').setAttribute('aria-pressed', String(metro));
  const beats = Math.round(number('beats', 1, 16, 4)); $('beats').value = beats;
  if ($('dots').children.length !== beats) $('dots').replaceChildren(...Array.from({length: beats}, () => {
    const dot = document.createElement('span'); dot.className = 'dot'; return dot;
  }));
  const rate = $('linked').checked ? number('speed', 25, 200, 100) / 100 : 1;
  $('effective').textContent = `${(number('bpm', 20, 400, 120) * rate).toFixed(1)} BPM`;
}
function updateMetroState() {
  let text = '꺼짐';
  if (metro) {
    if (number('metroVolume', 0, 100, 55) === 0) text = '음량 0% · 박자 설정에서 올려 주세요';
    else if (!$('linked').checked) text = '메트로놈 단독 재생 중';
    else if (!playing) text = '반주 대기 중';
    else if (segment && ctx.currentTime < segment.out) text = segment.countBeats ? '드럼 카운트 후 반주와 시작' : '반주 시작 준비 중';
    else if (nowPosition() < number('origin', -30, 1800, 0)) text = '설정한 첫 박을 기다리는 중';
    else text = '반주와 함께 재생 중';
  }
  if ($('metroState').textContent !== text) $('metroState').textContent = text;
}
function updateDots() {
  let beat = null;
  if (ctx && playing && segment && ctx.currentTime < segment.out && ctx.currentTime >= segment.countStart) {
    beat = segment.firstCountBeat + Math.floor((ctx.currentTime - segment.countStart) / segment.beatSeconds);
  } else if (ctx && metro) {
    if ($('linked').checked && playing && position >= number('origin', -30, 1800, 0)) beat = phaseAt(position).whole;
    if (!$('linked').checked && ctx.currentTime >= independentOrigin) beat = Math.floor((ctx.currentTime - independentOrigin) / phaseAt(0).step);
  }
  const dots = [...$('dots').children];
  dots.forEach((dot, i) => dot.classList.toggle('on', beat !== null && ((beat % dots.length) + dots.length) % dots.length === i));
}
async function showPage(index) {
  if (!archive || index < 0 || index >= archive.pages.length) return;
  if (shownPage === index || pendingPage === index) return;
  pendingPage = index;
  const generation = ++pageGeneration, selected = archive, pages = archive.pages;
  let url = urls.get(index);
  if (!url) {
    const bytes = await selected.blob(pages[index].path, 20000000);
    const head = new Uint8Array(await bytes.slice(0, 24).arrayBuffer());
    const view = new DataView(head.buffer), p = pages[index];
    requireMagic(head.length === 24 && head[0] === 137 && decoder.decode(head.slice(1, 4)) === 'PNG' && view.getUint32(16) === p.width && view.getUint32(20) === p.height, '악보 이미지가 손상되었습니다.');
    if (generation !== pageGeneration || archive !== selected) return;
    url = URL.createObjectURL(new Blob([bytes], {type: 'image/png'})); urls.set(index, url);
    while (urls.size > 3) { const key = urls.keys().next().value; URL.revokeObjectURL(urls.get(key)); urls.delete(key); }
  }
  if (generation !== pageGeneration) return;
  pendingPage = -1; shownPage = index; $('scoreImage').src = url; $('scoreImage').hidden = false; $('empty').hidden = true;
  $('pageText').textContent = `${index + 1} / ${archive.pages.length}`;
  $('viewport').scrollTo(0, 0); resizePaper();
  showHighlight(archive.timing.cues[cueAt(nowPosition())]);
}
function resizePaper() {
  if (!archive || shownPage < 0) return;
  const width = Math.max(200, $('viewport').clientWidth - 32) * Number($('zoom').value);
  const page = archive.pages[shownPage];
  $('paper').style.width = width + 'px'; $('paper').style.minHeight = '0';
  $('paper').style.height = (width * page.height / page.width) + 'px';
}
function showHighlight(cue) {
  const highlight = $('highlight'), box = cue?.box;
  highlight.hidden = !$('highlightEnabled').checked || !box || shownPage !== cue.page;
  if (highlight.hidden) return;
  Object.assign(highlight.style, {left: box.left * 100 + '%', top: box.top * 100 + '%', width: (box.right - box.left) * 100 + '%', height: (box.bottom - box.top) * 100 + '%'});
}
function showPosition() {
  const time = nowPosition(); position = time;
  $('clock').textContent = `${clockText(time)} / ${clockText(duration)}`;
  if (!seeking) $('seek').value = time;
  const index = cueAt(time); currentCue = index;
  const cue = archive?.timing.cues[index];
  $('measure').textContent = cue ? `${cue.measure}마디 · ${cue.visit}회차` : '—';
  showHighlight(cue);
  if (!cue) return;
  if ($('follow').checked && shownPage !== cue.page) { showPage(cue.page).catch(fail); return; }
  const b = cue.box;
  if (shownPage !== cue.page) return;
  const paper = $('paper'), view = $('viewport'), width = paper.clientWidth, height = paper.clientHeight;
  if ($('follow').checked) {
    const end = archive.timing.cues[index + 1]?.start ?? archive.timing.end;
    const fraction = clamp((time - archive.timing.offset - cue.start) / (end - cue.start), 0, 1);
    const x = (b.left + (b.right - b.left) * fraction) * width - view.clientWidth * .35;
    const tall = Math.max(0, (b.bottom - b.top) * height - view.clientHeight * .7);
    // Simultaneous staves stay together; bar time must not scan down the parts.
    const y = b.top * height - 32 + (archive.timing.camera_mode === 'system' ? 0 : tall * fraction);
    const desiredX = clamp(x, 0, Math.max(0, paper.offsetWidth + 32 - view.clientWidth));
    const desiredY = clamp(y, 0, Math.max(0, paper.offsetHeight + 32 - view.clientHeight));
    view.scrollLeft += (desiredX - view.scrollLeft) * .24;
    view.scrollTop += (desiredY - view.scrollTop) * .24;
  }
}
function frame() { if (ready) showPosition(); updateDots(); updateMetroState(); requestAnimationFrame(frame); }
function setAvailable(value) {
  for (const id of ['play', 'home', 'seek', 'back', 'forward', 'loop', 'loopStart', 'loopEnd']) $(id).disabled = !value;
  for (const id of ['file', 'sessionFile']) $(id).disabled = loading;
  ensemble?.update();
}
function clearScorePage() {
  pageGeneration++; shownPage = pendingPage = -1;
  $('scoreImage').hidden = true; $('highlight').hidden = true;
  $('scoreImage').removeAttribute('src');
  for (const url of urls.values()) URL.revokeObjectURL(url);
  urls.clear();
}
function releaseAudio() {
  if (audioTask) {
    audioTask.cancelled = true; discardNode(audioTask.node); audioTask.node = null; audioTask = null;
  }
  const node = stretch; stretch = null; decodedAudio = null;
  discardNode(node);
}
async function openFile(file) {
  if (!file || loading) return;
  ensemble?.unready();
  loading = true; pause(); ready = false; setAvailable(false);
  setStatus('연습 파일을 확인하고 있습니다…');
  clearScorePage();
  archive = null;
  try {
    releaseAudio();
    const fileError = '파일을 읽지 못했습니다. 파일 앱에서 기기에 다운로드한 뒤 다시 선택해 주세요.';
    const next = await deadline(new MagicArchive(file).open(), 60000, fileError);
    setStatus('반주를 준비하고 있습니다…');
    const blob = await deadline(next.blob(next.manifest.audio.path), 60000, fileError);
    const data = await deadline(blob.arrayBuffer(), 60000, fileError);
    if (crypto.subtle) {
      const digest = [...new Uint8Array(await crypto.subtle.digest('SHA-256', data))].map(n => n.toString(16).padStart(2, '0')).join('');
      requireMagic(digest === next.manifest.entries[next.manifest.audio.path].sha256, '반주 검증에 실패했습니다. 파일을 다시 가져와 주세요.');
    }
    const mp3 = next.manifest.version === 3;
    const mp3Error = 'MP3 반주를 원본 길이로 재생할 수 없습니다. WAV로 저장한 파일을 사용하거나 브라우저를 업데이트해 주세요.';
    if (mp3) validateMp3(data, next.manifest.audio);
    let decoded;
    try { decoded = await deadline(audioContext().decodeAudioData(data), 60000, '반주를 읽지 못했습니다. 파일을 다시 선택해 주세요.'); }
    catch (error) { if (mp3) throw new Error(mp3Error); throw error; }
    if (mp3) requireMagic(decoded.sampleRate === next.manifest.audio.sample_rate && decoded.length === next.manifest.audio.frames && decoded.numberOfChannels === 2, mp3Error);
    else requireMagic(Math.abs(decoded.duration - next.manifest.audio.duration) < .05 && decoded.numberOfChannels === 2, '반주 길이·채널 정보가 일치하지 않습니다.');
    decodedAudio = decoded;
    archive = next; duration = decoded.duration; position = 0; segment = upcoming = null;
    $('title').textContent = next.manifest.title;
    $('part').textContent = next.manifest.score.kind === 'gp' ? `GP 파트 · ${next.manifest.score.part}` : 'PDF 악보';
    $('partSelect').replaceChildren(...next.parts.map(part => {
      const option = document.createElement('option'); option.value = part.track;
      option.textContent = part.name + (part.percussion ? ' · 드럼' : ''); return option;
    }));
    $('partSelect').value = next.activePart.track; $('partChoice').hidden = next.parts.length < 2;
    $('seek').max = duration; $('seek').value = 0; $('loop').checked = false; $('key').value = '0';
    const p = next.manifest.practice || {};
    for (const [id, lo, hi, fallback] of [['bpm',20,400,120],['beats',1,16,4],['origin',-30,1800,0]]) $(id).value = Number.isFinite(p[id]) ? clamp(p[id],lo,hi) : fallback;
    $('unit').value = [2,4,8,16].includes(p.unit) ? p.unit : 4;
    for (const id of ['loopStart', 'loopEnd']) $(id).replaceChildren();
    const offset = next.timing.offset;
    next.timing.cues.forEach((cue, index) => {
      const end = next.timing.cues[index + 1]?.start ?? next.timing.end;
      if (cue.start + offset >= duration || end + offset <= 0) return;
      for (const id of ['loopStart', 'loopEnd']) {
        const option = document.createElement('option'); option.value = index;
        option.textContent = `${cue.measure}마디 · ${cue.visit}회차`; $(id).append(option);
      }
    });
    requireMagic($('loopStart').options.length > 0, '악보의 마디 시간이 반주 범위 밖입니다. 컴퓨터에서 시간표를 보정해 주세요.');
    $('loopEnd').selectedIndex = $('loopEnd').options.length - 1;
    await deadline(showPage(0), 60000, '악보를 읽지 못했습니다. 파일을 다시 선택해 주세요.');
    ready = true; setAvailable(true); updateMetro(); updateKey();
    setStatus(next.timing.reviewed ? '준비 완료 · 재생을 눌러 주세요.' : '준비 완료 · 자동 추정한 마디입니다. 반주와 맞는지 확인해 주세요.');
  } catch (error) {
    archive = null; ready = false; seeking = false; position = duration = 0;
    clearScorePage();
    $('empty').hidden = false;
    $('paper').removeAttribute('style');
    $('pageText').textContent = '0 / 0';
    $('title').textContent = '준비한 곡을 열어 주세요';
    $('part').textContent = '컴퓨터에서 만든 .magic 파일을 선택하세요.';
    $('partChoice').hidden = true;
    for (const id of ['partSelect', 'loopStart', 'loopEnd']) $(id).replaceChildren();
    $('loop').checked = false;
    $('key').value = '0'; $('seek').max = 1;
    showPosition(); updateKey();
    fail(error);
    releaseAudio();
  } finally {
    loading = false;
    for (const id of ['file', 'sessionFile']) $(id).value = '';
    setAvailable(ready);
  }
}
function updateKey() {
  const base = archive?.manifest.audio.pitch_semitones || 0, extra = Number($('key').value), total = base + extra;
  $('keyText').textContent = total ? `원곡 대비 ${total > 0 ? '+' : ''}${total}반음` : '원곡 키';
  $('key').title = `.magic 반주에 적용된 키 ${base >= 0 ? '+' : ''}${base}반음에서 추가 조절`;
}
async function retime() {
  const resume = playing, saved = nowPosition(); pause(); position = saved;
  updateMetro(); updateKey(); $('speedText').textContent = (Number($('speed').value) / 100).toFixed(2) + '×';
  if (resume) await play(false);
}
for (let n = -12; n <= 12; n++) { const option = document.createElement('option'); option.value = n; option.textContent = n ? `${n > 0 ? '+' : ''}${n}반음` : '추가 0'; $('key').append(option); }
$('key').value = 0;
for (const id of ['file', 'sessionFile']) $(id).onchange = event => openFile(event.target.files[0]);
$('play').onclick = () => playing ? pause() : play().catch(fail);
$('home').onclick = () => seek(0).catch(fail);
$('seek').oninput = () => { seeking = true; };
$('seek').onchange = () => { const target = Number($('seek').value); seeking = false; seek(target).catch(fail); };
$('seek').addEventListener('pointercancel', () => { seeking = false; });
for (const [id, direction] of [['back',-1],['forward',1]]) $(id).onclick = () => {
  if (!archive) return;
  const index = clamp(cueAt(nowPosition()) + direction * Math.round(number('step',1,10000,1)), 0, archive.timing.cues.length - 1);
  seek(archive.timing.cues[index].start + archive.timing.offset).catch(fail);
};
for (const id of ['speed','key']) $(id).onchange = () => retime().catch(fail);
$('speed').oninput = () => { $('speedText').textContent = (Number($('speed').value) / 100).toFixed(2) + '×'; };
$('speedReset').onclick = () => { $('speed').value = 100; retime().catch(fail); };
$('volume').oninput = () => { if (musicGain) musicGain.gain.value = number('volume',0,100,75) / 100; };
$('metroVolume').oninput = () => { if (clickGain) clickGain.gain.value = number('metroVolume',0,100,55) / 100; };
$('metro').onclick = async () => {
  try {
    if (!metro && !await audioReady()) return;
    metro = !metro; independentOrigin = ctx.currentTime + .1; silenceClicks(); updateMetro();
  } catch (e) { fail(e); }
};
for (const id of ['bpm','beats','unit','origin','linked','countBars','loopCount']) $(id).onchange = () => {
  silenceClicks(); if (ctx) independentOrigin = ctx.currentTime + .1; retime().catch(fail);
};
$('firstBeat').onclick = () => { $('origin').value = clamp(nowPosition(),0,1800).toFixed(3); retime().catch(fail); };
for (const id of ['loop','loopStart','loopEnd']) $(id).onchange = () => {
  const resume = playing; pause();
  try { const [lo,hi] = loopBounds(); position = clamp(position,lo,Math.max(lo,hi-.001)); if (resume) play($('loopCount').checked).catch(fail); }
  catch (e) { $('loop').checked = false; fail(e); }
};
$('partSelect').onchange = async () => {
  const selected = archive, track = Number($('partSelect').value);
  $('partSelect').disabled = true;
  try {
    await selected.selectPart(track);
    if (archive !== selected) return;
    clearScorePage();
    $('part').textContent = `GP 파트 · ${selected.activePart.name}`;
    await showPage(selected.timing.cues[clamp(cueAt(nowPosition()), 0, selected.timing.cues.length - 1)].page);
    setStatus('파트를 바꿨습니다. 반주 재생 위치는 그대로 유지됩니다.');
  } catch (error) { $('partSelect').value = selected.activePart.track; fail(error); }
  finally { $('partSelect').disabled = false; }
};
$('zoom').onchange = resizePaper; window.addEventListener('resize',resizePaper);
$('scoreImage').onload = resizePaper;
for (const [id, direction] of [['prevPage',-1],['nextPage',1]]) $(id).onclick = () => { $('follow').checked = false; showPage(shownPage + direction).catch(fail); };
for (const event of ['wheel','touchstart','pointerdown']) $('viewport').addEventListener(event, () => { $('follow').checked = false; }, {passive:true});
try { $('highlightEnabled').checked = localStorage.getItem('magic-measure-highlight') !== 'false'; } catch (_) {}
$('highlightEnabled').onchange = () => {
  try { localStorage.setItem('magic-measure-highlight', String($('highlightEnabled').checked)); } catch (_) {}
  showHighlight(archive?.timing.cues[cueAt(nowPosition())]);
};
function theme(mode) { document.documentElement.dataset.theme = mode; try { localStorage.setItem('magic-theme',mode); } catch (_) {} }
try { theme(localStorage.getItem('magic-theme') || (matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light')); } catch (_) { theme('light'); }
$('theme').onclick = () => theme(document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark');
function suspendAudio() {
  audioGeneration++; ensemble?.unready(); pause(); metro = false; updateMetro();
  if (ctx && ctx.state !== 'closed') ctx.suspend().catch(() => {});
}
document.addEventListener('visibilitychange', () => {
  if (document.hidden) {
    suspendAudio();
    if (ready) setStatus('화면을 벗어나 일시정지했습니다. 재생을 눌러 이어서 연습하세요.');
  }
});
window.addEventListener('pagehide', () => { ensemble?.leave(); suspendAudio(); });
updateMetro(); timer = setInterval(() => { try { tick(); } catch (error) { fail(error); } },25); requestAnimationFrame(frame);
if ('serviceWorker' in navigator && location.protocol !== 'file:') {
  navigator.serviceWorker.register('./sw.js').then(() => navigator.serviceWorker.ready).then(() => {
    $('offline').textContent = '오프라인 준비 완료 · 홈 화면에 추가해 두세요.';
  }).catch(() => { $('offline').textContent = '오프라인 저장을 준비하지 못했습니다. 인터넷 연결을 확인하고 다시 열어 주세요.'; });
} else $('offline').textContent = '단일 파일 재생기 · iPad·Android에서는 웹주소로 열고 홈 화면에 추가하세요.';
// Stable inspection surface for automated tests; no file contents or uploads.
window.MagicPlayer = {openFile, archive: () => archive, state: () => ({ready,loading,playing,position:nowPosition(),duration,page:shownPage,cue:currentCue,metro,latency}), pause, play, seek, context: () => ctx};

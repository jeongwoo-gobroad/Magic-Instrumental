/* SPDX-License-Identifier: GPL-3.0-only */
'use strict';
const $ = id => document.getElementById(id);
const clamp = (v, lo, hi) => Math.min(hi, Math.max(lo, v));
const number = (id, lo, hi, fallback) => {
  const v = Number($(id).value); return Number.isFinite(v) ? clamp(v, lo, hi) : fallback;
};
const clockText = t => `${String(Math.floor(Math.max(0, t) / 60)).padStart(2, '0')}:${String(Math.floor(Math.max(0, t)) % 60).padStart(2, '0')}`;
let archive = null, ctx = null, stretch = null, musicGain = null, clickGain = null;
let decodedAudio = null, audioTask = null, audioGeneration = 0, audioPending = 0;
let ready = false, loading = false, playing = false, metro = false, position = 0, duration = 0;
let segment = null, upcoming = null, latency = .1, independentOrigin = 0, clickUntil = 0;
let shownPage = -1, pendingPage = -1, pageGeneration = 0, currentCue = -1, urls = new Map(), sources = new Set();
let pageSlots = [], wantedPages = new Set(), pageLoads = new Map(), pageErrors = new Map();
let followCue = -1, followTime = 0, followStamp = 0, followMotion = null;
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
  audioPending++;
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
  } finally {
    audioPending--;
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
  resetFollow(); showPosition(); if (resume) await play(false);
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
function resetFollow() {
  followCue = -1; followMotion = null; followStamp = 0;
}
function makeScorePages() {
  if (!archive || pageSlots.length) return;
  const firstImage = $('scoreImage'), paper = $('paper'), fragment = document.createDocumentFragment();
  pageSlots = archive.pages.map((page, index) => {
    const element = document.createElement('div'), image = index ? document.createElement('img') : firstImage;
    element.className = 'score-page'; element.dataset.page = index;
    image.className = 'score-image'; image.alt = `악보 ${index + 1}페이지`; image.hidden = true;
    element.append(image); fragment.append(element);
    return {element, image, top: 0, height: 0};
  });
  paper.append(fragment); paper.classList.add('continuous'); $('empty').hidden = true;
  resizePaper();
}
function pageAtHeight(height) {
  let low = 0, high = pageSlots.length;
  while (low < high) {
    const middle = (low + high) >>> 1;
    if (pageSlots[middle].top <= height) low = middle + 1; else high = middle;
  }
  return clamp(low - 1, 0, pageSlots.length - 1);
}
function selectScorePage(index) {
  if (shownPage === index) return;
  $('scoreImage')?.removeAttribute('id');
  pageSlots[index].image.id = 'scoreImage'; shownPage = index;
  $('pageText').textContent = `${index + 1} / ${pageSlots.length}`;
}
function loadScorePage(index) {
  if (pageLoads.has(index)) return pageLoads.get(index);
  if (pageErrors.has(index)) return Promise.reject(pageErrors.get(index));
  if (urls.has(index)) return Promise.resolve(true);
  const selected = archive, pages = selected.pages, generation = pageGeneration;
  const slot = pageSlots[index];
  let cancelled = false;
  const sameScore = () => archive === selected && pages === selected.pages && generation === pageGeneration;
  const current = () => !cancelled && sameScore();
  const prepare = (async () => {
    const bytes = await selected.blob(pages[index].path, 20000000);
    const head = new Uint8Array(await bytes.slice(0, 24).arrayBuffer());
    const view = new DataView(head.buffer), page = pages[index];
    requireMagic(head.length === 24 && head[0] === 137 && decoder.decode(head.slice(1, 4)) === 'PNG' && view.getUint32(16) === page.width && view.getUint32(20) === page.height, '악보 이미지가 손상되었습니다.');
    if (!current() || !wantedPages.has(index)) return false;
    const url = URL.createObjectURL(new Blob([bytes], {type: 'image/png'}));
    urls.set(index, url); slot.image.src = url;
    try { await slot.image.decode(); }
    catch (_) {
      if (!current() || !wantedPages.has(index) || urls.get(index) !== url) return false;
      URL.revokeObjectURL(url); urls.delete(index); slot.image.removeAttribute('src');
      throw new Error('악보 이미지가 손상되었습니다. 파일을 다시 가져와 주세요.');
    }
    if (!current() || !wantedPages.has(index) || urls.get(index) !== url) return false;
    slot.image.hidden = false;
    showHighlight(archive.timing.cues[cueAt(nowPosition())]);
    return true;
  })();
  const task = deadline(prepare, 60000, '악보 페이지를 읽지 못했습니다. 파일을 다시 선택해 주세요.').catch(error => {
    if (!current()) return false;
    cancelled = true;
    const url = urls.get(index);
    if (url) { URL.revokeObjectURL(url); urls.delete(index); }
    slot.image.hidden = true; slot.image.removeAttribute('src');
    pageErrors.set(index, error); throw error;
  }).finally(() => {
    if (pageLoads.get(index) === task) pageLoads.delete(index);
    if (sameScore()) prepareVisiblePages();
  });
  pageLoads.set(index, task);
  return task;
}
function prepareVisiblePages() {
  if (!archive || !pageSlots.length) return;
  const center = Math.max(0, shownPage);
  const queue = [...wantedPages].filter(index => !urls.has(index) && !pageLoads.has(index) && !pageErrors.has(index))
    .sort((a, b) => Math.abs(a - center) - Math.abs(b - center));
  while (queue.length && pageLoads.size < 3) {
    const index = queue.shift();
    loadScorePage(index).catch(error => { if (index === shownPage && ready) fail(error); });
  }
}
function refreshVisiblePages() {
  if (!archive || !pageSlots.length) return;
  const view = $('viewport'), top = view.scrollTop - $('paper').offsetTop;
  const bottom = top + view.clientHeight;
  let first = pageAtHeight(top), last = pageAtHeight(bottom);
  if (first < last && pageSlots[first].top + pageSlots[first].height <= top) first++;
  if (last > first && pageSlots[last].top >= bottom) last--;
  // Keep all visible pages and one neighbor on either side, not the whole score.
  wantedPages = new Set();
  for (let index = Math.max(0, first - 1); index <= Math.min(pageSlots.length - 1, last + 1); index++) wantedPages.add(index);
  for (const [index, url] of urls) if (!wantedPages.has(index)) {
    pageSlots[index].image.hidden = true; pageSlots[index].image.removeAttribute('src');
    URL.revokeObjectURL(url); urls.delete(index);
  }
  if (!$('follow').checked) selectScorePage(pageAtHeight(top + view.clientHeight / 2));
  prepareVisiblePages(); showHighlight(archive.timing.cues[cueAt(nowPosition())]);
}
async function ensureScorePage(index) {
  const generation = pageGeneration;
  while (generation === pageGeneration && !pageLoads.has(index) && !urls.has(index) && pageLoads.size >= 3) {
    await Promise.race([...pageLoads.values()].map(task => task.catch(() => false)));
  }
  if (generation !== pageGeneration) return false;
  return loadScorePage(index);
}
async function showPage(index) {
  if (!archive || index < 0 || index >= archive.pages.length) return;
  makeScorePages();
  const generation = pageGeneration;
  pendingPage = index; selectScorePage(index); resetFollow();
  const view = $('viewport');
  view.scrollTo(0, Math.max(0, pageSlots[index].top + $('paper').offsetTop - 16));
  if ($('follow').checked) showPosition();
  refreshVisiblePages(); wantedPages.add(index);
  await ensureScorePage(index);
  if (generation !== pageGeneration || pendingPage !== index) return;
  pendingPage = -1;
  showHighlight(archive.timing.cues[cueAt(nowPosition())]);
}
function resizePaper() {
  if (!archive || !pageSlots.length) return;
  const paper = $('paper'), view = $('viewport');
  const previous = pageSlots[pageAtHeight(view.scrollTop + view.clientHeight / 2 - paper.offsetTop)];
  const fraction = previous.height ? (view.scrollTop + view.clientHeight / 2 - paper.offsetTop - previous.top) / previous.height : 0;
  const width = Math.max(200, view.clientWidth - 32) * Number($('zoom').value);
  const oldWidth = paper.clientWidth, oldHeight = previous.height;
  paper.style.width = width + 'px'; paper.style.minHeight = '0';
  paper.style.paddingBlock = view.clientHeight / 2 + 'px';
  let top = view.clientHeight / 2;
  pageSlots.forEach((slot, index) => {
    const page = archive.pages[index]; slot.top = top; slot.height = width * page.height / page.width;
    slot.element.style.height = slot.height + 'px'; top += slot.height + 16;
  });
  const paperTop = paper.getBoundingClientRect().top;
  pageSlots.forEach(slot => {
    const rect = slot.element.getBoundingClientRect(); slot.top = rect.top - paperTop; slot.height = rect.height;
  });
  resetFollow();
  if (!$('follow').checked && oldHeight) {
    view.scrollTop = paper.offsetTop + previous.top + previous.height * fraction - view.clientHeight / 2;
    if (oldWidth) view.scrollLeft *= width / oldWidth;
  }
  if ($('follow').checked) showPosition();
  refreshVisiblePages();
}
function showHighlight(cue) {
  const highlight = $('highlight'), box = cue?.box, slot = pageSlots[cue?.page];
  highlight.hidden = !$('highlightEnabled').checked || !box || !slot || !urls.has(cue.page);
  if (highlight.hidden) return;
  if (highlight.parentElement !== slot.element) slot.element.append(highlight);
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
  if (!cue || !$('follow').checked || !pageSlots.length) return;
  const previous = archive.timing.cues[followCue], slot = pageSlots[cue.page];
  const paper = $('paper'), view = $('viewport'), width = slot.element.clientWidth, box = cue.box;
  const end = archive.timing.cues[index + 1]?.start ?? archive.timing.end;
  const fraction = clamp((time - archive.timing.offset - cue.start) / (end - cue.start), 0, 1);
  const measureWidth = (box.right - box.left) * width;
  const focusX = box.left * width + measureWidth * (measureWidth > view.clientWidth ? fraction : .5);
  const x = paper.offsetLeft + focusX - view.clientWidth / 2;
  // A measure's simultaneous staves share one vertical center, including notation and TAB.
  const y = paper.offsetTop + slot.top + (box.top + box.bottom) * slot.height / 2 - view.clientHeight / 2;
  const desiredX = clamp(x, 0, Math.max(0, view.scrollWidth - view.clientWidth));
  const desiredY = clamp(y, 0, Math.max(0, view.scrollHeight - view.clientHeight));
  const now = performance.now(), elapsed = followStamp ? Math.min(50, now - followStamp) : 16;
  if (followCue !== index) {
    const adjacent = playing && index === followCue + 1 && previous && cue.source_index === previous.source_index + 1 &&
      cue.page >= previous.page && cue.page <= previous.page + 1 && time >= followTime && time - followTime < 1 &&
      Math.abs(desiredY - view.scrollTop) <= view.clientHeight;
    followMotion = adjacent && !matchMedia('(prefers-reduced-motion: reduce)').matches ?
      {fromX: view.scrollLeft, fromY: view.scrollTop, start: now} : null;
    if (!followMotion) { view.scrollLeft = desiredX; view.scrollTop = desiredY; }
    followCue = index; selectScorePage(cue.page);
    refreshVisiblePages();
    if (pageErrors.has(cue.page)) fail(pageErrors.get(cue.page));
  }
  if (followMotion) {
    const progress = clamp((now - followMotion.start) / 200, 0, 1), smooth = progress * progress * (3 - 2 * progress);
    view.scrollLeft = followMotion.fromX + (desiredX - followMotion.fromX) * smooth;
    view.scrollTop = followMotion.fromY + (desiredY - followMotion.fromY) * smooth;
    if (progress === 1) followMotion = null;
  } else {
    view.scrollLeft += (desiredX - view.scrollLeft) * (1 - Math.exp(-elapsed / 65));
    view.scrollTop = desiredY;
  }
  followTime = time; followStamp = now;
}
function frame() { if (ready) showPosition(); updateDots(); updateMetroState(); requestAnimationFrame(frame); }
function setAvailable(value) {
  for (const id of ['play', 'home', 'seek', 'back', 'forward', 'loop', 'loopStart', 'loopEnd']) $(id).disabled = !value;
  for (const id of ['file', 'sessionFile']) $(id).disabled = loading;
  ensemble?.update();
}
function clearScorePage() {
  pageGeneration++; shownPage = pendingPage = -1; resetFollow();
  const image = $('scoreImage'), highlight = $('highlight'), paper = $('paper');
  image.hidden = highlight.hidden = true; image.removeAttribute('src');
  paper.append(image, highlight);
  for (const slot of pageSlots) slot.element.remove();
  for (const url of urls.values()) URL.revokeObjectURL(url);
  urls.clear(); pageSlots = []; wantedPages.clear(); pageLoads.clear(); pageErrors.clear();
  paper.classList.remove('continuous'); paper.removeAttribute('style');
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
    const initialCue = next.timing.cues[clamp(cueAt(0), 0, next.timing.cues.length - 1)];
    await deadline(showPage(initialCue.page), 60000, '악보를 읽지 못했습니다. 파일을 다시 선택해 주세요.');
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
$('zoom').onchange = resizePaper;
let resizeFrame = 0;
new ResizeObserver(() => {
  if (!resizeFrame) resizeFrame = requestAnimationFrame(() => { resizeFrame = 0; resizePaper(); });
}).observe($('viewport'));
$('follow').onchange = () => { resetFollow(); if ($('follow').checked) showPosition(); };
$('viewport').addEventListener('scroll', refreshVisiblePages, {passive: true});
for (const [id, direction] of [['prevPage',-1],['nextPage',1]]) $(id).onclick = () => { $('follow').checked = false; showPage(shownPage + direction).catch(fail); };
for (const event of ['wheel','touchstart','pointerdown']) $('viewport').addEventListener(event, () => { $('follow').checked = false; resetFollow(); }, {passive:true});
$('viewport').addEventListener('keydown', event => {
  if (['ArrowUp','ArrowDown','ArrowLeft','ArrowRight','PageUp','PageDown','Home','End',' '].includes(event.key)) {
    $('follow').checked = false; resetFollow();
  }
});
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
(() => {
  const version = document.querySelector('meta[name="magic-version"]').content;
  const build = document.querySelector('meta[name="magic-build"]').content;
  const status = message => { $('updateStatus').textContent = message; };
  if (!('serviceWorker' in navigator) || !window.isSecureContext || location.protocol === 'file:') {
    $('offline').textContent = '단일 파일 재생기 · iPad·Android에서는 웹주소로 열고 홈 화면에 추가하세요.';
    status('이 환경에서는 자동 업데이트를 확인할 수 없습니다. 웹주소에서 열어 주세요.');
    $('checkUpdate').disabled = true;
    return;
  }
  const service = navigator.serviceWorker, watched = new WeakSet();
  let registration = null, registrationTask = null, checkTask = null, lastCheck = 0;
  let available = null, installFailed = false;
  const prepared = () => { $('offline').textContent = '오프라인 준비 완료 · 홈 화면에 추가해 두세요.'; };
  const availableMessage = () => `v${available.version} 업데이트 준비 완료 · 위 안내에서 적용해 주세요.`;
  const storedStatus = info => available ? availableMessage() : info.build === build ?
    `최신 버전입니다 · v${version}` : '현재 화면보다 이전 버전이 저장되어 있습니다. 온라인에서 다시 확인해 주세요.';
  function olderVersion(other) {
    const stored = other.split('.').map(Number), current = version.split('.').map(Number);
    for (let index = 0; index < 3; index++) {
      if (stored[index] !== current[index]) return stored[index] < current[index];
    }
    return false;
  }

  function workerVersion(worker) {
    return new Promise((resolve, reject) => {
      const channel = new MessageChannel();
      const finish = (error, value) => {
        clearTimeout(timeout); channel.port1.close(); channel.port2.close();
        if (error) reject(error); else resolve(value);
      };
      const timeout = setTimeout(() => finish(new Error('저장된 버전을 확인하지 못했습니다. 다시 확인해 주세요.')), 3000);
      channel.port1.onmessage = event => {
        const info = event.data;
        if (!info || !/^\d+\.\d+\.\d+$/.test(info.version) || !/^[a-f0-9]{64}$/.test(info.build)) {
          finish(new Error('저장된 버전 정보가 올바르지 않습니다. 다시 확인해 주세요.'));
        } else finish(null, info);
      };
      try { worker.postMessage({type: 'MAGIC_PLAYER_VERSION'}, [channel.port2]); }
      catch (error) { finish(error); }
    });
  }

  async function inspectController() {
    for (let attempt = 0; attempt < 3; attempt++) {
      const worker = service.controller;
      if (!worker) return null;
      let info;
      try { info = await workerVersion(worker); }
      catch (error) { if (worker !== service.controller) continue; throw error; }
      // A late reply from a replaced worker must not overwrite the new version.
      if (worker !== service.controller) continue;
      const older = olderVersion(info.version);
      available = older || info.build === build ? null : {...info, worker};
      $('appUpdate').hidden = !available;
      if (available) $('updateTitle').textContent = `v${info.version} 업데이트가 준비되었습니다.`;
      if (older) $('offline').textContent = `오프라인에는 v${info.version}이 저장되어 있습니다. 온라인에서 다시 확인해 주세요.`;
      else prepared();
      return info;
    }
    throw new Error('업데이트가 전환 중입니다. 잠시 뒤 다시 확인해 주세요.');
  }

  function watchInstall(worker) {
    if (!worker || watched.has(worker)) return;
    watched.add(worker); installFailed = false;
    const changed = () => {
      if (worker.state === 'redundant') {
        installFailed = true;
        status('새 버전을 받지 못했습니다. 현재 화면은 계속 쓸 수 있습니다. 인터넷 연결을 확인하고 다시 시도해 주세요.');
      } else if (worker.state === 'activated') {
        inspectController().then(info => {
          if (info) status(storedStatus(info));
        }).catch(error => status(error.message));
      } else {
        status('새 버전을 받아 확인하고 있습니다. 연습은 계속할 수 있습니다.');
      }
      if (['activated', 'redundant'].includes(worker.state)) worker.removeEventListener('statechange', changed);
    };
    worker.addEventListener('statechange', changed); changed();
  }

  function register() {
    if (registration) return Promise.resolve(registration);
    if (!registrationTask) registrationTask = deadline(
      service.register('./sw.js', {updateViaCache: 'none'}), 12000,
      '업데이트 확인이 지연되고 있습니다. 다시 확인해 주세요.'
    ).then(reg => {
      registration = reg;
      reg.addEventListener('updatefound', () => watchInstall(reg.installing));
      watchInstall(reg.installing || reg.waiting);
      return reg;
    }).catch(error => { registrationTask = null; throw error; });
    return registrationTask;
  }

  function installed(worker) {
    return new Promise((resolve, reject) => {
      const finish = error => {
        clearTimeout(timeout); worker.removeEventListener('statechange', changed);
        if (error) reject(error); else resolve();
      };
      const changed = () => {
        if (worker.state === 'activated') finish();
        else if (worker.state === 'redundant') finish(new Error('새 버전을 받지 못했습니다. 다시 확인해 주세요.'));
      };
      const timeout = setTimeout(() => finish(new Error('업데이트 다운로드가 지연되고 있습니다. 잠시 뒤 다시 확인해 주세요.')), 20000);
      worker.addEventListener('statechange', changed); changed();
    });
  }

  function check(force = false) {
    if (checkTask) return checkTask;
    if (!force && (document.hidden || Date.now() - lastCheck < 60000)) return;
    lastCheck = Date.now(); installFailed = false; $('checkUpdate').disabled = true;
    status('새 버전을 확인하고 있습니다…');
    checkTask = (async () => {
      // A previously downloaded update remains available even without a network.
      await inspectController().catch(() => null);
      const reg = await register();
      await deadline(reg.update(), 12000, '업데이트 확인이 지연되고 있습니다. 다시 확인해 주세요.');
      const worker = reg.installing || reg.waiting;
      if (worker) await installed(worker);
      if (installFailed) throw new Error('새 버전을 받지 못했습니다.');
      const info = await inspectController();
      if (!info) throw new Error('오프라인 저장을 아직 준비하지 못했습니다.');
      status(storedStatus(info));
    })().catch(() => {
      status(available ? `${availableMessage()} 최신 공개 버전은 연결 후 다시 확인해 주세요.` :
        '업데이트를 확인하지 못했습니다. 현재 화면은 계속 쓸 수 있습니다. 인터넷 연결을 확인한 뒤 다시 눌러 주세요.');
      if (!service.controller) $('offline').textContent = '오프라인 저장을 준비하지 못했습니다. 인터넷 연결을 확인하고 다시 열어 주세요.';
    }).finally(() => { checkTask = null; $('checkUpdate').disabled = false; });
    return checkTask;
  }

  function applyBlocked() {
    if (ensemble?.role || ensemble?.busy) return 'Session을 종료한 뒤 업데이트를 적용해 주세요.';
    if (loading || audioPending || audioTask || ensemble?.preparing || ensemble?.uploading || ensemble?.downloadTask)
      return '파일과 소리 준비가 끝난 뒤 업데이트를 적용해 주세요.';
    if (playing || metro) return '재생을 멈추고 메트로놈을 끈 뒤 업데이트를 적용해 주세요.';
    return '';
  }

  $('applyUpdate').onclick = async () => {
    const blocked = applyBlocked();
    if (blocked) { $('updateActionStatus').textContent = blocked; return; }
    $('applyUpdate').disabled = true;
    try {
      await inspectController();
      if (!available || available.worker !== service.controller) throw new Error('적용할 버전을 다시 확인해 주세요.');
      // Playback or Session may have started while the worker replied.
      const blockedNow = applyBlocked();
      if (blockedNow) throw new Error(blockedNow);
      location.reload();
    } catch (error) { $('updateActionStatus').textContent = error.message; }
    finally { $('applyUpdate').disabled = false; }
  };
  $('checkUpdate').onclick = () => check(true);
  service.addEventListener('controllerchange', () => {
    inspectController().then(info => {
      if (info && available) status(availableMessage());
    }).catch(error => status(error.message));
  });
  document.addEventListener('visibilitychange', () => { if (!document.hidden) check(); });
  window.addEventListener('online', () => check(true));
  check(true);
})();
// Stable inspection surface for automated tests; no file contents or uploads.
window.MagicPlayer = {openFile, archive: () => archive, state: () => ({ready,loading,playing,position:nowPosition(),duration,page:shownPage,cue:currentCue,metro,latency}), pause, play, seek, context: () => ctx};

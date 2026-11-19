/* SPDX-License-Identifier: GPL-3.0-only */
'use strict';
const ensembleTransport = ['play', 'home', 'seek', 'back', 'forward'];
const ensembleSettings = ['speed', 'speedReset', 'key', 'bpm', 'beats', 'unit', 'origin', 'firstBeat', 'count', 'countBars', 'loop', 'loopStart', 'loopEnd', 'loopCount'];
class Ensemble {
  constructor() {
    this.role = ''; this.ws = null; this.code = ''; this.auth = ''; this.samples = [];
    this.pending = null; this.prepared = false; this.remoteState = null; this.seq = 0;
    this.peers = []; this.busy = false; this.probes = new Map(); this.generation = 0;
    this.shared = false; this.qrURL = null;
    this.base = 'https://music-helper.duckdns.org:13579';
    try { this.base = localStorage.getItem('magic-relay') || this.base; } catch (_) {}
    const query = new URLSearchParams(location.hash.slice(1));
    if (query.has('relay')) this.base = query.get('relay');
    $('relayURL').value = this.base;
    if (location.hash === '#ensemble') $('ensemblePanel').open = true;
    if (query.has('join')) { $('inviteCode').value = query.get('join'); $('ensemblePanel').open = true; }
    $('leaderMode').onclick = () => this.role === 'leader' ? this.leave() : this.connect('leader').catch(e => this.error(e));
    $('memberMode').onclick = () => this.role === 'member' ? this.leave() : this.connect('member').catch(e => this.error(e));
    $('ensembleReady').onclick = () => this.prepare().catch(e => this.error(e));
    $('shareFile').onclick = () => this.share().catch(e => this.error(e));
    $('getShared').onclick = () => this.download().catch(e => this.error(e));
    this.clockTimer = setInterval(() => { if (this.ws?.readyState === WebSocket.OPEN) this.probe(); this.update(); }, 2000);
    $('exitSession').onclick = () => this.leave();
    $('sessionPrepare').onclick = () => this.prepare().catch(e => this.error(e));
    $('sessionRoom').onclick = () => document.body.classList.toggle('room-open');
    $('sessionOpenFile').onclick = () => $('file').click();
    $('sessionVolume').oninput = () => { $('volume').value = $('sessionVolume').value; $('volume').oninput(); };
    this.bindTransport(); this.update();
  }
  status(text) { $('ensembleStatus').textContent = text; $('sessionLive').textContent = text; }
  error(error) { this.status(error?.message || '합주 연결을 확인해 주세요.'); }
  media() {
    requireMagic(archive, '먼저 연습할 .magic 파일을 열어 주세요.');
    return {source: archive.manifest.audio.source_sha256 || archive.manifest.entries[archive.manifest.audio.path].sha256, duration};
  }
  async api(path, options = {}) {
    const response = await fetch(this.base + path, {...options, cache: 'no-store',
      headers: {...(this.auth ? {Authorization: `Bearer ${this.auth}`} : {}), ...options.headers}});
    if (!response.ok) {
      const data = await response.json().catch(() => ({}));
      throw new Error(data.error || '중계 서버와 연결하지 못했습니다. 주소·인터넷을 확인해 주세요.');
    }
    return response;
  }
  send(data) {
    if (this.ws?.readyState === WebSocket.OPEN) this.ws.send(JSON.stringify(data));
  }
  async connect(role) {
    if (this.busy) return;
    if (this.role) this.leave();
    this.busy = true; this.update();
    const generation = ++this.generation;
    try {
      const url = new URL($('relayURL').value.trim());
      requireMagic(url.protocol === 'https:' || (['localhost', '127.0.0.1'].includes(url.hostname) && url.protocol === 'http:'), '중계 주소는 https://로 시작해야 합니다.');
      requireMagic(!url.username && !url.password && url.pathname === '/' && !url.search && !url.hash, '중계 서버의 기본 주소만 입력하세요.');
      this.base = url.origin;
      try { localStorage.setItem('magic-relay', this.base); } catch (_) {}
      await audioReady(); pause();
      const body = {name: $('memberName').value.trim() || (role === 'leader' ? '리더' : '멤버')};
      if (role === 'leader') body.media = this.media();
      else body.code = $('inviteCode').value.trim().toUpperCase();
      const response = await this.api(role === 'leader' ? '/api/rooms' : '/api/join', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)});
      const data = await response.json();
      if (generation !== this.generation) return;
      document.body.classList.add('room-open');
      this.role = role; this.code = data.code; this.auth = data.token; this.expectedMedia = data.media;
      this.samples = []; this.probes.clear(); this.seq = 0; this.remoteState = null;
      const ws = this.ws = new WebSocket(this.base.replace(/^http/, 'ws') + '/api/ws');
      ws.onopen = () => { if (this.ws === ws) this.send({code: this.code, token: this.auth}); };
      ws.onmessage = event => { if (this.ws === ws) { try { this.receive(JSON.parse(event.data)); } catch (e) { this.unready(); this.error(e); } } };
      ws.onerror = () => { if (this.ws === ws) this.status('중계 연결 오류 · 주소와 인터넷을 확인하세요.'); };
      ws.onclose = () => { if (this.ws === ws) { this.leave(); this.status('연결이 종료되어 합주를 멈췄습니다. 다시 참가해 주세요.'); } };
      if (role === 'leader') {
        $('inviteLink').href = data.invite; $('inviteLink').textContent = '초대 링크 열기';
        const qr = await this.api(`/api/rooms/${this.code}/qr`);
        const blob = await qr.blob();
        if (generation === this.generation) { this.qrURL = URL.createObjectURL(blob); $('inviteQR').src = this.qrURL; }
      }
      this.status('시간을 맞추고 있습니다. 곡을 확인한 뒤 ‘합주 준비’를 누르세요.');
    } finally { this.busy = false; this.update(); }
  }
  leave() {
    ++this.generation;
    const ws = this.ws; this.ws = null; if (ws) ws.close();
    this.role = ''; this.auth = ''; this.code = ''; this.prepared = false;
    this.pending = null; this.remoteState = null; this.peers = []; this.shared = false;
    pause(); this.probes.clear();
    if (this.qrURL) URL.revokeObjectURL(this.qrURL); this.qrURL = null;
    this.status('합주 모드 꺼짐 · 혼자 연습할 수 있습니다.');
    setAvailable(ready); this.update();
  }
  unready() {
    if (!this.role) return;
    this.prepared = false; this.pending = null;
    this.send({type: 'ready', ready: false});
    pause(); this.update();
  }
  probe() {
    const id = performance.now(); this.probes.set(id, id);
    for (const [key, value] of this.probes) if (id - value > 10000) this.probes.delete(key);
    this.send({type: 'ping', id});
  }
  serverNow() {
    requireMagic(this.samples.length >= 3, '시간 확인 중입니다. 잠시 뒤 준비를 눌러 주세요.');
    const best = this.samples.reduce((a, b) => a.rtt < b.rtt ? a : b);
    return performance.now() + best.offset;
  }
  async prepare() {
    if (!this.role || !ready || loading) return;
    if (this.prepared) { this.unready(); return; }
    await audioReady(); this.serverNow();
    const media = this.media();
    requireMagic(media.source === this.expectedMedia.source && Math.abs(media.duration - this.expectedMedia.duration) < .05, '리더와 같은 원곡 음원으로 만든 .magic 파일을 열어 주세요. 악보·파트는 달라도 됩니다.');
    const rtt = Math.max(...this.samples.map(s => s.rtt));
    requireMagic(rtt <= 1300, '연결이 많이 지연됩니다. 인터넷 상태를 확인해 주세요.');
    this.prepared = true; document.body.classList.remove('room-open'); window.scrollTo(0,0); $('linked').checked = true;
    this.send({type: 'ready', ready: true, media, rtt});
    this.status(this.role === 'leader' ? '준비 완료 · 모두 준비되면 재생을 누르세요.' : '준비 완료 · 리더의 재생을 기다립니다.');
    this.update();
  }
  receive(data) {
    if (data.type === 'welcome') {
      for (let i = 0; i < 5; i++) setTimeout(() => this.probe(), i * 100);
    } else if (data.type === 'pong' && this.probes.has(data.id)) {
      const start = this.probes.get(data.id), end = performance.now(); this.probes.delete(data.id);
      const sample = {rtt: end - start, offset: data.time - (start + end) / 2};
      this.samples.push(sample); if (this.samples.length > 10) this.samples.shift();
      if (this.prepared && sample.rtt > 1300) { this.unready(); this.status('연결 지연이 커져 준비를 해제했습니다.'); }
      else if (this.prepared) this.send({type:'ready', ready:true, media:this.media(), rtt: Math.max(...this.samples.map(s => s.rtt))});
    } else if (data.type === 'presence') {
      this.peers = data.peers; this.shared = data.shared;
      $('members').replaceChildren(...data.peers.map(p => {
        const line = document.createElement('li');
        line.textContent = `${p.leader ? '리더' : '멤버'} · ${p.name} · ${p.ready ? '준비 완료' : '준비 대기'}`; return line;
      }));
    } else if (data.type === 'halt') {
      if (data.seq <= this.seq) return;
      this.seq = data.seq; this.pending = null; pause(); this.status(data.reason);
    } else if (data.type === 'error') this.status(data.message);
    else if (data.type === 'command') this.accept(data);
    this.update();
  }
  snapshot(target = nowPosition()) {
    return {position: target, rate: number('speed',25,200,100)/100,
      key: Number($('key').value) + archive.manifest.audio.pitch_semitones,
      bpm: number('bpm',20,400,120), beats: number('beats',1,16,4), unit: Number($('unit').value),
      origin: number('origin',0,1800,0), count: $('count').checked,
      countBars: Number($('countBars').value), loop: $('loop').checked ? loopBounds() : null,
      loopCount: $('loopCount').checked};
  }
  command(action, target = nowPosition(), resume = playing) {
    if (this.role !== 'leader' || this.pending || !this.prepared) return;
    const state = this.snapshot(target), bounds = state.loop || [0, duration];
    state.position = clamp(target, bounds[0], Math.max(bounds[0], bounds[1] - .001));
    this.send({type:'command', action, state, resume});
  }
  accept(data) {
    if (data.seq <= this.seq) return;
    requireMagic(this.prepared && ready && ctx?.state === 'running' && !document.hidden, '합주 준비를 다시 눌러 주세요.');
    const wait = (data.at - this.serverNow()) / 1000;
    requireMagic(wait >= latency + .2 && wait <= 6, '예약이 늦게 도착했습니다. 네트워크를 확인한 뒤 다시 시작하세요.');
    const extra = data.state.key - archive.manifest.audio.pitch_semitones;
    requireMagic(extra >= -12 && extra <= 12, '반주에 저장된 키 차이가 너무 큽니다. 리더가 공유한 파일을 열어 주세요.');
    this.seq = data.seq;
    this.pending = {data, at: ctx.currentTime + wait, armed:false, next:null};
    this.send({type:'ack', seq:data.seq});
    this.status(`${data.delay}초 뒤 ${data.action === 'pause' ? '일시정지' : data.action === 'seek' ? '위치 이동' : '합주 시작'} · 리더 예약`);
  }
  applyState(state) {
    this.remoteState = state;
    for (const id of ['bpm','beats','unit','origin','countBars']) $(id).value = state[id];
    for (const id of ['count','loopCount']) $(id).checked = state[id];
    $('key').value = state.key - archive.manifest.audio.pitch_semitones;
    $('speed').value = state.rate * 100;
    $('speedText').textContent = state.rate.toFixed(2) + '×';
    $('linked').checked = true; updateMetro(); updateKey();
  }
  arm() {
    const p = this.pending;
    if (!p || p.armed || p.at - ctx.currentTime > .55) return;
    requireMagic(p.at - ctx.currentTime > latency + .03, '기기 처리가 늦어 예약을 취소했습니다. 다시 준비해 주세요.');
    this.applyState(p.data.state);
    scheduleNode(false, p.at, p.data.state.position);
    if (p.data.action === 'play' || (p.data.action === 'seek' && p.data.resume)) {
      const count = p.data.action === 'play' && (p.data.state.count || (p.data.state.loop && p.data.state.loopCount));
      p.next = createSegment(p.data.state.position, p.at, count);
    }
    p.armed = true;
  }
  activate() {
    const p = this.pending;
    if (!p || ctx.currentTime < p.at) return;
    requireMagic(p.armed && ctx.currentTime - p.at < .2, '기기가 멈춰 합주를 일시정지했습니다. 다시 준비해 주세요.');
    const saved = nowPosition(p.at);
    this.pending = null; upcoming = null;
    if (p.next) { segment = p.next; playing = true; position = p.next.input; }
    else { segment = null; playing = false; position = p.data.action === 'pause' ? saved : p.data.state.position; }
    $('play').textContent = playing ? 'Ⅱ 일시정지' : '▶ 재생';
    this.status(playing ? '합주 재생 중 · 리더가 조작합니다.' : '합주 일시정지 · 리더가 이어서 시작할 수 있습니다.');
    if (playing && navigator.wakeLock) navigator.wakeLock.request('screen').then(lock => {
      if (playing) wakeLock = lock; else lock.release();
    }).catch(() => {});
    this.update();
  }
  async share() {
    requireMagic(this.role === 'leader' && archive?.manifest.version === 2 && archive.manifest.score.kind === 'gp', '전체 GP 파트가 담긴 .magic 파일을 열어 주세요.');
    this.busy = true; this.update(); this.status('멤버에게 공유할 파일을 서버로 보내고 있습니다…');
    try {
      await this.api(`/api/rooms/${this.code}/file`, {method:'POST', body:archive.file, headers:{'Content-Type':'application/octet-stream'}});
      this.status('공유 완료 · 멤버가 ‘리더 파일 받기’를 누르면 됩니다. 방이 끝나면 서버 사본은 삭제됩니다.');
    } finally { this.busy = false; this.update(); }
  }
  async download() {
    if (this.role !== 'member' || !this.shared || this.busy) return;
    this.unready(); this.busy = true; this.update(); const generation = this.generation;
    try {
      await audioReady(); this.status('리더의 연습 파일을 받고 있습니다…');
      const response = await this.api(`/api/rooms/${this.code}/file`);
      const length = Number(response.headers.get('Content-Length'));
      requireMagic(length >= 22 && length <= MAGIC_LIMIT, '공유 파일 크기를 확인할 수 없습니다.');
      const blob = await response.blob();
      requireMagic(blob.size === length, '파일 전송이 끝나지 않았습니다.');
      if (generation !== this.generation) return;
      await openFile(new File([blob], 'shared-practice.magic'));
      requireMagic(ready, $('status').textContent);
      this.status('파일을 받았습니다. 파트를 고른 뒤 ‘합주 준비’를 누르세요.');
    } finally { this.busy = false; this.update(); }
  }
  update() {
    const active = !!this.role, leader = this.role === 'leader', member = this.role === 'member';
    if (active) document.body.dataset.session = this.role; else delete document.body.dataset.session;
    $('playerControls').querySelector('.step').textContent = leader ? 'SESSION · 팀 전체 재생 조작' : '02 · 듣고 연습하기';
    $('loopControls').querySelector('.step').textContent = leader ? 'SESSION · 함께 반복 연습' : '03 · 어려운 구간 반복';
    document.querySelector('.metronome summary').textContent = member ? '내 메트로놈 음량' : '박자 설정';
    $('sessionBanner').hidden = !active; $('sessionSong').hidden = !active;
    $('sessionRole').textContent = `Session · ${leader ? '리더' : '멤버'} 활성 · ${this.code}`;
    $('appHeading').textContent = active ? (leader ? '우리의 합주를 이끄는 화면' : '나의 파트에 집중하는 화면') : '나의 속도로, 한 마디씩.';
    $('sessionLive').textContent = $('ensembleStatus').textContent;
    $('sessionTitle').textContent = archive?.manifest.title || '연습 파일을 선택해 주세요';
    $('sessionClock').textContent = `${clockText(nowPosition())} / ${clockText(duration)}`;
    $('sessionOpenFile').hidden = !member; $('sessionOpenFile').disabled = this.busy || loading;
    $('sessionVolumeLabel').hidden = !member;
    if (active) $('ensemblePanel').open = true;
    $('leaderMode').textContent = leader ? '리더 켜짐 · 끄기' : '리더 모드 켜기';
    $('memberMode').textContent = member ? '멤버 켜짐 · 끄기' : '멤버 모드 켜기';
    $('leaderMode').setAttribute('aria-pressed',String(leader)); $('memberMode').setAttribute('aria-pressed',String(member));
    $('leaderMode').disabled = this.busy || member; $('memberMode').disabled = this.busy || leader;
    $('relayURL').disabled = active || this.busy; $('memberName').disabled = active || this.busy; $('inviteCode').disabled = active || this.busy;
    $('invitation').hidden = !leader; $('roomCode').textContent = this.code;
    $('ensembleReady').hidden = !active; $('ensembleReady').disabled = !ready || loading || this.busy || this.samples.length < 3;
    $('ensembleReady').textContent = this.prepared ? '준비 완료 · 해제하기' : '합주 준비';
    $('ensembleReady').setAttribute('aria-pressed',String(this.prepared));
    $('sessionPrepare').textContent = $('ensembleReady').textContent;
    $('sessionPrepare').disabled = $('ensembleReady').disabled;
    $('sessionPrepare').setAttribute('aria-pressed',String(this.prepared));
    $('shareFile').hidden = !leader; $('shareFile').disabled = this.busy || archive?.manifest.version !== 2 || archive?.manifest.score.kind !== 'gp';
    $('getShared').hidden = !member; $('getShared').disabled = !this.shared || this.busy;
    $('file').disabled = loading || this.busy || leader;
    $('linked').disabled = active;
    const allReady = this.peers.length && this.peers.every(p => p.ready && p.connected);
    for (const id of ensembleTransport) $(id).disabled = !ready || member || (leader && (!this.prepared || !allReady || !!this.pending));
    for (const id of ensembleSettings) $(id).disabled = member || (leader && (playing || !!this.pending)) || (['loop','loopStart','loopEnd'].includes(id) && !ready);
    $('ensembleRules').hidden = !active;
  }
  bindTransport() {
    const wrap = (id, event, callback) => {
      const old = $(id)[event];
      $(id)[event] = e => { if (!this.role) return old?.(e); if (this.role === 'leader') { try { callback(e); } catch (error) { this.error(error); } } };
    };
    wrap('play','onclick',() => this.command(playing ? 'pause' : 'play'));
    wrap('home','onclick',() => this.command('seek',0));
    wrap('seek','onchange',() => { seeking = false; this.command('seek',Number($('seek').value)); });
    for (const [id, direction] of [['back',-1],['forward',1]]) wrap(id,'onclick',() => {
      const i = clamp(cueAt(nowPosition()) + direction * Math.round(number('step',1,10000,1)),0,archive.timing.cues.length - 1);
      this.command('seek',archive.timing.cues[i].start + archive.timing.offset);
    });
  }
}
ensemble = new Ensemble();
window.MagicEnsemble = {state: () => ({role:ensemble.role, code:ensemble.code, prepared:ensemble.prepared, pending:!!ensemble.pending, peers:ensemble.peers}), leave: () => ensemble.leave()};
